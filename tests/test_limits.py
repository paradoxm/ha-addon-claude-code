"""The add-on acting on the plan's allowance itself, rather than reporting it and hoping.

Why it acts: it owns the process. It is the only part of this that can stop the CLI and
every subagent under it the moment the wall appears — and it happens whether or not
anybody is watching, which is exactly what went wrong when nobody was.
"""

import signal
import time

import pytest
from conftest import wait_until
from test_jobs import create_job, wait_for_status


@pytest.fixture
def running(client, stub_behaviour, addon, monkeypatch):
    """A turn that is really running, so there is a process group to freeze.

    The guard is off unless a test turns it on: `main()` is never called here, so no
    watching thread is running, and tests call the guard themselves — which is the thing
    under test. Off by default so nothing reads the allowance behind a test's back.
    """
    monkeypatch.setattr(addon, "GUARD_LIMITS", False)
    # Generous on purpose: the stand-in sleeps by the wall clock, and freezing it does not
    # stop that clock. Thirty seconds ran out mid-test under a full suite, and the turn
    # ended between one freeze and the next.
    stub_behaviour("STUB_SLEEP", "300")
    job = create_job(client, "long enough to freeze", start=True)
    wait_for_status(client, job["id"], "running")
    yield job["id"]
    client.send_json("POST", f"/jobs/{job['id']}/cancel")
    wait_for_status(client, job["id"], "failed")


def usage_says(addon, monkeypatch, percent, kind="session", available=True, back_in_hours=4):
    """A reading, with its reset time relative to now.

    Written as a fixed stamp it passed until that moment arrived and then began failing on
    its own — a test that depends on the calendar is not a test.
    """
    reading = {"available": False, "checked_at": "now"}
    if available:
        back = addon.datetime.now(addon.UTC) + addon.timedelta(hours=back_in_hours)
        limit = addon.WEEK_THRESHOLD if kind == "week" else addon.SESSION_THRESHOLD
        worst = {"kind": kind, "percent": percent, "threshold": limit,
                 "resets_at": back.isoformat()}
        reading = {
            "available": True,
            "worst": worst,
            "thresholds": {"session": addon.SESSION_THRESHOLD, "week": addon.WEEK_THRESHOLD},
            "enough": percent < limit,
            "checked_at": "now",
        }
    monkeypatch.setattr(addon, "read_usage", lambda force=False: reading)
    return reading


def test_a_turn_is_refused_when_the_allowance_is_already_spent(addon, client, monkeypatch):
    reading = usage_says(addon, monkeypatch, 96.0)

    created = client.send_json("POST", "/jobs", {"prompt": "write the texts"})
    answer = client.send_json("POST", f"/jobs/{created.json['id']}/start")

    assert answer.status == 429
    assert "96.0% used" in answer.json["error"]
    # As somebody would say it, on this machine's clock — not a stamp with microseconds,
    # which is what a person was shown when a send was refused.
    stamp = reading["worst"]["resets_at"]
    assert addon.when_for_people(stamp) in answer.json["error"]
    assert stamp not in answer.json["error"]
    assert client.get(f"/jobs/{created.json['id']}").json["status"] == "created", (
        "and it stays as it was, so the caller may start it later without making it again"
    )


def test_a_turn_starts_when_there_is_room(addon, client, monkeypatch):
    usage_says(addon, monkeypatch, 12.0)

    created = client.send_json("POST", "/jobs", {"prompt": "write the texts"})
    answer = client.send_json("POST", f"/jobs/{created.json['id']}/start")

    assert answer.status == 200
    assert answer.json["status"] == "queued"
    wait_for_status(client, created.json["id"], "done")


def test_an_allowance_that_cannot_be_read_never_stops_a_turn(addon, client, monkeypatch):
    """The endpoint is undocumented. Its silence must not become a way to lose runs."""
    usage_says(addon, monkeypatch, 0, available=False)

    created = client.send_json("POST", "/jobs", {"prompt": "write the texts"})

    assert client.send_json("POST", f"/jobs/{created.json['id']}/start").status == 200
    wait_for_status(client, created.json["id"], "done")


