"""The parts a caller never sees directly: safety, parsing, and what happens when
the filesystem, the network or the CLI misbehaves.

These are unit tests on purpose. Every one of them stands for a way the add-on can
be handed something broken — a half-written transcript, a file that vanishes
mid-walk, a binary that will not run — and the behaviour under test is that it
carries on with something sensible instead of failing the request.
"""

import json
import subprocess
import threading
import time

import pytest

# --------------------------------------------------------------------------- #
# names and paths
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "name",
    ["ok\n", "../etc", "..", ".hidden", "", "with space", "sla/sh", "tab\t", "-leading"],
)
def test_a_name_that_could_leave_its_directory_is_rejected(addon, name):
    with pytest.raises(addon.ApiError) as refused:
        addon.safe_name(name)

    assert refused.value.status == 400


@pytest.mark.parametrize("name", ["my-skill.v2", "a", "A1_b-c.d", "release-notes"])
def test_an_ordinary_name_is_accepted(addon, name):
    assert addon.safe_name(name) == name


def test_a_path_inside_the_job_resolves_and_one_outside_is_refused(addon, tmp_path):
    inside = addon.safe_subpath(tmp_path, "out/result.md")
    assert inside == (tmp_path / "out" / "result.md").resolve()

    with pytest.raises(addon.ApiError) as refused:
        addon.safe_subpath(tmp_path, "../../etc/passwd")
    assert refused.value.status == 400


def test_a_symlink_planted_in_the_job_cannot_point_out_of_it(addon, tmp_path):
    (tmp_path / "escape").symlink_to("/etc")

    with pytest.raises(addon.ApiError):
        addon.safe_subpath(tmp_path, "escape/passwd")


def test_the_base_directory_itself_is_allowed(addon, tmp_path):
    assert addon.safe_subpath(tmp_path, "") == tmp_path.resolve()


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("a\r\nSet-Cookie: x=1", "a__Set-Cookie__x_1"),
        ("///", "___"),
        ("", "download"),
        ("result.md", "result.md"),
    ],
)
def test_a_filename_cannot_start_a_header_line_of_its_own(addon, filename, expected):
    cleaned = addon.header_safe(filename)

    assert cleaned == expected
    assert "\r" not in cleaned
    assert "\n" not in cleaned


def test_a_very_long_filename_is_cut_to_something_a_header_can_carry(addon):
    assert len(addon.header_safe("x" * 500)) == 100


@pytest.mark.parametrize("target", ["latest", "stable", "2.1.228", "3"])
def test_an_update_target_the_cli_understands_is_accepted(addon, target):
    assert addon.UPDATE_TARGET.fullmatch(target) is not None


@pytest.mark.parametrize("target", ["٣٤", "--dangerous", "2.1.228; ls", "", "latest\n"])
def test_anything_else_never_reaches_the_command_line(addon, target):
    assert addon.UPDATE_TARGET.fullmatch(target) is None


# --------------------------------------------------------------------------- #
# reading and writing state
# --------------------------------------------------------------------------- #

def test_a_reader_never_sees_half_a_written_file(addon, tmp_path):
    target = tmp_path / "state.json"
    addon.write_json(target, {"n": 0})
    torn_reads = []
    stop = threading.Event()

    def keep_writing():
        counter = 0
        while not stop.is_set():
            counter += 1
            addon.write_json(target, {"n": counter, "padding": "x" * 4000})

    def keep_reading():
        while not stop.is_set():
            if addon.read_json(target) is None:
                torn_reads.append(1)

    workers = [threading.Thread(target=keep_writing), threading.Thread(target=keep_reading)]
    for worker in workers:
        worker.start()
    stop.wait(1.0)
    stop.set()
    for worker in workers:
        worker.join()

    assert torn_reads == []


def test_reading_something_that_is_not_a_file_gives_the_default(addon, tmp_path):
    assert addon.read_json(tmp_path) is None
    assert addon.read_json(tmp_path / "missing.json", default={"empty": True}) == {"empty": True}


