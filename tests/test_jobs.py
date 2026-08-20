"""Jobs: what another add-on drives over the API — a prompt, files, a result."""

import json
import threading

import pytest
from conftest import wait_until


def create_job(client, prompt="do a thing", **fields):
    answer = client.send_json("POST", "/jobs", {"prompt": prompt, **fields})
    assert answer.status == 201, answer
    return answer.json


def wait_for_status(client, job_id, status):
    return wait_until(
        lambda: client.get(f"/jobs/{job_id}").json.get("status") == status,
        description=f"job {job_id} to be {status}",
    )


def test_a_prompt_with_an_input_file_runs_and_reports_what_it_produced(client):
    job = create_job(client, "read in/input.txt")
    assert job["status"] == "created"

    upload = client.request("PUT", f"/jobs/{job['id']}/files/input.txt", body=b"hello")
    assert upload.status == 201
    assert upload.json == {"path": "in/input.txt"}

    started = client.send_json("POST", f"/jobs/{job['id']}/start")
    assert started.status == 200
    assert started.json["status"] == "queued"

    wait_for_status(client, job["id"], "done")

    finished = client.get(f"/jobs/{job['id']}").json
    assert finished["result"].startswith("stub reply to: read in/input.txt")
    assert finished["exit_code"] == 0
    produced = [entry["path"] for entry in finished["files"]]
    assert "claude.log" in produced
    assert "in/input.txt" not in produced
    assert "job.json" not in produced
    assert "stream.jsonl" not in produced


def test_a_produced_file_can_be_downloaded_by_name(client):
    job = create_job(client, "write something", start=True)
    wait_for_status(client, job["id"], "done")

    download = client.get(f"/jobs/{job['id']}/files/claude.log")

    assert download.status == 200
    assert "attachment" in download.headers["Content-Disposition"]
    assert 'filename="claude.log"' in download.headers["Content-Disposition"]


def test_asking_for_a_file_the_run_never_wrote_is_a_404(client):
    job = create_job(client, "write something", start=True)
    wait_for_status(client, job["id"], "done")

    answer = client.get(f"/jobs/{job['id']}/files/not-written.txt")

    assert answer.status == 404
    assert "no such file" in answer.json["error"]


def test_the_file_listing_can_be_asked_for_on_its_own(client):
    job = create_job(client, "write something", start=True)
    wait_for_status(client, job["id"], "done")

    listing = client.get(f"/jobs/{job['id']}/files")

    assert listing.status == 200
    assert "claude.log" in [entry["path"] for entry in listing.json["files"]]


def test_an_upload_path_that_climbs_out_of_the_job_is_refused(client):
    job = create_job(client)

    answer = client.request("PUT", f"/jobs/{job['id']}/files/../escaped.txt", body=b"nope")

    assert answer.status == 400
    assert "escapes the job directory" in answer.json["error"]


def test_an_upload_without_a_name_is_refused(client):
    job = create_job(client)

    answer = client.request("PUT", f"/jobs/{job['id']}/files", body=b"nope")

    assert answer.status == 400
    assert answer.json["error"] == "a file name is required"


def test_an_upload_into_a_subfolder_is_kept(client):
    job = create_job(client)

    answer = client.request("PUT", f"/jobs/{job['id']}/files/nested/deep.txt", body=b"kept")

    assert answer.status == 201
    assert answer.json["path"] == "in/nested/deep.txt"


def test_uploading_to_a_job_that_has_already_started_is_refused(client):
    job = create_job(client, start=True)

    answer = client.request("PUT", f"/jobs/{job['id']}/files/late.txt", body=b"too late")

    assert answer.status == 409
    wait_for_status(client, job["id"], "done")


def test_starting_the_same_job_twice_is_refused(client):
    job = create_job(client)

    first = client.send_json("POST", f"/jobs/{job['id']}/start")
    second = client.send_json("POST", f"/jobs/{job['id']}/start")

    assert first.status == 200
    assert second.status == 409
    assert "already queued" in second.json["error"] or "already running" in second.json["error"]
    wait_for_status(client, job["id"], "done")


def test_a_job_that_does_not_exist_is_a_404(client):
    answer = client.get("/jobs/deadbeefdead")

    assert answer.status == 404
    assert "no such job" in answer.json["error"]


def test_a_job_id_that_could_escape_the_jobs_directory_is_refused(client):
    answer = client.get("/jobs/..")

    assert answer.status in (400, 404)


def test_a_finished_job_can_be_deleted_with_its_files(addon, client):
    job = create_job(client, start=True)
    wait_for_status(client, job["id"], "done")

    answer = client.request("DELETE", f"/jobs/{job['id']}")

    assert answer.status == 200
    assert answer.json == {"deleted": job["id"]}
    assert not (addon.JOBS_DIR / job["id"]).exists()
    assert client.get(f"/jobs/{job['id']}").status == 404


