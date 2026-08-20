#!/usr/bin/env python3
"""HTTP API in front of the Claude Code CLI.

Everything lives inside the add-on's own /data volume, which Home Assistant keeps
across restarts and add-on updates, so no HA folder needs to be mapped and no
other add-on needs filesystem access:

    /data/home/.claude/skills/<name>/   skills, uploaded as .tar.gz
    /data/jobs/<id>/in/                 files uploaded for a job
    /data/jobs/<id>/job.json            job state
    /data/jobs/<id>/stream.jsonl        the CLI's streaming output, written as it
                                        runs so a reply can be read in progress
    /data/jobs/<id>/claude.log          stderr
    /data/update.json                   progress of the last CLI update

HOME points at /data/home, which is how the CLI finds the skills: Claude Code
discovers personal skills in ~/.claude/skills and needs no configuration for it.

Runs at most one CLI process at a time — job or update, never both — because an
update replaces the very binary a job is executing.

SECURITY NOTE. A job prompt and an installed skill both run as root in this
container with Bash pre-approved and unrestricted outbound network. They can read
/data/home/.claude/.credentials.json and /data/options.json. `--allowedTools` is a
convenience, not a boundary: only install skills and accept job inputs you would
run on your own machine.
"""

import contextlib
import hmac
import io
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

DATA = Path(os.environ.get("ADDON_DATA", "/data"))
OPTIONS_PATH = DATA / "options.json"
HOME = DATA / "home"
SKILLS_DIR = HOME / ".claude" / "skills"
JOBS_DIR = DATA / "jobs"
# Every chat turn runs here, in one shared directory, because --resume looks for a
# session in the current working directory. Per-job directories would mean each
# message started a fresh conversation.
CHAT_DIR = DATA / "chat"
UPDATE_STATE_PATH = DATA / "update.json"
PORT = 7682

# `claude install` writes here, on the persistent volume, so an update outlives a
# restart. The packaged copy in the image layer does not, which is why this
# directory goes first on PATH.
LOCAL_BIN = HOME / ".local" / "bin"

# The channel path returns a bare version string, so the available version can be
# read without downloading anything.
RELEASES_URL = "https://downloads.claude.ai/claude-code-releases"

# What the account's own clients read to draw the usage bars: how much of the
# five-hour window and of the week is gone, and when each resets. Undocumented, so it
# is treated as best-effort throughout — a caller is told "unavailable" rather than
# being handed a guess, and nothing here refuses to run because this could not be
# read. The credentials are the CLI's own, which is what makes the answer this
# account's own usage.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS_PATH = HOME / ".claude" / ".credentials.json"

# The access token in that file lives about eight hours. The CLI renews it from the
# refresh token beside it whenever one of its own requests comes back 401 — but nothing
# here runs the CLI, so an add-on left alone overnight woke up with a spent token, read
# 401 from then on, and the guard went blind with it. So the same renewal is done here,
# with the CLI's own endpoint and its own file written back whole.
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"  # noqa: S105 - an address, not a secret
# The CLI's public client. Taken from the file when it records one, because the file is
# the account's own truth and this constant is only a last resort.
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Renewed a little early, so a reading is not spent discovering the token died.
TOKEN_MARGIN_SEC = 5 * 60
# Two renewals racing spend the same refresh token twice, and the loser is signed out
# rather than renewed. Only one at a time, and never while the CLI is running — it does
# its own renewing, and the file it writes is the one read here.
CREDENTIALS_LOCK = threading.Lock()
# Polled before each run by anything driving the API, and it changes slowly.


# Once a day: releases do not land more often than that, and each check spawns the
# binary and reaches the network.
CHECK_INTERVAL_SEC = 24 * 3600
INSTALLED_TTL_SEC = 60

# `fullmatch`, not `match`: with `$` a trailing newline slips through, which is
# enough to inject a header via the download filename.
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# `[0-9]`, not `\d`: `\d` matches other scripts' digits, which int() then accepts.
UPDATE_TARGET = re.compile(r"latest|stable|[0-9]+(\.[0-9]+)*")
# What the selector offers. Validation stays wider on purpose: an API caller may
# pin a full name such as claude-sonnet-5, which an alias cannot express.
MODEL_ALIASES = ("opus", "sonnet", "haiku", "fable")
SAFE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
# Exactly what `claude --effort` documents. Anything else is refused rather than
# handed to the CLI, where an unknown value aborts the run.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# The modes Claude Code documents. bypassPermissions is deliberately absent: it
# removes every check, and this container holds the account credentials.
#
# `manual` rather than `default`: that is the name the CLI, the extensions and the
# apps all use for the review-everything mode. `default` remains its config value
# and the CLI accepts either, so it stays valid for an API caller — but only one of
# the two is offered, since a list showing both names for one mode is worse than
# either name alone. The alias needs Claude Code 2.1.200+; this image ships newer.
PERMISSION_MODES = ("manual", "plan", "acceptEdits", "auto", "dontAsk")
PERMISSION_ALIASES = {"default": "manual"}
# Slash commands the chat is allowed to send. `/compact` and `/rename <title>`
# both work in -p mode; the value form needs Claude Code 2.1.205+.
CHAT_COMMANDS = ("compact", "rename")
VERSION_ONLY = re.compile(r"[0-9]+(\.[0-9]+)*")

MAX_UPLOAD = 256 * 1024 * 1024
# The compressed cap says nothing about what a tar expands to: zero-fill gzips at
# roughly 1000:1, so an uncapped extract turns a small upload into a full disk.
MAX_EXTRACTED = 512 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_RESULT_CHARS = 100_000
MAX_ERROR_CHARS = 4000
# Jobs carry uploads and produced files, so few are kept. Chat turns are text
# only, and they are the conversation history, so they are kept far longer.
JOBS_KEPT = 50
CHAT_KEPT = 500

INTERNAL_FILES = {"job.json", "stream.jsonl"}
TERMINAL_STATUSES = {"done", "failed", "interrupted"}


# Where this machine thinks it is. Home Assistant hands its own setting to every add-on in
# TZ; if that is missing, the Supervisor will say. Times shown to a person should be the
# ones on the clock they are looking at, and a browser in another country is not it.
TIMEZONE_CACHE: dict[str, str | None] = {"value": None}


def addon_timezone() -> str:
    if TIMEZONE_CACHE["value"]:
        return str(TIMEZONE_CACHE["value"])

    zone = os.environ.get("TZ", "").strip()
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not zone and token:
        request = urllib.request.Request(
            "http://supervisor/info", headers={"Authorization": f"Bearer {token}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as answer:  # noqa: S310 - fixed host
                zone = str((json.load(answer).get("data") or {}).get("timezone") or "")
        except (urllib.error.URLError, OSError, ValueError):
            zone = ""
    TIMEZONE_CACHE["value"] = zone or "UTC"
    return str(TIMEZONE_CACHE["value"])


def parse_when(value) -> datetime | None:
    """A reset time as the endpoint gives it, or None if it gave nonsense."""
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def when_for_people(value) -> str:
    """A moment as somebody would say it, on this machine's clock.

    The raw stamp is right for a machine and unreadable for a person — it went into a
    refusal a person reads, microseconds and offset and all.
    """
    when = parse_when(value)
    if when is None:
        return ""
    # astimezone() with no argument uses the system zone, which Home Assistant sets for
    # every add-on. Where the container has no zone data it stays UTC, which is honest.
    return when.astimezone().strftime("%d %b, %H:%M")


def now() -> str:
    """Microseconds, not seconds.

    Jobs are ordered by this, and with second precision two created in the same
    second sorted arbitrarily — enough for the chat to show the wrong turn's error
    and for pruning to delete the wrong ones.
    """
    return datetime.now(UTC).isoformat()


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data: dict) -> None:
    """Atomic: a poller reads either the old file or the new one, never half.

    `Path.write_text` truncates first, so a concurrent reader saw partial JSON —
    measured at 40% of reads under contention — and a power cut left the file
    corrupt for good.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


OPTIONS = read_json(OPTIONS_PATH, {}) or {}
API_TOKEN = str(OPTIONS.get("api_token") or "")
DEFAULT_MODEL = str(OPTIONS.get("model") or "opus")
TIMEOUT_SEC = int(OPTIONS.get("timeout_minutes") or 90) * 60
AUTO_UPDATE = bool(OPTIONS.get("auto_update", True))
UPDATE_CHANNEL = str(OPTIONS.get("update_channel") or "latest")
DEFAULT_EFFORT = str(OPTIONS.get("effort") or "")
# How full a window may get before work stops. One figure per window, because the two are
# not the same question: the five-hour window refills while the day goes on, so spending it
# to the brim costs an hour of waiting, while the week is what a fortnight of work has to
# fit inside — most people want to be stricter about the second.
SESSION_THRESHOLD = int(OPTIONS.get("session_threshold") or 90)
WEEK_THRESHOLD = int(OPTIONS.get("week_threshold") or 90)
# Whether the add-on acts on that number itself: refusing to start a turn against a wall,
# and freezing one that runs into it. Off means it only reports, and whoever drives it
# decides — which is how this started, and it left every caller to get it right alone.
GUARD_LIMITS = bool(OPTIONS.get("guard_limits", True))
# How often the allowance may be read, by anybody. Every reading is a request to Anthropic;
# this is the one place that decides how many of those there are.
USAGE_TTL_SEC = int(OPTIONS.get("usage_check_seconds") or 180)
DEFAULT_PERMISSION_MODE = str(OPTIONS.get("permission_mode") or "manual")

CHAT_STATE_PATH = DATA / "chat.json"
# Claude Code's own MCP configuration. Read from the files the CLI writes, because
# `claude mcp list` prints a health-checked table rather than anything parseable, and
# written through the CLI, because the format is its own.
#
#   ~/.claude.json      mcpServers                     -> every conversation ("user")
#                       projects/<cwd>/mcpServers      -> this folder only ("local")
#   <cwd>/.mcp.json     mcpServers                     -> shared with the folder ("project")
CLI_CONFIG_PATH = HOME / ".claude.json"
PROJECT_MCP_PATH = CHAT_DIR / ".mcp.json"
# Ours, and the reason a switch is a switch: a server turned off is lifted out of the
# CLI's config and kept here whole, so turning it back on restores it as it was
# rather than asking for the command and the secrets again.
MCP_OFF_PATH = DATA / "mcp-off.json"
MCP_SCOPES = ("user", "local", "project")

# The two files this add-on will show and let you edit, by key. A fixed list rather
# than a path parameter: the point is these files, not a file browser rooted at /data.
EDITABLE_FILES: dict[str, dict[str, Any]] = {
    "config": {
        "path": CLI_CONFIG_PATH,
        "kind": "json",
        # The CLI rewrites this itself, so a save while it is working could land on
        # top of its own change. Refused rather than risked.
        "busy_conflicts": True,
    },
    "memory": {
        "path": HOME / ".claude" / "CLAUDE.md",
        "kind": "markdown",
        "busy_conflicts": False,
    },
}
# A textarea is not a file editor. ~/.claude.json grows with every folder Claude Code
# has been run in, and past a couple of megabytes the terminal is the right tool.
MAX_EDITABLE_BYTES = 2 * 1024 * 1024
# Claude Code's own user settings, which is where permission rules live.
SETTINGS_PATH = HOME / ".claude" / "settings.json"
# Without this the bundled ripgrep is used, and it does not run on musl. It is
# reapplied on every save so a hand-edited file cannot break search.
REQUIRED_ENV = {"USE_BUILTIN_RIPGREP": "0"}
SESSION_ID = re.compile(r"[0-9a-fA-F-]{8,64}")
SLASH_COMMAND = re.compile(r"/(compact|rename|clear|model|effort)\b")

# Claude Code records far more than the conversation. A slash command, its output,
# an injected reminder, the `!` bash mode, a subagent's notifications and a failed
# tool all arrive as ordinary `user` records — so `/rename` alone puts three of
# them in the transcript. None is anything the person typed, and none belongs in
# the chat window. The list comes from reading real transcripts, not from guessing.
MACHINERY = re.compile(
    r"\s*<(?:local-command-caveat|command-name|command-message|command-args"
    r"|local-command-stdout|local-command-stderr|persisted-output|system-reminder"
    r"|bash-input|bash-stdout|bash-stderr|task-notification|tool_use_error)>"
)
# A real message can carry one of these appended to it, so the span is cut out and
# whatever the person actually wrote is kept.
REMINDER_SPAN = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.DOTALL)
TOOL_ERROR = re.compile(r"</?tool_use_error>", re.DOTALL)
# What Claude Code puts in `message.model` for a message it wrote itself rather
# than one the model produced. In headless mode a slash command is answered with a
# synthetic "No response requested.", which is bookkeeping and not a reply; the API
# errors among them are worth seeing, but beside the conversation rather than in it.
SYNTHETIC_MODEL = "<synthetic>"
# The CLI's own notices carry a severity; `info` is bookkeeping such as turn
# timings, which is not worth anyone's attention.
NOTICE_LEVELS = {"warning", "error"}
NOTICES_KEPT = 20
MAX_NOTICE_CHARS = 2000


def current_session() -> str | None:
    """Which conversation the next message continues. None starts a fresh one."""
    state = read_json(CHAT_STATE_PATH, {}) or {}
    value = state.get("session")
    return value if isinstance(value, str) and SESSION_ID.fullmatch(value) else None


def set_current_session(session: str | None) -> None:
    write_json(CHAT_STATE_PATH, {"session": session})


def write_text_atomic(path: Path, text: str) -> None:
    """Same care as write_json, for a file whose formatting is the author's own."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# A place for a caller's own notes, keyed by a name it chooses. The add-on itself never
# looks inside: what a bot remembers between its own runs — who it is talking to, which
# job it is watching, what it has already sent — is its business, and this only has to
# give it back exactly as it was left. Small on purpose; this is not a database.
STATE_DIR = DATA / "state"
MAX_STATE_BYTES = 512 * 1024


def state_path(key: str) -> Path:
    if not SAFE_NAME.fullmatch(key):
        raise ApiError(400, f"a key must be a plain name: {key}")
    return STATE_DIR / f"{key}.json"


def read_state(key: str) -> dict:
    path = state_path(key)
    if not path.is_file():
        raise ApiError(404, f"nothing stored under {key}")
    value = read_json(path)
    if value is None:
        raise ApiError(500, f"{key} could not be read back")
    return {"key": key, "value": value}


def write_state(key: str, value: dict) -> dict:
    path = state_path(key)
    text = json.dumps(value, ensure_ascii=False)
    if len(text.encode()) > MAX_STATE_BYTES:
        raise ApiError(413, f"a note must be under {MAX_STATE_BYTES // 1024} kb")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, text)
    return {"key": key, "bytes": len(text.encode())}