def test_reading_broken_json_gives_the_default(addon, tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{ truncated")

    assert addon.read_json(broken, default="fallback") == "fallback"


def test_timestamps_carry_microseconds_so_two_in_a_row_can_be_ordered(addon):
    first = addon.now()
    second = addon.now()

    assert first != second
    assert sorted([second, first]) == [first, second]


# --------------------------------------------------------------------------- #
# transcripts
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("plain text", "plain text"),
        ([{"type": "text", "text": "one"}, {"type": "text", "text": "two"}], "one\ntwo"),
        ([{"type": "thinking", "thinking": "hidden"}], ""),
        ([{"type": "tool_use", "name": "Bash"}], ""),
        ([{"type": "text", "text": ""}], ""),
        (None, ""),
        (42, ""),
        (["not a block"], ""),
    ],
)
def test_only_what_was_actually_said_is_read_from_a_message(addon, content, expected):
    assert addon.block_text(content) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ([{"is_error": True, "content": "<tool_use_error>gone</tool_use_error>"}], ["gone"]),
        ([{"is_error": True, "content": [{"text": "in blocks"}]}], ["in blocks"]),
        ([{"is_error": True, "content": ""}], []),
        ([{"is_error": False, "content": "fine"}], []),
        ("not a list", []),
        ([None], []),
    ],
)
def test_a_failed_tool_call_is_found_wherever_the_cli_puts_the_text(addon, content, expected):
    assert addon.block_errors(content) == expected


def test_a_refusal_is_reported_with_the_explanation_the_api_gave(addon):
    notice = addon.notice_of(
        {
            "type": "system",
            "subtype": "model_refusal_no_fallback",
            "level": "warning",
            "content": "",
            "apiRefusalExplanation": "This request triggered a policy",
            "timestamp": "2026-08-12T10:00:00Z",
        }
    )

    assert notice["kind"] == "model_refusal_no_fallback"
    assert notice["text"] == "This request triggered a policy"


@pytest.mark.parametrize(
    "record",
    [
        {"type": "system", "subtype": "turn_duration", "level": "info", "content": "16s"},
        {"type": "system", "subtype": "informational", "level": "warning", "content": "   "},
        {"type": "user", "level": "warning", "content": "not a system record"},
        {"type": "system", "subtype": "local_command"},
    ],
)
def test_bookkeeping_is_not_mistaken_for_a_warning(addon, record):
    assert addon.notice_of(record) is None


def test_a_transcript_that_does_not_exist_reads_as_an_empty_conversation(addon):
    turns, notices = addon.read_conversation("ffffffff-0000-4000-8000-000000000000")

    assert turns == []
    assert notices == []


@pytest.mark.parametrize("session", ["../../etc/passwd", "", "not a session id"])
def test_a_session_id_that_could_be_a_path_reads_as_an_empty_conversation(addon, session):
    assert addon.read_conversation(session) == ([], [])


def test_a_half_written_line_does_not_lose_the_rest_of_the_transcript(
    addon, transcripts_dir
):
    session = "cccccccc-0000-4000-8000-000000000001"
    (transcripts_dir / f"{session}.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "kept"}})
        + "\n{ half written\n"
        + json.dumps({"type": "summary", "summary": "ignored"})
        + "\n"
        + json.dumps(
            {"type": "assistant", "message": {"role": "assistant", "content": "also kept"}}
        )
        + "\n"
    )

    turns, _ = addon.read_conversation(session)

    assert [turn["text"] for turn in turns] == ["kept", "also kept"]


def test_a_subagent_s_own_chatter_is_not_part_of_the_conversation(addon, transcripts_dir):
    session = "cccccccc-0000-4000-8000-000000000002"
    (transcripts_dir / f"{session}.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "isSidechain": True,
                "message": {"role": "assistant", "content": "subagent thinking"},
            }
        )
        + "\n"
    )

    turns, _ = addon.read_conversation(session)

    assert turns == []


def test_a_reminder_appended_to_a_message_is_removed_and_the_message_kept(
    addon, transcripts_dir
):
    session = "cccccccc-0000-4000-8000-000000000003"
    (transcripts_dir / f"{session}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "what I typed\n<system-reminder>ignore this</system-reminder>",
                },
            }
        )
        + "\n"
    )

    turns, _ = addon.read_conversation(session)

    assert [turn["text"] for turn in turns] == ["what I typed"]


def test_a_conversation_of_nothing_but_a_command_has_no_name_to_show(addon, transcripts_dir):
    session = "cccccccc-0000-4000-8000-000000000004"
    (transcripts_dir / f"{session}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "<command-name>/clear</command-name>"},
            }
        )
        + "\n"
    )

    assert addon.session_title(session) is None


def test_a_transcript_that_cannot_be_read_is_skipped_rather_than_breaking_the_list(
    addon, transcripts_dir
):
    unreadable = transcripts_dir / "dddddddd-0000-4000-8000-000000000001.jsonl"
    unreadable.mkdir()
    try:
        assert addon.scan_session(unreadable) is None
        assert unreadable.stem not in [entry["id"] for entry in addon.list_sessions()]
    finally:
        unreadable.rmdir()