def test_a_running_job_cannot_be_deleted_out_from_under_the_process(
    client, stub_behaviour
):
    stub_behaviour("STUB_SLEEP", "12")
    job = create_job(client, start=True)
    wait_for_status(client, job["id"], "running")

    answer = client.request("DELETE", f"/jobs/{job['id']}")

    assert answer.status == 409
    assert "cannot delete it yet" in answer.json["error"]

    client.send_json("POST", f"/jobs/{job['id']}/cancel")


def test_the_job_list_is_newest_first(client):
    older = create_job(client, "older")
    newer = create_job(client, "newer")

    listed = [job["id"] for job in client.get("/jobs").json["jobs"]]

    assert listed.index(newer["id"]) < listed.index(older["id"])


def test_a_directory_without_a_readable_job_file_is_skipped_rather_than_breaking_the_list(
    addon, client
):
    (addon.JOBS_DIR / "brokenjob01").mkdir()
    (addon.JOBS_DIR / "brokenjob02").mkdir()
    (addon.JOBS_DIR / "brokenjob02" / "job.json").write_text("{ truncated")

    answer = client.get("/jobs")

    assert answer.status == 200
    listed = [job["id"] for job in answer.json["jobs"]]
    assert "brokenjob01" not in listed
    assert "brokenjob02" not in listed


def test_only_the_newest_finished_jobs_are_kept(addon, client, monkeypatch):
    monkeypatch.setattr(addon, "JOBS_KEPT", 3)
    created = [create_job(client, f"job {index}") for index in range(6)]
    for job in created:
        finished = {**job, "status": "done"}
        addon.write_job(finished)

    create_job(client, "the one that triggers pruning")

    surviving = [
        job for job in addon.list_jobs() if job.get("status") == "done" and not job.get("chat")
    ]
    assert len(surviving) == 3


def test_chat_turns_are_pruned_separately_from_jobs(addon, client, monkeypatch):
    monkeypatch.setattr(addon, "JOBS_KEPT", 1)
    monkeypatch.setattr(addon, "CHAT_KEPT", 500)
    chat_turn = create_job(client, "a chat turn", chat=True)
    addon.write_job({**chat_turn, "status": "done"})

    create_job(client, "an ordinary job")
    create_job(client, "another ordinary job")

    assert addon.read_job(chat_turn["id"])["id"] == chat_turn["id"]


def test_a_restart_in_the_middle_of_a_run_leaves_no_job_stuck_as_running(addon, client):
    job = create_job(client, "interrupted by a restart")
    addon.write_job({**job, "status": "running"})

    addon.reconcile_interrupted_jobs()

    recovered = client.get(f"/jobs/{job['id']}").json
    assert recovered["status"] == "failed"
    assert recovered["error"] == "the add-on restarted while this job was in flight"
    assert recovered["finished_at"]