def forget_state(key: str) -> dict:
    path = state_path(key)
    existed = path.is_file()
    path.unlink(missing_ok=True)
    return {"key": key, "existed": existed}


def list_state() -> list:
    if not STATE_DIR.is_dir():
        return []
    return sorted(p.stem for p in STATE_DIR.glob("*.json") if SAFE_NAME.fullmatch(p.stem))


def editable_file(key: str) -> dict:
    entry = EDITABLE_FILES.get(key)
    if entry is None:
        raise ApiError(404, f"no such file: {key}")
    return entry


def read_editable(key: str) -> dict:
    entry = editable_file(key)
    path = entry["path"]
    try:
        size = path.stat().st_size
    except OSError:
        return {"key": key, "path": str(path), "kind": entry["kind"], "exists": False, "text": ""}
    if size > MAX_EDITABLE_BYTES:
        raise ApiError(
            413,
            f"{path} is {size // 1024} kb; too large to edit here — use the terminal",
        )
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        raise ApiError(500, f"{path} could not be read: {exc}") from exc
    return {"key": key, "path": str(path), "kind": entry["kind"], "exists": True, "text": text}


def write_editable(key: str, text: str) -> dict:
    entry = editable_file(key)
    if len(text.encode()) > MAX_EDITABLE_BYTES:
        raise ApiError(413, "that is larger than this editor will save")
    if entry["kind"] == "json":
        try:
            json.loads(text)
        except ValueError as exc:
            raise ApiError(400, f"not valid JSON: {exc}") from exc
    if entry["busy_conflicts"] and RUNNING_JOB is not None:
        raise ApiError(409, "Claude is working; this file is its own and would be overwritten")

    path = entry["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, text)
    print(f"[api] wrote {path}", flush=True)
    return {"key": key, "path": str(path), "bytes": len(text.encode())}


def read_settings() -> dict:
    return read_json(SETTINGS_PATH, {}) or {}


def write_settings(text: str) -> dict:
    """Validate and store Claude Code's user settings.

    The schema belongs to Claude Code, so this checks only that the file is a JSON
    object and that the permission rules are the shape it documents — lists of
    strings. Anything stricter would reject a key the CLI understands and this
    add-on has not heard of.
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ApiError(400, f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "settings must be a JSON object")

    permissions = data.get("permissions")
    if permissions is not None:
        if not isinstance(permissions, dict):
            raise ApiError(400, "'permissions' must be an object")
        for key in ("allow", "deny", "ask"):
            rules = permissions.get(key)
            if rules is None:
                continue
            if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
                raise ApiError(400, f"'permissions.{key}' must be a list of strings")

    env = data.setdefault("env", {})
    if not isinstance(env, dict):
        raise ApiError(400, "'env' must be an object")
    env.update(REQUIRED_ENV)

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json(SETTINGS_PATH, data)
    print("[api] settings saved", flush=True)
    return data


def sessions_dir() -> Path | None:
    """Where Claude Code keeps this directory's transcripts.

    It derives the folder name from the working directory, replacing everything
    that is not alphanumeric with a dash. Rather than rely on that rule, the
    computed name is checked first and the whole projects folder scanned as a
    fallback, so a change in the CLI's naming cannot silently empty the history.
    """
    root = HOME / ".claude" / "projects"
    guess = root / re.sub(r"[^A-Za-z0-9]", "-", str(CHAT_DIR))
    if guess.is_dir():
        return guess
    if not root.is_dir():
        return None
    for path in root.iterdir():
        if path.is_dir() and path.name.endswith(re.sub(r"[^A-Za-z0-9]", "-", CHAT_DIR.name)):
            return path
    return None


def block_text(content) -> str:
    """The visible text of a transcript message, without thinking or tool calls."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part).strip()


def block_errors(content) -> list:
    """The text of any failed tool call in one message.

    A tool failure is not a text block, so it never reaches `block_text`: the CLI
    records it as a tool result carrying `is_error`. These are worth showing, but
    beside the conversation rather than in it — they are about the run, not about
    what was said.
    """
    if not isinstance(content, list):
        return []
    errors = []
    for block in content:
        if not isinstance(block, dict) or not block.get("is_error"):
            continue
        body = block.get("content")
        if isinstance(body, list):
            body = "\n".join(
                part.get("text", "") for part in body if isinstance(part, dict)
            )
        text = TOOL_ERROR.sub("", str(body or "")).strip()
        if text:
            errors.append(text)
    return errors


def notice_of(record: dict) -> dict | None:
    """One of the CLI's own warnings, if this record is one.

    Claude Code reports things like a refused request or an unknown command as
    `system` records with a `level`, which is how the add-on can surface them
    without inventing a channel of its own.
    """
    if record.get("type") != "system" or record.get("level") not in NOTICE_LEVELS:
        return None
    text = str(record.get("content") or "").strip()
    explanation = str(record.get("apiRefusalExplanation") or "").strip()
    text = "\n\n".join(part for part in (text, explanation) if part)
    if not text:
        return None
    return {
        "kind": str(record.get("subtype") or "notice"),
        "level": record.get("level"),
        "text": text[:MAX_NOTICE_CHARS],
        "at": record.get("timestamp"),
    }


def read_conversation(session: str) -> tuple:
    """One conversation, read from Claude Code's own transcript.

    Returns what was said and, separately, what went wrong along the way. One pass
    for both: this is re-read on every poll of the chat, and a long conversation's
    transcript is not small.
    """
    directory = sessions_dir()
    checked = SESSION_ID.fullmatch(session)
    if not directory or not checked:
        return [], []
    # The id is checked here and not only at the routes, because it is about to be
    # joined onto a path.
    path = directory / f"{checked.group()}.jsonl"
    if not path.is_file():
        return [], []

    turns: list = []
    notices: list = []
    with path.open(errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue

            notice = notice_of(record)
            if notice:
                notices.append(notice)
                continue

            if record.get("type") not in {"user", "assistant"}:
                continue
            # Subagent chatter belongs to the run, not to the conversation.
            if record.get("isSidechain"):
                continue
            message = record.get("message") or {}
            content = message.get("content")

            # A message the CLI wrote itself is not a reply. `/rename` in headless
            # mode is answered with "No response requested.", which was showing up
            # as something Claude had said. An API error is synthetic too, and that
            # one is worth keeping — as a notice.
            if message.get("model") == SYNTHETIC_MODEL:
                if record.get("isApiErrorMessage"):
                    notices.append(
                        {
                            "kind": "api_error",
                            "level": "error",
                            "text": block_text(content)[:MAX_NOTICE_CHARS],
                            "at": record.get("timestamp"),
                        }
                    )
                continue

            for failure in block_errors(content):
                notices.append(
                    {
                        "kind": "tool_error",
                        "level": "error",
                        "text": failure[:MAX_NOTICE_CHARS],
                        "at": record.get("timestamp"),
                    }
                )

            text = REMINDER_SPAN.sub("", block_text(content)).strip()
            if not text:
                continue
            # A slash command and everything the CLI wraps in a tag of its own are
            # instructions and machinery, not lines of the conversation.
            if message.get("role") == "user" and (
                SLASH_COMMAND.match(text) or MACHINERY.match(text)
            ):
                continue
            turns.append(
                {
                    "role": message.get("role") or record["type"],
                    "text": text[:MAX_RESULT_CHARS],
                    "at": record.get("timestamp"),
                }
            )
    return turns, notices[-NOTICES_KEPT:]


def scan_session(path: Path) -> dict | None:
    """Title, first prompt and message count for one transcript.

    Shared by the history list and by the header, so a title is picked the same way
    in both: one the user set with /rename wins over the one Claude generated.
    """
    title = ai_title = first_prompt = None
    messages = 0
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                kind = record.get("type")
                if kind == "custom-title":
                    title = record.get("customTitle") or title
                elif kind == "ai-title":
                    ai_title = record.get("aiTitle") or ai_title
                elif kind in {"user", "assistant"} and not record.get("isSidechain"):
                    message = record.get("message") or {}
                    if message.get("model") == SYNTHETIC_MODEL:
                        continue
                    text = block_text(message.get("content"))
                    text = REMINDER_SPAN.sub("", text or "").strip()
                    # A slash command, its output and the CLI's other machinery are
                    # not messages: counting them made a conversation holding only a
                    # /rename look like one worth listing, and previewing them showed
                    # "<command-name>/rename</command-name>" as the first thing said.
                    if not text or (
                        kind == "user" and (SLASH_COMMAND.match(text) or MACHINERY.match(text))
                    ):
                        continue
                    messages += 1
                    if kind == "user" and first_prompt is None:
                        first_prompt = text
        stat = path.stat()
    except OSError:
        return None

    return {
        "id": path.stem,
        "title": (title or ai_title or first_prompt or "Untitled")[:200],
        "custom": bool(title),
        "preview": (first_prompt or "")[:300],
        "messages": messages,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(
            timespec="seconds"
        ),
    }


def session_title(session: str) -> str | None:
    """The name of one conversation, for the header."""
    directory = sessions_dir()
    if not directory or not session:
        return None
    path = directory / f"{session}.jsonl"
    if not path.is_file():
        return None
    scanned = scan_session(path)
    return scanned["title"] if scanned and scanned["messages"] else None


def set_session_title(session: str, title: str) -> bool:
    """Name a conversation without spending a turn on it.

    The name lives in the transcript, as the `custom-title` record `/rename` writes — so
    writing that record is all renaming is, and `scan_session` above reads it back. Doing
    it here rather than by asking the CLI is what lets a conversation be named while its
    first turn is still running: a `/rename` turn would queue behind that turn and arrive
    hours later, and would spend a request from the plan's allowance saying something the
    file already holds.

    False when there is no transcript to name — a job that is not a chat turn keeps its
    conversation somewhere else entirely, and naming it would mean nothing.
    """
    directory = sessions_dir()
    path = directory / f"{safe_name(session)}.jsonl" if directory else None
    if path is None or not path.is_file():
        return False
    # Written as the CLI writes it: one line, and not ascii-escaped. The title arrives
    # already collapsed and capped — see `create_job`, which is where it is let in.
    record = {"type": "custom-title", "customTitle": title, "sessionId": session}
    with path.open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True


def forget_session(session: str) -> dict:
    """Delete a conversation: its transcript, which is the only place it lives.

    Refused while a turn of that conversation is in flight — the CLI is writing that very
    file, and deleting it under the process would lose the reply and leave a job pointing
    at nothing.
    """
    if not SESSION_ID.fullmatch(session):
        raise ApiError(400, f"not a session id: {session}")
    for job in list_jobs():
        if job.get("status") in TERMINAL_STATUSES:
            continue
        # Which conversation a turn belongs to is only written into its record once it
        # starts, so a chat turn in flight also counts when the console is looking at this
        # conversation: that is the transcript the CLI has open.
        if session in {job.get("session_id"), job.get("resumed_from")} or (
            job.get("chat") and session == current_session()
        ):
            raise ApiError(409, "a turn of this conversation is still running")

    directory = sessions_dir()
    path = directory / f"{session}.jsonl" if directory else None
    if path is None or not path.is_file():
        raise ApiError(404, f"no such conversation: {session}")
    path.unlink()
    # The console was looking at it; leave it looking at a new conversation rather than at
    # a conversation that is not there.
    if current_session() == session:
        set_current_session(None)
    print(f"[api] conversation {session} deleted", flush=True)
    return {"deleted": session}


def list_sessions() -> list:
    """Every conversation in this directory, newest first, with Claude's own title."""
    directory = sessions_dir()
    if not directory:
        return []

    sessions = []
    for path in directory.glob("*.jsonl"):
        if not SESSION_ID.fullmatch(path.stem):
            continue
        scanned = scan_session(path)
        if scanned and scanned["messages"]:
            sessions.append(scanned)
    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return sessions


def prompt_size(usage) -> int:
    """The size of one request's prompt: everything the model was sent to read."""
    if not isinstance(usage, dict):
        return 0
    return sum(
        int(usage.get(key) or 0)
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )


def last_prompt_size(bookkeeping: Path) -> int:
    """How big the conversation had grown by the last thing the CLI said.

    The turn's own totals cannot answer this: they add up every request it made, so a
    turn that asked twenty times reports twenty prompts' worth and sails past the
    window — 1.1M of 1M, as the console showed. What fills the window is the *last*
    prompt, which is the last assistant record's usage. Subagents are skipped: they
    read their own context, not this conversation's.
    """
    path = bookkeeping / "stream.jsonl"
    if not path.is_file():
        return 0

    size = 0
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # half-written while the turn runs
                if record.get("type") != "assistant" or record.get("parent_tool_use_id"):
                    continue
                found = prompt_size((record.get("message") or {}).get("usage"))
                if found:
                    size = found
    except OSError:
        return 0
    return size


def context_of(payload: dict, bookkeeping: Path | None = None) -> dict | None:
    """How much of the context window the conversation fills.

    The limit comes from the CLI's own report — `modelUsage[...].contextWindow` — so
    nothing is hardcoded per model, and the smaller helper models it uses for side
    tasks are ignored. The size comes from the last request rather than from the
    turn's totals; see last_prompt_size.
    """
    usage = payload.get("usage")
    models = payload.get("modelUsage")
    if not isinstance(usage, dict) or not isinstance(models, dict):
        return None

    used = (last_prompt_size(bookkeeping) if bookkeeping else 0) or prompt_size(usage)
    main = max(
        (m for m in models.values() if isinstance(m, dict)),
        key=lambda m: int(m.get("cacheReadInputTokens") or 0) + int(m.get("inputTokens") or 0),
        default=None,
    )
    window = int((main or {}).get("contextWindow") or 0)
    if not used or not window:
        return None
    return {
        "used": used,
        "window": window,
        "left_percent": round(max(0.0, 100.0 * (1 - used / window)), 1),
    }


def _build_env() -> dict:
    env = dict(os.environ)
    env["HOME"] = str(HOME)
    env["PATH"] = f"{LOCAL_BIN}:{env.get('PATH', '/usr/bin:/bin')}"
    # The Supervisor injects this into every add-on. There is no reason for an
    # LLM-driven Bash tool to be able to call the Supervisor as this add-on.
    env.pop("SUPERVISOR_TOKEN", None)
    return env


# Built once: the environment does not change after start, and this is read on
# every health poll.
CLAUDE_ENV = _build_env()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def safe_name(name: str) -> str:
    """Reject anything that could escape the directory it is joined onto."""
    if not SAFE_NAME.fullmatch(name):
        raise ApiError(400, f"unsafe name: {name!r}")
    return name


def safe_subpath(base: Path, relative: str) -> Path:
    """Resolve `relative` inside `base`, refusing to leave it.

    Compares resolved paths, so a symlink planted inside the tree cannot point
    out of it either.
    """
    base = base.resolve()
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise ApiError(400, f"path escapes the job directory: {relative!r}")
    return target


def header_safe(value: str) -> str:
    """Strip anything that could start a new header line.

    http.server writes header values verbatim, so a filename carrying CR/LF
    injects headers into the response — on the Home Assistant origin.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value)[:100]
    return cleaned or "download"


# --------------------------------------------------------------------------- #
# one CLI at a time
# --------------------------------------------------------------------------- #

# Held by whoever runs the `claude` binary — the job worker or an update. One lock
# for one resource: guarding jobs with a flag and updates with a lock left both
# directions unprotected, since a queued job starts after the flag was checked.
CLI_LOCK = threading.Lock()

JOB_QUEUE: "queue.Queue[str]" = queue.Queue()
JOB_LOCK = threading.Lock()

# Purely for display, and for refusing to delete a job that is still running.
RUNNING_JOB = None
# The process of the turn in flight, so it can be stopped, and the ids of turns that
# were stopped, so the outcome reads as stopped rather than as a crash.
RUNNING_PROC: "subprocess.Popen | None" = None
CANCELLED: set = set()
# When the turn in flight was frozen, if it was. A frozen run spends nothing and
# loses nothing: the process and its subagents are still there, holding everything
# they had worked out, and the timeout below stops counting while they are stopped.
PAUSED_AT: float | None = None


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #

def job_dir(job_id: str) -> Path:
    return JOBS_DIR / safe_name(job_id)


def read_job(job_id: str) -> dict:
    job = read_json(job_dir(job_id) / "job.json")
    if job is None:
        raise ApiError(404, f"no such job: {job_id}")
    return job


def write_job(job: dict) -> None:
    # safe_name on the way out too: the run's cwd *is* the job directory and the
    # CLI may write, so job.json is attacker-writable from inside a job.
    write_json(JOBS_DIR / safe_name(job["id"]) / "job.json", job)


def update_job(job_id: str, **fields) -> dict:
    """Change some fields of a job without trampling the rest.

    Two threads write a job record: the worker that runs it, and the guard that freezes
    it. Each used to read the whole thing, change its own part and write all of it back,
    so whichever wrote last silently undid the other — the guard's `paused_reason`
    vanished when the turn ended, and a guard write landing after the turn's final one
    brought a finished job back to `running`. Read-change-write belongs under the lock.
    """
    with JOB_LOCK:
        job = read_job(job_id)
        for key, value in fields.items():
            if value is None:
                job.pop(key, None)
            else:
                job[key] = value
        write_job(job)
        return job


def list_jobs() -> list:
    """Newest first, skipping anything unreadable.

    A single directory without a parseable job.json used to 404 the whole list,
    and nothing healed it — including the reconciler.
    """
    jobs: list[dict] = []
    if not JOBS_DIR.is_dir():
        return jobs
    for path in JOBS_DIR.iterdir():
        if not path.is_dir() or not SAFE_NAME.fullmatch(path.name):
            continue
        job = read_json(path / "job.json")
        if isinstance(job, dict) and job.get("id"):
            jobs.append(job)
    # The id breaks a tie so the order is at least stable if two ever collide.
    jobs.sort(key=lambda j: (j.get("created_at") or "", j.get("id") or ""), reverse=True)
    return jobs


def prune_jobs() -> None:
    """Keep the newest few of each kind. Nothing else deletes a job, and /data is
    shared with Home Assistant's own storage.

    Chat turns are counted separately and kept far longer: they hold only text, and
    the conversation itself lives in Claude Code's transcript rather than here.
    """
    kept = {True: 0, False: 0}
    for job in list_jobs():
        chat = bool(job.get("chat"))
        kept[chat] += 1
        limit = CHAT_KEPT if chat else JOBS_KEPT
        if kept[chat] > limit and job.get("status") in TERMINAL_STATUSES:
            shutil.rmtree(JOBS_DIR / job["id"], ignore_errors=True)


def list_job_files(job_id: str) -> list:
    """Everything the run produced, excluding uploads and bookkeeping."""
    base = job_dir(job_id)
    files = []
    for path in sorted(base.rglob("*")):
        try:
            if not path.is_file():
                continue
            rel = path.relative_to(base)
            if rel.parts[0] == "in" or str(rel) in INTERNAL_FILES:
                continue
            files.append({"path": str(rel), "size": path.stat().st_size})
        except OSError:
            # The tree is live; a file can vanish between the walk and the stat.
            continue
    return files


def create_job(payload: dict) -> dict:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise ApiError(400, "'prompt' is required")

    model = str(payload.get("model") or DEFAULT_MODEL)
    if not SAFE_MODEL.fullmatch(model):
        raise ApiError(400, f"unsafe model name: {model!r}")

    effort = str(payload.get("effort") or DEFAULT_EFFORT)
    if effort and effort not in EFFORTS:
        raise ApiError(400, f"effort must be one of {', '.join(EFFORTS)}")

    mode = str(payload.get("permission_mode") or DEFAULT_PERMISSION_MODE)
    mode = PERMISSION_ALIASES.get(mode, mode)
    if mode not in PERMISSION_MODES:
        raise ApiError(400, f"permission_mode must be one of {', '.join(PERMISSION_MODES)}")

    chat = bool(payload.get("chat"))
    # Who sent it. The console leaves this alone; another caller — a script driving
    # through the API, say — names itself, and the console then knows that a turn
    # running in some other conversation is not one of its own.
    source = str(payload.get("source") or "console")
    if not SAFE_NAME.fullmatch(source):
        raise ApiError(400, f"unsafe source: {source!r}")

    command = payload.get("command")
    if command is not None and command not in CHAT_COMMANDS:
        raise ApiError(400, f"command must be one of {', '.join(CHAT_COMMANDS)}")

    # What to call the conversation this turn creates, applied as soon as the CLI says
    # which conversation that is. Every conversation a skill opens is otherwise listed
    # under the skill's own first lines, identical for all of them, so a caller that runs
    # one has nothing to tell its own conversations apart by. Collapsed to one line here,
    # since it is written into a JSONL record further on.
    title = re.sub(r"\s+", " ", str(payload.get("title") or "")).strip()[:120] or None

    # An explicit session lets the history list act on a conversation that is not
    # the current one. For an ordinary chat message the session is deliberately NOT
    # captured here: a message queued while Claude is still answering would freeze
    # a session id that does not exist yet and start a second conversation. It is
    # resolved when the turn actually runs.
    resume = str(payload.get("resume") or "") or None
    if resume and not SESSION_ID.fullmatch(resume):
        raise ApiError(400, "resume must be a session id")

    prune_jobs()

    job_id = uuid.uuid4().hex[:12]
    (JOBS_DIR / job_id / "in").mkdir(parents=True)

    # A prompt is written before the job exists, so a caller that needs to name the
    # job's own directory — "the uploads are in there, put the archive next to them" —
    # has nothing to name it with. These two stand in for it.
    prompt = prompt.replace("{job_dir}", str(JOBS_DIR / job_id)).replace("{job_id}", job_id)

    job = {
        "id": job_id,
        "status": "created",
        "prompt": prompt,
        "model": model,
        "effort": effort or None,
        "permission_mode": mode,
        "chat": chat,
        "source": source,
        "command": command,
        "title": title,
        "resumed_from": resume,
        "session_id": None,
        "context": None,
        "created_at": now(),
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "result": None,
        "error": None,
    }
    write_job(job)

    if payload.get("start"):
        try:
            return start_job(job_id)
        except ApiError:
            # The job existed only to be started. Left behind at `created` it would sit in
            # the console's queue for good — nothing prunes a job that never ran, and a
            # caller cannot delete one either.
            shutil.rmtree(job_dir(job_id), ignore_errors=True)
            raise
    return job


def start_job(job_id: str) -> dict:
    if GUARD_LIMITS:
        usage = read_usage()
        worst = usage.get("worst") if usage.get("available") else None
        # Refused rather than held: a caller that asked at a bad moment should hear so at
        # once and decide for itself. A reading that cannot be had lets the turn through.
        if worst and not usage.get("enough", True):
            back = when_for_people(worst.get("resets_at"))
            raise ApiError(
                429,
                f"the plan's {worst['kind']} allowance is {worst['percent']}% used, over "
                f"the {worst['threshold']}% this add-on is set to stop at"
                + (f"; it resets {back}" if back else ""),
            )
    # One lock across read-check-write: without it two concurrent starts both saw
    # "created" and the job was enqueued — and run — twice.
    with JOB_LOCK:
        job = read_job(job_id)
        if job["status"] != "created":
            raise ApiError(409, f"job is already {job['status']}")
        job["status"] = "queued"
        write_job(job)
    JOB_QUEUE.put(job_id)
    return job


def run_claude(
    workdir: Path,
    bookkeeping: Path,
    argv: list,
    prompt: str,
    on_session: Callable[[str], None] | None = None,
) -> int:
    """Run the CLI, streaming its output to a file as it arrives.

    `workdir` is the CLI's working directory, which is what scopes its session
    history; `bookkeeping` is where this add-on keeps its own files, so a shared
    chat directory does not collect one log per turn.

    `on_session` is called once, with the session id, the moment the CLI names the
    conversation it is working in — which is seconds in, and not at the end.

    stdout goes straight to `stream.jsonl` rather than through a pipe, so the file
    grows while the turn runs and the reply can be read as it is produced. No
    reader thread is needed for that; the kernel does the writing.

    stdin rather than argv because `-p` is a boolean flag and the prompt is a
    positional: a prompt beginning with `--` would be parsed as an option.

    start_new_session makes the child a process-group leader, so a timeout or a
    cancellation can take its subagents with it. subprocess.run only SIGKILLs the
    direct child, which left subagents running for the rest of the container's life.
    """
    global RUNNING_PROC, PAUSED_AT
    with open(bookkeeping / "claude.log", "wb") as stderr, open(
        bookkeeping / "stream.jsonl", "wb"
    ) as stream:
        # The credentials lock, held only across the spawn: the CLI renews the account's
        # token itself, and a renewal here at the same moment would spend the same refresh
        # token twice — which signs the account out rather than renewing it. Whoever takes
        # the lock first wins cleanly: a renewal already under way finishes and the CLI
        # starts on the token it wrote, or the CLI starts first and the renewal sees a
        # running turn and declines.
        with CREDENTIALS_LOCK:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                argv,
                cwd=workdir,
                env=CLAUDE_ENV,
                stdin=subprocess.PIPE,
                stdout=stream,
                stderr=stderr,
                start_new_session=True,
            )
            RUNNING_PROC = process
        stdin = process.stdin
        if stdin is not None:
            stdin.write(prompt.encode())
            stdin.close()
        try:
            # Waited for a second at a time rather than in one call, so that a frozen
            # run does not burn its own timeout while it is frozen.
            deadline = time.monotonic() + TIMEOUT_SEC
            while True:
                try:
                    return process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                # Asked for every second until it can be answered, which is the first
                # line the CLI writes; after that there is nothing left to look for.
                if on_session is not None:
                    session = stream_session(bookkeeping)
                    if session:
                        on_session(session)
                        on_session = None
                if PAUSED_AT is not None:
                    deadline += 1
                if time.monotonic() >= deadline:
                    kill_tree(process, signal.SIGKILL)
                    raise subprocess.TimeoutExpired(argv, TIMEOUT_SEC)
        finally:
            RUNNING_PROC = None
            PAUSED_AT = None


def kill_tree(process: subprocess.Popen, sig: int) -> None:
    """Signal the whole process group; the CLI's subagents are in it."""
    try:
        os.killpg(process.pid, sig)
    except OSError:
        # Nothing left to signal is the outcome we wanted anyway.
        with contextlib.suppress(OSError):
            process.send_signal(sig)


def stream_text(bookkeeping: Path) -> str:
    """The assistant text produced so far, one message to a line.

    A long turn says several things — a run of two hours reported its progress every
    couple of minutes — and each of those is a message of its own. A finished one arrives
    as an `assistant` record; the one still being produced exists only as deltas, which is
    what makes a reply appear word by word. Both are kept, in order, and the line break
    between them is the point: glued into one 2400-character run of text, "the last thing
    said" could not be told from the first, and a caller showing progress had nothing to
    show. Subagents are left out — their words are their own conversation.
    """
    path = bookkeeping / "stream.jsonl"
    if not path.is_file():
        return ""

    said: list[str] = []
    streaming: list[str] = []
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    # The last line may be half-written while the turn runs.
                    continue
                kind = record.get("type")
                if kind == "stream_event":
                    delta = (record.get("event") or {}).get("delta") or {}
                    if delta.get("type") == "text_delta":
                        streaming.append(delta.get("text") or "")
                elif kind == "assistant" and not record.get("parent_tool_use_id"):
                    # The record carries the whole message the deltas were building, so
                    # they have done their work and the next ones belong to the next one.
                    said.append(block_text((record.get("message") or {}).get("content")))
                    streaming = []
    except OSError:
        return ""

    parts = [part.strip() for part in [*said, "".join(streaming)]]
    return "\n".join(part for part in parts if part)[:MAX_RESULT_CHARS]


# How many of the last tool calls a caller is told about. Enough to see movement,
# not so many that a poll carries a log.
ACTIVITY_KEPT = 6


def stream_activity(bookkeeping: Path) -> list:
    """What the run has been doing lately, from the tool calls it made.

    A long run can go minutes without producing a word — reading files, running
    something — and a caller with nothing to show would look stuck. This is what it
    shows instead: the tools, in order, with the thing each one was pointed at.
    """
    path = bookkeeping / "stream.jsonl"
    if not path.is_file():
        return []

    activity: list = []
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or '"tool_use"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") != "assistant":
                    continue
                content = (record.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    activity.append(
                        {
                            "tool": str(block.get("name") or "tool"),
                            "target": tool_target(block.get("input")),
                        }
                    )
    except OSError:
        return []
    return activity[-ACTIVITY_KEPT:]


def tool_target(arguments) -> str:
    """The one thing worth reading out of a tool call: a file, or a command."""
    if not isinstance(arguments, dict):
        return ""
    for key in ("file_path", "path", "notebook_path", "pattern", "url"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value.rsplit("/", 1)[-1][:80]
    command = arguments.get("command")
    if isinstance(command, str) and command:
        return command.strip().splitlines()[0][:80]
    prompt = arguments.get("prompt") or arguments.get("description")
    return str(prompt)[:80] if prompt else ""


def stream_result(bookkeeping: Path) -> dict | None:
    """The final `result` record, which carries the outcome, session and usage."""
    path = bookkeeping / "stream.jsonl"
    if not path.is_file():
        return None
    found = None
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") == "result":
                    found = record
    except OSError:
        return None
    return found


def stream_session(bookkeeping: Path) -> str | None:
    """Which conversation the turn is in, as soon as the CLI has said which.

    Every record the CLI streams carries the session id, starting with the `init` one it
    writes before doing any work — so the answer is in the first line of the file, seconds
    after the turn starts. Taken from the final `result` record instead, as the outcome is,
    it arrives only when the turn ends: a turn that runs for hours could not be named, and
    one that died without a result record — a timeout, a restart — left nothing to carry
    the conversation on with.
    """
    path = bookkeeping / "stream.jsonl"
    if not path.is_file():
        return None
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # the last line may be half-written while the turn runs
                session = record.get("session_id")
                if isinstance(session, str) and SESSION_ID.fullmatch(session):
                    return session
    except OSError:
        return None
    return None


def run_job(job_id: str) -> None:
    bookkeeping = JOBS_DIR / job_id
    job = read_job(job_id)
    # Chat turns all share one directory so that --resume finds the conversation;
    # a one-off job gets its own, which is also where its outputs land.
    workdir = CHAT_DIR if job.get("chat") else bookkeeping
    job["status"] = "running"
    job["started_at"] = now()
    write_job(job)

    argv = [
        "claude",
        "-p",
        "--model",
        job["model"],
        "--permission-mode",
        job.get("permission_mode") or DEFAULT_PERMISSION_MODE,
        # Skills run their own scripts, so Bash has to be allowed. This is a
        # convenience for the agent, not a security boundary — see the module
        # docstring.
        "--allowedTools",
        "Bash,Read,Write,Edit,Glob,Grep",
        # Streaming so the reply can be shown as it is produced. --verbose is
        # required alongside --include-partial-messages, and the final `result`
        # record carries everything the single-shot json format did.
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
    ]
    if job.get("effort"):
        argv += ["--effort", job["effort"]]
    # Resolved now rather than at queue time, so a message that waited its turn
    # continues the conversation the previous turn created.
    #
    # Only for the console's own turns: another caller that did not name a
    # conversation gets a new one rather than joining whatever this window happens to
    # be showing. Without that, a bot's first message landed in the middle of
    # somebody's chat.
    own = job.get("source", "console") == "console"
    resume = job.get("resumed_from") or (
        current_session() if job.get("chat") and own else None
    )
    if resume:
        job["resumed_from"] = resume
        # Without --fork-session this keeps the same session id, so a conversation
        # stays one conversation however many turns it runs to.
        argv += ["--resume", resume]

    named = False

    def publish(session: str) -> None:
        """Write down which conversation this turn is in, and name it if it is new.

        Called from inside the run, as soon as the CLI says which conversation that is:
        everything that has to survive the turn — a caller carrying the work on after a
        restart, the name the conversation is listed under — needed this hours before the
        turn ends. Written both to the record in hand, which is what the end of this
        function saves, and through the lock, which is what a caller polling the job
        reads.

        Safe to call again, and called again when the turn ends: the id is written once,
        and so is the name — but the name can only be written once there is a transcript
        to write it into, and the CLI names the conversation a moment before it opens one.
        """
        nonlocal named
        if job.get("session_id") != session:
            job["session_id"] = session
            update_job(job_id, session_id=session)
        # Only a conversation this turn opened. One that was resumed has a name already —
        # its own, or one its caller gave it when it was new.
        if job.get("title") and not job.get("resumed_from") and not named:
            named = set_session_title(session, job["title"])

    try:
        returncode = run_claude(workdir, bookkeeping, argv, job["prompt"], publish)
        job["exit_code"] = returncode

        # The CLI reports failures in its output rather than on stderr: a run with
        # no credentials exits 1 with an empty stderr and a result record carrying
        # {"is_error": true, "result": "Not logged in · Please run /login"}.
        payload = stream_result(bookkeeping)
        message = payload.get("result") if isinstance(payload, dict) else None
        if not message:
            message = stream_text(bookkeeping) or None

        cancelled = job_id in CANCELLED
        failed = cancelled or returncode != 0 or (
            isinstance(payload, dict) and payload.get("is_error")
        )

        if cancelled:
            job["status"] = "failed"
            job["error"] = "stopped"
            # Whatever had been said before the stop is worth keeping.
            job["result"] = (stream_text(bookkeeping) or "")[:MAX_RESULT_CHARS] or None
        elif failed:
            job["status"] = "failed"
            stderr_text = (bookkeeping / "claude.log").read_text(errors="replace").strip()
            job["error"] = (message or stderr_text or "the CLI failed without a message")[
                -MAX_ERROR_CHARS:
            ]
        else:
            job["status"] = "done"
            job["result"] = (message or "")[:MAX_RESULT_CHARS] or None

        if isinstance(payload, dict):
            session = payload.get("session_id")
            if isinstance(session, str) and SESSION_ID.fullmatch(session):
                # Ordinarily this happened while the turn ran. A turn short enough to
                # finish inside the first second is written down here instead, and so is
                # one whose transcript did not exist yet when it was first tried.
                publish(session)
                # A chat turn defines which conversation the next one continues,
                # including the first turn, which had nothing to resume.
                #
                # Unless the conversation was left while the turn was in flight:
                # pressing New chat, or opening another conversation, used to be
                # undone the moment the running turn finished and pointed the chat
                # back at itself. Comparing against what this turn was continuing
                # is what tells the two apart.
                # And only the console's own turns move the console on: a bot
                # holding its own conversation through the API does not decide what
                # this window is looking at.
                own = job.get("source", "console") == "console"
                if job.get("chat") and not failed and own and (
                    current_session() == job.get("resumed_from")
                ):
                    set_current_session(session)
            job["context"] = context_of(payload, bookkeeping)
    except subprocess.TimeoutExpired:
        job["status"] = "failed"
        job["error"] = f"timed out after {TIMEOUT_SEC}s"
        job["result"] = (stream_text(bookkeeping) or "")[:MAX_RESULT_CHARS] or None
    except Exception as exc:  # noqa: BLE001 - reported to the caller instead
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"

    CANCELLED.discard(job_id)
    job["finished_at"] = now()
    # Whatever the guard wrote while this ran belonged to a turn that is now over.
    for key in ("paused_at", "paused_reason", "resumes_at", "limit_override"):
        job.pop(key, None)
    # Under the lock, and last: the guard may have written to this record while the turn
    # ran, and whichever of the two writes second must not undo the other's decision.
    with JOB_LOCK:
        write_job(job)
    print(f"[api] job {job_id} {job['status']}", flush=True)


def worker() -> None:
    global RUNNING_JOB
    while True:
        job_id = JOB_QUEUE.get()
        # It may have been stopped, or deleted, while it waited.
        state = read_json(JOBS_DIR / job_id / "job.json") or {}
        if state.get("status") != "queued":
            JOB_QUEUE.task_done()
            continue
        # Blocks while an update is installing, which is the point: the binary
        # must not be replaced under a run, nor a run started mid-install.
        with CLI_LOCK:
            RUNNING_JOB = job_id
            try:
                run_job(job_id)
            except Exception as exc:  # noqa: BLE001 - a bad job must not kill the worker
                print(f"[api] job {job_id} crashed: {exc}", flush=True)
            finally:
                RUNNING_JOB = None
                JOB_QUEUE.task_done()


# The moment between a job being marked "running" and its CLI existing: the worker writes
# the status, then opens its files and spawns. Usually milliseconds — but the spawn waits on
# the credentials lock, so a renewal in flight can stretch it. A caller that reads the status
# and acts on it at once was told the turn could not be frozen, and the tests hit it about
# one run in two, always in a different test.
SPAWN_GRACE_SEC = 5.0


def process_of_the_running_turn() -> "subprocess.Popen | None":
    """The CLI of the turn in flight, waiting out the moment before it exists."""
    deadline = time.monotonic() + SPAWN_GRACE_SEC
    while RUNNING_PROC is None and RUNNING_JOB is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    return RUNNING_PROC


def freeze_job(
    job_id: str, freeze: bool, *, by_guard: bool = False, reason: str | None = None,
    until: str | None = None,
) -> dict:
    """Stop the turn where it stands, or let it carry on.

    A frozen turn is not a stopped turn: nothing is lost and nothing more is spent,
    which is the point — a run that has already paid for its thinking should not have
    to pay again because a limit is about to be reached. The whole process group is
    signalled, so the subagents freeze with it.
    """
    global PAUSED_AT
    job = read_job(job_id)
    process = process_of_the_running_turn()
    if job_id != RUNNING_JOB or process is None:
        raise ApiError(409, f"job is {job.get('status')}; only a running turn can be frozen")

    if freeze:
        if PAUSED_AT is not None:
            return {**job, "paused": True, "changed": False}
        if process.poll() is not None:
            # It finished between the decision and here; there is nothing to stop, and
            # saying otherwise would leave a pause on a turn that is already over.
            raise ApiError(409, "the turn finished; there is nothing to freeze")
        kill_tree(process, signal.SIGSTOP)
        PAUSED_AT = time.monotonic()
        job = update_job(job_id, paused_at=now(), paused_reason=reason, resumes_at=until)
        print(f"[api] job {job_id} frozen", flush=True)
        return {**job, "paused": True, "changed": True}

    if PAUSED_AT is None:
        return {**job, "paused": False, "changed": False}
    kill_tree(process, signal.SIGCONT)
    PAUSED_AT = None
    # Let go by hand against the guard's advice: it does not get to overrule that.
    by_hand = not by_guard and job.get("paused_reason") == "limits"
    job = update_job(
        job_id,
        paused_at=None,
        resumes_at=None,
        paused_reason=None,
        limit_override=True if by_hand else None,
    )
    print(f"[api] job {job_id} carrying on", flush=True)
    return {**job, "paused": False, "changed": True}


# How the allowance is watched while a turn runs. Rarely when there is room, closely when
# the wall is near: a reading costs a request to Anthropic, and asking every minute for
# half an hour is both wasteful and rude. Far from the threshold the answer cannot change
# fast enough to matter; near it, it can.
WATCH_STEPS = ((20, 15 * 60), (5, 5 * 60), (0, 2 * 60))
WATCH_IDLE_SEC = 30
# After a window resets, a moment's grace before trusting it.
THAW_GRACE_SEC = 5 * 60

# The sign-in is kept alive rather than only repaired. A token lives about eight hours, and
# one with less than this left is renewed — so a renewal happens every six hours or so
# whether or not anybody is looking, the sign-in never dies of silence, and the first turn
# after a quiet week does not wait on a renewal it could have had for nothing. The margin is
# generous on purpose: it leaves hours of chances to get it done if the network is out.
TOKEN_KEEP_SEC = 2 * 3600
# A renewal that failed is not tried again at once: a refresh token that really is spent
# would otherwise have this asking every few minutes for the rest of the day.
KEEP_BACKOFF_SEC = 30 * 60
# `looked` is the last time the token's own clock was consulted, and `until` how long a
# failed renewal is left alone. The watcher ticks every half minute so it can notice a turn
# starting and ending — nothing to do with Anthropic — but nothing hung on that tick may run
# at that pace: `usage_check_seconds` is the one setting that governs how often this add-on
# touches anything of theirs, and it governs this too.
TOKEN_KEEP: dict[str, float] = {"until": 0.0, "looked": float("-inf")}


def watch_interval(worst: dict | None) -> int:
    """How long to wait before reading the allowance again.

    Measured in room left to the window's own figure, not in per cent used: with the two
    windows set apart, 70% means "plenty" for one and "about to stop" for the other.

    Never sooner than the reading is allowed to be taken: the closest step of the ladder is
    a floor on attention, not a licence to ask more often than the setting says.
    """
    if not worst:
        return max(WATCH_STEPS[0][1], USAGE_TTL_SEC)
    room = worst["threshold"] - worst["percent"]
    for edge, wait in WATCH_STEPS:
        if room > edge:
            return max(wait, USAGE_TTL_SEC)
    return max(WATCH_STEPS[-1][1], USAGE_TTL_SEC)


# A pause is worth it for a window that comes back within the working day; a weekly
# window that resets in six days is not something to hold a frozen process — and the whole
# add-on with it — through. Past this, the turn is left to run into the wall, where the
# conversation survives and can be carried on later.
MAX_HOLD_SEC = 6 * 60 * 60


def thaw_after_limits(job_id: str, why: str) -> dict:
    thawed = freeze_job(job_id, False, by_guard=True)
    print(f"[api] job {job_id} carrying on: {why}", flush=True)
    return thawed


def due_back(job: dict) -> bool:
    """Whether the window a frozen turn is waiting for should be back by now."""
    when = parse_when(job.get("resumes_at"))
    if when is None:
        return False
    return datetime.now(UTC) >= when + timedelta(seconds=THAW_GRACE_SEC)


def hold_for_limits(job_id: str) -> dict | None:
    """Freeze a running turn if the allowance has run out, or let it go once it is back.

    The add-on does this itself rather than leaving it to whoever drives it: it owns the
    process, so it is the only one that can stop the CLI and its subagents the moment the
    wall appears — and it happens whether or not anybody is watching, which is exactly
    what went wrong when nobody was.
    """
    usage = read_usage()
    worst = usage.get("worst") if usage.get("available") else None
    job = read_job(job_id)
    if job.get("limit_override"):
        return None

    frozen_for_limits = PAUSED_AT is not None and job.get("paused_reason") == "limits"
    # A reading that cannot be had is not a reason to stop working — and it is not a reason
    # to keep a turn stopped either. Failing open on the way in and closed on the way out
    # would leave a frozen turn frozen for good the moment the endpoint went quiet.
    if not worst:
        if frozen_for_limits and due_back(job):
            return thaw_after_limits(job_id, "the window was due back and cannot be read")
        return None

    if not usage.get("enough", True):
        if PAUSED_AT is not None:
            return None
        when = parse_when(worst.get("resets_at"))
        if when and when - datetime.now(UTC) > timedelta(seconds=MAX_HOLD_SEC):
            print(
                f"[api] job {job_id} left to run on: the {worst['kind']} window is at "
                f"{worst['percent']}% but does not reset until {worst.get('resets_at')}",
                flush=True,
            )
            return None
        frozen = freeze_job(
            job_id, True, by_guard=True, reason="limits", until=worst.get("resets_at")
        )
        print(
            f"[api] job {job_id} frozen: {worst['kind']} window at {worst['percent']}%"
            f" of {worst['threshold']}%, back at {worst.get('resets_at')}",
            flush=True,
        )
        return frozen

    if frozen_for_limits:
        return thaw_after_limits(job_id, f"{worst['percent']}% used")
    return None


def keep_the_sign_in_warm() -> None:
    """Renew the account's token before it runs out, rather than after.

    Nothing else does it while the add-on sits idle: the CLI renews its own only while it is
    running, and a reading is only taken when somebody is looking. So a token would quietly
    die every night, and — since a refresh token has a life of its own — a long enough
    silence would end with a sign-in that has to be done by hand again. This costs a file
    and a clock reading on each tick, and a request once every few hours.
    """
    ticks = time.monotonic()
    if RUNNING_PROC is not None or ticks < TOKEN_KEEP["until"]:
        return
    # No faster than the one setting that governs how often anything of Anthropic's is
    # touched, even though a look is only a file and a clock. Two hours of margin against a
    # three-minute pace is forty chances an hour; nothing is lost by keeping to it.
    if ticks - TOKEN_KEEP["looked"] < USAGE_TTL_SEC:
        return
    TOKEN_KEEP["looked"] = ticks
    expires_at = (credentials().get("claudeAiOauth") or {}).get("expiresAt")
    if not isinstance(expires_at, int | float) or isinstance(expires_at, bool):
        return
    if expires_at / 1000 - time.time() > TOKEN_KEEP_SEC:
        return
    if renew_access_token():
        TOKEN_KEEP["until"] = 0.0
        print("[api] the sign-in was renewed before it ran out", flush=True)
    else:
        TOKEN_KEEP["until"] = ticks + KEEP_BACKOFF_SEC
        print("[api] the sign-in could not be renewed; signing in again may be needed",
              flush=True)


def limit_watch_once() -> float:
    """One look at the allowance, and how long to wait before the next one."""
    try:
        keep_the_sign_in_warm()
    except Exception as exc:  # noqa: BLE001 - the watcher must outlive any one renewal
        print(f"[api] keeping the sign-in: {type(exc).__name__}: {exc}", flush=True)
    job_id = RUNNING_JOB
    if not GUARD_LIMITS or job_id is None:
        return WATCH_IDLE_SEC
    try:
        hold_for_limits(job_id)
        worst = read_usage().get("worst")
    except Exception as exc:  # noqa: BLE001 - the watcher must outlive any one reading
        print(f"[api] limit watch: {type(exc).__name__}: {exc}", flush=True)
        worst = None
    # While frozen, look again once the window is due back rather than on the ladder.
    return THAW_GRACE_SEC if PAUSED_AT is not None else watch_interval(worst)


def limit_watch() -> None:
    """The loop behind limit_watch_once, on its own thread."""
    while True:
        wait = limit_watch_once()
        watching = RUNNING_JOB
        slept = 0.0
        # Broken up so a turn ending is noticed at once rather than a quarter of an hour
        # later, and so a fresh turn is looked at when it starts rather than inherited.
        while slept < wait and watching == RUNNING_JOB:
            time.sleep(min(WATCH_IDLE_SEC, wait - slept))
            slept += WATCH_IDLE_SEC


def cancel_job(job_id: str) -> dict:
    """Stop a turn, whether it is running or still waiting its turn."""
    job = read_job(job_id)
    if job.get("status") in TERMINAL_STATUSES:
        raise ApiError(409, f"job is already {job['status']}")

    if job_id != RUNNING_JOB:
        # Still queued: mark it terminal and let the worker skip it. The queue
        # itself cannot have an entry plucked out of the middle.
        with JOB_LOCK:
            job = read_job(job_id)
            job["status"] = "failed"
            job["error"] = "stopped before it started"
            job["finished_at"] = now()
            write_job(job)
        print(f"[api] job {job_id} dropped from the queue", flush=True)
        return job

    CANCELLED.add(job_id)
    # The same wait as a freeze: a turn cancelled in the moment before its CLI exists used
    # to be left running, with nothing to signal.
    process = process_of_the_running_turn()
    if process is None:
        return read_job(job_id)

    # A frozen process would not act on the stop until it was let go, so it is let go
    # first and then asked to end.
    global PAUSED_AT
    if PAUSED_AT is not None:
        kill_tree(process, signal.SIGCONT)
        PAUSED_AT = None

    # SIGTERM is what the CLI documents for an abort: it ends the turn, stops any
    # Bash it started, and runs SessionEnd hooks.
    kill_tree(process, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        kill_tree(process, signal.SIGKILL)
    print(f"[api] job {job_id} stopped", flush=True)

    # The worker writes the outcome a moment later; without this wait the caller
    # would be told the job is still running, having just stopped it.
    for _ in range(30):
        job = read_job(job_id)
        if job.get("status") in TERMINAL_STATUSES:
            return job
        time.sleep(0.1)
    return read_job(job_id)


def reconcile_interrupted_jobs() -> None:
    """A restart mid-run leaves 'running' behind; that job is not coming back."""
    for job in list_jobs():
        if job.get("status") in {"running", "queued"}:
            job["status"] = "failed"
            job["error"] = "the add-on restarted while this job was in flight"
            for key in ("paused_at", "paused_reason", "resumes_at", "limit_override"):
                job.pop(key, None)
            job["finished_at"] = now()
            try:
                write_job(job)
            except (OSError, ApiError):
                continue


# --------------------------------------------------------------------------- #
# updating the CLI
# --------------------------------------------------------------------------- #

USAGE_CACHE: dict[str, Any] = {"checked_at": float("-inf"), "value": None}
AVAILABLE: dict[str, str | None] = {"version": None}
# One entry is a version and the other a clock reading, so the values are of no
# one type; naming that beats pretending otherwise.
INSTALLED_CACHE: dict[str, Any] = {"version": None, "checked_at": float("-inf")}


def installed_version(force: bool = False) -> str | None:
    """The version on PATH, which is the installed update when there is one.

    Cached briefly: the web UI polls health often, and spawning the binary each
    time for a string that only changes on update is waste. `checked_at` starts at
    -inf rather than 0 because time.monotonic() is host uptime, so on a freshly
    booted machine 0 was already "recent" and the first probe never ran.
    """
    if not force and time.monotonic() - INSTALLED_CACHE["checked_at"] < INSTALLED_TTL_SEC:
        return INSTALLED_CACHE["version"]
    try:
        # `claude` rather than an absolute path throughout: resolving it on PATH is how
        # an installed update, which lands in ~/.local/bin, takes precedence over the
        # copy baked into the image. That is the mechanism, not an oversight.
        output = subprocess.run(
            ["claude", "--version"],  # noqa: S607 - PATH resolution is deliberate
            capture_output=True,
            text=True,
            timeout=30,
            env=CLAUDE_ENV,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        output = ""
    # "2.1.228 (Claude Code)" -> "2.1.228"
    version = output.split()[0] if output else None
    # Only cache a real answer: caching None right after an install would report
    # the CLI as missing for a minute.
    if version or not INSTALLED_CACHE["version"]:
        INSTALLED_CACHE.update(version=version, checked_at=time.monotonic())
    return version


def usage_window(payload, key: str) -> dict | None:
    """One window of the plan's allowance, as a percentage and a reset time."""
    window = payload.get(key)
    if not isinstance(window, dict):
        return None
    percent = window.get("utilization")
    if percent is None:
        return None
    return {"percent": round(float(percent), 1), "resets_at": window.get("resets_at")}


# Asked too often, the endpoint says so — 429 — and asking again immediately is how a
# refusal becomes a ban. So a 429 is taken at its word: nothing is asked for a good while,
# and a refresh by hand cannot shorten that, because the button being pressed is exactly
# how the wall was hit in the first place.
BACKOFF_SEC = 15 * 60
USAGE_BACKOFF: dict[str, float] = {"until": 0.0}


def write_credentials(stored: dict) -> None:
    """Written the way the CLI keeps it: whole, atomic, and readable by nobody else.

    `write_json` would do the first two, but a fresh file takes the umask's mode, and a
    world-readable copy of the account's tokens is not a detail to leave to chance.
    """
    tmp = CREDENTIALS_PATH.with_name(CREDENTIALS_PATH.name + ".tmp")
    handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as file:
        os.fchmod(file.fileno(), 0o600)
        json.dump(stored, file, indent=2)
    os.replace(tmp, CREDENTIALS_PATH)


def credentials() -> dict:
    """What the CLI has written where it keeps the sign-in. Any other shape is no sign-in."""
    stored = read_json(CREDENTIALS_PATH, {})
    return stored if isinstance(stored, dict) else {}


def renew_access_token() -> str | None:
    """Trade the refresh token for a fresh access token, and write it back.

    Best-effort like everything around it: a file with no refresh token in it, an endpoint
    that has moved on, a refusal — all answer None, and the caller reports the allowance as
    unreadable rather than failing.
    """
    with CREDENTIALS_LOCK:
        # Under the lock, because a turn takes the same one across its spawn: this is what
        # makes "never while the CLI is running" true rather than merely likely.
        if RUNNING_PROC is not None:
            return None
        stored = credentials()
        oauth = stored.get("claudeAiOauth")
        if not isinstance(oauth, dict) or not isinstance(oauth.get("refreshToken"), str):
            return None
        body: dict[str, Any] = {
            "grant_type": "refresh_token",
            "refresh_token": oauth["refreshToken"],
            "client_id": str(oauth.get("clientId") or OAUTH_CLIENT_ID),
        }
        scopes = oauth.get("scopes")
        if isinstance(scopes, list) and scopes:
            body["scope"] = " ".join(str(scope) for scope in scopes)
        request = urllib.request.Request(
            TOKEN_URL,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "claude-code-addon"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                answer = json.loads(response.read())
            token = answer["access_token"]
            lifetime = int(answer.get("expires_in") or 0)
        except (urllib.error.URLError, OSError, ValueError, TypeError, KeyError, IndexError):
            return None
        if not isinstance(token, str) or not token:
            return None
        renewed = {
            **oauth,
            "accessToken": token,
            # A renewal may hand back a new refresh token, and when it does the old one is
            # spent — keeping it would make the next renewal the last.
            "refreshToken": answer.get("refresh_token") or oauth["refreshToken"],
        }
        if lifetime:
            renewed["expiresAt"] = int(time.time() * 1000) + lifetime * 1000
        else:
            # A renewal that does not say how long the token lasts leaves nothing to judge
            # it by, and keeping the old expiry would have this renewing on every reading.
            renewed.pop("expiresAt", None)
        if isinstance(answer.get("scope"), str) and answer["scope"]:
            renewed["scopes"] = answer["scope"].split()
        # The rest of the file belongs to somebody else — every MCP server keeps its own
        # tokens in here — so it goes back untouched. And a token that could not be saved is
        # still a good token: the next reading simply renews again.
        with contextlib.suppress(OSError):
            write_credentials({**stored, "claudeAiOauth": renewed})
        return token


def signed_in_token() -> tuple[str | None, str]:
    """The CLI's access token, renewed first if it is spent — and why not, when there is none.

    The reason travels with the answer so a token known to be dead is never spent on a
    request that can only come back 401.
    """
    oauth = credentials().get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None, "not signed in"
    token = oauth.get("accessToken")
    token = token if isinstance(token, str) and token else None
    expires_at = oauth.get("expiresAt")
    # No expiry recorded means nothing can be said about the token, so it is used as it is
    # and a 401 settles the question.
    spent = (
        isinstance(expires_at, int | float)
        and not isinstance(expires_at, bool)
        and expires_at / 1000 - time.time() < TOKEN_MARGIN_SEC
    )
    # While the CLI runs it renews its own token and writes the file this reads, so the
    # renewal is left to it and the token in hand is used as it stands.
    if spent and RUNNING_PROC is None:
        return (renew_access_token(), "the sign-in has expired")
    if not token:
        return None, "not signed in"
    return token, ""


def ask_for_usage(token: str) -> dict:
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code-addon",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - constant https url
        return json.loads(response.read())


def read_usage(force: bool = False) -> dict:
    """How much of the plan's allowance is gone.

    Best-effort by design: a missing sign-in, an expired token, a changed endpoint or one
    refusing to answer this often all report `available: false`, because the point of this
    is to let a caller hold back before a long run — not to become a new way for runs to
    fail.
    """
    waiting = USAGE_BACKOFF["until"] - time.monotonic()
    fresh = time.monotonic() - USAGE_CACHE["checked_at"] < USAGE_TTL_SEC
    if (waiting > 0 or (not force and fresh)) and USAGE_CACHE["value"] is not None:
        return USAGE_CACHE["value"]

    token, no_token = signed_in_token()
    answer: dict[str, Any] = {
        "available": False,
        "checked_at": now(),
        "thresholds": {"session": SESSION_THRESHOLD, "week": WEEK_THRESHOLD},
        "acting": GUARD_LIMITS,
        "check_every": USAGE_TTL_SEC,
        "timezone": addon_timezone(),
    }
    if not token:
        answer["reason"] = no_token
        USAGE_CACHE.update(checked_at=time.monotonic(), value=answer)
        return answer

    try:
        try:
            payload = ask_for_usage(token)
        except urllib.error.HTTPError as refused:
            # 401 here means one thing: the token is spent. The CLI answers it by renewing
            # and asking again, and so does this — otherwise the first stale token ends the
            # readings for good. Not while the CLI is running, though: it renews its own,
            # and both of us renewing at once would spend the refresh token twice.
            if refused.code != 401 or RUNNING_PROC is not None:
                raise
            renewed = renew_access_token()
            if not renewed:
                answer["reason"] = "the sign-in has expired"
                USAGE_CACHE.update(checked_at=time.monotonic(), value=answer)
                return answer
            payload = ask_for_usage(renewed)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            # Its own Retry-After if it gave one, and a quarter of an hour otherwise.
            try:
                wait = max(int(exc.headers.get("Retry-After") or 0), BACKOFF_SEC)
            except ValueError:
                wait = BACKOFF_SEC
            USAGE_BACKOFF["until"] = time.monotonic() + wait
            answer["reason"] = "asked too often; leaving it alone for a while"
            answer["retry_at"] = (datetime.now(UTC) + timedelta(seconds=wait)).isoformat()
        else:
            answer["reason"] = f"HTTP {exc.code}"
        USAGE_CACHE.update(checked_at=time.monotonic(), value=answer)
        return answer
    except (urllib.error.URLError, OSError, ValueError) as exc:
        answer["reason"] = f"{type(exc).__name__}: {exc}"
        USAGE_CACHE.update(checked_at=time.monotonic(), value=answer)
        return answer

    try:
        session = usage_window(payload, "five_hour")
        week = usage_window(payload, "seven_day")
    except (AttributeError, TypeError, ValueError) as exc:
        answer["reason"] = f"unexpected answer: {type(exc).__name__}: {exc}"
        USAGE_CACHE.update(checked_at=time.monotonic(), value=answer)
        return answer
    # Each window carries the figure it is judged against, so nothing downstream has to
    # know which figure belongs to which window — or keep a copy of either.
    session = {**session, "threshold": SESSION_THRESHOLD} if session else None
    week = {**week, "threshold": WEEK_THRESHOLD} if week else None
    windows = [
        {"kind": kind, **window}
        for kind, window in (("session", session), ("week", week))
        if window
    ]
    if not windows:
        answer["reason"] = "no windows reported"
        USAGE_CACHE.update(checked_at=time.monotonic(), value=answer)
        return answer

    # The one that will stop a run first — which is the one with the least room left to its
    # own figure, not the fuller one: a week at 70% of a 75% limit bites before a session at
    # 80% of 95%.
    worst = min(windows, key=lambda w: w["threshold"] - w["percent"])
    USAGE_BACKOFF["until"] = 0.0
    answer = {
        "available": True,
        "session": session,
        "week": week,
        "worst": worst,
        "thresholds": {"session": SESSION_THRESHOLD, "week": WEEK_THRESHOLD},
        # The answer to the only question a caller has: may work start, or carry on? Each
        # window is judged against its own figure, and one over is enough to stop.
        "enough": all(window["percent"] < window["threshold"] for window in windows),
        # And whether anything is actually done about the answer, so nobody has to say
        # "work is held" about an add-on that only reports.
        "acting": GUARD_LIMITS,
        # So a caller polling this does not have to guess how often is polite.
        "check_every": USAGE_TTL_SEC,
        # And so anything showing these times to a person shows them on the house's clock
        # rather than keeping its own copy of where the house is.
        "timezone": addon_timezone(),
        "checked_at": now(),
    }
    USAGE_CACHE.update(checked_at=time.monotonic(), value=answer)
    return answer


def refresh_available() -> str | None:
    """Only the configured channel is ever compared, so only it is fetched."""
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed https host
            f"{RELEASES_URL}/{UPDATE_CHANNEL}", timeout=20
        ) as response:
            value = response.read(64).decode().strip()
    except (urllib.error.URLError, OSError, UnicodeDecodeError):
        return AVAILABLE["version"]
    if VERSION_ONLY.fullmatch(value):
        AVAILABLE["version"] = value
    return AVAILABLE["version"]


def is_newer(candidate: str | None, current: str | None) -> bool:
    if not candidate or not current:
        return False
    try:
        parse = lambda value: tuple(int(part) for part in value.split("."))  # noqa: E731
        return parse(candidate) > parse(current)
    except ValueError:
        return False


def read_update_state() -> dict:
    """Update progress lives on disk, so a reloaded page still sees a run underway."""
    return read_json(UPDATE_STATE_PATH, {"status": "idle"}) or {"status": "idle"}


def reconcile_interrupted_update() -> None:
    """A restart mid-install leaves 'running' behind; that run is not coming back."""
    state = read_update_state()
    if state.get("status") == "running":
        state.update(
            status="interrupted",
            finished_at=now(),
            error="the add-on restarted while the update was running",
        )
        write_json(UPDATE_STATE_PATH, state)
        # An interrupted install could in principle have left the binary on the
        # persistent volume unusable, and it shadows the packaged copy.
        if installed_version(force=True) is None:
            print(
                "[api] the CLI does not run after an interrupted update; "
                "remove /data/home/.local/bin/claude from the terminal to fall "
                "back to the packaged copy",
                flush=True,
            )


def _run_update(target: str, state: dict) -> None:
    """Runs `claude install <target>`. Owns releasing CLI_LOCK."""
    try:
        completed = subprocess.run(  # noqa: S603 - target gated by UPDATE_TARGET
            ["claude", "install", target],  # noqa: S607 - PATH resolution is deliberate
            capture_output=True,
            text=True,
            timeout=900,
            env=CLAUDE_ENV,
            check=False,
        )
        state["output"] = (completed.stdout + completed.stderr).strip()[-MAX_ERROR_CHARS:]
        if completed.returncode != 0:
            state["status"] = "failed"
            state["error"] = state["output"] or f"claude install exited {completed.returncode}"
        else:
            state["status"] = "done"
            state["installed"] = installed_version(force=True)
            state["changed"] = state["installed"] != state["previous"]
            refresh_available()
            # Only when the binary actually moved: no point re-running this on a
            # reinstall of the same version.
            if state["changed"]:
                state["plugins"] = refresh_plugins()
    except subprocess.TimeoutExpired:
        state["status"] = "failed"
        state["error"] = "claude install timed out"
    except Exception as exc:  # noqa: BLE001 - reported through the state instead
        state["status"] = "failed"
        state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        state["finished_at"] = now()
        try:
            write_json(UPDATE_STATE_PATH, state)
        finally:
            CLI_LOCK.release()

    if state["status"] == "done":
        print(f"[api] Claude Code {state['previous']} -> {state['installed']}", flush=True)
    else:
        print(f"[api] claude install {target} {state['status']}: {state['error']}", flush=True)


def run_cli(argv: list, timeout: int = 300, cwd: Path | None = None) -> tuple:
    """A short CLI call whose output we want. Returns (ok, text).

    `cwd` matters for the mcp commands: the CLI resolves the local and project scopes
    against the directory it is run in.
    """
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["claude", *argv],  # noqa: S607 - PATH resolution is deliberate
            capture_output=True,
            text=True,
            timeout=timeout,
            env=CLAUDE_ENV,
            cwd=cwd,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return done.returncode == 0, (done.stdout + done.stderr).strip()


def installed_plugins() -> list:
    """Plugin names from `claude plugin list --json`, tolerant of its shape."""
    ok, text = run_cli(["plugin", "list", "--json"], timeout=120)
    if not ok:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = data.get("plugins", data)
    names = []
    if isinstance(data, dict):
        names = [str(key) for key in data]
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("id")
                if name:
                    names.append(str(name))
    return [n for n in names if SAFE_MODEL.fullmatch(n)]


def refresh_plugins() -> dict:
    """After the CLI moves, bring marketplaces and plugins along with it.

    A plugin is built against a CLI version, so leaving them behind is how a
    working setup quietly rots. `marketplace update` with no name updates them all;
    plugins have no --all, so they are listed and updated one by one.
    """
    report: dict[str, Any] = {"marketplaces": None, "plugins": {}}

    ok, text = run_cli(["plugin", "marketplace", "update"], timeout=600)
    report["marketplaces"] = "ok" if ok else (text[-500:] or "failed")
    print(f"[api] marketplace update: {report['marketplaces']}", flush=True)

    for name in installed_plugins():
        ok, text = run_cli(["plugin", "update", name], timeout=600)
        report["plugins"][name] = "ok" if ok else (text[-300:] or "failed")
        print(f"[api] plugin {name}: {report['plugins'][name]}", flush=True)

    return report


def start_update(target: str, wait: bool = False) -> dict:
    """Begin an update. Returns the live state at once unless `wait`."""
    if not UPDATE_TARGET.fullmatch(target):
        raise ApiError(400, f"target must be 'latest', 'stable' or a version: {target!r}")
    if not CLI_LOCK.acquire(blocking=False):
        raise ApiError(409, "the CLI is busy with a job or another update")

    # Written here, synchronously, so the caller and any page reload see "running"
    # immediately. Doing it in the worker meant the first poll could still read
    # the previous run's state and re-enable the button.
    state = {
        "status": "running",
        "target": target,
        "previous": installed_version(),
        "installed": None,
        "started_at": now(),
        "finished_at": None,
        "changed": False,
        "error": None,
        "output": None,
    }
    # `_run_update` always releases the lock, in its own finally. So we release it
    # only while ownership has not been handed over — which covers a failing
    # write_json and a Thread.start() that cannot allocate a thread.
    handed_off = False
    try:
        write_json(UPDATE_STATE_PATH, state)
        print(f"[api] claude install {target} started (from {state['previous']})", flush=True)
        if wait:
            handed_off = True
            _run_update(target, state)
            return read_update_state()
        threading.Thread(target=_run_update, args=(target, state), daemon=True).start()
        handed_off = True
    finally:
        if not handed_off:
            CLI_LOCK.release()
    return state


def auto_update_pass() -> str:
    """One check for a newer CLI, installing it if that is what was asked for.

    Separate from the loop that calls it so that this — the part with the decisions
    in it — can be run once, and tested, rather than only as a step of something
    that sleeps for a day. Returns what it did, for the same reason.
    """
    try:
        wanted = refresh_available()
        current = installed_version(force=True)
        if not is_newer(wanted, current):
            print(f"[api] Claude Code {current} is current on '{UPDATE_CHANNEL}'", flush=True)
            return "current"
        if not AUTO_UPDATE:
            # Saying "is current" here would be a lie: there is a newer one, we
            # are just not installing it.
            print(f"[api] Claude Code {wanted} is available; auto_update is off", flush=True)
            return "available"
        print(f"[api] auto-update {current} -> {wanted}", flush=True)
        start_update(UPDATE_CHANNEL, wait=True)
        return "updated"
    except ApiError as exc:
        print(f"[api] auto-update skipped: {exc.message}", flush=True)
        return "skipped"
    except Exception as exc:  # noqa: BLE001 - the check must survive anything
        print(f"[api] update check failed: {exc}", flush=True)
        return "failed"


def auto_update_loop() -> None:
    """Check at start, then once a day. A busy CLI defers it to the next pass."""
    while True:
        auto_update_pass()
        time.sleep(CHECK_INTERVAL_SEC)


# --------------------------------------------------------------------------- #
# skills
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# MCP servers
# --------------------------------------------------------------------------- #

def mcp_transport(definition: dict) -> str:
    """stdio, http or sse — how the CLI will talk to this server."""
    declared = str(definition.get("type") or "").lower()
    if declared in {"stdio", "http", "sse", "ws"}:
        return declared
    return "http" if definition.get("url") else "stdio"


def mcp_summary(definition: dict) -> str:
    """What the server is, in one line, with nothing secret in it.

    An MCP definition carries `env` and `headers`, which is where API keys live, and
    a URL's query string is another place people put them. None of that is returned:
    the point of the list is which servers exist, not what they are authenticated
    with.
    """
    url = definition.get("url")
    if url:
        return str(url).split("?")[0]
    parts = [str(definition.get("command") or "")]
    parts += [str(argument) for argument in definition.get("args") or []]
    return " ".join(part for part in parts if part)[:200]


def _servers_from(section, scope: str) -> dict:
    if not isinstance(section, dict):
        return {}
    found = {}
    for name, definition in section.items():
        if isinstance(definition, dict) and SAFE_NAME.fullmatch(str(name)):
            found[str(name)] = {"scope": scope, "definition": definition}
    return found


def live_mcp_servers() -> dict:
    """Every MCP server the CLI is currently configured with, by name."""
    cli_config = read_json(CLI_CONFIG_PATH, {}) or {}
    projects = cli_config.get("projects")
    here = (projects or {}).get(str(CHAT_DIR)) if isinstance(projects, dict) else None
    project_file = read_json(PROJECT_MCP_PATH, {}) or {}

    servers = {}
    # Later scopes win the name, which is the order the CLI itself resolves them in.
    servers.update(_servers_from(cli_config.get("mcpServers"), "user"))
    servers.update(_servers_from((here or {}).get("mcpServers"), "local"))
    servers.update(_servers_from(project_file.get("mcpServers"), "project"))
    return servers


def list_mcp() -> list:
    """The servers that are on, and the ones being kept aside, in one list."""
    off = read_json(MCP_OFF_PATH, {}) or {}
    entries = []
    for name, entry in live_mcp_servers().items():
        entries.append(
            {
                "name": name,
                "scope": entry["scope"],
                "transport": mcp_transport(entry["definition"]),
                "summary": mcp_summary(entry["definition"]),
                "enabled": True,
            }
        )
    for name, entry in off.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("definition"), dict):
            continue
        entries.append(
            {
                "name": str(name),
                "scope": entry.get("scope") if entry.get("scope") in MCP_SCOPES else "user",
                "transport": mcp_transport(entry["definition"]),
                "summary": mcp_summary(entry["definition"]),
                "enabled": False,
            }
        )
    entries.sort(key=lambda server: (server["scope"], server["name"].lower()))
    return entries


def set_mcp_enabled(name: str, enabled: bool) -> dict:
    """Switch one server on or off, through the CLI's own commands.

    The definition is moved between the CLI's config and this add-on's store rather
    than edited in place: `claude mcp add-json` and `claude mcp remove` own that file
    and validate what goes into it.
    """
    safe_name(name)
    off = read_json(MCP_OFF_PATH, {}) or {}
    live = live_mcp_servers()

    if enabled:
        if name in live:
            return {"name": name, "enabled": True, "changed": False}
        entry = off.get(name)
        if not isinstance(entry, dict) or not isinstance(entry.get("definition"), dict):
            raise ApiError(404, f"no server is being kept aside under that name: {name}")
        scope = entry.get("scope") if entry.get("scope") in MCP_SCOPES else "user"
        ok, text = run_cli(
            ["mcp", "add-json", name, json.dumps(entry["definition"]), "-s", scope],
            cwd=CHAT_DIR,
        )
        if not ok:
            raise ApiError(502, f"the CLI would not add {name}: {text[-MAX_ERROR_CHARS:]}")
        off.pop(name, None)
        write_json(MCP_OFF_PATH, off)
        print(f"[api] mcp {name} on ({scope})", flush=True)
        return {"name": name, "enabled": True, "changed": True, "scope": scope}

    if name in off and name not in live:
        return {"name": name, "enabled": False, "changed": False}
    entry = live.get(name)
    if entry is None:
        raise ApiError(404, f"no such MCP server: {name}")

    # Stored before it is removed: a definition lost here would have to be typed in
    # again, secrets and all.
    off[name] = entry
    write_json(MCP_OFF_PATH, off)
    ok, text = run_cli(["mcp", "remove", name, "-s", entry["scope"]], cwd=CHAT_DIR)
    if not ok:
        off.pop(name, None)
        write_json(MCP_OFF_PATH, off)
        raise ApiError(502, f"the CLI would not remove {name}: {text[-MAX_ERROR_CHARS:]}")
    print(f"[api] mcp {name} off ({entry['scope']})", flush=True)
    return {"name": name, "enabled": False, "changed": True, "scope": entry["scope"]}


def count_skills() -> int:
    """Cheap: /health used to walk every file of every skill to produce this."""
    if not SKILLS_DIR.is_dir():
        return 0
    return sum(1 for p in SKILLS_DIR.iterdir() if p.is_dir() and not p.name.startswith("."))


def frontmatter_field(skill_md: Path, field: str) -> str | None:
    """One field of a SKILL.md's frontmatter.

    Deliberately not a YAML parser: only two scalar fields are ever wanted, and a
    real parser is not available in this image. The value may be a folded block
    scalar spanning several indented lines, which is common for `description`.
    """
    try:
        text = skill_md.read_text(errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None

    prefix = f"{field}:"
    lines = text[3:end].splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(prefix):
            continue
        value = line.split(":", 1)[1].strip()
        if value in {">-", ">", "|", "|-", ""}:
            collected = []
            for following in lines[index + 1:]:
                if following.strip() and not following.startswith((" ", "\t")):
                    break
                collected.append(following.strip())
            value = " ".join(part for part in collected if part)
        return value.strip().strip("'\"") or None
    return None


def skill_meta(path: Path) -> dict:
    """One walk, not two, and tolerant of files vanishing mid-walk."""
    files = 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                files += 1
                total += child.stat().st_size
        except OSError:
            continue

    try:
        updated_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(
            timespec="seconds"
        )
    except OSError:
        updated_at = None

    skill_md = path / "SKILL.md"
    return {
        "name": path.name,
        "files": files,
        "bytes": total,
        "has_skill_md": skill_md.is_file(),
        "description": frontmatter_field(skill_md, "description") if skill_md.is_file() else None,
        "updated_at": updated_at,
    }


def list_skills() -> list:
    if not SKILLS_DIR.is_dir():
        return []
    return [
        skill_meta(path)
        for path in sorted(SKILLS_DIR.iterdir())
        if path.is_dir() and not path.name.startswith(".")
    ]


def check_archive(tar: tarfile.TarFile) -> None:
    """Refuse an archive that expands to far more than it weighs.

    getmembers rather than iterating the TarFile: iteration consumes the stream,
    and extractall would then find nothing left. getmembers caches the list, so
    the following extractall reuses it.
    """
    members = tar.getmembers()
    if len(members) > MAX_MEMBERS:
        raise ApiError(413, f"archive holds more than {MAX_MEMBERS} entries")
    total = sum(max(member.size, 0) for member in members)
    if total > MAX_EXTRACTED:
        raise ApiError(413, "archive expands to more than 512 mb")


def install_skill(archive: bytes, name: str | None = None) -> dict:
    """Unpack a .tar.gz into <skills>/<name>, replacing any previous version.

    The name is taken from `name:` in the archive's SKILL.md unless one is given
    explicitly. Asking the uploader for it invited a mismatch with what the skill
    calls itself, and Claude Code goes by the frontmatter.
    """
    if name is not None:
        safe_name(name)
    if not archive:
        raise ApiError(400, "the request body is empty")

    staging = SKILLS_DIR / ".incoming"
    shutil.rmtree(staging, ignore_errors=True)
    unpacked = staging / "unpacked"
    unpacked.mkdir(parents=True)

    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            check_archive(tar)
            # filter="data" rejects absolute paths, traversal and special files.
            tar.extractall(unpacked, filter="data")
    except ApiError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ApiError(400, f"not a readable .tar.gz: {exc}") from exc

    entries = list(unpacked.iterdir())
    # Archives usually carry one top-level folder; use its contents so the skill
    # does not end up nested as <name>/<name>/SKILL.md.
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else unpacked

    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise ApiError(400, "no SKILL.md at the top level of the archive")

    if name is None:
        declared = frontmatter_field(skill_md, "name")
        # The wrapping folder is the fallback, and only when there was one.
        fallback = root.name if root is not unpacked else None
        name = declared or fallback
        if not name:
            shutil.rmtree(staging, ignore_errors=True)
            raise ApiError(
                400,
                "the archive's SKILL.md has no `name:` and it has no top-level "
                "folder to take one from; pass ?name= instead",
            )
        try:
            safe_name(name)
        except ApiError:
            shutil.rmtree(staging, ignore_errors=True)
            raise ApiError(400, f"the name in SKILL.md is not usable: {name!r}") from None

    target = SKILLS_DIR / name

    # rmtree does not remove a symlink, and shutil.move would then follow it and
    # write the skill inside whatever it points at.
    if target.is_symlink():
        target.unlink()
    shutil.rmtree(target, ignore_errors=True)
    shutil.move(str(root), str(target))
    shutil.rmtree(staging, ignore_errors=True)

    print(f"[api] installed skill {name}", flush=True)
    return skill_meta(target)


def archive_skill(name: str) -> bytes:
    """Pack a skill back into a .tar.gz, so it can be kept outside the add-on."""
    target = SKILLS_DIR / safe_name(name)
    if not target.is_dir():
        raise ApiError(404, f"no such skill: {name}")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(target, arcname=name)
    return buffer.getvalue()


def delete_skill(name: str) -> None:
    target = SKILLS_DIR / safe_name(name)
    if not target.is_dir():
        raise ApiError(404, f"no such skill: {name}")
    shutil.rmtree(target)
    print(f"[api] removed skill {name}", flush=True)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "claude-code-addon"
    protocol_version = "HTTP/1.1"
    # Without this a client that connects and says nothing parks a thread in
    # readline() forever, before any authentication.
    timeout = 60

    def log_message(self, fmt, *args):
        print(f"[api] {fmt % args}", flush=True)

    def _send(self, status: int, payload, content_type="application/json", filename=None):
        if content_type == "application/json":
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        else:
            body = payload
        # An error may be answered before the request body has been read, and
        # those unread bytes would be parsed as the next request on a kept-alive
        # connection. Close instead.
        if status >= 400:
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if status >= 400:
            self.send_header("Connection", "close")
        if filename:
            # Both forms: the ASCII one for anything old, and RFC 5987 so a name with
            # spaces or Cyrillic in it survives the trip. Without the second, a file
            # Claude produced arrived as a row of underscores.
            encoded = quote(filename, safe="")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{header_safe(filename)}"; filename*=UTF-8\'\'{encoded}',
            )
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        # With no token the API binds to localhost only, so the sole way in is
        # the ingress web UI, which Home Assistant has already authenticated.
        if not API_TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        return hmac.compare_digest(header, f"Bearer {API_TOKEN}")

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            # Content-Length is absent, so the body would silently read as empty
            # and a PUT would store a 0-byte file with a 201.
            raise ApiError(411, "chunked bodies are not supported; send Content-Length")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ApiError(400, "invalid Content-Length") from exc
        if length < 0 or length > MAX_UPLOAD:
            raise ApiError(413, f"body must be between 0 and {MAX_UPLOAD} bytes")
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict:
        raw = self._body()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise ApiError(400, f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ApiError(400, "the body must be a JSON object")
        return payload

    def _handle(self, method: str):
        parsed = urlparse(self.path)
        parts = [unquote(p) for p in parsed.path.strip("/").split("/") if p]
        # keep_blank_values, or `?refresh` — a flag with no value — is dropped and the
        # thing it asks for silently does not happen.
        query = parse_qs(parsed.query, keep_blank_values=True)

        # Deliberately open and deliberately empty of detail, so a watchdog can
        # probe liveness without a token.
        if parts == ["ping"] and method == "GET":
            return self._send(200, {"status": "ok"})
        if not self._authorised():
            return self._send(401, {"error": "a valid Bearer token is required"})
        return self._route(method, parts, query)

    def _route(self, method: str, parts: list, query: dict):
        if parts == ["health"] and method == "GET":
            return self._send(200, self._health())

        if parts == ["version"] and method == "GET":
            return self._send(200, self._version(refresh="refresh" in query))

        if parts == ["update"]:
            if method == "GET":
                return self._send(200, read_update_state())
            if method == "POST":
                target = (query.get("target") or [UPDATE_CHANNEL])[0]
                # 202: the install runs in the background, poll GET /update.
                return self._send(202, start_update(target))

        if parts == ["settings"]:
            if method == "GET":
                return self._send(
                    200,
                    {
                        "settings": read_settings(),
                        "path": str(SETTINGS_PATH),
                        "enforced_env": REQUIRED_ENV,
                    },
                )
            if method == "PUT":
                raw = self._body().decode("utf-8", "replace")
                return self._send(200, {"settings": write_settings(raw)})

        if parts == ["chat"]:
            if method == "GET":
                session = current_session()
                # Turns of this conversation, and the console's own. A bot driving a
                # different conversation through the API is not what this window is
                # showing, and its turns used to appear here as though they were.
                jobs = [
                    j
                    for j in list_jobs()
                    if j.get("chat")
                    and (
                        j.get("source", "console") == "console"
                        or j.get("resumed_from") == session
                    )
                ]
                latest = next((j for j in jobs if j.get("context")), None)
                waiting = [
                    j for j in jobs if j.get("status") in {"created", "queued", "running"}
                ]
                waiting.reverse()  # oldest first: that is the order they will run in
                running = None
                for job in waiting:
                    # What Claude has said so far, so the reply appears as it is
                    # written rather than all at once at the end.
                    if job.get("status") == "running":
                        job["partial"] = stream_text(JOBS_DIR / job["id"])
                        # A frozen turn looks exactly like a working one otherwise, and
                        # the console would show a spinner for hours with no way out.
                        job["paused"] = job["id"] == RUNNING_JOB and PAUSED_AT is not None
                        running = job

                turns, notices = read_conversation(session) if session else ([], [])
                if running and not running.get("command"):
                    # Claude Code writes its reply into the transcript as it goes, so
                    # while a turn is in flight those records and `partial` are the
                    # same words. The transcript is cut back to the last thing the
                    # user said and the live text is served on its own, or the reply
                    # appears twice.
                    while turns and turns[-1]["role"] != "user":
                        turns.pop()
                newest = jobs[0] if jobs else None
                return self._send(
                    200,
                    {
                        "session": session,
                        "title": session_title(session) if session else None,
                        # The transcript is Claude Code's own, so a conversation
                        # continued from the terminal shows up here too.
                        "turns": turns,
                        # Everything still to run, so the UI can show a queue and
                        # mark which one Claude is on.
                        "pending": waiting,
                        # A failed turn never reaches the transcript, so without
                        # this the message and its reason would both vanish.
                        "failed": newest if (newest or {}).get("status") == "failed" else None,
                        # Everything that went wrong along the way — a refused
                        # request, a tool that failed — kept out of the conversation
                        # and offered beside it instead.
                        "notices": notices,
                        "context": (latest or {}).get("context"),
                    },
                )
            if method == "POST":
                payload = self._json_body()
                payload["chat"] = True
                payload["start"] = True
                return self._send(201, create_job(payload))

        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel" and method == "POST":
            return self._send(200, cancel_job(parts[1]))

        if (
            len(parts) == 3
            and parts[0] == "jobs"
            and parts[2] in {"pause", "resume"}
            and method == "POST"
        ):
            return self._send(200, freeze_job(parts[1], parts[2] == "pause"))

        if parts == ["chat", "compact"] and method == "POST":
            session = current_session()
            if not session:
                raise ApiError(409, "there is no conversation to compact yet")
            # /compact summarises the conversation. It cannot shrink the fixed
            # overhead — system prompt, tool definitions, skill metadata — so on a
            # short conversation the figure barely moves.
            return self._send(
                201,
                create_job(
                    {
                        "prompt": "/compact",
                        "chat": True,
                        "command": "compact",
                        "resume": session,
                        "start": True,
                    }
                ),
            )

        if parts == ["chat", "rename"] and method == "POST":
            payload = self._json_body()
            session = str(payload.get("session") or "")
            title = str(payload.get("title") or "").strip()
            if not SESSION_ID.fullmatch(session):
                raise ApiError(400, "a session id is required")
            if not title:
                raise ApiError(400, "a title is required")
            # Newlines would end the command; the length keeps a title a title.
            title = re.sub(r"\s+", " ", title)[:120]
            return self._send(
                201,
                create_job(
                    {
                        "prompt": f"/rename {title}",
                        "chat": True,
                        "command": "rename",
                        "resume": session,
                        "start": True,
                    }
                ),
            )

        if parts == ["chat", "new"] and method == "POST":
            # /clear has no headless equivalent; a fresh conversation is simply one
            # started without --resume.
            set_current_session(None)
            return self._send(200, {"session": None})

        if parts == ["chat", "resume"] and method == "POST":
            session = str(self._json_body().get("session") or "")
            if not SESSION_ID.fullmatch(session):
                raise ApiError(400, "a session id is required")
            set_current_session(session)
            turns, notices = read_conversation(session)
            return self._send(200, {"session": session, "turns": turns, "notices": notices})

        if parts == ["chat", "sessions"] and method == "GET":
            return self._send(200, {"sessions": list_sessions(), "current": current_session()})

        if len(parts) == 3 and parts[:2] == ["chat", "sessions"] and method == "DELETE":
            return self._send(200, forget_session(parts[2]))

        if parts == ["usage"] and method == "GET":
            return self._send(200, read_usage(force="refresh" in query))

        if parts == ["files"] and method == "GET":
            return self._send(
                200,
                {
                    "files": [
                        {
                            "key": key,
                            "path": str(entry["path"]),
                            "kind": entry["kind"],
                            "exists": entry["path"].is_file(),
                        }
                        for key, entry in EDITABLE_FILES.items()
                    ]
                },
            )

        if len(parts) == 2 and parts[0] == "files":
            if method == "GET":
                return self._send(200, read_editable(parts[1]))
            if method == "PUT":
                return self._send(
                    200, write_editable(parts[1], self._body().decode("utf-8", "replace"))
                )

        if parts == ["state"] and method == "GET":
            return self._send(200, {"keys": list_state()})

        if len(parts) == 2 and parts[0] == "state":
            if method == "GET":
                return self._send(200, read_state(parts[1]))
            if method == "PUT":
                return self._send(200, write_state(parts[1], self._json_body()))
            if method == "DELETE":
                return self._send(200, forget_state(parts[1]))

        if parts == ["mcp"] and method == "GET":
            return self._send(
                200,
                {
                    "servers": list_mcp(),
                    "config_path": str(CLI_CONFIG_PATH),
                    "project_path": str(PROJECT_MCP_PATH),
                },
            )

        if len(parts) == 2 and parts[0] == "mcp" and method == "POST":
            payload = self._json_body()
            if not isinstance(payload.get("enabled"), bool):
                raise ApiError(400, "'enabled' must be true or false")
            return self._send(200, set_mcp_enabled(parts[1], payload["enabled"]))

        if parts == ["skills"]:
            if method == "GET":
                return self._send(200, {"skills": list_skills()})
            if method == "POST":
                # Optional: without it the name comes from the archive's SKILL.md.
                name = (query.get("name") or [None])[0]
                return self._send(201, install_skill(self._body(), name))

        if len(parts) == 2 and parts[0] == "skills":
            if method == "DELETE":
                delete_skill(parts[1])
                return self._send(200, {"deleted": parts[1]})
            if method == "GET":
                target = SKILLS_DIR / safe_name(parts[1])
                if not target.is_dir():
                    raise ApiError(404, f"no such skill: {parts[1]}")
                return self._send(200, skill_meta(target))

        if len(parts) == 3 and parts[0] == "skills" and parts[2] == "archive" and method == "GET":
            return self._send(
                200,
                archive_skill(parts[1]),
                "application/gzip",
                filename=f"{safe_name(parts[1])}.tar.gz",
            )

        if parts == ["jobs"]:
            if method == "GET":
                return self._send(200, {"jobs": list_jobs()})
            if method == "POST":
                return self._send(201, create_job(self._json_body()))

        if len(parts) == 2 and parts[0] == "jobs":
            if method == "GET":
                job = read_job(parts[1])
                job["files"] = list_job_files(parts[1])
                if job.get("status") == "running":
                    # So a caller polling a long run has something to show for it.
                    job["partial"] = stream_text(JOBS_DIR / safe_name(parts[1]))
                    job["activity"] = stream_activity(JOBS_DIR / safe_name(parts[1]))
                    # Whether it is moving at all: a caller showing a status to somebody
                    # must not say "working" about a turn that stands frozen.
                    job["paused"] = parts[1] == RUNNING_JOB and PAUSED_AT is not None
                return self._send(200, job)
            if method == "DELETE":
                job = read_job(parts[1])
                if job.get("status") in {"queued", "running"}:
                    # The job directory is the running process's cwd, and a queued one is
                    # about to become that. A job that was made and never started owns
                    # nothing, and refusing to delete it left it in the console for good.
                    raise ApiError(409, f"job is {job['status']}; cannot delete it yet")
                shutil.rmtree(job_dir(parts[1]), ignore_errors=True)
                return self._send(200, {"deleted": parts[1]})

        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "start" and method == "POST":
            return self._send(200, start_job(parts[1]))

        if len(parts) >= 3 and parts[0] == "jobs" and parts[2] == "files":
            job = read_job(parts[1])
            relative = "/".join(parts[3:])
            if method == "GET":
                if not relative:
                    return self._send(200, {"files": list_job_files(parts[1])})
                path = safe_subpath(job_dir(parts[1]), relative)
                if not path.is_file():
                    raise ApiError(404, f"no such file: {relative}")
                return self._send(
                    200,
                    path.read_bytes(),
                    "application/octet-stream",
                    filename=path.name,
                )
            if method == "PUT":
                if job["status"] != "created":
                    raise ApiError(409, f"job is already {job['status']}")
                if not relative:
                    raise ApiError(400, "a file name is required")
                path = safe_subpath(job_dir(parts[1]) / "in", relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self._body())
                return self._send(201, {"path": f"in/{relative}"})

        raise ApiError(404, f"no route for {method} /{'/'.join(parts)}")

    def _health(self) -> dict:
        installed = installed_version()
        available = AVAILABLE["version"]
        # Read from disk rather than from the lock, so a page opened after a
        # restart still learns what the last update did.
        update = read_update_state()
        return {
            "status": "ok",
            "claude_version": installed,
            "available_version": available,
            "update_available": is_newer(available, installed),
            "update_channel": UPDATE_CHANNEL,
            "auto_update": AUTO_UPDATE,
            # Home Assistant's own setting, so times read the same here as everywhere else
            # in the house.
            "timezone": addon_timezone(),
            "updating": update.get("status") == "running",
            "update": update,
            "logged_in": (HOME / ".claude" / ".credentials.json").is_file(),
            "skills": count_skills(),
            "queued": JOB_QUEUE.qsize(),
            "job_running": RUNNING_JOB is not None,
            "job_paused": PAUSED_AT is not None,
            "default_model": DEFAULT_MODEL,
            "default_effort": DEFAULT_EFFORT or "medium",
            # Normalised, so a stored `default` from an older config still matches
            # an entry in the list below. A selector whose value is absent from its
            # options silently shows the first one instead.
            "default_permission_mode": PERMISSION_ALIASES.get(
                DEFAULT_PERMISSION_MODE, DEFAULT_PERMISSION_MODE
            ),
            # The UI builds its selectors from these rather than hardcoding a list
            # that would drift from what the server accepts.
            "models": list(MODEL_ALIASES),
            "efforts": list(EFFORTS),
            "permission_modes": list(PERMISSION_MODES),
            "timeout_minutes": TIMEOUT_SEC // 60,
            "token_required": bool(API_TOKEN),
        }

    def _version(self, refresh: bool = False) -> dict:
        available = refresh_available() if refresh else AVAILABLE["version"]
        installed = installed_version(force=refresh)
        return {
            "installed": installed,
            "available": available,
            "channel": UPDATE_CHANNEL,
            "update_available": is_newer(available, installed),
            "auto_update": AUTO_UPDATE,
            "binary": shutil.which("claude", path=CLAUDE_ENV["PATH"]),
            "last_update": read_update_state(),
        }

    def _dispatch(self, method: str):
        try:
            self._handle(method)
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001 - never drop the connection silently
            self.log_message("unhandled %s: %s", type(exc).__name__, exc)
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


def main() -> None:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    reconcile_interrupted_jobs()
    reconcile_interrupted_update()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=auto_update_loop, daemon=True).start()
    threading.Thread(target=limit_watch, daemon=True).start()

    # Without a token there is nothing to authenticate with, so the API is kept
    # off the network and only nginx (the ingress web UI) can reach it.
    host = "0.0.0.0" if API_TOKEN else "127.0.0.1"  # noqa: S104 - gated on a token
    print(f"[api] listening on {host}:{PORT}", flush=True)
    ThreadingHTTPServer((host, PORT), Handler).serve_forever()


if __name__ == "__main__":  # pragma: no cover - the entry point
    main()
