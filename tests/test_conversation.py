"""The console's side of the add-on: one conversation with Claude, over HTTP."""

import json

import pytest
from conftest import wait_until


def send_message(client, prompt, **fields):
    answer = client.send_json("POST", "/chat", {"prompt": prompt, **fields})
    assert answer.status == 201, answer
    return answer.json


def wait_for_status(client, job_id, status):
    return wait_until(
        lambda: client.get(f"/jobs/{job_id}").json.get("status") == status,
        description=f"job {job_id} to be {status}",
    )


def wait_for_reply(client, job_id):
    wait_for_status(client, job_id, "done")
    return client.get(f"/jobs/{job_id}").json


def test_a_message_gets_a_reply_and_both_sides_are_in_the_transcript(fresh_conversation):
    client = fresh_conversation

    turn = send_message(client, "first message")
    wait_for_reply(client, turn["id"])

    conversation = client.get("/chat").json
    assert conversation["session"]
    assert [line["role"] for line in conversation["turns"]] == ["user", "assistant"]
    assert conversation["turns"][0]["text"] == "first message"
    assert conversation["turns"][1]["text"].startswith("stub reply to: first message")


def test_a_second_message_continues_the_same_conversation(fresh_conversation):
    client = fresh_conversation

    first = send_message(client, "first message")
    wait_for_reply(client, first["id"])
    started_conversation = client.get("/chat").json["session"]

    second = send_message(client, "second message")
    wait_for_reply(client, second["id"])

    conversation = client.get("/chat").json
    assert conversation["session"] == started_conversation
    assert len(conversation["turns"]) == 4


def test_the_reply_can_be_read_while_it_is_still_being_written(
    fresh_conversation, stub_behaviour
):
    client = fresh_conversation
    stub_behaviour("STUB_SLEEP", "12")

    turn = send_message(client, "a slow one")

    def partial_text():
        pending = client.get("/chat").json.get("pending") or []
        return next((job.get("partial") for job in pending if job.get("partial")), None)

    written_so_far = wait_until(partial_text, description="the reply to start arriving")

    assert written_so_far
    # One message to a line, and the reply is the one still being written: a turn that has
    # said several things before it — as a long one does, reporting its progress — carries
    # them above, and the newest is the last line.
    assert "stub reply to: a slow one".startswith(written_so_far.splitlines()[-1])

    client.send_json("POST", f"/jobs/{turn['id']}/cancel")


def test_the_reply_being_written_is_not_also_served_as_a_finished_turn(
    fresh_conversation, stub_behaviour
):
    client = fresh_conversation
    stub_behaviour("STUB_SLEEP", "12")

    turn = send_message(client, "a slow one")
    wait_until(
        lambda: any(
            job.get("partial") for job in client.get("/chat").json.get("pending") or []
        ),
        description="the reply to start arriving",
    )

    mid_run = client.get("/chat").json
    roles = [line["role"] for line in mid_run["turns"]]
    assert roles[-1:] in ([], ["user"]), roles

    client.send_json("POST", f"/jobs/{turn['id']}/cancel")


def test_cancelling_a_running_turn_settles_it_as_stopped_and_keeps_what_was_said(
    fresh_conversation, stub_behaviour
):
    client = fresh_conversation
    stub_behaviour("STUB_SLEEP", "12")
    turn = send_message(client, "a slow one")
    wait_until(
        lambda: any(
            job.get("partial") for job in client.get("/chat").json.get("pending") or []
        ),
        description="the reply to start arriving",
    )

    answer = client.send_json("POST", f"/jobs/{turn['id']}/cancel")

    assert answer.status == 200
    assert answer.json["status"] == "failed"
    assert answer.json["error"] == "stopped"
    assert "stub reply to: a slow one".startswith(answer.json["result"].splitlines()[-1])