def test_with_the_guard_off_the_add_on_only_reports(addon, client, monkeypatch):
    usage_says(addon, monkeypatch, 99.0)
    monkeypatch.setattr(addon, "GUARD_LIMITS", False)

    created = client.send_json("POST", "/jobs", {"prompt": "write the texts"})

    assert client.send_json("POST", f"/jobs/{created.json['id']}/start").status == 200
    wait_for_status(client, created.json["id"], "done")


@pytest.mark.parametrize(
    ("percent", "expected_minutes"),
    [(10.0, 15), (65.0, 15), (75.0, 5), (88.0, 2), (89.9, 2), (95.0, 2)],
)
def test_the_closer_to_the_wall_the_closer_it_looks(
    addon, monkeypatch, percent, expected_minutes
):
    """A reading costs a request to Anthropic; asking every minute for half an hour is
    both wasteful and rude. Far from the threshold the answer cannot change fast enough
    to matter."""
    assert addon.SESSION_THRESHOLD == 90, "the ladder below is written against ninety"
    # With the setting at its default the closest step is the setting, so the ladder is
    # only visible below it; how the two combine is its own test.
    monkeypatch.setattr(addon, "USAGE_TTL_SEC", 30)

    worst = {"kind": "session", "percent": percent, "threshold": addon.SESSION_THRESHOLD}
    assert addon.watch_interval(worst) == expected_minutes * 60


def test_with_no_reading_at_all_it_looks_as_rarely_as_it_ever_does(addon):
    assert addon.watch_interval(None) == 15 * 60


def test_a_running_turn_is_frozen_when_the_window_runs_out(addon, client, monkeypatch, running):
    reading = usage_says(addon, monkeypatch, 96.0)

    addon.hold_for_limits(running)

    job = client.get(f"/jobs/{running}").json
    assert job["paused"] is True
    assert job["paused_reason"] == "limits"
    assert job["resumes_at"] == reading["worst"]["resets_at"]


def test_and_carries_on_by_itself_once_the_window_is_back(addon, client, monkeypatch, running):
    usage_says(addon, monkeypatch, 96.0)
    addon.hold_for_limits(running)

    usage_says(addon, monkeypatch, 3.0)
    addon.hold_for_limits(running)

    job = client.get(f"/jobs/{running}").json
    assert job["paused"] is False
    assert "paused_reason" not in job
    assert "resumes_at" not in job


def test_freezing_twice_is_not_a_second_freeze(addon, monkeypatch, running):
    usage_says(addon, monkeypatch, 96.0)
    addon.hold_for_limits(running)

    assert addon.hold_for_limits(running) is None


def test_a_turn_let_go_by_hand_is_left_alone(addon, client, monkeypatch, running):
    """Whoever overrules the guard on purpose gets to keep their decision."""
    usage_says(addon, monkeypatch, 96.0)
    addon.hold_for_limits(running)

    client.send_json("POST", f"/jobs/{running}/resume")
    addon.hold_for_limits(running)

    job = client.get(f"/jobs/{running}").json
    assert job["paused"] is False
    assert job["limit_override"] is True


def test_a_turn_frozen_by_hand_is_not_thawed_by_the_guard(addon, client, monkeypatch, running):
    usage_says(addon, monkeypatch, 3.0)
    client.send_json("POST", f"/jobs/{running}/pause")

    addon.hold_for_limits(running)

    assert client.get(f"/jobs/{running}").json["paused"] is True, (
        "the guard only lets go of what the guard stopped"
    )


def test_a_reading_that_cannot_be_had_leaves_a_running_turn_alone(addon, monkeypatch, running):
    usage_says(addon, monkeypatch, 0, available=False)

    assert addon.hold_for_limits(running) is None


