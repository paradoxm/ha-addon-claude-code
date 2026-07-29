#!/usr/bin/env python3
"""HTTP API in front of the Claude Code CLI.

Everything lives inside the add-on's own /data volume, which Home Assistant keeps
across restarts and add-on updates, so no HA folder needs to be mapped and no
other add-on needs filesystem access:

    /data/home/.claude/skills/<name>/   skills, uploaded as .tar.gz
    /data/jobs/<id>/in/                 files uploaded for a job
    /data/jobs/<id>/job.json            job state
    /data/jobs/<id>/claude.json         raw --output-format json result
    /data/jobs/<id>/claude.log          stderr

HOME points at /data/home, which is how the CLI finds the skills: Claude Code
discovers personal skills in ~/.claude/skills and needs no configuration for it.

Runs at most one job at a time: these runs are long and memory-hungry, and this
shares a box with Home Assistant.
"""

import io
import json
import os
import queue
import re
import shutil
import subprocess
import tarfile
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

OPTIONS_PATH = Path("/data/options.json")
HOME = Path("/data/home")
SKILLS_DIR = HOME / ".claude" / "skills"
JOBS_DIR = Path("/data/jobs")
PORT = 7682

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_UPLOAD = 256 * 1024 * 1024
INTERNAL_FILES = {"job.json", "claude.json", "claude.log"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_options() -> dict:
    try:
        return json.loads(OPTIONS_PATH.read_text())
    except (OSError, ValueError):
        return {}


OPTIONS = load_options()
API_TOKEN = str(OPTIONS.get("api_token") or "")
DEFAULT_MODEL = str(OPTIONS.get("model") or "opus")
TIMEOUT_SEC = int(OPTIONS.get("timeout_minutes") or 90) * 60


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def safe_name(name: str) -> str:
    """Reject anything that could escape the directory it is joined onto."""
    if not SAFE_NAME.match(name):
        raise ApiError(400, f"unsafe name: {name!r}")
    return name


def safe_subpath(base: Path, relative: str) -> Path:
    """Resolve `relative` inside `base`, refusing to leave it."""
    base = base.resolve()
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise ApiError(400, f"path escapes the job directory: {relative!r}")
    return target


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #

JOB_QUEUE: "queue.Queue[str]" = queue.Queue()
STATE_LOCK = threading.Lock()


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / safe_name(job_id)


def read_job(job_id: str) -> dict:
    path = job_dir(job_id) / "job.json"
    if not path.is_file():
        raise ApiError(404, f"no such job: {job_id}")
    return json.loads(path.read_text())


def write_job(job: dict) -> None:
    with STATE_LOCK:
        (JOBS_DIR / job["id"] / "job.json").write_text(json.dumps(job, indent=2))


def list_job_files(job_id: str) -> list:
    """Everything the run produced, excluding uploads and bookkeeping."""
    base = job_dir(job_id)
    files = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if rel.parts[0] == "in" or str(rel) in INTERNAL_FILES:
            continue
        files.append({"path": str(rel), "size": path.stat().st_size})
    return files


def create_job(payload: dict) -> dict:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise ApiError(400, "'prompt' is required")

    job_id = uuid.uuid4().hex[:12]
    (JOBS_DIR / job_id / "in").mkdir(parents=True)

    job = {
        "id": job_id,
        "status": "created",
        "prompt": prompt,
        "model": str(payload.get("model") or DEFAULT_MODEL),
        "created_at": now(),
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "result": None,
        "error": None,
    }
    write_job(job)

    if payload.get("start"):
        return start_job(job_id)
    return job


def start_job(job_id: str) -> dict:
    job = read_job(job_id)
    if job["status"] != "created":
        raise ApiError(409, f"job is already {job['status']}")
    job["status"] = "queued"
    write_job(job)
    JOB_QUEUE.put(job_id)
    return job


def run_job(job_id: str) -> None:
    directory = JOBS_DIR / job_id
    job = read_job(job_id)
    job["status"] = "running"
    job["started_at"] = now()
    write_job(job)

    command = [
        "claude",
        "-p",
        job["prompt"],
        "--model",
        job["model"],
        "--permission-mode",
        "acceptEdits",
        # Skills run their own scripts, so Bash has to be allowed. The container
        # reaches only its own /data, which is what keeps this reasonable.
        "--allowedTools",
        "Bash,Read,Write,Edit,Glob,Grep",
        "--output-format",
        "json",
    ]

    try:
        with open(directory / "claude.log", "wb") as stderr:
            completed = subprocess.run(
                command,
                cwd=directory,
                env=dict(os.environ, HOME=str(HOME)),
                stdout=subprocess.PIPE,
                stderr=stderr,
                timeout=TIMEOUT_SEC,
            )
        (directory / "claude.json").write_bytes(completed.stdout)
        job["exit_code"] = completed.returncode

        # The CLI reports failures in its stdout JSON rather than on stderr: a run
        # with no credentials exits 1 with an empty stderr and a body carrying
        # {"is_error": true, "result": "Not logged in · Please run /login"}.
        try:
            payload = json.loads(completed.stdout)
        except ValueError:
            payload = None

        message = payload.get("result") if isinstance(payload, dict) else None
        if message is None:
            message = completed.stdout.decode("utf-8", "replace").strip() or None

        failed = completed.returncode != 0 or (
            isinstance(payload, dict) and payload.get("is_error")
        )

        if failed:
            job["status"] = "failed"
            stderr_text = (directory / "claude.log").read_text(errors="replace").strip()
            job["error"] = (message or stderr_text or "the CLI failed without a message")[-4000:]
        else:
            job["status"] = "done"
            job["result"] = message
    except subprocess.TimeoutExpired:
        job["status"] = "failed"
        job["error"] = f"timed out after {TIMEOUT_SEC}s"
    except Exception as exc:  # noqa: BLE001 - reported to the caller instead
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"

    job["finished_at"] = now()
    write_job(job)
    print(f"[api] job {job_id} {job['status']}", flush=True)


def worker() -> None:
    while True:
        job_id = JOB_QUEUE.get()
        try:
            run_job(job_id)
        except Exception as exc:  # noqa: BLE001 - a bad job must not kill the worker
            print(f"[api] job {job_id} crashed: {exc}", flush=True)
        finally:
            JOB_QUEUE.task_done()


def reconcile_interrupted_jobs() -> None:
    """A restart mid-run leaves 'running' behind; that job is not coming back."""
    for path in JOBS_DIR.glob("*/job.json"):
        try:
            job = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if job.get("status") in {"running", "queued"}:
            job["status"] = "failed"
            job["error"] = "the add-on restarted while this job was in flight"
            job["finished_at"] = now()
            path.write_text(json.dumps(job, indent=2))


# --------------------------------------------------------------------------- #
# skills
# --------------------------------------------------------------------------- #

def skill_meta(path: Path) -> dict:
    """Name and description from the SKILL.md frontmatter, if it parses."""
    meta = {
        "name": path.name,
        "files": sum(1 for p in path.rglob("*") if p.is_file()),
        "bytes": sum(p.stat().st_size for p in path.rglob("*") if p.is_file()),
        "has_skill_md": (path / "SKILL.md").is_file(),
        "description": None,
        "updated_at": None,
    }
    try:
        meta["updated_at"] = datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc
        ).isoformat(timespec="seconds")
    except OSError:
        pass

    skill_md = path / "SKILL.md"
    if not skill_md.is_file():
        return meta

    try:
        text = skill_md.read_text(errors="replace")
    except OSError:
        return meta

    if not text.startswith("---"):
        return meta
    end = text.find("\n---", 3)
    if end == -1:
        return meta

    # Deliberately not a YAML parser: only the description is wanted, and the
    # value may be a folded block scalar spanning several indented lines.
    lines = text[3:end].splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("description:"):
            continue
        value = line.split(":", 1)[1].strip()
        if value in {">-", ">", "|", "|-", ""}:
            collected = []
            for following in lines[index + 1:]:
                if following.strip() and not following.startswith((" ", "\t")):
                    break
                collected.append(following.strip())
            value = " ".join(part for part in collected if part)
        meta["description"] = value.strip().strip("'\"") or None
        break
    return meta