def test_cancelling_a_turn_that_has_already_finished_is_refused(fresh_conversation):
    client = fresh_conversation
    turn = send_message(client, "quick one")
    wait_for_reply(client, turn["id"])

    answer = client.send_json("POST", f"/jobs/{turn['id']}/cancel")

    assert answer.status == 409
    assert "already done" in answer.json["error"]


def test_a_message_sent_while_claude_is_answering_waits_its_turn(
    fresh_conversation, stub_behaviour
):
    client = fresh_conversation
    stub_behaviour("STUB_SLEEP", "12")
    occupying = send_message(client, "occupies the CLI")
    wait_for_status(client, occupying["id"], "running")

    waiting = send_message(client, "waits in the queue")

    queue = client.get("/chat").json["pending"]
    assert [job["id"] for job in queue] == [occupying["id"], waiting["id"]]
    assert client.get(f"/jobs/{waiting['id']}").json["status"] == "queued"

    client.send_json("POST", f"/jobs/{waiting['id']}/cancel")
    client.send_json("POST", f"/jobs/{occupying['id']}/cancel")


def test_a_queued_message_can_be_dropped_before_it_ever_starts(
    fresh_conversation, stub_behaviour
):
    client = fresh_conversation
    stub_behaviour("STUB_SLEEP", "12")
    occupying = send_message(client, "occupies the CLI")
    wait_for_status(client, occupying["id"], "running")
    waiting = send_message(client, "never runs")

    answer = client.send_json("POST", f"/jobs/{waiting['id']}/cancel")

    assert answer.status == 200
    assert answer.json["error"] == "stopped before it started"
    assert answer.json["result"] is None

    client.send_json("POST", f"/jobs/{occupying['id']}/cancel")


def test_a_failing_turn_stays_visible_with_the_reason_it_failed(
    fresh_conversation, stub_behaviour
):
    client = fresh_conversation
    stub_behaviour("STUB_FAIL", "1")

    turn = send_message(client, "this will fail")
    wait_for_status(client, turn["id"], "failed")

    conversation = client.get("/chat").json
    assert conversation["failed"]["id"] == turn["id"]
    assert conversation["failed"]["error"] == "stub failure"


def test_a_turn_that_outlives_the_timeout_is_killed_and_reported(
    addon, fresh_conversation, stub_behaviour, monkeypatch
):
    client = fresh_conversation
    monkeypatch.setattr(addon, "TIMEOUT_SEC", 1)
    stub_behaviour("STUB_SLEEP", "20")

    turn = send_message(client, "runs too long")
    wait_for_status(client, turn["id"], "failed")

    assert client.get(f"/jobs/{turn['id']}").json["error"] == "timed out after 1s"


def test_the_context_left_is_how_full_the_conversation_is_not_what_the_turn_spent(
    fresh_conversation,
):
    """The turn's totals add up every request it made, and there were three.

    Read that way a long turn reports more tokens than the window holds — the console
    showed 1.1M of 1M. What fills the window is the last request, and 600 of it here.
    """
    client = fresh_conversation

    turn = send_message(client, "how much room is left")
    finished = wait_for_reply(client, turn["id"])

    context = finished["context"]
    assert context["window"] == 10_000
    assert context["used"] == 600
    assert context["left_percent"] == 94.0


def test_the_conversation_is_named_by_claude_and_the_name_can_be_changed(fresh_conversation):
    client = fresh_conversation
    turn = send_message(client, "name this conversation")
    wait_for_reply(client, turn["id"])
    session = client.get("/chat").json["session"]

    assert client.get("/chat").json["title"].startswith("About")

    renaming = client.send_json(
        "POST", "/chat/rename", {"session": session, "title": "My own title"}
    )
    assert renaming.status == 201
    wait_for_status(client, renaming.json["id"], "done")

    assert client.get("/chat").json["title"] == "My own title"
    listed = client.get("/chat/sessions").json["sessions"]
    named = next(entry for entry in listed if entry["id"] == session)
    assert named["title"] == "My own title"
    assert named["custom"] is True