def test_with_nothing_running_the_watcher_barely_stirs(addon):
    """RUNNING_JOB is written by the worker thread, so it is never patched here: a test
    that lies about it also lies to the thread that is watching."""
    assert addon.RUNNING_JOB is None

    assert addon.limit_watch_once() == addon.WATCH_IDLE_SEC


def test_with_the_guard_off_the_watcher_does_not_even_ask(addon, monkeypatch, running):
    asked = []
    monkeypatch.setattr(addon, "read_usage", lambda force=False: asked.append(1) or {})

    assert addon.limit_watch_once() == addon.WATCH_IDLE_SEC
    assert asked == [], "no reading means no request to Anthropic"


def test_the_watcher_waits_by_the_ladder_while_a_turn_runs(addon, monkeypatch, running):
    usage_says(addon, monkeypatch, 40.0)
    monkeypatch.setattr(addon, "GUARD_LIMITS", True)
    monkeypatch.setattr(addon, "hold_for_limits", lambda _job: None)

    assert addon.limit_watch_once() == 15 * 60


def test_a_frozen_turn_is_looked_at_when_the_window_is_due_back(
    addon, client, monkeypatch, running
):
    client.send_json("POST", f"/jobs/{running}/pause")
    usage_says(addon, monkeypatch, 96.0)
    monkeypatch.setattr(addon, "GUARD_LIMITS", True)
    monkeypatch.setattr(addon, "hold_for_limits", lambda _job: None)

    assert addon.limit_watch_once() == addon.THAW_GRACE_SEC


def test_a_reading_that_throws_does_not_end_the_watch(addon, monkeypatch, running):
    monkeypatch.setattr(addon, "GUARD_LIMITS", True)

    def explode(*_args, **_kwargs):
        raise RuntimeError("the endpoint moved")

    monkeypatch.setattr(addon, "hold_for_limits", explode)
    monkeypatch.setattr(addon, "read_usage", lambda force=False: {"available": False})

    assert addon.limit_watch_once() == 15 * 60, "it waits as it would with no reading at all"


def test_the_watch_loop_keeps_looking(addon, monkeypatch):
    """The loop itself: one look, one wait, and round again."""
    looks, slept = [], []

    class Enough(Exception):
        pass

    def look():
        looks.append(1)
        if len(looks) > 2:
            raise Enough
        return 40.0

    monkeypatch.setattr(addon, "limit_watch_once", look)
    monkeypatch.setattr(addon.time, "sleep", lambda seconds: slept.append(seconds))

    with pytest.raises(Enough):
        addon.limit_watch()

    assert len(looks) == 3
    assert slept == [30.0, 10.0, 30.0, 10.0], "forty seconds, in the watcher's own steps"


def test_a_pause_does_not_outlive_the_turn_it_belongs_to(addon, client, stub_behaviour):
    """PAUSED_AT is one process's business.

    Left set after a turn ended, it defeated the next turn's timeout — the deadline was
    extended for every second the flag was up — and stopped the guard from ever freezing
    anything again, because it takes "already frozen" for an answer.
    """
    stub_behaviour("STUB_SLEEP", "10")
    job = create_job(client, "frozen, then finished", start=True)
    wait_for_status(client, job["id"], "running")
    client.send_json("POST", f"/jobs/{job['id']}/pause")
    assert addon.PAUSED_AT is not None

    client.send_json("POST", f"/jobs/{job['id']}/cancel")
    wait_for_status(client, job["id"], "failed")

    assert addon.PAUSED_AT is None
    assert client.get("/health").json["job_paused"] is False


def test_the_guard_freezing_into_that_same_window_stops_nothing_and_says_nothing(
    addon, monkeypatch, running
):
    """The reachable version of it: the guard decided while the process was still there,
    and by the time the signal would land it had gone."""
    usage_says(addon, monkeypatch, 96.0)
    monkeypatch.setattr(addon.RUNNING_PROC, "poll", lambda: 0)

    with pytest.raises(addon.ApiError):
        addon.hold_for_limits(running)

    assert addon.PAUSED_AT is None


