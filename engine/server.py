"""The engine's local HTTP API (127.0.0.1 only), used by the in-game panel.

Every request needs the token from engine.json in the data folder, as an
"X-Token" header or a "token" query parameter (so the panel page and <img>
tags can use it).

    GET  /ui                         the panel page
    GET  /api/status                 bridge state, loaded career, settings
    GET  /api/contacts               recent conversations and the squad
    GET  /api/person/<id>            person snapshot + relationship (?refresh=1 re-reads FM)
    GET  /api/messages/<id>          private conversation with that person
    POST /api/messages/<id>          {"text": "..."} -> the person's reply (takes a few seconds)
    GET  /api/settings               settings (the API key is never returned, only a hint)
    POST /api/settings               {"language", "provider", "api_key", "model", "base_url"}
    GET  /api/providers              model providers the panel can offer
    POST /api/models                 models available with the given (or saved) settings
    POST /api/test                   one tiny request with the given (or saved) settings
    GET  /face/<uid>                 FM portrait (PNG) from the graphics folder
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import dialogue, snapshot
from .bridge import DATA_DIR, GRAPHICS, Bridge, BridgeUnavailable, _text
from .career import CareerTracker
from .model import PRESETS, ModelError, detect_provider, provider_from_settings
from .native import NativeSync
from .story import StoryEngine
from .press import PressRoom
from .contacts import ContactEngine
from .social import SocialFeed
from . import graphics

PORT = 27271
LANGUAGES = ("zh", "en")
SETTINGS_FILE = DATA_DIR / "settings.json"
UI_DIR = Path(__file__).resolve().parent / "ui"
SETTING_KEYS = ("language", "provider", "api_key", "model", "base_url")


# --- settings ----------------------------------------------------------------------
def load_settings() -> dict:
    try:
        s = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        s = {}
    if s.get("language") not in LANGUAGES:
        s["language"] = "zh"
    if s.get("provider") not in PRESETS and s.get("provider") != "echo":
        s["provider"] = "claude-cli"
    return s


def save_settings(s: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(SETTINGS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds the API key
    with os.fdopen(fd, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.chmod(SETTINGS_FILE, 0o600)


def public_settings(s: dict) -> dict:
    """Settings as shown to the panel: the key only as a hint."""
    out = {k: s.get(k) for k in SETTING_KEYS if k != "api_key"}
    key = s.get("api_key") or ""
    out["api_key_set"] = bool(key)
    out["api_key_hint"] = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else ("…" if key else "")
    return out


def merge_settings(base: dict, body: dict) -> dict:
    """Applies panel input over saved settings. An empty api_key keeps the saved
    one; null clears it. Pasting a recognisable key selects its provider."""
    s = dict(base)
    for k in ("language", "provider", "model", "base_url"):
        if k in body and body[k] is not None:
            s[k] = str(body[k]).strip()
    if "api_key" in body:
        if body["api_key"] is None:
            s.pop("api_key", None)
        elif str(body["api_key"]).strip():
            s["api_key"] = str(body["api_key"]).strip()
            if "provider" not in body:
                s["provider"] = detect_provider(s["api_key"]) or s.get("provider")
    if s.get("language") not in LANGUAGES:
        raise ValueError("language must be zh or en")
    if s.get("provider") not in PRESETS:
        raise ValueError("unknown provider")
    return s


class Engine:
    def __init__(self):
        self.bridge = Bridge()
        self.careers = CareerTracker(self.bridge)
        self.settings = load_settings()
        self.token = secrets.token_hex(16)
        self.native = NativeSync(self.bridge)
        self.story = StoryEngine(self.bridge)
        self.press = PressRoom(self.bridge, self.native)
        self.contacts = ContactEngine(self.story)
        self.social = SocialFeed(self.story)
        self._squad_cache: tuple[str, str, list] | None = None  # (career key, date, squad)
        self._squad_lock = threading.Lock()

    def squad(self, career) -> list[dict]:
        """First-team players with name, position and portrait id (cached per game day)."""
        with self._squad_lock:
            c = self._squad_cache
            if c and c[0] == career.key and c[1] == career.date:
                return c[2]
            out = []
            for ref in self.bridge.team(career.team_id)["squad"]:
                raw = self.bridge.query(ref, ["eman", "dinu", "SspP"])
                out.append({"id": ref, "name": _text(raw.get("eman")) or ref,
                            "position": _text(raw.get("SspP")) or "",
                            "uid": raw.get("dinu") if isinstance(raw.get("dinu"), int) else None})
            self._squad_cache = (career.key, career.date, out)
            return out


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

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("JSON object expected")
            return body

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
                if method == "GET" and path == "/ui":
                    return self._send(200, (UI_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                if method == "GET" and path == "/api/status":
                    return self._status()
                if method == "GET" and path == "/api/settings":
                    return self._json(200, public_settings(engine.settings))
                if method == "POST" and path == "/api/settings":
                    engine.settings = merge_settings(engine.settings, self._body())
                    save_settings(engine.settings)
                    return self._json(200, public_settings(engine.settings))
                if method == "GET" and path == "/api/providers":
                    return self._json(200, {pid: {k: v for k, v in p.items() if k != "key_prefix"}
                                            for pid, p in PRESETS.items()})
                if method == "POST" and path == "/api/models":
                    return self._models(merge_settings(engine.settings, self._body()))
                if method == "POST" and path == "/api/test":
                    return self._test(merge_settings(engine.settings, self._body()))
                if method == "GET" and path == "/api/contacts":
                    return self._contacts()
                if method == "GET" and path == "/api/press/active":
                    status = engine.press.status()
                    try:
                        career = engine.careers.current()
                        status["messages"] = sum(1 for c in career.store.events("contact", limit=200)
                                                 if (c["payload"] or {}).get("status") == "unread")
                        status["social"] = len(engine.social.notifications(career))
                        status["unread"] = status["messages"] + status["social"]
                    except BridgeUnavailable:
                        status["unread"] = 0
                    return self._json(200, status)
                if method == "GET" and path == "/api/social":
                    career = engine.careers.current()
                    return self._json(200, {"posts": engine.social.posts(career), "fan_mood": engine.social.fan_mood(career),
                                            "pending": len(engine.social.pending(career)),
                                            "notifications": engine.social.notifications(career)})
                if method == "POST" and path == "/api/social/seen":
                    engine.social.mark_seen(engine.careers.current())
                    return self._json(200, {"ok": True})
                if method == "POST" and path == "/api/social/refresh":
                    model = self._model()
                    if model is None:
                        return self._json(502, {"error": "no model configured", "code": "model"})
                    return self._json(200, {"added": engine.social.refresh(engine.careers.current(), model)})
                if method == "POST" and path == "/api/social/reply":
                    body = self._body()
                    text = str(body.get("text") or "").strip()[:500]
                    if not text:
                        return self._json(400, {"error": "text required"})
                    model = self._model()
                    if model is None:
                        return self._json(502, {"error": "no model configured", "code": "model"})
                    try:
                        reply_to = body.get("reply_to") if isinstance(body.get("reply_to"), int) else None
                        return self._json(200, engine.social.reply(engine.careers.current(), model,
                                                                   body.get("key") or None, text, reply_to))
                    except ModelError as e:
                        return self._json(502, {"error": str(e), "code": "model"})
                if method == "GET" and path == "/api/inbox":
                    return self._inbox()
                if method == "POST" and path == "/api/inbox/open":
                    return self._open_contact()
                if method == "POST" and path == "/api/press/resume":
                    engine.press.resume()
                    return self._json(200, {"ok": True})
                if method == "GET" and path == "/api/press":
                    return self._press_state()
                if method == "POST" and path == "/api/press/answer":
                    return self._press_answer()
                if method == "POST" and path == "/api/press/dismiss":
                    engine.press.dismiss()
                    return self._json(200, {"ok": True})
                if method == "GET" and path == "/api/feed":
                    return self._feed(bool(query.get("all")))
                m = re.fullmatch(r"/api/person/([0-9a-f]{8})", path)
                if method == "GET" and m:
                    career = engine.careers.current()
                    snap = snapshot.get(engine.bridge, career, m.group(1), refresh=bool(query.get("refresh")))
                    msgs = career.store.messages(m.group(1))
                    rel = career.store.relation(m.group(1))
                    return self._json(200, snap | {"relation": rel, "message_count": len(msgs)})
                m = re.fullmatch(r"/api/messages/([0-9a-f]{8})", path)
                if method == "GET" and m:
                    return self._json(200, engine.careers.current().store.messages(m.group(1)))
                if method == "POST" and m:
                    return self._send_message(m.group(1))
                m = re.fullmatch(r"/logo/(club|comp)/(\d+)", path)
                if method == "GET" and m:
                    f = graphics.logo(m.group(1), int(m.group(2)))
                    if not f:
                        return self._json(404, {"error": "no logo"})
                    return self._send(200, f.read_bytes(), "image/png" if f.suffix == ".png" else "image/jpeg")
                m = re.fullmatch(r"/face/(\d+)", path)
                if method == "GET" and m:
                    png = GRAPHICS / f"faces/face_{m.group(1)}.png"
                    if not png.is_file():
                        return self._json(404, {"error": "no portrait"})
                    return self._send(200, png.read_bytes(), "image/png")
                return self._json(404, {"error": "unknown path"})
            except BridgeUnavailable as e:
                return self._json(503, {"error": str(e), "code": "bridge"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})

        def _status(self) -> None:
            out = {"settings": public_settings(engine.settings), "bridge": False, "career": None}
            try:
                out["career"] = engine.careers.current().summary()
                out["bridge"] = True
            except BridgeUnavailable as e:
                out["reason"] = str(e)
            self._json(200, out)

        def _contacts(self) -> None:
            career = engine.careers.current()
            squad = engine.squad(career)
            by_id = {p["id"]: p for p in squad}
            recent = []
            journalists = {e["payload"]["id"]: e["payload"] for e in career.store.events("journalist", limit=500)
                           if (e["payload"] or {}).get("id")}
            for person_id, last in career.store.recent_threads(20):
                j = journalists.get(person_id)
                p = by_id.get(person_id) or ({"id": person_id, "name": j.get("name") or person_id,
                                              "position": f"记者 · {j.get('outlet')}" if j.get("outlet") else "记者",
                                              "uid": j.get("uid")} if j else
                                             {"id": person_id, "name": person_id, "position": "", "uid": None})
                cached, _ = career.store.get_snapshot(person_id)
                if cached and person_id not in by_id:
                    p = {"id": person_id, "name": cached.get("name"), "position": cached.get("position") or "",
                         "uid": cached.get("uid")}
                recent.append(p | {"last": last})
            self._json(200, {"recent": recent, "squad": squad})

        def _feed(self, everything: bool) -> None:
            """Recent club results and news, newest first (routine inbox items only with ?all=1)."""
            career = engine.careers.current()
            items = []
            for e in career.store.events("press_answer", limit=60):
                p = e["payload"] or {}
                if not p.get("headline"):
                    continue
                j = p.get("journalist") or {}
                items.append({"kind": "news", "date": e["game_date"], "weight": 1.5, "title": p["headline"],
                              "body": f"{j.get('outlet', '')} {j.get('name', '')}：{p.get('question')}\n主教练：{p.get('answer')}",
                              "people": [{"id": j["id"], "name": j.get("name")}] if j.get("id") else []})
            for e in career.store.events("match", limit=40) + career.store.events("news", limit=200):
                p = e["payload"] or {}
                if e["kind"] == "news" and p.get("weight", 1) < 1 and not everything:
                    continue
                item = {"kind": e["kind"], "date": e["game_date"], "weight": p.get("weight", 1)}
                if e["kind"] == "match":
                    item |= {k: p.get(k) for k in ("home_name", "away_name", "score", "competition", "result",
                                                   "goals", "player_of_match", "player_of_match_name")}
                else:
                    names = p.get("names") or {}
                    people = [{"id": r, "name": names.get(r) or r} for r in (p.get("people") or [])[:6]]
                    item |= {"title": p.get("title"), "body": p.get("body"), "people": people}
                items.append(item)
            items.sort(key=lambda i: (i["date"] or "", i["kind"] == "match"), reverse=True)
            self._json(200, {"items": items[:120]})

        def _inbox(self) -> None:
            career = engine.careers.current()
            items = [{"key": c["event_key"], "date": c["game_date"], **(c["payload"] or {})}
                     for c in career.store.events("contact", limit=40)]
            items.reverse()
            self._json(200, {"items": items, "unread": sum(1 for i in items if i.get("status") == "unread")})

        def _open_contact(self) -> None:
            key = str(self._body().get("key") or "")
            career = engine.careers.current()
            c = career.store.event_by_key(key)
            if not c or c["kind"] != "contact":
                return self._json(404, {"error": "no such contact"})
            p = c["payload"] or {}
            if p.get("status") == "unread":
                model = self._model()
                if model is None:
                    return self._json(502, {"error": "no model configured", "code": "model"})
                snap = snapshot.get(engine.bridge, career, p["person"])
                try:
                    dialogue.open_contact(model, career, snap, p.get("reason") or "", engine.settings["language"])
                except ModelError as e:
                    return self._json(502, {"error": str(e), "code": "model"})
                career.store.update_event(key, p | {"status": "opened"})
            self._json(200, {"person": p["person"]})

        def _model(self):
            try:
                return provider_from_settings(engine.settings)
            except ModelError:
                return None

        def _press_state(self) -> None:
            self._json(200, engine.press.state(engine.careers.current(), self._model()))

        def _press_answer(self) -> None:
            text = str(self._body().get("text") or "").strip()[:1000]
            if not text:
                return self._json(400, {"error": "text required"})
            model = self._model()
            if model is None:
                return self._json(502, {"error": "no model configured", "code": "model"})
            self._json(200, engine.press.answer(engine.careers.current(), model, text))

        def _send_message(self, person_id: str) -> None:
            text = str(self._body().get("text") or "").strip()
            if not text:
                return self._json(400, {"error": "text required"})
            career = engine.careers.current()
            snap = snapshot.get(engine.bridge, career, person_id)
            try:
                reply = dialogue.converse(provider_from_settings(engine.settings), career, snap, text[:2000],
                                          engine.settings["language"])
            except ModelError as e:
                return self._json(502, {"error": str(e), "code": "model"})
            self._json(200, reply)

        def _models(self, settings: dict) -> None:
            try:
                provider = provider_from_settings(settings | {"model": settings.get("model") or "-"})
                lister = getattr(provider, "list_models", None)
                if not lister:
                    return self._json(200, {"models": [PRESETS[settings["provider"]].get("model")]})
                return self._json(200, {"models": lister()})
            except ModelError as e:
                return self._json(502, {"error": str(e)})

        def _test(self, settings: dict) -> None:
            schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}},
                      "additionalProperties": False}
            try:
                provider_from_settings(settings).complete("Reply with ok = true.", "ok?", schema)
            except ModelError as e:
                return self._json(200, {"ok": False, "error": str(e)})
            self._json(200, {"ok": True})

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


def background(engine: Engine, interval: float = 3.0) -> None:
    """Keeps the world in step with the game even when nobody uses the panel:
    notices saves and loads (memory checkpoints) and native FM interactions."""
    import time
    while True:
        try:
            career = engine.careers.current()
            engine.story.poll(career)
            engine.contacts.poll(career)
            engine.story.backfill_posts(career)
            engine.social.ingest_fm(career)
        except BridgeUnavailable:
            pass
        except Exception as e:  # never let the loop die
            print(f"background: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(interval)


def native_loop(engine: Engine, interval: float = 0.5) -> None:
    """FM's own dialogs (conversations, press conferences): read often enough to catch
    every question before it is answered. Cheap while no dialog is in use."""
    import time
    while True:
        try:
            engine.native.poll(engine.careers.current())
        except BridgeUnavailable:
            time.sleep(2)
        except Exception as e:  # never let the loop die
            print(f"native: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(interval)


def serve() -> None:
    engine = Engine()
    threading.Thread(target=background, args=(engine,), daemon=True, name="background").start()
    threading.Thread(target=native_loop, args=(engine,), daemon=True, name="native").start()
    threading.Thread(target=graphics.logo, args=("club", 0), daemon=True, name="logos").start()  # build the index
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(engine))  # never reachable from the network
    path = write_api_file(engine)
    print(f"FM27Real engine on http://127.0.0.1:{PORT} (token in {path})", file=sys.stderr)
    httpd.serve_forever()