def test_concurrent_starts_enqueue_the_job_once(client):
    job = create_job(client, "started twice at once")
    outcomes = []
    both_ready = threading.Barrier(2)

    def start_it():
        both_ready.wait()
        answer = client.send_json("POST", f"/jobs/{job['id']}/start")
        outcomes.append(answer.status)

    racers = [threading.Thread(target=start_it) for _ in range(2)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join()

    assert sorted(outcomes) == [200, 409]
    wait_for_status(client, job["id"], "done")


def test_a_job_carries_the_model_effort_and_mode_it_was_asked_for(client):
    job = create_job(client, "with settings", model="haiku", effort="high", permission_mode="plan")

    assert job["model"] == "haiku"
    assert job["effort"] == "high"
    assert job["permission_mode"] == "plan"


def test_a_job_falls_back_to_the_add_on_s_own_defaults(addon, client):
    job = create_job(client, "with no settings")

    assert job["model"] == addon.DEFAULT_MODEL
    assert job["effort"] == addon.DEFAULT_EFFORT
    assert job["permission_mode"] == addon.DEFAULT_PERMISSION_MODE


def test_the_worker_survives_a_job_that_crashes_it(addon, client, monkeypatch):
    # Waited for by the crash itself, not by the queue emptying: the queue hands the job
    # over before the worker has done anything with it, so waiting on that alone let the
    # patch below be undone in between — and the real thing then ran the job, which is a
    # green test proving nothing. Seen once in CI, where the machine is slow enough.
    crashed = threading.Event()

    def explode(job_id):
        crashed.set()
        raise RuntimeError("something went badly wrong")

    monkeypatch.setattr(addon, "run_job", explode)
    crashing = create_job(client, "crashes the worker", start=True)
    wait_until(
        lambda: crashed.is_set() and addon.JOB_QUEUE.qsize() == 0 and addon.RUNNING_JOB is None,
        description="the worker to swallow the crash",
    )
    monkeypatch.undo()

    surviving = create_job(client, "runs after the crash", start=True)
    wait_for_status(client, surviving["id"], "done")

    assert client.get(f"/jobs/{crashing['id']}").json["status"] == "queued"


def test_a_job_stopped_while_it_waited_is_never_run(addon, client):
    job = create_job(client, "cancelled before the worker got to it")
    addon.write_job({**job, "status": "failed", "error": "stopped before it started"})

    addon.JOB_QUEUE.put(job["id"])

    wait_until(
        lambda: addon.JOB_QUEUE.qsize() == 0,
        description="the worker to skip the stopped job",
    )

    assert client.get(f"/jobs/{job['id']}").json["status"] == "failed"
    assert client.get(f"/jobs/{job['id']}").json["started_at"] is None


def test_a_prompt_can_name_the_job_s_own_directory(addon, client):
    job = create_job(client, "read {job_dir}/in/form.docx, write {job_dir}/out.zip")

    expected = str(addon.JOBS_DIR / job["id"])
    assert job["prompt"] == f"read {expected}/in/form.docx, write {expected}/out.zip"


def test_the_job_id_can_be_named_on_its_own(client):
    job = create_job(client, "this is job {job_id}")

    assert job["prompt"] == f"this is job {job['id']}"


def test_a_prompt_without_either_is_left_exactly_as_it_was(client):
    job = create_job(client, "nothing to fill in {here} or {json}")

    assert job["prompt"] == "nothing to fill in {here} or {json}"


def test_a_running_job_says_what_it_is_doing(addon, client, stub_behaviour):
    """A long run can go minutes without a word; a caller polling it needs movement."""
    stub_behaviour("STUB_SLEEP", "12")
    stub_behaviour("STUB_TOOLS", "1")
    job = create_job(client, "reads two files", start=True)
    wait_for_status(client, job["id"], "running")

    reported = wait_until(
        lambda: client.get(f"/jobs/{job['id']}").json.get("activity"),
        description="the tool calls to be reported",
    )

    assert [step["tool"] for step in reported] == ["Read", "Bash"]
    assert reported[0]["target"] == "notes.docx"
    assert reported[1]["target"] == "python3 -c 'zipfile'"
    assert client.get(f"/jobs/{job['id']}").json["partial"]

    client.send_json("POST", f"/jobs/{job['id']}/cancel")


def test_a_finished_job_is_not_asked_what_it_is_doing(client):
    job = create_job(client, "quick", start=True)
    wait_for_status(client, job["id"], "done")

    finished = client.get(f"/jobs/{job['id']}").json

    assert "activity" not in finished
    assert "partial" not in finished


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"file_path": "/data/jobs/x/in/Марина.docx"}, "Марина.docx"),
        ({"path": "/var/deep/notes.md"}, "notes.md"),
        ({"command": "python3 -c 'x'\nsecond line"}, "python3 -c 'x'"),
        ({"pattern": "*.docx"}, "*.docx"),
        ({"description": "look something up"}, "look something up"),
        ({}, ""),
        ("not a dict", ""),
    ],
)
def test_a_tool_call_is_summarised_by_what_it_was_pointed_at(addon, arguments, expected):
    assert addon.tool_target(arguments) == expected


def test_the_job_record_on_disk_is_json_a_caller_could_read(addon, client):
    job = create_job(client, "written to disk")

    stored = json.loads((addon.JOBS_DIR / job["id"] / "job.json").read_text())

    assert stored["prompt"] == "written to disk"
    assert stored["created_at"].endswith("+00:00")


def test_a_running_turn_can_be_frozen_and_let_go_again(addon, client, stub_behaviour):
    """Frozen is not stopped: nothing is lost, and nothing more is spent."""
    stub_behaviour("STUB_SLEEP", "20")
    job = create_job(client, "long enough to freeze", start=True)
    wait_for_status(client, job["id"], "running")

    frozen = client.send_json("POST", f"/jobs/{job['id']}/pause")

    assert frozen.status == 200
    assert frozen.json["paused"] is True
    assert frozen.json["changed"] is True
    assert frozen.json["paused_at"]
    assert client.get("/health").json["job_paused"] is True
    assert client.get(f"/jobs/{job['id']}").json["status"] == "running", "still its own turn"

    again = client.send_json("POST", f"/jobs/{job['id']}/pause")
    assert again.json["changed"] is False, "freezing twice is not an error"

    thawed = client.send_json("POST", f"/jobs/{job['id']}/resume")
    assert thawed.json["paused"] is False
    assert thawed.json["changed"] is True
    assert client.get("/health").json["job_paused"] is False

    wait_for_status(client, job["id"], "done")
    assert client.get(f"/jobs/{job['id']}").json["result"].startswith("stub reply")


