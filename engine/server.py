"""The engine's local HTTP API (127.0.0.1 only), used by the in-game panel.

Every request needs the token from engine.json in the data folder, as an
"X-Token" header or a "token" query parameter (so <img> tags can use it).

    GET  /api/status                 bridge state, loaded career, settings
    GET  /api/person/<id>            person snapshot (?refresh=1 to re-read FM)
    GET  /api/messages/<id>          private conversation with that person
    GET  /api/settings               {"language": "zh" | "en"}
    POST /api/settings               {"language": "en"}
    GET  /face/<uid>                 FM portrait (PNG) from the graphics folder
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import snapshot
from .bridge import DATA_DIR, GRAPHICS, Bridge, BridgeUnavailable
from .career import CareerTracker

PORT = 27271
LANGUAGES = ("zh", "en")
SETTINGS_FILE = DATA_DIR / "settings.json"


def load_settings() -> dict:
    try:
        s = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        s = {}
    if s.get("language") not in LANGUAGES:
        s["language"] = "zh"
    return s


def save_settings(s: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1))


class Engine:
    def __init__(self):
        self.bridge = Bridge()
        self.careers = CareerTracker(self.bridge)
        self.settings = load_settings()
        self.token = secrets.token_hex(16)


def make_handler(engine: Engine):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FM27Real"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _authorised(self, query: dict) -> bool:
            token = self.headers.get("X-Token") or (query.get("token") or [""])[0]
            return secrets.compare_digest(token, engine.token)

        def _route(self, method: str) -> None:
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            if not self._authorised(query):
                return self._json(401, {"error": "bad token"})
            path = url.path
            try:
                if method == "GET" and path == "/api/status":
                    return self._status()
                if method == "GET" and path == "/api/settings":
                    return self._json(200, engine.settings)
                if method == "POST" and path == "/api/settings":
                    return self._update_settings()
                m = re.fullmatch(r"/api/person/([0-9a-f]{8})", path)
                if method == "GET" and m:
                    career = engine.careers.current()
                    snap = snapshot.get(engine.bridge, career, m.group(1), refresh=bool(query.get("refresh")))
                    return self._json(200, snap)
                m = re.fullmatch(r"/api/messages/([0-9a-f]{8})", path)
                if method == "GET" and m:
                    career = engine.careers.current()
                    return self._json(200, career.store.messages(m.group(1)))
                m = re.fullmatch(r"/face/(\d+)", path)
                if method == "GET" and m:
                    png = GRAPHICS / f"faces/face_{m.group(1)}.png"
                    if not png.is_file():
                        return self._json(404, {"error": "no portrait"})
                    return self._send(200, png.read_bytes(), "image/png")
                return self._json(404, {"error": "unknown path"})
            except BridgeUnavailable as e:
                return self._json(503, {"error": str(e)})

        def _status(self) -> None:
            out = {"settings": engine.settings, "bridge": False, "career": None}
            try:
                out["career"] = engine.careers.current().summary()
                out["bridge"] = True
            except BridgeUnavailable as e:
                out["reason"] = str(e)
            self._json(200, out)

        def _update_settings(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid JSON"})
            if "language" in body:
                if body["language"] not in LANGUAGES:
                    return self._json(400, {"error": f"language must be one of {LANGUAGES}"})
                engine.settings["language"] = body["language"]
            save_settings(engine.settings)
            self._json(200, engine.settings)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler


def write_api_file(engine: Engine) -> Path:
    path = DATA_DIR / "engine.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # token: this user only
    with os.fdopen(fd, "w") as f:
        json.dump({"url": f"http://127.0.0.1:{PORT}", "token": engine.token}, f)
    return path


def serve() -> None:
    engine = Engine()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(engine))  # never reachable from the network
    path = write_api_file(engine)
    print(f"FM27Real engine on http://127.0.0.1:{PORT} (token in {path})", file=sys.stderr)
    httpd.serve_forever()
