"""Claude Code's own settings file, edited through the add-on."""

import json
import shutil

import pytest


def test_the_settings_file_and_its_path_are_reported(addon, client):
    answer = client.get("/settings")

    assert answer.status == 200
    assert answer.json["path"] == str(addon.SETTINGS_PATH)
    assert answer.json["enforced_env"] == addon.REQUIRED_ENV
    assert isinstance(answer.json["settings"], dict)


def test_permission_rules_are_stored_as_given(client):
    rules = {"permissions": {"allow": ["Bash(git *)"], "deny": ["Read(./secrets/**)"]}}

    answer = client.send_json("PUT", "/settings", rules)

    assert answer.status == 200
    assert answer.json["settings"]["permissions"] == rules["permissions"]
    assert client.get("/settings").json["settings"]["permissions"] == rules["permissions"]


def test_the_setting_search_depends_on_is_put_back_whatever_the_file_says(addon, client):
    answer = client.send_json("PUT", "/settings", {"env": {"USE_BUILTIN_RIPGREP": "1"}})

    assert answer.json["settings"]["env"] == addon.REQUIRED_ENV


def test_keys_the_add_on_has_never_heard_of_are_left_alone(client):
    answer = client.send_json("PUT", "/settings", {"somethingNewInTheCli": {"nested": [1, 2]}})

    assert answer.status == 200
    assert answer.json["settings"]["somethingNewInTheCli"] == {"nested": [1, 2]}


def test_the_file_on_disk_is_what_the_cli_will_read(addon, client):
    client.send_json("PUT", "/settings", {"permissions": {"ask": ["WebFetch"]}})

    stored = json.loads(addon.SETTINGS_PATH.read_text())

    assert stored["permissions"]["ask"] == ["WebFetch"]
    assert stored["env"]["USE_BUILTIN_RIPGREP"] == "0"


@pytest.mark.parametrize(
    ("body", "expected_fragment"),
    [
        (b"{ not json", "not valid JSON"),
        (b"[]", "must be a JSON object"),
        (b'{"permissions": "everything"}', "'permissions' must be an object"),
        (b'{"permissions": {"allow": [1]}}', "'permissions.allow' must be a list of strings"),
        (b'{"permissions": {"deny": "Bash"}}', "'permissions.deny' must be a list of strings"),
        (b'{"env": []}', "'env' must be an object"),
    ],
)
def test_a_file_the_cli_could_not_use_is_refused(client, body, expected_fragment):
    answer = client.request("PUT", "/settings", body=body)

    assert answer.status == 400
    assert expected_fragment in answer.json["error"]


def test_a_refused_file_does_not_overwrite_the_good_one(addon, client):
    client.send_json("PUT", "/settings", {"permissions": {"allow": ["Bash(ls)"]}})

    client.request("PUT", "/settings", body=b'{"permissions": {"allow": [42]}}')

    assert client.get("/settings").json["settings"]["permissions"]["allow"] == ["Bash(ls)"]


def test_an_empty_permissions_section_is_accepted(client):
    answer = client.send_json("PUT", "/settings", {"permissions": {}})

    assert answer.status == 200
    assert answer.json["settings"]["permissions"] == {}


# --------------------------------------------------------------------------- #
# plugins synced from claude.ai
# --------------------------------------------------------------------------- #


@pytest.fixture
def synced(addon):
    """An organisation's plugins, the way the CLI lays them out — one directory carrying a
    generation suffix its plugin's id does not have."""
    root = addon.HOME / ".claude" / "plugins" / "synced" / "org_account"
    root.mkdir(parents=True)
    (root / "figma~g2").mkdir()
    (root / "manifest.json").write_text(json.dumps({"plugins": [
        {"name": "figma", "generation": 2},
        {"name": "engineering"},
        {"name": "../not-a-name"},
        "not an entry",
    ]}))
    before = addon.SETTINGS_PATH.read_text() if addon.SETTINGS_PATH.exists() else None
    addon.SETTINGS_PATH.write_text(json.dumps({"theme": "dark"}))
    yield root
    shutil.rmtree(root.parent)
    if before is None:
        addon.SETTINGS_PATH.unlink(missing_ok=True)
    else:
        addon.SETTINGS_PATH.write_text(before)


def enabled_plugins(addon) -> dict:
    return json.loads(addon.SETTINGS_PATH.read_text()).get("enabledPlugins", {})


def test_synced_plugins_are_switched_off_by_their_ids(addon, synced):
    assert addon.keep_synced_plugins_off() == ["engineering@synced", "figma@synced"]

    assert enabled_plugins(addon) == {"engineering@synced": False, "figma@synced": False}
    assert json.loads(addon.SETTINGS_PATH.read_text())["theme"] == "dark", "the rest is kept"


def test_a_plugin_switched_back_on_by_hand_stays_on(addon, synced):
    addon.SETTINGS_PATH.write_text(json.dumps({"enabledPlugins": {"figma@synced": True}}))

    assert addon.keep_synced_plugins_off() == ["engineering@synced"]

    assert enabled_plugins(addon) == {"figma@synced": True, "engineering@synced": False}


def test_nothing_new_means_nothing_written(addon, synced):
    addon.keep_synced_plugins_off()
    written = addon.SETTINGS_PATH.stat().st_mtime_ns

    assert addon.keep_synced_plugins_off() == []
    assert addon.SETTINGS_PATH.stat().st_mtime_ns == written


def test_asked_for_they_are_left_as_claude_ai_sends_them(addon, synced, monkeypatch):
    monkeypatch.setattr(addon, "SYNCED_PLUGINS", True)

    assert addon.keep_synced_plugins_off() == []
    assert enabled_plugins(addon) == {}


def test_a_manifest_in_another_shape_is_no_plugins(addon, synced):
    (synced / "manifest.json").write_text(json.dumps({"plugins": "none"}))
    addon.SETTINGS_PATH.write_text(json.dumps({"enabledPlugins": "broken"}))

    assert addon.keep_synced_plugins_off() == []


def test_a_turn_switches_off_what_synced_since_the_last_one(addon, client, synced):
    from test_jobs import create_job, wait_for_status

    job = create_job(client, "after a sync", start=True)
    wait_for_status(client, job["id"], "done")

    assert enabled_plugins(addon) == {"engineering@synced": False, "figma@synced": False}


def test_a_settings_file_that_cannot_be_written_does_not_cost_the_turn(
    addon, client, monkeypatch
):
    from test_jobs import create_job, wait_for_status

    def refuse():
        raise OSError("read-only filesystem")

    monkeypatch.setattr(addon, "keep_synced_plugins_off", refuse)

    job = create_job(client, "despite the settings", start=True)
    wait_for_status(client, job["id"], "done")
