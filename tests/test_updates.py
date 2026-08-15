"""Keeping the CLI, its marketplaces and its plugins up to date."""

import urllib.error
from urllib.parse import quote

import pytest
from conftest import wait_until


def wait_for_the_update_to_finish(client):
    return wait_until(
        lambda: client.get("/update").json["status"] in ("done", "failed"),
        description="the update to finish",
    )


@pytest.fixture(autouse=True)
def a_freshly_installed_cli(addon):
    """Every test starts from the packaged version, with nothing cached.

    The version is cached for a minute, which would hide an install, and the
    stand-in remembers what was installed into it — both have to go, or one test's
    update is the next one's starting point.
    """
    marker = addon.HOME / ".stub-installed-version"

    def reset():
        marker.unlink(missing_ok=True)
        addon.INSTALLED_CACHE.update(version=None, checked_at=float("-inf"))

    reset()
    yield
    reset()


def test_the_version_and_the_binary_that_is_actually_on_the_path_are_reported(client):
    answer = client.get("/version")

    assert answer.status == 200
    assert answer.json["installed"] == "9.9.9"
    assert answer.json["binary"].endswith("/claude")
    assert answer.json["channel"] == "latest"


def test_the_installed_version_is_probed_once_a_minute_not_once_a_poll(addon, client):
    first = addon.installed_version(force=True)
    checked_at = addon.INSTALLED_CACHE["checked_at"]

    second = addon.installed_version()

    assert second == first
    assert addon.INSTALLED_CACHE["checked_at"] == checked_at


def test_a_freshly_booted_machine_still_probes_on_the_first_call(addon):
    addon.INSTALLED_CACHE.update(version=None, checked_at=0.0)

    assert addon.installed_version() == "9.9.9"


def test_a_version_the_binary_cannot_report_is_not_cached_over_a_good_one(
    addon, stub_behaviour
):
    assert addon.installed_version(force=True) == "9.9.9"
    stub_behaviour("STUB_VERSION", "")

    assert addon.installed_version(force=True) is None
    assert addon.INSTALLED_CACHE["version"] == "9.9.9"


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("2.1.229", "2.1.228", True),
        ("2.2.0", "2.1.999", True),
        ("2.1.228", "2.1.228", False),
        ("2.1.227", "2.1.228", False),
        (None, "2.1.228", False),
        ("2.1.229", None, False),
        ("2.1.228-beta", "2.1.227", False),
    ],
)
def test_only_a_higher_version_counts_as_newer(addon, candidate, current, expected):
    assert addon.is_newer(candidate, current) is expected


def test_the_available_version_is_read_from_the_release_channel(addon, monkeypatch):
    class Channel:
        def read(self, _size):
            return b"9.9.11\n"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(addon.urllib.request, "urlopen", lambda *a, **k: Channel())

    assert addon.refresh_available() == "9.9.11"
    assert addon.AVAILABLE["version"] == "9.9.11"


def test_a_channel_that_answers_with_nonsense_is_ignored(addon, monkeypatch):
    addon.AVAILABLE["version"] = "9.9.11"

    class Channel:
        def read(self, _size):
            return b"<!doctype html>"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(addon.urllib.request, "urlopen", lambda *a, **k: Channel())

    assert addon.refresh_available() == "9.9.11"


def test_a_channel_that_cannot_be_reached_leaves_the_last_answer_standing(addon, monkeypatch):
    addon.AVAILABLE["version"] = "9.9.11"

    def unreachable(*_args, **_kwargs):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(addon.urllib.request, "urlopen", unreachable)

    assert addon.refresh_available() == "9.9.11"


@pytest.mark.parametrize("target", ["--dangerous", "٣٤", "latest; rm -rf /", "1.2.3.4.5x"])
def test_a_target_that_is_not_a_channel_or_a_version_is_refused(client, target):
    answer = client.send_json("POST", f"/update?target={quote(target)}")

    assert answer.status == 400
    assert "must be 'latest', 'stable' or a version" in answer.json["error"]


def test_an_update_reports_progress_from_disk_so_a_reload_sees_it(client):
    started = client.send_json("POST", "/update?target=9.9.12")

    assert started.status == 202
    assert started.json["status"] == "running"
    assert started.json["previous"] == "9.9.9"

    wait_for_the_update_to_finish(client)
    state = client.get("/update").json
    assert state["status"] == "done"
    assert state["installed"] == "9.9.12"
    assert state["changed"] is True
    assert state["target"] == "9.9.12"


def test_a_version_change_brings_marketplaces_and_plugins_along_with_it(client):
    client.send_json("POST", "/update?target=9.9.13")
    wait_for_the_update_to_finish(client)

    plugins = client.get("/update").json["plugins"]
    assert plugins["marketplaces"] == "ok"
    assert plugins["plugins"] == {"demo-plugin": "ok"}


def test_reinstalling_the_same_version_does_not_touch_the_plugins(client):
    client.send_json("POST", "/update?target=9.9.9")
    wait_for_the_update_to_finish(client)

    state = client.get("/update").json
    assert state["status"] == "done"
    assert state["changed"] is False
    assert "plugins" not in state


