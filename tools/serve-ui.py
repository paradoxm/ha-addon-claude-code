#!/usr/bin/env python3
"""Serve the add-on's UI on this machine, so a layout can be looked at.

Runs the real `api.py` against a throwaway /data with `stub-claude.py` standing in
for the CLI, and serves `www/` in front of it the way nginx does in the image —
static files at the root, the API under /api/. Nothing here ships.

    python3 tools/serve-ui.py            # http://127.0.0.1:8099
    python3 tools/serve-ui.py 9000

The point is the pixels: jsdom can drive the page but cannot show it, and this
add-on's UI has been broken twice by things that only a rendered page reveals.
"""

import http.client
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADDON = HERE.parent / "claude-code"
WWW = ADDON / "www"

DATA = Path(tempfile.mkdtemp(prefix="serve-ui-"))
(DATA / "home" / ".claude").mkdir(parents=True)
(DATA / "options.json").write_text(
    json.dumps(
        {
            "model": "opus",
            "effort": "medium",
            "permission_mode": "manual",
            "api_token": "",
            "timeout_minutes": 5,
            "auto_update": True,
            "update_channel": "latest",
        }
    )
)
BIN = DATA / "bin"
BIN.mkdir()
shutil.copy(HERE / "stub-claude.py", BIN / "claude")
(BIN / "claude").chmod(0o755)
os.environ["PATH"] = f"{BIN}{os.pathsep}{os.environ['PATH']}"
os.environ["ADDON_DATA"] = str(DATA)

sys.path.insert(0, str(ADDON))
import api  # noqa: E402  the environment above has to be in place first

if os.environ.get("FAKE_USAGE"):
    # This machine has no sign-in, so the real reading is "not available" and the usage
    # tab would only ever be looked at in its empty state.
    import datetime

    def _reading(force: bool = False) -> dict:
        soon = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=136)
        later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3)
        session: dict[str, float | str] = {
            "percent": float(os.environ.get("FAKE_SESSION", 34)),
            "threshold": float(api.SESSION_THRESHOLD), "resets_at": soon.isoformat()}
        week: dict[str, float | str] = {
            "percent": float(os.environ.get("FAKE_WEEK", 8)),
            "threshold": float(api.WEEK_THRESHOLD), "resets_at": later.isoformat()}
        room = lambda w: float(w["threshold"]) - float(w["percent"])  # noqa: E731
        # The one with the least room left to its own figure, as the add-on decides it.
        worst = min(({"kind": "session", **session}, {"kind": "week", **week}), key=room)
        return {"available": True, "session": session, "week": week, "worst": worst,
                "thresholds": {"session": api.SESSION_THRESHOLD, "week": api.WEEK_THRESHOLD},
                "enough": all(room(w) > 0 for w in (session, week)),
                "checked_at": api.now()}

    api.read_usage = _reading

API_PORT = 7699


class Proxy(SimpleHTTPRequestHandler):
    """Static files, with /api/ handed to the add-on — nginx's job, in miniature."""

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        if not self.path.startswith("/api/"):
            if method == "GET":
                return SimpleHTTPRequestHandler.do_GET(self)
            self.send_error(405)
            return None

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        upstream = http.client.HTTPConnection("127.0.0.1", API_PORT, timeout=120)
        upstream.request(
            method,
            self.path[4:] or "/",
            body=body,
            headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
        )
        answer = upstream.getresponse()
        payload = answer.read()
        self.send_response(answer.status)
        self.send_header("Content-Type", answer.getheader("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        upstream.close()
        return None

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            return
        print(f"[ui] {fmt % args}", flush=True)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    api.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    api.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    api.CHAT_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=api.worker, daemon=True).start()
    threading.Thread(
        target=ThreadingHTTPServer(("127.0.0.1", API_PORT), api.Handler).serve_forever,
        daemon=True,
    ).start()

    socketserver.TCPServer.allow_reuse_address = True
    handler = partial(Proxy, directory=str(WWW))
    print(f"[ui] http://127.0.0.1:{port}  (data in {DATA})", flush=True)
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        server.serve_forever()


if __name__ == "__main__":  # pragma: no cover - a development helper
    main()
