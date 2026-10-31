"""Several turns at once: different conversations side by side, one conversation in order."""

import uuid

import pytest
from conftest import wait_until
from test_jobs import create_job, wait_for_status


@pytest.fixture
def two_slots(addon, monkeypatch, stub_behaviour):
    """Room for two turns, each long enough to still be running when it is looked at."""
    monkeypatch.setattr(addon, "MAX_PARALLEL", 2)
    with addon.SLOTS:
        addon.SLOTS.notify_all()
    stub_behaviour("STUB_SLEEP", "300")
    started = []
    yield started
    for job_id in started:
        if addon.read_job(job_id)["status"] not in addon.TERMINAL_STATUSES:
            addon.cancel_job(job_id)


def start(client, started, prompt, **fields):
    job = create_job(client, prompt, start=True, **fields)
    started.append(job["id"])
    return job["id"]


def test_two_conversations_run_side_by_side(addon, client, two_slots):
    first = start(client, two_slots, "one conversation", chat=True, source="alice")
    second = start(client, two_slots, "another", chat=True, source="alice")

    wait_for_status(client, first, "running")
    wait_for_status(client, second, "running")

    health = client.get("/health").json
    assert health["jobs_running"] == 2
    assert health["max_parallel"] == 2


def test_a_conversation_still_takes_its_turns_one_at_a_time(addon, client, two_slots):
    """Two turns resuming one transcript would both write it; the second waits, and a turn
    of another conversation goes past it meanwhile."""
    session = str(uuid.uuid4())
    first = start(client, two_slots, "first", chat=True, source="alice", resume=session)
    second = start(client, two_slots, "second", chat=True, source="alice", resume=session)
    other = start(client, two_slots, "elsewhere", chat=True, source="alice")

    wait_for_status(client, first, "running")
    wait_for_status(client, other, "running")
    assert client.get(f"/jobs/{second}").json["status"] == "queued"

    addon.cancel_job(first)
    wait_for_status(client, second, "running")


def test_no_more_turns_run_than_there_are_slots(addon, client, two_slots):
    running = [start(client, two_slots, f"turn {n}") for n in range(2)]
    waiting = start(client, two_slots, "one too many")

    for job_id in running:
        wait_for_status(client, job_id, "running")

    assert client.get(f"/jobs/{waiting}").json["status"] == "queued"
    assert client.get("/health").json["queued"] == 1


def test_an_update_waits_for_every_turn_not_only_the_first(addon, client, two_slots):
    start(client, two_slots, "still working")
    wait_until(lambda: addon.turns_running(), description="a turn to be running")

    answer = client.send_json("POST", "/update")

    assert answer.status == 409
    assert not addon.CLI_LOCK.locked()


@pytest.mark.parametrize(
    ("job", "lane"),
    [
        ({"id": "a", "chat": True}, "console"),
        ({"id": "a", "chat": True, "source": "console"}, "console"),
        ({"id": "a", "chat": True, "source": "console", "resumed_from": "s"}, "s"),
        ({"id": "a", "chat": True, "source": "alice", "resumed_from": "s"}, "s"),
        ({"id": "a", "chat": True, "source": "alice"}, "a"),
        ({"id": "a"}, "a"),
    ],
)
def test_which_conversation_a_job_waits_behind(addon, job, lane):
    assert addon.lane_of(job) == lane