def test_a_turn_that_has_just_finished_cannot_be_frozen(addon, client, stub_behaviour):
    """The window between a process exiting and the worker noticing is milliseconds wide,
    and freezing into it used to set the flag with nothing stopped."""
    job = create_job(client, "already over", start=True)
    wait_for_status(client, job["id"], "done")

    answer = client.send_json("POST", f"/jobs/{job['id']}/pause")

    assert answer.status == 409
    assert addon.PAUSED_AT is None


def test_a_window_that_will_not_be_back_for_days_is_not_worth_freezing_for(
    addon, client, monkeypatch, running
):
    """A weekly window resetting in six days would hold the frozen process — and every
    job behind it — for six days. Better to run into the wall: the conversation survives
    and can be carried on."""
    week = {"kind": "week", "percent": 96.0, "threshold": 90,
            "resets_at": (addon.datetime.now(addon.UTC) + addon.timedelta(days=6)).isoformat()}
    monkeypatch.setattr(addon, "read_usage", lambda force=False: {
        "available": True, "worst": week, "enough": False, "checked_at": "now"})

    assert addon.hold_for_limits(running) is None
    assert client.get(f"/jobs/{running}").json["paused"] is False


def test_a_frozen_turn_is_let_go_when_its_window_was_due_back(addon, client, monkeypatch, running):
    """Failing open on the way in and closed on the way out would leave a turn frozen for
    good the first time the reading went quiet."""
    usage_says(addon, monkeypatch, 96.0)
    addon.hold_for_limits(running)
    addon.update_job(running, resumes_at=(
        addon.datetime.now(addon.UTC) - addon.timedelta(minutes=10)).isoformat())
    usage_says(addon, monkeypatch, 0, available=False)

    addon.hold_for_limits(running)

    job = client.get(f"/jobs/{running}").json
    assert job["paused"] is False
    assert "paused_reason" not in job


def test_a_frozen_turn_whose_window_is_not_due_yet_stays_frozen(
    addon, client, monkeypatch, running
):
    usage_says(addon, monkeypatch, 96.0)
    addon.hold_for_limits(running)
    usage_says(addon, monkeypatch, 0, available=False)

    assert addon.hold_for_limits(running) is None
    assert client.get(f"/jobs/{running}").json["paused"] is True


def test_what_the_guard_wrote_survives_the_turn_writing_its_own_record(addon, running):
    """Two threads, one record: each writes its own fields under the lock rather than
    reading the whole thing and putting all of it back."""
    addon.update_job(running, paused_reason="limits",
                     resumes_at=(addon.datetime.now(addon.UTC)
                                 + addon.timedelta(hours=4)).isoformat())

    addon.update_job(running, partial_marker="from the other writer")

    job = addon.read_job(running)
    assert job["paused_reason"] == "limits"
    assert job["partial_marker"] == "from the other writer"


def test_a_start_the_guard_refuses_leaves_nothing_behind(addon, client, monkeypatch):
    """A chat message refused at the wall used to sit in the console's queue for good:
    nothing prunes a job that never ran, and a caller cannot delete one either."""
    usage_says(addon, monkeypatch, 96.0)

    answer = client.send_json("POST", "/jobs", {"prompt": "no room for this", "start": True})

    assert answer.status == 429
    assert not any(job["prompt"] == "no room for this" for job in client.get("/jobs").json["jobs"])