def test_renaming_does_not_leave_the_command_in_the_transcript(fresh_conversation):
    client = fresh_conversation
    turn = send_message(client, "a conversation to rename")
    wait_for_reply(client, turn["id"])
    session = client.get("/chat").json["session"]

    renaming = client.send_json("POST", "/chat/rename", {"session": session, "title": "Renamed"})
    wait_for_status(client, renaming.json["id"], "done")

    texts = [line["text"] for line in client.get("/chat").json["turns"]]
    assert not any(text.startswith("/rename") for text in texts)


@pytest.mark.parametrize(
    ("payload", "expected_message"),
    [
        ({"title": "no session"}, "a session id is required"),
        ({"session": "../etc", "title": "traversal"}, "a session id is required"),
        ({"session": "0" * 12}, "a title is required"),
        ({"session": "0" * 12, "title": "   "}, "a title is required"),
    ],
)
def test_a_rename_without_a_session_and_a_title_is_refused(client, payload, expected_message):
    answer = client.send_json("POST", "/chat/rename", payload)

    assert answer.status == 400
    assert answer.json["error"] == expected_message


def test_a_long_multiline_title_is_flattened_to_one_line(addon, fresh_conversation):
    client = fresh_conversation
    turn = send_message(client, "a conversation to rename")
    wait_for_reply(client, turn["id"])
    session = client.get("/chat").json["session"]

    renaming = client.send_json(
        "POST", "/chat/rename", {"session": session, "title": "two\nlines " + "x" * 200}
    )

    assert renaming.status == 201
    assert "\n" not in renaming.json["prompt"]
    assert len(renaming.json["prompt"]) == len("/rename ") + 120


def test_new_chat_empties_the_transcript_and_resume_brings_a_conversation_back(
    fresh_conversation,
):
    client = fresh_conversation
    turn = send_message(client, "worth coming back to")
    wait_for_reply(client, turn["id"])
    session = client.get("/chat").json["session"]

    client.send_json("POST", "/chat/new")
    emptied = client.get("/chat").json
    assert emptied["session"] is None
    assert emptied["turns"] == []
    assert emptied["title"] is None

    resumed = client.send_json("POST", "/chat/resume", {"session": session})
    assert resumed.status == 200
    assert len(resumed.json["turns"]) == 2
    assert client.get("/chat").json["session"] == session


def test_resuming_something_that_is_not_a_session_id_is_refused(client):
    answer = client.send_json("POST", "/chat/resume", {"session": "../../etc/passwd"})

    assert answer.status == 400
    assert answer.json["error"] == "a session id is required"


def test_resuming_a_session_that_was_never_written_gives_an_empty_transcript(client):
    answer = client.send_json("POST", "/chat/resume", {"session": "abcdef12-0000-0000"})

    assert answer.status == 200
    assert answer.json["turns"] == []

    client.send_json("POST", "/chat/new")


def test_compacting_is_refused_until_there_is_a_conversation(fresh_conversation):
    client = fresh_conversation

    answer = client.send_json("POST", "/chat/compact")

    assert answer.status == 409
    assert answer.json["error"] == "there is no conversation to compact yet"


def test_compacting_a_real_conversation_runs_the_command(fresh_conversation):
    client = fresh_conversation
    turn = send_message(client, "long enough to compact")
    wait_for_reply(client, turn["id"])

    answer = client.send_json("POST", "/chat/compact")

    assert answer.status == 201
    assert answer.json["prompt"] == "/compact"
    assert answer.json["command"] == "compact"
    wait_for_status(client, answer.json["id"], "done")


def test_only_the_commands_the_chat_knows_are_accepted(client):
    answer = client.send_json("POST", "/chat", {"prompt": "x", "command": "login"})

    assert answer.status == 400
    assert "command must be one of" in answer.json["error"]