def list_skills() -> list:
    if not SKILLS_DIR.is_dir():
        return []
    return [
        skill_meta(path)
        for path in sorted(SKILLS_DIR.iterdir())
        if path.is_dir() and not path.name.startswith(".")
    ]


def install_skill(name: str, archive: bytes) -> dict:
    """Unpack a .tar.gz over <skills>/<name>, replacing any previous version."""
    safe_name(name)
    if not archive:
        raise ApiError(400, "the request body is empty")

    target = SKILLS_DIR / name
    staging = SKILLS_DIR / f".{name}.incoming"
    shutil.rmtree(staging, ignore_errors=True)
    unpacked = staging / "unpacked"
    unpacked.mkdir(parents=True)

    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            # filter="data" rejects absolute paths, traversal and special files.
            tar.extractall(unpacked, filter="data")
    except (tarfile.TarError, OSError, EOFError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ApiError(400, f"not a readable .tar.gz: {exc}") from exc

    entries = list(unpacked.iterdir())
    # Archives usually carry one top-level folder; use its contents so the skill
    # does not end up nested as <name>/<name>/SKILL.md.
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else unpacked

    if not (root / "SKILL.md").is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise ApiError(400, "no SKILL.md at the top level of the archive")

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

    def log_message(self, fmt, *args):
        print(f"[api] {fmt % args}", flush=True)

    def _send(self, status: int, payload, content_type="application/json", filename=None):
        if content_type == "application/json":
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        else:
            body = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        # With no token the API binds to localhost only, so the sole way in is
        # the ingress web UI, which Home Assistant has already authenticated.
        if not API_TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {API_TOKEN}"

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ApiError(413, f"body larger than {MAX_UPLOAD} bytes")
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
        query = parse_qs(parsed.query)

        if parts == ["health"] and method == "GET":
            return self._send(200, self._health())
        if not self._authorised():
            return self._send(401, {"error": "a valid Bearer token is required"})
        return self._route(method, parts, query)

    def _route(self, method: str, parts: list, query: dict):
        if parts == ["skills"]:
            if method == "GET":
                return self._send(200, {"skills": list_skills()})
            if method == "POST":
                name = (query.get("name") or [""])[0]
                if not name:
                    raise ApiError(400, "the 'name' query parameter is required")
                return self._send(201, install_skill(name, self._body()))

        if len(parts) == 2 and parts[0] == "skills":
            if method == "DELETE":
                delete_skill(parts[1])
                return self._send(200, {"deleted": parts[1]})
            if method == "GET":
                for skill in list_skills():
                    if skill["name"] == parts[1]:
                        return self._send(200, skill)
                raise ApiError(404, f"no such skill: {parts[1]}")

        if len(parts) == 3 and parts[0] == "skills" and parts[2] == "archive":
            if method == "GET":
                return self._send(
                    200,
                    archive_skill(parts[1]),
                    "application/gzip",
                    filename=f"{parts[1]}.tar.gz",
                )

        if parts == ["jobs"]:
            if method == "GET":
                ids = sorted(p.name for p in JOBS_DIR.iterdir() if p.is_dir())
                jobs = [read_job(i) for i in ids]
                jobs.sort(key=lambda j: j["created_at"], reverse=True)
                return self._send(200, {"jobs": jobs})
            if method == "POST":
                return self._send(201, create_job(self._json_body()))

        if len(parts) == 2 and parts[0] == "jobs":
            if method == "GET":
                job = read_job(parts[1])
                job["files"] = list_job_files(parts[1])
                return self._send(200, job)
            if method == "DELETE":
                shutil.rmtree(job_dir(parts[1]), ignore_errors=True)
                return self._send(200, {"deleted": parts[1]})

        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "start":
            if method == "POST":
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
        try:
            version = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=20,
                env=dict(os.environ, HOME=str(HOME)),
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            version = None
        return {
            "status": "ok",
            "claude_version": version,
            "logged_in": (HOME / ".claude" / ".credentials.json").is_file(),
            "skills": len(list_skills()),
            "queued": JOB_QUEUE.qsize(),
            "default_model": DEFAULT_MODEL,
            "timeout_minutes": TIMEOUT_SEC // 60,
            "token_required": bool(API_TOKEN),
        }

    def _dispatch(self, method: str):
        try:
            self._handle(method)
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001 - never drop the connection silently
            self.log_message("unhandled %s: %s", type(exc).__name__, exc)
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self):  # noqa: N802 - names fixed by BaseHTTPRequestHandler
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self):  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self):  # noqa: N802
        self._dispatch("DELETE")


def main() -> None:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    reconcile_interrupted_jobs()
    threading.Thread(target=worker, daemon=True).start()

    # Without a token there is nothing to authenticate with, so the API is kept
    # off the network and only nginx (the ingress web UI) can reach it.
    host = "0.0.0.0" if API_TOKEN else "127.0.0.1"
    print(f"[api] listening on {host}:{PORT}", flush=True)
    ThreadingHTTPServer((host, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