def test_an_answer_the_endpoint_never_used_to_give_does_not_stop_the_work(
    addon, client, monkeypatch
):
    """It only ever broke GET /usage before; with the guard in the way it would have
    stopped every job from starting."""
    class Nonsense:
        def read(self):
            # A shape usage_window does not defend against: the percentage as words.
            return b'{"five_hour": {"utilization": "most of it"}}'

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(addon, "GUARD_LIMITS", True)
    monkeypatch.setattr(addon.urllib.request, "urlopen", lambda *a, **k: Nonsense())
    monkeypatch.setattr(addon, "CREDENTIALS_PATH", addon.HOME / ".credentials.json")
    addon.CREDENTIALS_PATH.write_text('{"claudeAiOauth": {"accessToken": "tok"}}')
    addon.USAGE_CACHE.update(checked_at=float("-inf"), value=None)

    reading = client.get("/usage").json
    created = client.send_json("POST", "/jobs", {"prompt": "still allowed"})

    assert reading["available"] is False
    assert "unexpected answer" in reading["reason"]
    assert client.send_json("POST", f"/jobs/{created.json['id']}/start").status == 200
    wait_for_status(client, created.json["id"], "done")
    addon.CREDENTIALS_PATH.unlink(missing_ok=True)
    addon.USAGE_CACHE.update(checked_at=float("-inf"), value=None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-13T13:20:00+00:00", True),
        ("2026-08-13T13:20:00Z", True),
        ("2026-08-13T13:20:00", True),   # no zone: read as UTC, which is what it means
        ("not a time", False),
        (None, False),
        (1786646400, False),
    ],
)
def test_a_reset_time_is_read_when_it_is_one_and_ignored_when_it_is_not(addon, value, expected):
    assert (addon.parse_when(value) is not None) is expected


def test_a_frozen_turn_with_no_reset_time_is_not_let_go_on_a_guess(addon):
    assert addon.due_back({"resumes_at": None}) is False


# --------------------------------------------------------------------------- #
# that the guard is wired up at all, and that a freeze really stops a process
# --------------------------------------------------------------------------- #

def test_the_watcher_actually_looks_at_the_turn_that_is_running(addon, monkeypatch, running):
    """Without this, deleting the guard's one call from the loop changed nothing that any
    test could see: a watcher that polls Anthropic on a ladder and freezes nothing."""
    looked = []
    monkeypatch.setattr(addon, "GUARD_LIMITS", True)
    monkeypatch.setattr(addon, "hold_for_limits", lambda job_id: looked.append(job_id))
    usage_says(addon, monkeypatch, 40.0)

    addon.limit_watch_once()

    assert looked == [running], "the running turn, by name"


def process_state(pid: int) -> str:
    """What the kernel says the process is doing: T is stopped."""
    with open(f"/proc/{pid}/stat") as handle:
        return handle.read().rsplit(") ", 1)[1].split()[0]


def waits_until_state(pid: int, wanted: set) -> str:
    """A signal is delivered when the kernel gets round to it, not when it is sent."""
    return wait_until(
        lambda: (found := process_state(pid)) in wanted and found,
        timeout=5.0,
        description=f"the process to be {'/'.join(sorted(wanted))}",
    )


def test_freezing_a_turn_stops_the_process_itself_not_only_the_record(addon, client, running):
    """The freeze used to be provable only by the flags it set. Take the signals out and
    every assertion still passed — a paused label on a turn that kept spending."""
    pid = addon.RUNNING_PROC.pid

    client.send_json("POST", f"/jobs/{running}/pause")
    stopped = waits_until_state(pid, {"T"})
    client.send_json("POST", f"/jobs/{running}/resume")

    assert stopped == "T", f"the kernel should have it stopped, not {stopped!r}"
    assert waits_until_state(pid, {"S", "R", "D"}), "and running again once let go"


def test_the_guard_freezes_the_process_too(addon, monkeypatch, running):
    pid = addon.RUNNING_PROC.pid
    usage_says(addon, monkeypatch, 96.0)

    addon.hold_for_limits(running)
    stopped = waits_until_state(pid, {"T"})
    usage_says(addon, monkeypatch, 3.0)
    addon.hold_for_limits(running)

    assert stopped == "T"
    assert waits_until_state(pid, {"S", "R", "D"})