@pytest.mark.parametrize(
    ("payload", "expected_fragment"),
    [
        ({"prompt": "   "}, "'prompt' is required"),
        ({"prompt": "x", "effort": "turbo"}, "effort must be one of"),
        ({"prompt": "x", "permission_mode": "bypassPermissions"}, "permission_mode must be one of"),
        ({"prompt": "x", "model": "bad model!"}, "unsafe model name"),
        ({"prompt": "x", "resume": "not a session"}, "resume must be a session id"),
    ],
)
def test_a_message_the_cli_would_choke_on_is_refused_before_it_runs(
    client, payload, expected_fragment
):
    answer = client.send_json("POST", "/chat", payload)

    assert answer.status == 400
    assert expected_fragment in answer.json["error"]


def test_the_older_name_for_the_review_everything_mode_is_still_accepted(client):
    answer = client.send_json("POST", "/chat", {"prompt": "x", "permission_mode": "default"})

    assert answer.status == 201
    assert answer.json["permission_mode"] == "manual"
    client.send_json("POST", f"/jobs/{answer.json['id']}/cancel")


def test_renaming_leaves_none_of_its_own_bookkeeping_in_the_window(
    transcripts_dir, fresh_conversation
):
    client = fresh_conversation
    turn = send_message(client, "a conversation to rename")
    wait_for_reply(client, turn["id"])
    session = client.get("/chat").json["session"]

    renaming = client.send_json("POST", "/chat/rename", {"session": session, "title": "Renamed"})
    wait_for_status(client, renaming.json["id"], "done")

    transcript = (transcripts_dir / f"{session}.jsonl").read_text()
    assert "<command-name>" in transcript
    assert "No response requested." in transcript

    shown = [line["text"] for line in client.get("/chat").json["turns"]]
    assert shown == ["a conversation to rename", "stub reply to: a conversation to rename"]


def test_a_warning_and_a_failed_tool_call_are_reported_beside_the_conversation(
    fresh_conversation, stub_behaviour
):
    client = fresh_conversation
    stub_behaviour("STUB_NOTICE", "1")

    turn = send_message(client, "this warns and fails a tool")
    wait_for_reply(client, turn["id"])

    conversation = client.get("/chat").json
    assert [notice["kind"] for notice in conversation["notices"]] == [
        "informational",
        "tool_error",
    ]
    assert conversation["notices"][0]["level"] == "warning"
    assert conversation["notices"][0]["text"].startswith("Unknown command")
    assert conversation["notices"][1]["text"] == "File has not been read yet."
    assert [line["role"] for line in conversation["turns"]] == ["user", "assistant"]


def test_an_api_error_is_a_notice_rather_than_something_claude_said(
    fresh_conversation, stub_behaviour
):
    client = fresh_conversation
    stub_behaviour("STUB_API_ERROR", "1")

    turn = send_message(client, "the model was unavailable")
    wait_for_reply(client, turn["id"])

    conversation = client.get("/chat").json
    assert [notice["kind"] for notice in conversation["notices"]] == ["api_error"]
    assert "temporarily unavailable" in conversation["notices"][0]["text"]
    assert not any("API Error" in line["text"] for line in conversation["turns"])


def test_a_conversation_with_no_warnings_reports_none(fresh_conversation):
    client = fresh_conversation
    turn = send_message(client, "nothing goes wrong here")
    wait_for_reply(client, turn["id"])

    assert client.get("/chat").json["notices"] == []


def test_only_the_most_recent_warnings_are_kept(addon, transcripts_dir, client):
    session = "bbbbbbbb-0000-4000-8000-000000000001"
    lines = [
        json.dumps(
            {
                "type": "system",
                "subtype": "informational",
                "level": "warning",
                "content": f"warning number {index}",
            }
        )
        for index in range(addon.NOTICES_KEPT + 5)
    ]
    (transcripts_dir / f"{session}.jsonl").write_text("\n".join(lines) + "\n")

    _, notices = addon.read_conversation(session)

    assert len(notices) == addon.NOTICES_KEPT
    assert notices[-1]["text"] == f"warning number {addon.NOTICES_KEPT + 4}"