def test_letting_go_a_turn_that_was_never_frozen_changes_nothing(client, stub_behaviour):
    stub_behaviour("STUB_SLEEP", "10")
    job = create_job(client, "not frozen", start=True)
    wait_for_status(client, job["id"], "running")

    answer = client.send_json("POST", f"/jobs/{job['id']}/resume")

    assert answer.json["changed"] is False
    client.send_json("POST", f"/jobs/{job['id']}/cancel")


def test_a_frozen_turn_can_still_be_stopped(addon, client, stub_behaviour):
    """A stop reaches a frozen process only after it is let go, so it is let go first."""
    stub_behaviour("STUB_SLEEP", "30")
    job = create_job(client, "frozen then stopped", start=True)
    wait_for_status(client, job["id"], "running")
    client.send_json("POST", f"/jobs/{job['id']}/pause")

    stopped = client.send_json("POST", f"/jobs/{job['id']}/cancel")

    assert stopped.json["status"] == "failed"
    assert stopped.json["error"] == "stopped"
    assert addon.PAUSED_AT is None


@pytest.mark.parametrize("what", ["pause", "resume"])
def test_only_a_running_turn_can_be_frozen(client, what):
    job = create_job(client, "not started")

    answer = client.send_json("POST", f"/jobs/{job['id']}/{what}")

    assert answer.status == 409
    assert "only a running turn" in answer.json["error"]


def test_a_source_that_could_escape_a_field_is_refused(client):
    answer = client.send_json("POST", "/jobs", {"prompt": "x", "source": "../etc"})

    assert answer.status == 400
    assert "unsafe source" in answer.json["error"]


def test_a_frozen_turn_does_not_spend_its_own_timeout(addon, client, stub_behaviour, monkeypatch):
    """The clock stops while the run is stopped, or a pause would end it."""
    monkeypatch.setattr(addon, "TIMEOUT_SEC", 4)
    stub_behaviour("STUB_SLEEP", "2")
    job = create_job(client, "frozen while the clock would have run out", start=True)
    wait_for_status(client, job["id"], "running")

    client.send_json("POST", f"/jobs/{job['id']}/pause")
    wait_until(lambda: addon.PAUSED_AT is not None, description="the freeze to take")
    import time as clock

    clock.sleep(3)  # so the wall clock outlasts the timeout while the run does not
    assert client.get(f"/jobs/{job['id']}").json["status"] == "running", "not timed out"

    client.send_json("POST", f"/jobs/{job['id']}/resume")

    wait_for_status(client, job["id"], "done")


def test_nothing_streamed_yet_means_nothing_to_report(addon, tmp_path):
    assert addon.stream_activity(tmp_path) == []


def test_a_half_written_stream_still_reports_what_it_can(addon, tmp_path):
    # Only lines mentioning a tool call are looked at, so the broken ones here say
    # "tool_use" too — otherwise they would be skipped before anything could go wrong.
    (tmp_path / "stream.jsonl").write_text(
        "\n".join([
            '{ "type": "assistant", "tool_use" truncated',
            json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_use", "name": "NotClaude"}]}}),
            json.dumps({"type": "assistant", "tool_use": True,
                        "message": {"role": "assistant", "content": "prose, not blocks"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "a line that says tool_use and means nothing"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ]}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "thinking out loud"},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/x/form.docx"}},
            ]}}),
        ]) + "\n"
    )

    # In the order they were made, which is what a reader wants to see.
    assert addon.stream_activity(tmp_path) == [
        {"tool": "Bash", "target": "ls"},
        {"tool": "Read", "target": "form.docx"},
    ]


def test_a_stream_that_cannot_be_opened_reports_nothing(addon, tmp_path, monkeypatch):
    (tmp_path / "stream.jsonl").write_text("{}\n")
    original_open = addon.Path.open

    def refuse(self, *args, **kwargs):
        if self.name == "stream.jsonl":
            raise OSError("input/output error")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(addon.Path, "open", refuse)

    assert addon.stream_activity(tmp_path) == []


def test_a_job_that_was_made_and_never_started_can_be_thrown_away(client):
    """It owns nothing — no process, no place in the queue. Refusing to delete one left it
    in the console's waiting list for good, with the remove button answering 409."""
    job = create_job(client, "made, never started")

    gone = client.send_json("DELETE", f"/jobs/{job['id']}")

    assert gone.status == 200
    assert client.get(f"/jobs/{job['id']}").status == 404