def test_the_guard_can_freeze_the_same_turn_more_than_once(addon, client, monkeypatch, running):
    """A long turn spans more than one window. The guard's own thaw must not read as
    somebody overruling it, or it would freeze once and never again."""
    usage_says(addon, monkeypatch, 96.0)
    addon.hold_for_limits(running)
    usage_says(addon, monkeypatch, 3.0)
    addon.hold_for_limits(running)
    assert "limit_override" not in addon.read_job(running)

    usage_says(addon, monkeypatch, 97.0)
    addon.hold_for_limits(running)

    job = client.get(f"/jobs/{running}").json
    assert job["paused"] is True
    assert job["paused_reason"] == "limits"


def test_a_freeze_never_shows_half_written(addon, monkeypatch, running):
    """One write, not two: a poll used to be able to catch `paused_at` with no reason."""
    usage_says(addon, monkeypatch, 96.0)

    addon.hold_for_limits(running)

    job = addon.read_job(running)
    assert bool(job.get("paused_at")) == bool(job.get("paused_reason"))
    assert job["resumes_at"]


def test_the_subagents_freeze_with_the_turn(addon, client, stub_behaviour):
    """Why the signal goes to the whole process group: a skill's real work happens in
    subagents and Bash calls, and a freeze that misses them stops nothing that costs."""
    stub_behaviour("STUB_CHILD", "1")
    stub_behaviour("STUB_SLEEP", "30")   # or the turn is over before it can be frozen
    job = create_job(client, "with a child of its own", start=True)
    wait_for_status(client, job["id"], "running")
    ticks = addon.JOBS_DIR / job["id"] / "child-ticks"
    wait_until(lambda: ticks.is_file() and ticks.stat().st_size > 0,
               description="the child to start working")

    assert client.send_json("POST", f"/jobs/{job['id']}/pause").json["paused"] is True
    frozen_at = ticks.stat().st_size
    time.sleep(1.5)
    while_frozen = ticks.stat().st_size
    client.send_json("POST", f"/jobs/{job['id']}/resume")
    wait_until(lambda: ticks.stat().st_size > while_frozen,
               description="the child to carry on once let go")

    assert while_frozen == frozen_at, "the child kept working through the freeze"
    client.send_json("POST", f"/jobs/{job['id']}/cancel")
    wait_for_status(client, job["id"], "failed")


def test_a_turn_that_dies_while_frozen_is_not_left_looking_paused(addon, client, stub_behaviour):
    """Nothing guarantees a frozen process comes back — the OOM killer does not ask.
    What must not happen is a finished record that still reads as waiting."""
    stub_behaviour("STUB_SLEEP", "30")
    job = create_job(client, "frozen, then killed", start=True)
    wait_for_status(client, job["id"], "running")
    addon.update_job(job["id"], paused_reason="limits", resumes_at="2026-08-13T13:20:00+00:00")
    client.send_json("POST", f"/jobs/{job['id']}/pause")

    addon.kill_tree(addon.RUNNING_PROC, signal.SIGKILL)
    wait_for_status(client, job["id"], "failed")

    finished = client.get(f"/jobs/{job['id']}").json
    assert "paused_reason" not in finished
    assert "resumes_at" not in finished
    assert addon.PAUSED_AT is None


def test_how_often_the_allowance_may_be_read_is_one_setting(addon, monkeypatch):
    """The ladder is a floor on attention, not a licence to ask more often than the setting
    allows: every reading is a request to Anthropic."""
    monkeypatch.setattr(addon, "USAGE_TTL_SEC", 600)

    close = {"kind": "session", "percent": 89.0, "threshold": 90}
    far = {"kind": "session", "percent": 10.0, "threshold": 90}
    assert addon.watch_interval(close) == 600, "close to the wall, but not closer than allowed"
    assert addon.watch_interval(far) == 15 * 60, "and the wide step still wins when it is wider"
    assert addon.watch_interval(None) == 15 * 60


