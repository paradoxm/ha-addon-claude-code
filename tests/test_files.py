"""The two files the add-on will show and let you edit: the CLI's config, and the
instructions it reads before every conversation."""

import json

import pytest


@pytest.fixture(autouse=True)
def empty_files(addon):
    """Neither file exists until a test writes one."""

    def clear():
        for entry in addon.EDITABLE_FILES.values():
            entry["path"].unlink(missing_ok=True)

    clear()
    yield
    clear()


def test_both_files_are_offered_with_their_paths_and_kinds(addon, client):
    answer = client.get("/files")

    assert answer.status == 200
    assert answer.json["files"] == [
        {
            "key": "config",
            "path": str(addon.CLI_CONFIG_PATH),
            "kind": "json",
            "exists": False,
        },
        {
            "key": "memory",
            "path": str(addon.HOME / ".claude" / "CLAUDE.md"),
            "kind": "markdown",
            "exists": False,
        },
    ]


def test_a_file_that_does_not_exist_yet_reads_as_empty(client):
    answer = client.get("/files/memory")

    assert answer.status == 200
    assert answer.json["exists"] is False
    assert answer.json["text"] == ""


def test_a_file_is_returned_exactly_as_it_is_on_disk(addon, client):
    written = "# House rules\n\nAnswer in Russian.\n\ttabs and  spaces  kept\n"
    (addon.HOME / ".claude" / "CLAUDE.md").write_text(written)

    answer = client.get("/files/memory")

    assert answer.json["text"] == written
    assert answer.json["exists"] is True
    assert answer.json["kind"] == "markdown"


def test_what_is_saved_is_what_lands_on_disk(addon, client):
    answer = client.request("PUT", "/files/memory", body=b"Keep it short.\n")

    assert answer.status == 200
    assert answer.json["bytes"] == len("Keep it short.\n")
    assert (addon.HOME / ".claude" / "CLAUDE.md").read_text() == "Keep it short.\n"


def test_the_directory_is_made_if_it_is_not_there(addon, client):
    directory = addon.HOME / ".claude"
    for leftover in directory.iterdir():
        if leftover.is_file():
            leftover.unlink()

    answer = client.request("PUT", "/files/memory", body=b"first thing written")

    assert answer.status == 200
    assert (directory / "CLAUDE.md").is_file()


def test_the_config_is_saved_when_it_parses(addon, client):
    answer = client.request("PUT", "/files/config", body=b'{\n  "mcpServers": {}\n}\n')

    assert answer.status == 200
    assert json.loads(addon.CLI_CONFIG_PATH.read_text()) == {"mcpServers": {}}


def test_the_config_keeps_the_formatting_it_was_given(addon, client):
    written = '{"mcpServers":{},   "trailing": "spacing"}'

    client.request("PUT", "/files/config", body=written.encode())

    assert addon.CLI_CONFIG_PATH.read_text() == written


def test_a_config_that_would_not_parse_is_refused_and_nothing_is_written(addon, client):
    addon.CLI_CONFIG_PATH.write_text('{"kept": true}')

    answer = client.request("PUT", "/files/config", body=b"{ truncated")

    assert answer.status == 400
    assert "not valid JSON" in answer.json["error"]
    assert json.loads(addon.CLI_CONFIG_PATH.read_text()) == {"kept": True}


def test_broken_markdown_is_a_contradiction_so_anything_saves(addon, client):
    answer = client.request("PUT", "/files/memory", body=b"{ this is not json, and need not be")

    assert answer.status == 200


def test_saving_the_cli_s_own_file_is_refused_while_claude_is_working(
    addon, client, monkeypatch
):
    monkeypatch.setattr(addon, "RUNNING_JOB", "abc123def456")

    answer = client.request("PUT", "/files/config", body=b"{}")

    assert answer.status == 409
    assert "Claude is working" in answer.json["error"]
    assert not addon.CLI_CONFIG_PATH.exists()


def test_the_instructions_can_be_saved_while_claude_is_working(addon, client, monkeypatch):
    monkeypatch.setattr(addon, "RUNNING_JOB", "abc123def456")

    answer = client.request("PUT", "/files/memory", body=b"changed mid-run")

    assert answer.status == 200


@pytest.mark.parametrize("key", ["settings", "../etc/passwd", "credentials", "options"])
def test_no_other_file_is_reachable(client, key):
    read = client.get(f"/files/{key}")
    written = client.request("PUT", f"/files/{key}", body=b"x")

    assert read.status == 404
    assert written.status == 404


def test_asking_with_no_key_at_all_is_the_listing(client):
    answer = client.get("/files/")

    assert answer.status == 200
    assert [entry["key"] for entry in answer.json["files"]] == ["config", "memory"]


def test_a_file_too_large_for_an_editor_says_to_use_the_terminal(addon, client):
    addon.CLI_CONFIG_PATH.write_text("x" * (addon.MAX_EDITABLE_BYTES + 1))

    answer = client.get("/files/config")

    assert answer.status == 413
    assert "too large to edit here" in answer.json["error"]


def test_more_than_the_editor_will_hold_is_refused_before_it_is_written(addon, client):
    answer = client.request("PUT", "/files/memory", body=b"x" * (addon.MAX_EDITABLE_BYTES + 1))

    assert answer.status == 413
    assert not (addon.HOME / ".claude" / "CLAUDE.md").exists()


def test_a_file_that_cannot_be_read_is_reported_rather_than_returned_empty(
    addon, client, monkeypatch
):
    original_read_text = addon.Path.read_text

    def refuse(self, *args, **kwargs):
        if self.name == "CLAUDE.md":
            raise OSError("input/output error")
        return original_read_text(self, *args, **kwargs)

    (addon.HOME / ".claude" / "CLAUDE.md").write_text("something")
    monkeypatch.setattr(addon.Path, "read_text", refuse)

    answer = client.get("/files/memory")

    assert answer.status == 500
    assert "could not be read" in answer.json["error"]


def test_text_the_editor_writes_is_never_half_written(addon, tmp_path):
    target = tmp_path / "atomic.md"
    addon.write_text_atomic(target, "first")

    addon.write_text_atomic(target, "second, and longer")

    assert target.read_text() == "second, and longer"
    assert not (tmp_path / "atomic.md.tmp").exists()
