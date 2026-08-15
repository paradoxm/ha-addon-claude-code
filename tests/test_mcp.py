"""Switching MCP servers on and off, which is moving them in and out of the CLI."""

import json

import pytest


@pytest.fixture(autouse=True)
def no_servers_configured(addon):
    """Each test decides what is configured; none inherits it."""

    def clear():
        addon.CLI_CONFIG_PATH.write_text("{}")
        addon.PROJECT_MCP_PATH.unlink(missing_ok=True)
        addon.MCP_OFF_PATH.unlink(missing_ok=True)

    clear()
    yield
    clear()


def configure(addon, scope, name, definition):
    """Put a server where the CLI would have put it for that scope."""
    if scope == "project":
        addon.PROJECT_MCP_PATH.write_text(json.dumps({"mcpServers": {name: definition}}))
        return

    config = json.loads(addon.CLI_CONFIG_PATH.read_text() or "{}")
    if scope == "user":
        config.setdefault("mcpServers", {})[name] = definition
    else:
        projects = config.setdefault("projects", {})
        here = projects.setdefault(str(addon.CHAT_DIR), {})
        here.setdefault("mcpServers", {})[name] = definition
    addon.CLI_CONFIG_PATH.write_text(json.dumps(config))


STDIO = {"command": "npx", "args": ["-y", "some-mcp-server"], "env": {"API_KEY": "s3cret"}}
REMOTE = {"type": "http", "url": "https://mcp.example.com/mcp?key=s3cret"}


def test_no_servers_configured_lists_nothing(client):
    answer = client.get("/mcp")

    assert answer.status == 200
    assert answer.json["servers"] == []


def test_a_server_is_listed_with_how_it_is_reached_and_where_it_applies(addon, client):
    configure(addon, "user", "everywhere", STDIO)
    configure(addon, "local", "this-folder", REMOTE)

    listed = client.get("/mcp").json["servers"]

    assert [server["name"] for server in listed] == ["this-folder", "everywhere"]
    assert [server["scope"] for server in listed] == ["local", "user"]
    assert [server["transport"] for server in listed] == ["http", "stdio"]
    assert [server["enabled"] for server in listed] == [True, True]


def test_the_summary_says_what_a_server_is_without_saying_its_secrets(addon, client):
    configure(addon, "user", "with-a-key", STDIO)
    configure(addon, "local", "with-a-token-in-the-url", REMOTE)

    listed = client.get("/mcp").json["servers"]
    everything = json.dumps(listed)

    assert "s3cret" not in everything
    assert "API_KEY" not in everything
    by_name = {server["name"]: server for server in listed}
    assert by_name["with-a-key"]["summary"] == "npx -y some-mcp-server"
    assert by_name["with-a-token-in-the-url"]["summary"] == "https://mcp.example.com/mcp"


def test_a_server_that_shares_a_name_across_scopes_is_listed_once(addon, client):
    configure(addon, "user", "shared", STDIO)
    configure(addon, "project", "shared", REMOTE)

    listed = client.get("/mcp").json["servers"]

    assert len(listed) == 1
    assert listed[0]["scope"] == "project", "the narrower scope is the one in force"


@pytest.mark.parametrize("scope", ["user", "local", "project"])
def test_switching_a_server_off_lifts_it_out_of_the_cli_and_keeps_it(addon, client, scope):
    configure(addon, scope, "switchable", STDIO)

    answer = client.send_json("POST", "/mcp/switchable", {"enabled": False})

    assert answer.status == 200
    assert answer.json == {
        "name": "switchable",
        "enabled": False,
        "changed": True,
        "scope": scope,
    }
    assert "switchable" not in addon.live_mcp_servers()
    kept = json.loads(addon.MCP_OFF_PATH.read_text())
    assert kept["switchable"]["definition"] == STDIO
    assert kept["switchable"]["scope"] == scope

    listed = client.get("/mcp").json["servers"]
    assert listed == [
        {
            "name": "switchable",
            "scope": scope,
            "transport": "stdio",
            "summary": "npx -y some-mcp-server",
            "enabled": False,
        }
    ]


@pytest.mark.parametrize("scope", ["user", "local", "project"])
def test_switching_it_back_on_restores_it_exactly_as_it_was(addon, client, scope):
    configure(addon, scope, "switchable", STDIO)
    client.send_json("POST", "/mcp/switchable", {"enabled": False})

    answer = client.send_json("POST", "/mcp/switchable", {"enabled": True})

    assert answer.status == 200
    assert answer.json["changed"] is True
    restored = addon.live_mcp_servers()["switchable"]
    assert restored["definition"] == STDIO
    assert restored["scope"] == scope
    assert json.loads(addon.MCP_OFF_PATH.read_text()) == {}