def test_a_file_that_is_not_a_session_is_not_listed_as_one(addon, transcripts_dir):
    (transcripts_dir / "notes.jsonl").write_text("{}\n")
    try:
        assert "notes" not in [entry["id"] for entry in addon.list_sessions()]
    finally:
        (transcripts_dir / "notes.jsonl").unlink()


def test_a_scan_of_a_transcript_with_a_broken_line_still_reports_the_rest(
    addon, transcripts_dir
):
    path = transcripts_dir / "dddddddd-0000-4000-8000-000000000002.jsonl"
    path.write_text(
        "{ broken\n"
        + json.dumps({"type": "custom-title", "customTitle": "Named by hand"})
        + "\n"
        + json.dumps({"type": "user", "message": {"role": "user", "content": "the question"}})
        + "\n"
    )

    scanned = addon.scan_session(path)

    assert scanned["title"] == "Named by hand"
    assert scanned["custom"] is True
    assert scanned["messages"] == 1
    assert scanned["preview"] == "the question"


def test_no_transcript_directory_at_all_reads_as_no_history(addon, monkeypatch, tmp_path):
    monkeypatch.setattr(addon, "HOME", tmp_path)

    assert addon.sessions_dir() is None
    assert addon.list_sessions() == []
    assert addon.session_title("ffffffff-0000-4000-8000-000000000000") is None
    assert addon.read_conversation("ffffffff-0000-4000-8000-000000000000") == ([], [])


def test_a_projects_folder_with_nothing_of_ours_in_it_reads_as_no_history(
    addon, monkeypatch, tmp_path
):
    monkeypatch.setattr(addon, "HOME", tmp_path)
    (tmp_path / ".claude" / "projects" / "-home-someone-else").mkdir(parents=True)

    assert addon.sessions_dir() is None


def test_a_conversation_with_no_transcript_yet_has_no_name(addon, transcripts_dir):
    assert addon.session_title("eeeeeeee-0000-4000-8000-000000000000") is None


def test_the_transcript_directory_is_found_even_if_the_cli_renames_it(
    addon, monkeypatch, tmp_path
):
    monkeypatch.setattr(addon, "HOME", tmp_path)
    renamed = tmp_path / ".claude" / "projects" / "something-else-entirely-chat"
    renamed.mkdir(parents=True)

    assert addon.sessions_dir() == renamed


# --------------------------------------------------------------------------- #
# how much of the window is left
# --------------------------------------------------------------------------- #

def test_the_context_reading_comes_from_the_cli_s_own_numbers(addon):
    context = addon.context_of(
        {
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 90,
                "cache_read_input_tokens": 900,
            },
            "modelUsage": {
                "claude-opus-5": {"contextWindow": 10_000, "cacheReadInputTokens": 900},
                "claude-haiku-4-5": {"contextWindow": 200_000, "inputTokens": 5},
            },
        }
    )

    assert context == {"used": 1000, "window": 10_000, "left_percent": 90.0}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"usage": {"input_tokens": 10}},
        {"modelUsage": {"m": {"contextWindow": 1000}}},
        {"usage": "not a dict", "modelUsage": {}},
        {"usage": {"input_tokens": 0}, "modelUsage": {"m": {"contextWindow": 1000}}},
        {"usage": {"input_tokens": 10}, "modelUsage": {"m": {"contextWindow": 0}}},
        {"usage": {"input_tokens": 10}, "modelUsage": {"m": "not a dict"}},
    ],
)
def test_a_run_that_reported_no_usage_reports_no_context(addon, payload):
    assert addon.context_of(payload) is None


def test_the_size_in_use_is_the_last_request_not_all_of_them_added_up(addon, tmp_path):
    """Three requests, growing, and a subagent's among them.

    Added together they come to more than the window; what fills the window is the last
    one of this conversation's own — 700.
    """
    stream = tmp_path / "stream.jsonl"
    stream.write_text(
        json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 500}}}) + "\n"
        + json.dumps({"type": "assistant", "parent_tool_use_id": "toolu_Task",
                      "message": {"usage": {"input_tokens": 9000}}}) + "\n"
        + json.dumps({"type": "assistant", "message": {
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 600}}}) + "\n"
        + '{"type": "assistant", "message": {"usage": {"inp'  # half-written, as it is mid-turn
    )

    assert addon.last_prompt_size(tmp_path) == 700