def test_the_reading_says_how_often_it_is_willing_to_be_asked(addon, client, monkeypatch):
    monkeypatch.setattr(addon, "USAGE_TTL_SEC", 240)
    addon.USAGE_CACHE.update(checked_at=float("-inf"), value=None)

    assert client.get("/usage").json["check_every"] == 240


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("2026-08-13T13:20:00.124379+00:00", "13 Aug"),
        ("2026-08-13T13:20:00Z", "13 Aug"),
        ("not a time", ""),
        (None, ""),
    ],
)
def test_a_moment_is_said_the_way_somebody_would_say_it(addon, value, shown):
    said = addon.when_for_people(value)

    assert (shown in said) if shown else (said == "")
    assert "+00:00" not in said
    assert "." not in said


def test_a_turn_takes_the_credentials_lock_across_its_spawn(
    client, addon, monkeypatch, stub_behaviour
):
    """So nothing renews the account's token while the CLI is starting up on it. The CLI
    renews its own, and two renewals spend the same refresh token twice — which signs the
    account out rather than renewing it."""
    held = []
    spawn = addon.subprocess.Popen

    def watch(argv, *args, **kwargs):
        if "-p" in argv:
            held.append(addon.CREDENTIALS_LOCK.locked())
        return spawn(argv, *args, **kwargs)

    monkeypatch.setattr(addon.subprocess, "Popen", watch)

    job = create_job(client, "a turn like any other", start=True)
    wait_for_status(client, job["id"], "done")

    assert held == [True], "the turn spawned without holding the credentials lock"


def test_the_guard_freezes_on_the_week_though_the_session_has_room(
    addon, client, monkeypatch, running
):
    """The two windows are set apart on purpose, and the guard acts on whichever runs out —
    a week at 80 of 75 stops work while the session sits at half of ninety."""
    monkeypatch.setattr(addon, "GUARD_LIMITS", True)
    back = addon.datetime.now(addon.UTC) + addon.timedelta(hours=4)
    monkeypatch.setattr(addon, "read_usage", lambda force=False: {
        "available": True,
        "session": {"percent": 45.0, "threshold": 90, "resets_at": back.isoformat()},
        "week": {"percent": 80.0, "threshold": 75, "resets_at": back.isoformat()},
        "worst": {"kind": "week", "percent": 80.0, "threshold": 75,
                  "resets_at": back.isoformat()},
        "enough": False,
        "checked_at": "now",
    })

    frozen = addon.hold_for_limits(running)

    assert frozen["paused"] is True
    assert client.get(f"/jobs/{running}").json["paused_reason"] == "limits"


def test_how_close_the_wall_is_counts_the_window_s_own_figure(addon, monkeypatch):
    """Sixty-five per cent is a wide margin against ninety and none at all against sixty-seven;
    the ladder has to be measured in room left, not in per cent used."""
    monkeypatch.setattr(addon, "USAGE_TTL_SEC", 30)

    roomy = {"kind": "session", "percent": 65.0, "threshold": 90}
    tight = {"kind": "week", "percent": 65.0, "threshold": 67}

    assert addon.watch_interval(roomy) == 15 * 60
    assert addon.watch_interval(tight) == 2 * 60


def test_a_turn_can_be_frozen_the_moment_it_is_reported_running(
    addon, client, stub_behaviour, monkeypatch
):
    """«running» is written when the worker takes the job; the CLI appears a beat later, and
    the spawn waits on the credentials lock, so the beat can be a long one. A caller that
    reads the status and acts on it at once must not be told there is nothing to freeze."""
    monkeypatch.setattr(addon, "GUARD_LIMITS", False)
    stub_behaviour("STUB_SLEEP", "300")
    job = create_job(client, "frozen the instant it starts", start=True)
    wait_for_status(client, job["id"], "running")

    frozen = client.send_json("POST", f"/jobs/{job['id']}/pause")

    assert frozen.status == 200, frozen.json
    assert frozen.json["paused"] is True
    client.send_json("POST", f"/jobs/{job['id']}/cancel")
    wait_for_status(client, job["id"], "failed")