def test_resuming_a_conversation_brings_its_warnings_with_it(
    fresh_conversation, stub_behaviour
):
    client = fresh_conversation
    stub_behaviour("STUB_NOTICE", "1")
    turn = send_message(client, "warns once")
    wait_for_reply(client, turn["id"])
    session = client.get("/chat").json["session"]
    client.send_json("POST", "/chat/new")

    resumed = client.send_json("POST", "/chat/resume", {"session": session})

    assert [notice["kind"] for notice in resumed.json["notices"]] == [
        "informational",
        "tool_error",
    ]


def test_starting_a_new_conversation_survives_the_turn_that_was_still_running(
    fresh_conversation, stub_behaviour
):
    client = fresh_conversation
    established = send_message(client, "the conversation we are leaving")
    wait_for_reply(client, established["id"])
    stub_behaviour("STUB_SLEEP", "3")
    inflight = send_message(client, "answered after we left")
    wait_for_status(client, inflight["id"], "running")

    client.send_json("POST", "/chat/new")
    wait_for_status(client, inflight["id"], "done")

    left = client.get("/chat").json
    assert left["session"] is None
    assert left["turns"] == []


def test_another_caller_starts_its_own_conversation_rather_than_joining_this_one(
    fresh_conversation,
):
    client = fresh_conversation
    mine = send_message(client, "the conversation in this window")
    wait_for_reply(client, mine["id"])
    console_session = client.get("/chat").json["session"]

    theirs = client.send_json(
        "POST", "/jobs", {"prompt": "a bot's first message", "chat": True, "source": "kitchen-tablet"}
    )
    client.send_json("POST", f"/jobs/{theirs.json['id']}/start")
    wait_for_status(client, theirs.json["id"], "done")

    bot_session = client.get(f"/jobs/{theirs.json['id']}").json["session_id"]
    assert bot_session != console_session, "it did not join this conversation"
    assert client.get("/chat").json["session"] == console_session, "and did not move it"
    assert len(client.get("/chat").json["turns"]) == 2, "and left no turns in it"


def test_another_caller_can_carry_on_its_own_conversation(fresh_conversation):
    client = fresh_conversation
    first = client.send_json(
        "POST", "/jobs", {"prompt": "the bot's first", "chat": True, "source": "kitchen-tablet"}
    )
    client.send_json("POST", f"/jobs/{first.json['id']}/start")
    wait_for_status(client, first.json["id"], "done")
    session = client.get(f"/jobs/{first.json['id']}").json["session_id"]

    second = client.send_json(
        "POST",
        "/jobs",
        {"prompt": "and its second", "chat": True, "source": "kitchen-tablet", "resume": session},
    )
    client.send_json("POST", f"/jobs/{second.json['id']}/start")
    wait_for_status(client, second.json["id"], "done")

    assert client.get(f"/jobs/{second.json['id']}").json["session_id"] == session
    assert client.get("/chat").json["pending"] == [], "and stays out of this window"


def test_the_conversation_list_leaves_out_transcripts_with_nothing_in_them(
    addon, transcripts_dir, client
):
    empty_transcript = transcripts_dir / "abcdef01-0000-4000-8000-000000000000.jsonl"
    empty_transcript.write_text('{"type": "summary"}\n')
    try:
        listed = client.get("/chat/sessions").json["sessions"]
    finally:
        empty_transcript.unlink()

    assert empty_transcript.stem not in [entry["id"] for entry in listed]