def test_the_turn_s_own_totals_are_used_when_the_stream_says_nothing(addon, tmp_path):
    payload = {"usage": {"input_tokens": 250}, "modelUsage": {"m": {"contextWindow": 1000}}}

    assert addon.context_of(payload, tmp_path)["used"] == 250


@pytest.mark.parametrize(
    "written",
    ["", "\n\n", '{"type": "system", "level": "warning"}\n',
     '{"type": "assistant", "message": {"usage": {}}}\n',
     '{"type": "assistant", "message": {"usage": "not a dict"}}\n'],
)
def test_a_stream_with_nothing_to_read_reports_no_size(addon, tmp_path, written):
    (tmp_path / "stream.jsonl").write_text(written)

    assert addon.last_prompt_size(tmp_path) == 0


def test_a_run_with_no_stream_file_at_all_reports_no_size(addon, tmp_path):
    assert addon.last_prompt_size(tmp_path / "nothing here") == 0


def test_a_stream_that_cannot_be_read_reports_no_size(addon, tmp_path, monkeypatch):
    (tmp_path / "stream.jsonl").write_text("{}\n")

    def refuse(*_args, **_kwargs):
        raise OSError("file is on fire")

    monkeypatch.setattr(addon.Path, "open", refuse)

    assert addon.last_prompt_size(tmp_path) == 0


def test_a_full_window_never_reads_as_less_than_nothing_left(addon):
    context = addon.context_of(
        {
            "usage": {"input_tokens": 2000},
            "modelUsage": {"m": {"contextWindow": 1000, "inputTokens": 2000}},
        }
    )

    assert context["left_percent"] == 0.0


# --------------------------------------------------------------------------- #
# the streaming output
# --------------------------------------------------------------------------- #

def test_nothing_streamed_yet_reads_as_no_text_and_no_result(addon, tmp_path):
    assert addon.stream_text(tmp_path) == ""
    assert addon.stream_result(tmp_path) is None


def test_the_reply_is_assembled_from_the_deltas(addon, tmp_path):
    (tmp_path / "stream.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "stream_event",
                        "event": {"delta": {"type": "text_delta", "text": "Hello"}},
                    }
                ),
                "",
                "{ half written",
                json.dumps(
                    {
                        "type": "stream_event",
                        "event": {"delta": {"type": "text_delta", "text": " there"}},
                    }
                ),
                json.dumps(
                    {
                        "type": "stream_event",
                        "event": {"delta": {"type": "thinking_delta", "text": "hmm"}},
                    }
                ),
            ]
        )
        + "\n"
    )

    assert addon.stream_text(tmp_path) == "Hello there"


def test_whole_blocks_are_used_when_no_deltas_arrived(addon, tmp_path):
    (tmp_path / "stream.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "all at once"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "parent_tool_use_id": "toolu_1",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "a subagent"}],
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    assert addon.stream_text(tmp_path) == "all at once"


def stream_of(*records) -> str:
    return "\n".join(json.dumps(record) for record in records) + "\n"


def said(text: str, **extra) -> dict:
    return {"type": "assistant", **extra,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def delta(text: str) -> dict:
    return {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": text}}}


def test_each_message_of_a_long_turn_is_a_line_of_its_own(addon, tmp_path):
    """A two-hour turn reported its progress every couple of minutes, and every one of those
    is a message. Glued together — which is what happened — the newest could not be told from
    the first, and a caller showing progress had nothing to show."""
    (tmp_path / "stream.jsonl").write_text(stream_of(
        delta("Три черновика написаны."), said("Три черновика написаны."),
        delta("Жду разборов."), said("Жду разборов."),
        delta("Вариант 1 доведён."),
    ))

    assert addon.stream_text(tmp_path).splitlines() == [
        "Три черновика написаны.",
        "Жду разборов.",
        "Вариант 1 доведён.",
    ], "the finished messages, then the one still being written"


def test_a_message_is_not_counted_twice_when_its_deltas_are_also_there(addon, tmp_path):
    """The CLI reports a message both ways: word by word as it comes, then whole."""
    (tmp_path / "stream.jsonl").write_text(stream_of(
        delta("Hello"), delta(" there"), said("Hello there"),
    ))

    assert addon.stream_text(tmp_path) == "Hello there"


def test_a_subagent_s_message_does_not_join_the_lines(addon, tmp_path):
    """Its words are its own conversation, and it says a great deal more than the turn does."""
    (tmp_path / "stream.jsonl").write_text(stream_of(
        said("Жду разборов."),
        said("a stylist's whole verdict", parent_tool_use_id="toolu_Task"),
        delta("Вариант 1 доведён."),
    ))

    assert addon.stream_text(tmp_path).splitlines() == ["Жду разборов.", "Вариант 1 доведён."]


def test_a_message_with_nothing_but_a_tool_call_adds_no_empty_line(addon, tmp_path):
    (tmp_path / "stream.jsonl").write_text(stream_of(
        said("Читаю анкеты."),
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}]}},
        delta("Готово."),
    ))

    assert addon.stream_text(tmp_path).splitlines() == ["Читаю анкеты.", "Готово."]


