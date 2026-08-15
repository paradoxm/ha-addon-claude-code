"""Claude Code's own settings file, edited through the add-on."""

import json

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