def test_a_conversation_can_be_deleted(addon, client, transcripts_dir):
    """Its transcript is the only place it lives, so that is what goes."""
    session = "abcdef12-0000-4000-8000-00000000dead"
    transcript = transcripts_dir / f"{session}.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hello"},
                    "timestamp": "2026-01-01T00:00:00.000Z", "sessionId": session}) + "\n"
    )
    assert any(s["id"] == session for s in client.get("/chat/sessions").json["sessions"])

    gone = client.send_json("DELETE", f"/chat/sessions/{session}")

    assert gone.json == {"deleted": session}
    assert not transcript.exists()
    assert not any(s["id"] == session for s in client.get("/chat/sessions").json["sessions"])


def test_deleting_the_one_being_looked_at_leaves_a_new_conversation(
    addon, client, transcripts_dir
):
    session = "abcdef12-0000-4000-8000-0000000beef1"
    (transcripts_dir / f"{session}.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"},
                    "timestamp": "2026-01-01T00:00:00.000Z", "sessionId": session}) + "\n"
    )
    addon.set_current_session(session)

    client.send_json("DELETE", f"/chat/sessions/{session}")

    assert addon.current_session() is None
    assert client.get("/chat").json["session"] is None


def test_a_conversation_that_is_not_there_says_so(client):
    answer = client.send_json("DELETE", "/chat/sessions/abcdef12-0000-4000-8000-000000000404")

    assert answer.status == 404
    assert "no such conversation" in answer.json["error"]


@pytest.mark.parametrize("session", ["../escape", "not-hex-at-all!", ""])
def test_something_that_is_not_a_session_id_is_refused(client, session):
    answer = client.send_json("DELETE", f"/chat/sessions/{session}")

    assert answer.status in (400, 404)


def test_a_conversation_with_a_turn_in_flight_is_not_deleted(client, stub_behaviour):
    """The CLI is writing that very file; deleting it under the process would lose the
    reply and leave the job pointing at nothing.

    Two turns, because the first one has no conversation to resume yet — it is the turn
    that makes it.
    """
    first = send_message(client, "the first thing said")
    wait_for_reply(client, first["id"])
    session = client.get("/chat").json["session"]
    assert session, "the first turn is what names the conversation"

    stub_behaviour("STUB_SLEEP", "20")
    second = send_message(client, "and the second, while it runs")
    wait_for_status(client, second["id"], "running")

    answer = client.send_json("DELETE", f"/chat/sessions/{session}")

    assert answer.status == 409
    assert "still running" in answer.json["error"]
    client.send_json("POST", f"/jobs/{second['id']}/cancel")
    wait_for_status(client, second["id"], "failed")


def test_the_transcript_is_cut_back_to_the_last_thing_the_user_said(
    fresh_conversation, stub_behaviour, transcripts_dir
):
    """Claude Code writes its reply into the transcript while the turn runs, and the same
    words are served live as `partial` — so a page shown both would show the reply twice,
    once finished and once still arriving.

    The second message, not the first: until the CLI reports the session there is no
    transcript to serve, so the cutting back only has anything to cut on a conversation
    that is already under way.
    """
    client = fresh_conversation
    first = send_message(client, "the one that names the conversation")
    wait_for_reply(client, first["id"])
    assert [line["role"] for line in client.get("/chat").json["turns"]] == ["user", "assistant"]

    stub_behaviour("STUB_SLEEP", "12")
    turn = send_message(client, "a slow one")
    transcript = transcripts_dir / f"{client.get('/chat').json['session']}.jsonl"
    wait_until(
        lambda: "stub reply to: a slow one" in transcript.read_text(),
        description="the second reply to reach the transcript",
    )

    mid_run = client.get("/chat").json

    assert [line["role"] for line in mid_run["turns"]] == ["user", "assistant", "user"], \
        "the reply being written belongs in `partial`, not in the transcript"
    assert any(job.get("partial") for job in mid_run["pending"]), "and it is there"

    client.send_json("POST", f"/jobs/{turn['id']}/cancel")