def test_a_very_long_reply_is_cut_to_what_a_job_record_will_hold(addon, tmp_path):
    (tmp_path / "stream.jsonl").write_text(
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "delta": {
                        "type": "text_delta",
                        "text": "x" * (addon.MAX_RESULT_CHARS + 50),
                    }
                },
            }
        )
        + "\n"
    )

    assert len(addon.stream_text(tmp_path)) == addon.MAX_RESULT_CHARS


def test_the_last_result_record_is_the_one_that_counts(addon, tmp_path):
    (tmp_path / "stream.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "result", "session_id": "first"}),
                "",
                "{ half written",
                json.dumps({"type": "result", "session_id": "second"}),
            ]
        )
        + "\n"
    )

    assert addon.stream_result(tmp_path)["session_id"] == "second"


def test_the_session_is_taken_from_the_first_record_that_carries_one(addon, tmp_path):
    """The `init` record, which the CLI writes before it does any work — so the answer is
    there seconds in, rather than at the end with the result."""
    session = "b3dbc67b-fe8e-432e-976b-2892995e726e"
    (tmp_path / "stream.jsonl").write_text(
        "\n".join(
            [
                "",
                "{ half written",
                json.dumps({"type": "stream_event", "event": {}}),
                json.dumps({"type": "system", "subtype": "init", "session_id": session}),
                json.dumps({"type": "result", "session_id": "0" * 12}),
            ]
        )
        + "\n"
    )

    assert addon.stream_session(tmp_path) == session


def test_a_stream_with_no_session_in_it_yet_names_no_conversation(addon, tmp_path):
    assert addon.stream_session(tmp_path) is None, "nothing written at all"

    (tmp_path / "stream.jsonl").write_text(
        json.dumps({"type": "stream_event", "event": {}}) + "\n"
    )

    assert addon.stream_session(tmp_path) is None, "and nothing that says which"


def test_a_stream_that_cannot_be_read_is_reported_as_empty(addon, tmp_path, monkeypatch):
    (tmp_path / "stream.jsonl").write_text("{}\n")

    original_open = addon.Path.open

    def refuse(self, *args, **kwargs):
        if self.name == "stream.jsonl":
            raise OSError("input/output error")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(addon.Path, "open", refuse)

    assert addon.stream_text(tmp_path) == ""
    assert addon.stream_result(tmp_path) is None
    assert addon.stream_session(tmp_path) is None


# --------------------------------------------------------------------------- #
# stopping a run
# --------------------------------------------------------------------------- #

class FakeClock:
    """Stands in for the `time` module for one caller, keeping monotonic honest."""

    def __init__(self, sleep):
        self.sleep = sleep

    monotonic = staticmethod(__import__("time").monotonic)


class StubbornProcess:
    """A process that ignores the first signal, the way a wedged CLI would."""

    def __init__(self):
        self.pid = -1
        self.signals = []

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)


def test_a_process_with_no_group_is_signalled_directly(addon):
    process = StubbornProcess()

    addon.kill_tree(process, addon.signal.SIGTERM)

    assert process.signals == [addon.signal.SIGTERM]


def test_a_process_that_cannot_be_signalled_at_all_does_not_raise(addon):
    class Unsignallable(StubbornProcess):
        def send_signal(self, sig):
            raise OSError("no such process")

    addon.kill_tree(Unsignallable(), addon.signal.SIGKILL)


def test_a_turn_that_ignores_the_stop_is_killed(addon, client, monkeypatch):
    job = addon.create_job({"prompt": "wedged"})
    addon.write_job({**job, "status": "running"})
    process = StubbornProcess()
    monkeypatch.setattr(addon, "RUNNING_JOB", job["id"])
    monkeypatch.setattr(addon, "RUNNING_PROC", process)
    monkeypatch.setattr(addon, "time", FakeClock(lambda _seconds: None))  # the settle loop

    settled = addon.cancel_job(job["id"])

    assert process.signals == [addon.signal.SIGTERM, addon.signal.SIGKILL]
    assert settled["status"] == "running", "the worker, not the caller, writes the outcome"