def test_an_update_is_refused_while_the_cli_is_busy(addon, client):
    assert addon.CLI_LOCK.acquire(blocking=False)
    try:
        answer = client.send_json("POST", "/update")
    finally:
        addon.CLI_LOCK.release()

    assert answer.status == 409
    assert "busy" in answer.json["error"]
    assert not addon.CLI_LOCK.locked()


def test_an_install_that_fails_is_reported_with_what_the_cli_said(addon, client, stub_behaviour):
    stub_behaviour("STUB_INSTALL_FAIL", "1")

    client.send_json("POST", "/update?target=stable")
    wait_for_the_update_to_finish(client)

    state = client.get("/update").json
    assert state["status"] == "failed"
    assert "install refused by the stub" in state["error"]
    assert not addon.CLI_LOCK.locked()


def test_a_restart_in_the_middle_of_an_install_does_not_leave_it_running_forever(addon):
    addon.write_json(addon.UPDATE_STATE_PATH, {"status": "running", "target": "latest"})

    addon.reconcile_interrupted_update()

    state = addon.read_update_state()
    assert state["status"] == "interrupted"
    assert state["error"] == "the add-on restarted while the update was running"
    assert state["finished_at"]


def test_an_update_that_finished_before_the_restart_is_left_as_it_was(addon):
    addon.write_json(addon.UPDATE_STATE_PATH, {"status": "done", "installed": "9.9.9"})

    addon.reconcile_interrupted_update()

    assert addon.read_update_state()["status"] == "done"


def test_the_health_reading_says_when_a_newer_cli_is_waiting(addon, client):
    addon.AVAILABLE["version"] = "99.0.0"

    health = client.get("/health").json

    assert health["available_version"] == "99.0.0"
    assert health["update_available"] is True
    addon.AVAILABLE["version"] = None


@pytest.mark.parametrize(
    ("cli_output", "expected"),
    [
        ('[{"name": "one"}, {"id": "two"}]', ["one", "two"]),
        ('["one", "two"]', ["one", "two"]),
        ('{"plugins": {"one": {}, "two": {}}}', ["one", "two"]),
        ('{"one": {}}', ["one"]),
        ("not json at all", []),
        ('[{"name": "bad name!"}]', []),
    ],
)
def test_the_plugin_list_is_read_whatever_shape_the_cli_answers_in(
    addon, monkeypatch, cli_output, expected
):
    monkeypatch.setattr(addon, "run_cli", lambda *a, **k: (True, cli_output))

    assert addon.installed_plugins() == expected


def test_no_plugins_are_claimed_when_the_cli_cannot_list_them(addon, monkeypatch):
    monkeypatch.setattr(addon, "run_cli", lambda *a, **k: (False, "not signed in"))

    assert addon.installed_plugins() == []


def test_a_marketplace_refresh_that_fails_is_reported_rather_than_swallowed(addon, monkeypatch):
    monkeypatch.setattr(addon, "run_cli", lambda *a, **k: (False, "network unreachable"))

    report = addon.refresh_plugins()

    assert report["marketplaces"] == "network unreachable"
    assert report["plugins"] == {}


def test_the_daily_check_says_the_cli_is_current_when_it_is(addon, monkeypatch):
    monkeypatch.setattr(addon, "refresh_available", lambda: "9.9.9")

    assert addon.auto_update_pass() == "current"


def test_the_daily_check_does_not_install_when_auto_update_is_off(addon, monkeypatch):
    monkeypatch.setattr(addon, "refresh_available", lambda: "99.0.0")
    monkeypatch.setattr(addon, "AUTO_UPDATE", False)

    assert addon.auto_update_pass() == "available"


def test_the_daily_check_installs_when_auto_update_is_on(addon, monkeypatch, stub_behaviour):
    monkeypatch.setattr(addon, "refresh_available", lambda: "99.0.0")
    monkeypatch.setattr(addon, "AUTO_UPDATE", True)
    stub_behaviour("STUB_INSTALL_TO", "99.0.0")

    assert addon.auto_update_pass() == "updated"

    state = addon.read_update_state()
    assert state["status"] == "done"
    assert state["previous"] == "9.9.9"
    assert state["installed"] == "99.0.0"


def test_the_daily_check_waits_for_another_pass_when_the_cli_is_busy(addon, monkeypatch):
    monkeypatch.setattr(addon, "refresh_available", lambda: "99.0.0")
    monkeypatch.setattr(addon, "AUTO_UPDATE", True)
    assert addon.CLI_LOCK.acquire(blocking=False)
    try:
        assert addon.auto_update_pass() == "skipped"
    finally:
        addon.CLI_LOCK.release()


def test_the_daily_check_survives_anything_the_check_itself_throws(addon, monkeypatch):
    def explode():
        raise RuntimeError("dns is on fire")

    monkeypatch.setattr(addon, "refresh_available", explode)

    assert addon.auto_update_pass() == "failed"


def test_a_flag_with_no_value_still_asks_for_a_refresh(addon, client, monkeypatch):
    """`?refresh` carries no value, and parse_qs drops those unless told not to."""
    asked = []

    class Channel:
        def read(self, _size):
            asked.append(1)
            return b"9.9.50\n"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(addon.urllib.request, "urlopen", lambda *a, **k: Channel())

    client.get("/version")
    assert asked == [], "without the flag the cached answer is enough"

    client.get("/version?refresh")

    assert len(asked) == 1
    assert client.get("/version").json["available"] == "9.9.50"
