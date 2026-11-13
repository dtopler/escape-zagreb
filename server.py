"""Local web app for Zagreb escape-room availability.

    python server.py [--port 8765] [--days 30]

Serves the static UI from ``web/`` plus a tiny JSON API:

    GET  /api/state       aggregated availability (data/availability.json)
    GET  /api/progress    progress of a running/previous scan
    POST /api/refresh     start a background scan {"days":31,"start":"2026-11-13",
                          "providers":["cluego"],"force":false}
    GET  /api/health      liveness + last scan info
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from aggregate import DATA, STATE_PATH, TZ, load_rooms, progress, scan_async
from providers import load_errors

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
_start_lock = threading.Lock()
_started_once = False


def read_state() -> dict | None:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "EscapeZagreb/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ helpers --
    def _send(self, status: int, body: bytes, ctype: str,
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, val in (extra or {}).items():
            self.send_header(key, val)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        if os.environ.get("ESCAPE_VERBOSE"):
            super().log_message(fmt, *args)

    def _serve_file(self, path: str) -> None:
        if not os.path.isfile(path):
            self._json(404, {"error": "not found", "path": path})
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json"):
            ctype += "; charset=utf-8"
        with open(path, "rb") as fh:
            body = fh.read()
        self._send(200, body, ctype)

    # --------------------------------------------------------------- verbs --
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path

        if path in ("/", "/index.html"):
            self._serve_file(os.path.join(WEB, "index.html"))
            return
        if path == "/api/state":
            state = read_state()
            if state is None:
                self._json(200, {"status": "empty", "progress": progress(),
                                 "rooms": load_rooms()})
            else:
                self._json(200, state)
            return
        if path == "/api/progress":
            self._json(200, progress())
            return
        if path == "/api/health":
            state = read_state() or {}
            self._json(200, {
                "ok": True,
                "generated_at": state.get("generated_at"),
                "window": state.get("window"),
                "adapter_errors": load_errors(),
            })
            return
        if path == "/api/rooms":
            self._json(200, load_rooms())
            return

        # static assets, sandboxed to web/
        target = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if target.startswith(WEB) and os.path.isfile(target):
            self._serve_file(target)
            return
        self._json(404, {"error": "not found"})

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return

        if url.path == "/api/refresh":
            current = progress()
            if current.get("running"):
                self._json(409, {"error": "scan already running", "progress": current})
                return
            days = int(payload.get("days") or 30)
            days = max(1, min(days, 120))
            scan_async(days_ahead=days, start=payload.get("start"),
                       providers=payload.get("providers") or None,
                       force=bool(payload.get("force")))
            self._json(202, {"started": True, "days": days,
                             "force": bool(payload.get("force"))})
            return

        self._json(404, {"error": "not found"})


def ensure_first_scan(days: int) -> None:
    """Kick off a scan the first time the app runs without any data."""
    global _started_once
    with _start_lock:
        if _started_once:
            return
        _started_once = True
        if read_state() is None and not progress().get("running"):
            print(f"  no cached data - scanning {days} days in the background ...")
            scan_async(days_ahead=days)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--days", type=int, default=30,
                        help="horizon for the automatic first scan")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    ensure_first_scan(args.days)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"\n  Escape Room Zagreb availability  ->  {url}")
    print(f"  data: {os.path.relpath(STATE_PATH, ROOT)}"
          f"   timezone: {TZ}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