def test_stopping_a_turn_whose_process_has_already_gone_is_not_an_error(
    addon, monkeypatch
):
    job = addon.create_job({"prompt": "already finished"})
    addon.write_job({**job, "status": "running"})
    monkeypatch.setattr(addon, "RUNNING_JOB", job["id"])
    monkeypatch.setattr(addon, "RUNNING_PROC", None)

    settled = addon.cancel_job(job["id"])

    assert settled["id"] == job["id"]
    addon.CANCELLED.discard(job["id"])


def test_a_run_that_throws_something_unexpected_is_reported_on_the_job(
    addon, client, monkeypatch
):
    def explode(*_args, **_kwargs):
        raise RuntimeError("the volume went away")

    monkeypatch.setattr(addon, "run_claude", explode)
    job = addon.create_job({"prompt": "cannot run"})

    addon.run_job(job["id"])

    settled = addon.read_job(job["id"])
    assert settled["status"] == "failed"
    assert settled["error"] == "RuntimeError: the volume went away"


def test_a_job_record_that_cannot_be_rewritten_does_not_stop_the_reconciler(
    addon, monkeypatch
):
    job = addon.create_job({"prompt": "unwritable"})
    addon.write_job({**job, "status": "running"})

    def refuse(_job):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(addon, "write_job", refuse)

    addon.reconcile_interrupted_jobs()

    assert addon.read_job(job["id"])["status"] == "running"


# --------------------------------------------------------------------------- #
# files a run produced
# --------------------------------------------------------------------------- #

def test_no_jobs_directory_at_all_lists_no_jobs(addon, monkeypatch, tmp_path):
    monkeypatch.setattr(addon, "JOBS_DIR", tmp_path / "not-created")

    assert addon.list_jobs() == []


def test_a_stray_file_among_the_job_directories_is_ignored(addon):
    stray = addon.JOBS_DIR / "loose-note.txt"
    stray.write_text("not a job")
    try:
        assert all(job["id"] != "loose-note.txt" for job in addon.list_jobs())
    finally:
        stray.unlink()


def test_a_file_that_vanishes_mid_walk_does_not_break_the_listing(
    addon, monkeypatch, client
):
    """The tree is live: a run can delete its own scratch file mid-listing."""
    job = addon.create_job({"prompt": "produces a file"})
    (addon.JOBS_DIR / job["id"] / "result.md").write_text("done")
    original_is_file = addon.Path.is_file

    def delete_it_between_the_two_calls(self, *args, **kwargs):
        answer = original_is_file(self, *args, **kwargs)
        if self.name == "result.md":
            self.unlink()
        return answer

    monkeypatch.setattr(addon.Path, "is_file", delete_it_between_the_two_calls)

    assert addon.list_job_files(job["id"]) == []


# --------------------------------------------------------------------------- #
# skills on disk
# --------------------------------------------------------------------------- #

def test_no_skills_directory_lists_nothing_rather_than_failing(addon, monkeypatch, tmp_path):
    monkeypatch.setattr(addon, "SKILLS_DIR", tmp_path / "not-created")

    assert addon.list_skills() == []
    assert addon.count_skills() == 0


@pytest.mark.parametrize(
    ("frontmatter", "field", "expected"),
    [
        ("---\nname: plain\n---\n", "name", "plain"),
        ("---\ndescription: 'quoted'\n---\n", "description", "quoted"),
        ('---\ndescription: "double"\n---\n', "description", "double"),
        (
            "---\ndescription: >-\n  folded over\n  two lines\n---\n",
            "description",
            "folded over two lines",
        ),
        ("---\ndescription: |\n  kept\n---\n", "description", "kept"),
        ("---\ndescription:\n  indented\nname: after\n---\n", "description", "indented"),
        ("---\nname: only\n---\n", "description", None),
        ("no frontmatter at all\n", "name", None),
        ("---\nname: unterminated\n", "name", None),
        ("---\ndescription:\n---\n", "description", None),
    ],
)
def test_one_field_is_read_out_of_a_skill_s_frontmatter(
    addon, tmp_path, frontmatter, field, expected
):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(frontmatter)

    assert addon.frontmatter_field(skill_md, field) == expected


def test_a_skill_md_that_cannot_be_read_yields_nothing(addon, tmp_path):
    assert addon.frontmatter_field(tmp_path, "name") is None


