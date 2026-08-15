"""Shared setup for the add-on's tests.

`api.py` reads its options, its environment and its data directory at import time,
so all of that has to exist before the module is imported. That happens here, at
conftest import time, because pytest imports this file before any test module —
which is the only ordering that makes a module-level snapshot safe to test.

Nothing here mocks the add-on's own code. The tests drive the real HTTP server and
a stand-in `claude` binary that produces genuine `stream-json` output, so a test
that passes says something about what a caller will see.
"""

import http.client
import io
import json
import os
import re
import shutil
import socket
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
ADDON_DIR = REPO_ROOT / "claude-code"
STUB_CLAUDE_SOURCE = REPO_ROOT / "tools" / "stub-claude.py"

API_TOKEN = "tests-token-tests-token"
JOB_TIMEOUT_MINUTES = 5

DATA_DIR = Path(tempfile.mkdtemp(prefix="addon-tests-"))
(DATA_DIR / "home" / ".claude").mkdir(parents=True)
(DATA_DIR / "options.json").write_text(
    json.dumps(
        {
            "model": "opus",
            "effort": "medium",
            "permission_mode": "manual",
            "api_token": API_TOKEN,
            "timeout_minutes": JOB_TIMEOUT_MINUTES,
            "auto_update": False,
            "update_channel": "latest",
        }
    )
)

# The stand-in has to be found as `claude`, and the add-on snapshots the
# environment at import time, so PATH is set before the import below.
STUB_BIN_DIR = DATA_DIR / "bin"
STUB_BIN_DIR.mkdir()
shutil.copy(STUB_CLAUDE_SOURCE, STUB_BIN_DIR / "claude")
(STUB_BIN_DIR / "claude").chmod(0o755)
os.environ["PATH"] = f"{STUB_BIN_DIR}{os.pathsep}{os.environ['PATH']}"
os.environ["ADDON_DATA"] = str(DATA_DIR)

sys.path.insert(0, str(ADDON_DIR))
import api  # noqa: E402  the environment above has to be in place first


class Response:
    """One HTTP answer, with the body already decoded when it is JSON."""

    def __init__(self, status: int, headers: dict, body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def json(self) -> dict:
        return json.loads(self.body)

    def __repr__(self) -> str:
        return f"<Response {self.status} {self.body[:120]!r}>"


class Client:
    """Drives the add-on over HTTP, the way another add-on and the browser do."""

    def __init__(self, port: int):
        self.port = port

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        token: str | None = API_TOKEN,
        headers: dict | None = None,
    ) -> Response:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        sent_headers = dict(headers or {})
        if token is not None:
            sent_headers.setdefault("Authorization", f"Bearer {token}")
        try:
            connection.request(method, path, body=body, headers=sent_headers)
            answer = connection.getresponse()
            return Response(answer.status, dict(answer.getheaders()), answer.read())
        finally:
            connection.close()

    def send_json(self, method: str, path: str, payload=None, **kwargs) -> Response:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json", **kwargs.pop("headers", {})}
        return self.request(method, path, body=body, headers=headers, **kwargs)

    def get(self, path: str, **kwargs) -> Response:
        return self.request("GET", path, **kwargs)


def wait_until(predicate, *, timeout: float = 30.0, description: str = "the expected state"):
    """Poll until `predicate` returns something truthy, then return it.

    Fails the test rather than returning None, so a timeout reads as the assertion
    it is instead of surfacing later as an unrelated comparison against None.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        outcome = predicate()
        if outcome:
            return outcome
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {description}")


def tar_skill(*, name=None, description="a stub skill", extra_files=None) -> bytes:
    """A `.tar.gz` shaped like a real skill: one wrapping folder around SKILL.md."""
    frontmatter = "---\n"
    if name:
        frontmatter += f"name: {name}\n"
    frontmatter += f"description: {description}\n---\n\n# body\n"

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        members = {"SKILL.md": frontmatter, **(extra_files or {})}
        for relative_path, text in members.items():
            payload = text.encode()
            entry = tarfile.TarInfo(f"wrapper/{relative_path}")
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
    return buffer.getvalue()


@pytest.fixture(scope="session")
def addon():
    """The add-on's module, with its directories in place and its worker running."""
    api.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    api.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    api.CHAT_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=api.worker, daemon=True).start()
    return api


@pytest.fixture(scope="session")
def client(addon) -> Iterator[Client]:
    """The real server, on a port the operating system picks."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = addon.ThreadingHTTPServer(("127.0.0.1", port), addon.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Client(port)
    server.shutdown()


@pytest.fixture(autouse=True)
def settled_queue(addon, client):
    """Leave the add-on idle after every test.

    One worker runs every turn, so a run left in flight would be the next test's
    first surprise. This waits for the queue to drain rather than sleeping.
    """
    yield
    wait_until(
        lambda: not (client.get("/chat").json.get("pending") or []),
        description="the queue to drain after the test",
    )


@pytest.fixture
def transcripts_dir(addon) -> Path:
    """Where Claude Code keeps this working directory's transcripts.

    Created here rather than waited for, so a test about the history list does not
    depend on another test having held a conversation first.
    """
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(addon.CHAT_DIR))
    directory = addon.HOME / ".claude" / "projects" / slug
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def fresh_conversation(client):
    """Start from an empty transcript, so a test's turns are the only ones in it."""
    client.send_json("POST", "/chat/new")
    return client


@pytest.fixture
def stub_behaviour(addon):
    """Steer the stand-in `claude` for one test, then put the environment back.

    Both the process environment and the add-on's snapshot of it have to be set:
    the snapshot is what child processes inherit, and the environment is what a
    directly spawned stub reads.
    """
    original = {}

    def set_variable(name: str, value: str) -> None:
        original.setdefault(name, (os.environ.get(name), addon.CLAUDE_ENV.get(name)))
        os.environ[name] = value
        addon.CLAUDE_ENV[name] = value

    yield set_variable

    for name, (process_value, snapshot_value) in original.items():
        for target, value in ((os.environ, process_value), (addon.CLAUDE_ENV, snapshot_value)):
            if value is None:
                target.pop(name, None)
            else:
                target[name] = value


@pytest.fixture(scope="session", autouse=True)
def _remove_data_directory():
    yield
    shutil.rmtree(DATA_DIR, ignore_errors=True)
