"""Notes a caller keeps between its own runs, under names it chooses.

What this is for: a bot driving the add-on has state of its own — who it is talking to,
which job it is watching, what it has already sent — and nowhere honest to put it. Its
own automation platform may hand it a copy of that state taken minutes ago and write it
back over the newest one, which is how a single delivery became forty. Here a read is a
read and a write is a write.
"""

import json

import pytest


@pytest.fixture(autouse=True)
def nothing_stored(addon):
    def clear():
        if addon.STATE_DIR.is_dir():
            for path in addon.STATE_DIR.glob("*"):
                path.unlink()

    clear()
    yield
    clear()


def test_a_note_comes_back_exactly_as_it_was_left(client):
    # The shape a caller really keeps — nested, mixed types, ids as keys — with an id
    # nobody has. Test data copied from a live caller puts a real person into a public
    # repository, which is not a thing a test needs to prove anything.
    kept = {"chats": {"100000000": {"watch": {"job_id": "6110a32aa945", "tries": 1}}}}

    stored = client.send_json("PUT", "/state/some-caller", kept)

    assert stored.status == 200
    assert stored.json["key"] == "some-caller"
    assert client.get("/state/some-caller").json["value"] == kept


def test_a_second_write_replaces_the_first(client):
    client.send_json("PUT", "/state/bot", {"stage": "date"})

    client.send_json("PUT", "/state/bot", {"stage": "files", "date": "22.08"})

    assert client.get("/state/bot").json["value"] == {"stage": "files", "date": "22.08"}


def test_a_note_survives_a_restart_because_it_is_on_disk(addon, client):
    client.send_json("PUT", "/state/bot", {"delivered": "22-08.zip"})

    on_disk = json.loads((addon.STATE_DIR / "bot.json").read_text())

    assert on_disk == {"delivered": "22-08.zip"}


def test_asking_for_a_note_nobody_wrote_says_so(client):
    answer = client.get("/state/never-written")

    assert answer.status == 404
    assert "nothing stored under never-written" in answer.json["error"]


def test_the_keys_in_use_can_be_listed(client):
    client.send_json("PUT", "/state/some-caller", {"a": 1})
    client.send_json("PUT", "/state/another-caller", {"b": 2})

    assert client.get("/state").json["keys"] == ["another-caller", "some-caller"]


def test_nothing_stored_lists_nothing(addon, client):
    """Including on a machine where nothing has ever been stored, so the folder is not
    there at all."""
    if addon.STATE_DIR.is_dir():
        addon.STATE_DIR.rmdir()

    assert client.get("/state").json["keys"] == []


def test_a_note_can_be_thrown_away(client):
    client.send_json("PUT", "/state/bot", {"a": 1})

    gone = client.send_json("DELETE", "/state/bot")

    assert gone.json == {"key": "bot", "existed": True}
    assert client.get("/state/bot").status == 404


def test_throwing_away_a_note_that_was_never_there_is_not_an_error(client):
    gone = client.send_json("DELETE", "/state/bot")

    assert gone.status == 200
    assert gone.json == {"key": "bot", "existed": False}


@pytest.mark.parametrize("key", ["../escape", "with%20space", ".hidden", "", "a/b"])
def test_a_key_that_could_reach_outside_is_refused(client, key):
    answer = client.send_json("PUT", f"/state/{key}", {"a": 1})

    assert answer.status in (400, 404)
    assert "/data/state" not in json.dumps(answer.json)


def test_a_note_too_large_to_be_a_note_is_refused(addon, client):
    answer = client.send_json("PUT", "/state/bot", {"padding": "x" * addon.MAX_STATE_BYTES})

    assert answer.status == 413
    assert "under 512 kb" in answer.json["error"]
    assert client.get("/state/bot").status == 404, "and nothing is left half-written"


def test_a_note_must_be_a_json_object(client):
    answer = client.send_json("PUT", "/state/bot", ["not", "an", "object"])

    assert answer.status == 400
    assert "must be a JSON object" in answer.json["error"]


def test_russian_in_a_note_is_kept_readable_on_disk(addon, client):
    client.send_json("PUT", "/state/bot", {"reply": "Готово — тексты в архиве."})

    assert "Готово" in (addon.STATE_DIR / "bot.json").read_text()


def test_a_note_that_was_damaged_on_disk_is_reported_rather_than_guessed(addon, client):
    addon.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (addon.STATE_DIR / "bot.json").write_text("{ this is not json")

    answer = client.get("/state/bot")

    assert answer.status == 500
    assert "could not be read back" in answer.json["error"]


def test_a_stray_file_in_the_folder_is_not_offered_as_a_key(addon, client):
    addon.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (addon.STATE_DIR / ".hidden.json").write_text("{}")
    (addon.STATE_DIR / "notes.txt").write_text("hello")

    assert client.get("/state").json["keys"] == []
