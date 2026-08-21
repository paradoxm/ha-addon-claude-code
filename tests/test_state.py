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


def test_a_patch_changes_one_part_and_leaves_the_rest_alone(client):
    client.send_json("PUT", "/state/bot", {
        "chats": {"100000000": {"stage": "files"}, "100000001": {"delivered": "22-08.zip"}}})

    answer = client.send_json("PATCH", "/state/bot", {"chats": {"100000000": {"stage": "open"}}})

    assert answer.status == 200
    assert answer.json["value"] == {
        "chats": {"100000000": {"stage": "open"}, "100000001": {"delivered": "22-08.zip"}}
    }, "the other conversation is exactly as it was"


def test_a_patch_answers_with_what_is_there_now(client):
    client.send_json("PUT", "/state/bot", {"stage": "date"})

    answer = client.send_json("PATCH", "/state/bot", {"date": "22.08"})

    assert answer.json["value"] == {"stage": "date", "date": "22.08"}
    written = json.dumps(answer.json["value"], ensure_ascii=False).encode()
    assert answer.json["bytes"] == len(written)


def test_a_null_in_a_patch_takes_the_field_out(client):
    client.send_json("PUT", "/state/bot", {"stage": "open", "session": "abc", "date": "22.08"})

    client.send_json("PATCH", "/state/bot", {"session": None, "stage": "done"})

    assert client.get("/state/bot").json["value"] == {"stage": "done", "date": "22.08"}


def test_taking_out_a_field_that_was_never_there_is_not_an_error(client):
    client.send_json("PUT", "/state/bot", {"stage": "date"})

    answer = client.send_json("PATCH", "/state/bot", {"session": None})

    assert answer.status == 200
    assert answer.json["value"] == {"stage": "date"}


def test_a_patch_replaces_a_list_rather_than_adding_to_it(client):
    client.send_json("PUT", "/state/bot", {"files": ["1-groom.docx", "2-bride.docx"]})

    client.send_json("PATCH", "/state/bot", {"files": []})

    assert client.get("/state/bot").json["value"] == {"files": []}


def test_a_patch_can_put_an_object_where_a_plain_value_was(client):
    client.send_json("PUT", "/state/bot", {"watch": "6110a32aa945"})

    client.send_json("PATCH", "/state/bot", {"watch": {"job_id": "6110a32aa945", "tries": 0}})

    assert client.get("/state/bot").json["value"] == {
        "watch": {"job_id": "6110a32aa945", "tries": 0}}


def test_a_patch_on_a_note_nobody_wrote_writes_it(client):
    answer = client.send_json("PATCH", "/state/first-time", {"chats": {"100000000": {}}})

    assert answer.status == 200
    assert client.get("/state/first-time").json["value"] == {"chats": {"100000000": {}}}


def test_two_patches_at_once_both_survive(addon, client):
    """The race this endpoint exists for, run for real.

    Two callers change two different conversations in the same instant. With GET and PUT
    the one that writes second puts its own copy — taken before the other's change — over
    the lot, and the first change is gone. Here the add-on is the one holding both.
    """
    import threading

    client.send_json("PUT", "/state/bot", {"chats": {}})
    ready, done = threading.Barrier(9), []

    def patch(which):
        ready.wait()
        answer = client.send_json("PATCH", "/state/bot",
                                  {"chats": {f"10000000{which}": {"stage": "date"}}})
        done.append(answer.status)

    workers = [threading.Thread(target=patch, args=(n,)) for n in range(8)]
    for worker in workers:
        worker.start()
    ready.wait()
    for worker in workers:
        worker.join(timeout=30)

    assert done == [200] * 8
    assert sorted(client.get("/state/bot").json["value"]["chats"]) == \
        [f"10000000{n}" for n in range(8)], "every one of the eight is there"


def test_a_patch_that_would_make_the_note_too_large_is_refused(addon, client):
    client.send_json("PUT", "/state/bot", {"stage": "date"})

    answer = client.send_json("PATCH", "/state/bot", {"padding": "x" * addon.MAX_STATE_BYTES})

    assert answer.status == 413
    assert client.get("/state/bot").json["value"] == {"stage": "date"}, "and nothing changed"


def test_a_patch_must_be_a_json_object(client):
    answer = client.send_json("PATCH", "/state/bot", ["not", "an", "object"])

    assert answer.status == 400
    assert "must be a JSON object" in answer.json["error"]


def test_a_note_that_is_not_an_object_cannot_be_patched(addon, client):
    addon.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (addon.STATE_DIR / "bot.json").write_text("[1, 2, 3]")

    answer = client.send_json("PATCH", "/state/bot", {"stage": "date"})

    assert answer.status == 409
    assert "cannot be patched" in answer.json["error"]


def test_a_stray_file_in_the_folder_is_not_offered_as_a_key(addon, client):
    addon.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (addon.STATE_DIR / ".hidden.json").write_text("{}")
    (addon.STATE_DIR / "notes.txt").write_text("hello")

    assert client.get("/state").json["keys"] == []