def test_a_skill_whose_files_vanish_mid_walk_still_reports(addon, monkeypatch):
    skill = addon.SKILLS_DIR / "vanishing"
    skill.mkdir(exist_ok=True)
    (skill / "notes.md").write_text("about to disappear")
    original_is_file = addon.Path.is_file

    def delete_it_between_the_two_calls(self, *args, **kwargs):
        answer = original_is_file(self, *args, **kwargs)
        if self.name == "notes.md":
            self.unlink()
        return answer

    monkeypatch.setattr(addon.Path, "is_file", delete_it_between_the_two_calls)
    try:
        meta = addon.skill_meta(skill)
    finally:
        monkeypatch.undo()
        addon.delete_skill("vanishing")

    assert meta["name"] == "vanishing"
    assert meta["files"] == 1
    assert meta["bytes"] == 0


def test_a_skill_directory_that_is_no_longer_there_still_reports(addon, tmp_path):
    meta = addon.skill_meta(tmp_path / "deleted-while-we-looked")

    assert meta["files"] == 0
    assert meta["updated_at"] is None
    assert meta["has_skill_md"] is False


# --------------------------------------------------------------------------- #
# the CLI, when it will not behave
# --------------------------------------------------------------------------- #

def test_a_binary_that_cannot_be_run_reports_no_version(addon, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("exec format error")

    monkeypatch.setattr(addon.subprocess, "run", refuse)
    addon.INSTALLED_CACHE.update(version=None, checked_at=float("-inf"))

    assert addon.installed_version(force=True) is None


def test_a_short_cli_call_that_cannot_start_is_reported_not_raised(addon, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise subprocess.SubprocessError("no pty")

    monkeypatch.setattr(addon.subprocess, "run", refuse)

    ok, text = addon.run_cli(["plugin", "list"])

    assert ok is False
    assert "SubprocessError" in text


def test_an_install_that_hangs_is_reported_as_a_timeout(addon, monkeypatch):
    def hang(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="claude install", timeout=900)

    monkeypatch.setattr(addon.subprocess, "run", hang)

    addon.start_update("latest", wait=True)

    state = addon.read_update_state()
    assert state["status"] == "failed"
    assert state["error"] == "claude install timed out"
    assert not addon.CLI_LOCK.locked()


def test_an_install_that_throws_something_else_entirely_is_still_reported(
    addon, monkeypatch
):
    def explode(*_args, **_kwargs):
        raise MemoryError("out of memory")

    monkeypatch.setattr(addon.subprocess, "run", explode)

    addon.start_update("latest", wait=True)

    assert addon.read_update_state()["error"] == "MemoryError: out of memory"
    assert not addon.CLI_LOCK.locked()


def test_the_lock_is_given_back_if_the_update_cannot_even_be_recorded(addon, monkeypatch):
    def refuse(_path, _data):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(addon, "write_json", refuse)

    with pytest.raises(OSError):
        addon.start_update("latest")

    assert not addon.CLI_LOCK.locked()


def test_a_cli_that_does_not_run_after_an_interrupted_install_is_called_out(
    addon, stub_behaviour, capsys
):
    addon.write_json(addon.UPDATE_STATE_PATH, {"status": "running", "target": "latest"})
    stub_behaviour("STUB_VERSION", "")
    addon.INSTALLED_CACHE.update(version=None, checked_at=float("-inf"))

    addon.reconcile_interrupted_update()

    assert "does not run after an interrupted update" in capsys.readouterr().out
    addon.INSTALLED_CACHE.update(version=None, checked_at=float("-inf"))


def test_the_daily_loop_keeps_checking(addon, monkeypatch):
    passes = []

    class Stop(Exception):
        pass

    monkeypatch.setattr(addon, "auto_update_pass", lambda: passes.append("checked"))

    slept = []

    def sleep_once(seconds):
        slept.append(seconds)
        raise Stop

    # Only this loop loses its clock: the worker and cancel_job sleep on the same
    # module, and patching it globally would stop them too.
    monkeypatch.setattr(addon, "time", FakeClock(sleep_once))

    with pytest.raises(Stop):
        addon.auto_update_loop()

    assert passes == ["checked"]
    assert slept == [addon.CHECK_INTERVAL_SEC]


# --------------------------------------------------------------------------- #
# starting up
# --------------------------------------------------------------------------- #

def test_with_no_token_the_api_is_bound_to_localhost_only(addon, monkeypatch):
    bound = {}

    class FakeServer:
        def __init__(self, address, _handler):
            bound["address"] = address

        def serve_forever(self):
            bound["served"] = True

    started = []
    monkeypatch.setattr(addon, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(addon, "API_TOKEN", "")
    monkeypatch.setattr(
        addon.threading,
        "Thread",
        lambda target, daemon=False: type(
            "Recorded", (), {"start": lambda _self: started.append(target.__name__)}
        )(),
    )

    addon.main()

    assert bound["address"] == ("127.0.0.1", addon.PORT)
    assert bound["served"] is True
    assert started == ["worker", "auto_update_loop", "limit_watch"]


def test_with_a_token_the_api_is_offered_to_the_network(addon, monkeypatch):
    bound = {}

    class FakeServer:
        def __init__(self, address, _handler):
            bound["address"] = address

        def serve_forever(self):
            pass

    monkeypatch.setattr(addon, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(
        addon.threading,
        "Thread",
        lambda target, daemon=False: type("Recorded", (), {"start": lambda _self: None})(),
    )

    addon.main()

    assert bound["address"] == ("0.0.0.0", addon.PORT)


def test_without_a_token_a_request_needs_none(addon, client, monkeypatch):
    monkeypatch.setattr(addon, "API_TOKEN", "")

    answer = client.get("/health", token=None)

    assert answer.status == 200


# --------------------------------------------------------------------------- #
# whose clock the times are on
# --------------------------------------------------------------------------- #

def test_the_house_s_own_timezone_is_what_home_assistant_set(addon, monkeypatch):
    """Every add-on is handed it in TZ; times shown to a person should be the ones on the
    clock they are looking at."""
    monkeypatch.setitem(addon.TIMEZONE_CACHE, "value", None)
    monkeypatch.setenv("TZ", "Asia/Yekaterinburg")

    assert addon.addon_timezone() == "Asia/Yekaterinburg"


def test_without_it_the_supervisor_is_asked(addon, monkeypatch):
    monkeypatch.setitem(addon.TIMEZONE_CACHE, "value", None)
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    asked = {}

    class Answer:
        def read(self):
            return b'{"result": "ok", "data": {"timezone": "Europe/Berlin"}}'

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_open(request, timeout=None):
        asked["url"] = request.full_url
        asked["auth"] = request.headers.get("Authorization")
        return Answer()

    monkeypatch.setattr(addon.urllib.request, "urlopen", fake_open)

    assert addon.addon_timezone() == "Europe/Berlin"
    assert asked == {"url": "http://supervisor/info", "auth": "Bearer tok"}


def test_a_supervisor_that_does_not_answer_leaves_the_times_in_utc(addon, monkeypatch):
    monkeypatch.setitem(addon.TIMEZONE_CACHE, "value", None)
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")

    def unreachable(*_args, **_kwargs):
        raise addon.urllib.error.URLError("no supervisor here")

    monkeypatch.setattr(addon.urllib.request, "urlopen", unreachable)

    assert addon.addon_timezone() == "UTC"


def test_it_is_asked_for_once_and_remembered(addon, monkeypatch):
    monkeypatch.setitem(addon.TIMEZONE_CACHE, "value", None)
    monkeypatch.setenv("TZ", "Asia/Yekaterinburg")
    addon.addon_timezone()
    monkeypatch.setenv("TZ", "Pacific/Auckland")

    assert addon.addon_timezone() == "Asia/Yekaterinburg"


def test_the_reading_carries_it_so_a_page_can_use_it(addon, client, monkeypatch):
    monkeypatch.setitem(addon.TIMEZONE_CACHE, "value", "Asia/Yekaterinburg")

    assert client.get("/health").json["timezone"] == "Asia/Yekaterinburg"


def test_the_turn_s_process_is_waited_for_rather_than_missed(addon, monkeypatch):
    """«running» is written when the worker takes the job and the CLI appears a beat later.
    A caller acting on the status at once — freezing the turn, or stopping it — used to be
    told there was nothing there."""
    monkeypatch.setattr(addon, "RUNNING_JOB", "a-job")
    monkeypatch.setattr(addon, "RUNNING_PROC", None)
    started = object()

    def spawn_soon():
        time.sleep(0.1)
        addon.RUNNING_PROC = started

    thread = threading.Thread(target=spawn_soon)
    thread.start()
    try:
        assert addon.process_of_the_running_turn() is started
    finally:
        thread.join()


def test_waiting_for_a_process_gives_up_rather_than_hanging(addon, monkeypatch):
    monkeypatch.setattr(addon, "RUNNING_JOB", "a-job")
    monkeypatch.setattr(addon, "RUNNING_PROC", None)
    monkeypatch.setattr(addon, "SPAWN_GRACE_SEC", 0.05)

    assert addon.process_of_the_running_turn() is None