def test_switching_off_something_already_off_changes_nothing(addon, client):
    configure(addon, "user", "switchable", STDIO)
    client.send_json("POST", "/mcp/switchable", {"enabled": False})

    answer = client.send_json("POST", "/mcp/switchable", {"enabled": False})

    assert answer.status == 200
    assert answer.json == {"name": "switchable", "enabled": False, "changed": False}


def test_switching_on_something_already_on_changes_nothing(addon, client):
    configure(addon, "user", "switchable", STDIO)

    answer = client.send_json("POST", "/mcp/switchable", {"enabled": True})

    assert answer.json == {"name": "switchable", "enabled": True, "changed": False}


def test_switching_off_a_server_that_does_not_exist_is_a_404(client):
    answer = client.send_json("POST", "/mcp/never-configured", {"enabled": False})

    assert answer.status == 404
    assert "no such MCP server" in answer.json["error"]


def test_switching_on_a_server_that_was_never_kept_aside_is_a_404(client):
    answer = client.send_json("POST", "/mcp/never-configured", {"enabled": True})

    assert answer.status == 404
    assert "kept aside" in answer.json["error"]


def test_a_name_that_could_escape_a_config_file_is_refused(client):
    answer = client.send_json("POST", "/mcp/..%2Fescaped", {"enabled": False})

    assert answer.status == 400
    assert "unsafe name" in answer.json["error"]


@pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": 1}, {"enabled": None}])
def test_the_switch_has_to_say_which_way(client, body):
    answer = client.send_json("POST", "/mcp/anything", body)

    assert answer.status == 400
    assert answer.json["error"] == "'enabled' must be true or false"


def test_a_cli_that_refuses_to_remove_leaves_the_server_alone(addon, client, monkeypatch):
    configure(addon, "user", "stubborn", STDIO)
    monkeypatch.setattr(addon, "run_cli", lambda *a, **k: (False, "config is read-only"))

    answer = client.send_json("POST", "/mcp/stubborn", {"enabled": False})

    assert answer.status == 502
    assert "config is read-only" in answer.json["error"]
    assert "stubborn" in addon.live_mcp_servers(), "still configured"
    assert json.loads(addon.MCP_OFF_PATH.read_text()) == {}, "and not left in limbo"


def test_a_cli_that_refuses_to_add_keeps_the_definition_aside(addon, client, monkeypatch):
    configure(addon, "user", "stubborn", STDIO)
    client.send_json("POST", "/mcp/stubborn", {"enabled": False})
    monkeypatch.setattr(addon, "run_cli", lambda *a, **k: (False, "not a valid definition"))

    answer = client.send_json("POST", "/mcp/stubborn", {"enabled": True})

    assert answer.status == 502
    assert json.loads(addon.MCP_OFF_PATH.read_text())["stubborn"]["definition"] == STDIO


def test_a_config_file_that_is_not_readable_lists_nothing_rather_than_failing(addon, client):
    addon.CLI_CONFIG_PATH.write_text("{ truncated")

    answer = client.get("/mcp")

    assert answer.status == 200
    assert answer.json["servers"] == []


def test_entries_that_are_not_definitions_are_skipped(addon, client):
    addon.CLI_CONFIG_PATH.write_text(
        json.dumps({"mcpServers": {"fine": STDIO, "../escaped": STDIO, "wrong": "a string"}})
    )

    listed = client.get("/mcp").json["servers"]

    assert [server["name"] for server in listed] == ["fine"]


def test_something_kept_aside_that_is_not_a_definition_is_ignored(addon, client):
    addon.MCP_OFF_PATH.write_text(json.dumps({"broken": "not an entry", "worse": {}}))

    assert client.get("/mcp").json["servers"] == []


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        ({"command": "npx", "args": ["a", "b"]}, "stdio"),
        ({"url": "https://example.com/mcp"}, "http"),
        ({"type": "sse", "url": "https://example.com/sse"}, "sse"),
        ({"type": "SSE", "url": "https://example.com/sse"}, "sse"),
        ({"type": "nonsense", "command": "x"}, "stdio"),
        ({}, "stdio"),
    ],
)
def test_the_transport_is_read_from_the_definition(addon, definition, expected):
    assert addon.mcp_transport(definition) == expected


def test_a_very_long_command_is_cut_to_something_a_row_can_hold(addon):
    definition = {"command": "npx", "args": ["x" * 500]}

    assert len(addon.mcp_summary(definition)) == 200
