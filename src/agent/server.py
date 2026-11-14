"""HTTP server: static UI plus a small authenticated JSON/SSE API.

Routes::

    GET  /                     → web UI (public, asks for the token)
    GET  /chat.js /chat.css    → static assets
    GET  /healthz              → liveness probe (public)
    GET  /api/config           → redacted configuration
    GET  /api/sessions         → list conversations
    POST /api/sessions         → create conversation
    GET  /api/sessions/<id>    → load one conversation
    DELETE /api/sessions/<id>  → delete conversation
    POST /api/chat             → SSE stream: {session_id, message}
    GET  /api/memory           → list memories
    POST /api/memory           → save a memory {name, content, type, description}
    DELETE /api/memory/<name>  → delete a memory

Auth: `Authorization: Bearer <AGENT_TOKEN>` or `X-Agent-Token: <AGENT_TOKEN>`.
Everything under `/api/` requires the token; the UI itself is public but inert
without it.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import posixpath
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .config import Config
from .engine import Engine, valid_session_id
from .memory import MemoryError, MemoryStore

MAX_BODY_BYTES = 2_000_000
MAX_MESSAGE_CHARS = 32_000
STATIC_FILES = frozenset({"index.html", "chat.js", "chat.css", "logo.svg"})

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _session_lock(session_id: str) -> threading.Lock:
    """One lock per conversation: two tabs on different chats never block."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(session_id, threading.Lock())


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class AgentHandler(BaseHTTPRequestHandler):
    server_version = "SelfHostedAgentKit/1.0"
    protocol_version = "HTTP/1.1"

    # injected by make_server
    cfg: Config
    engine: Engine

    # ----------------------------------------------------------------- utils
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write("[agent] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _authorized(self) -> bool:
        expected = self.cfg.token
        if not expected:
            return False
        header = self.headers.get("Authorization") or ""
        token = ""
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
        if not token:
            token = (self.headers.get("X-Agent-Token") or "").strip()
        return bool(token) and hmac.compare_digest(token, expected)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._send_json(401, {"error": "unauthorized", "hint": "send Authorization: Bearer <AGENT_TOKEN>"})
        return False

    # -------------------------------------------------------------- routing
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if path.startswith("/api/"):
            if not self._require_auth():
                return
            self._route_get_api(path)
            return
        self._serve_static(path)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._require_auth():
            return
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        if path == "/api/chat":
            self._handle_chat(payload)
        elif path == "/api/sessions":
            session = self.engine.sessions.create(title=str(payload.get("title") or ""))
            self._send_json(201, session)
        elif path == "/api/memory":
            self._handle_memory_save(payload)
        elif path == "/api/approve":
            ok = self.engine.approvals.resolve(str(payload.get("id") or ""), bool(payload.get("ok")))
            self._send_json(200 if ok else 404, {"ok": ok})
        else:
            self._send_json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._require_auth():
            return
        if path.startswith("/api/sessions/"):
            session_id = path.rsplit("/", 1)[-1]
            if not valid_session_id(session_id):
                self._send_json(400, {"error": "invalid session id"})
                return
            deleted = self.engine.sessions.delete(session_id)
            self._send_json(200 if deleted else 404, {"deleted": deleted})
        elif path.startswith("/api/memory/"):
            name = unquote(path.rsplit("/", 1)[-1])
            deleted = MemoryStore(self.cfg.memory_dir).delete(name)
            self._send_json(200 if deleted else 404, {"deleted": deleted})
        else:
            self._send_json(404, {"error": "not found"})

    # ------------------------------------------------------------------ api
    def _route_get_api(self, path: str) -> None:
        if path == "/api/config":
            self._send_json(200, self.cfg.redacted())
        elif path == "/api/sessions":
            self._send_json(200, {"sessions": self.engine.sessions.list()})
        elif path.startswith("/api/sessions/"):
            session_id = path.rsplit("/", 1)[-1]
            if not valid_session_id(session_id):
                self._send_json(400, {"error": "invalid session id"})
                return
            session = self.engine.sessions.load(session_id)
            if session is None:
                self._send_json(404, {"error": "unknown session"})
            else:
                self._send_json(200, session)
        elif path == "/api/memory":
            memories = [
                {
                    "name": m.name,
                    "type": m.type,
                    "description": m.description,
                    "updated": m.updated,
                    "chars": len(m.content),
                }
                for m in MemoryStore(self.cfg.memory_dir).all()
            ]
            self._send_json(200, {"memories": memories})
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_memory_save(self, payload: dict[str, Any]) -> None:
        try:
            memory = MemoryStore(self.cfg.memory_dir).save(
                name=str(payload.get("name") or ""),
                content=str(payload.get("content") or ""),
                mem_type=str(payload.get("type") or "reference"),
                description=str(payload.get("description") or ""),
            )
        except MemoryError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(201, {"name": memory.name, "type": memory.type, "updated": memory.updated})

    def _handle_chat(self, payload: dict[str, Any]) -> None:
        message = str(payload.get("message") or "").strip()
        if not message:
            self._send_json(400, {"error": "message must not be empty"})
            return
        if len(message) > MAX_MESSAGE_CHARS:
            self._send_json(
                400, {"error": f"message too long (max {MAX_MESSAGE_CHARS} chars)"}
            )
            return
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            session_id = self.engine.sessions.create()["id"]
        elif not valid_session_id(session_id):
            self._send_json(400, {"error": "invalid session_id"})
            return

        lock = _session_lock(session_id)
        if not lock.acquire(blocking=False):
            # Same conversation already streaming: reject instead of queueing a
            # request that would hang silently for minutes.
            self._send_json(
                409, {"error": "this conversation is already processing a message"}
            )
            return

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            # Close the connection so clients see a clean end-of-stream: with
            # keep-alive the browser/urllib body reader would wait for more data.
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True

            def send(event: dict[str, Any]) -> None:
                try:
                    self.wfile.write(b"data: " + _json_bytes(event) + b"\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    raise KeyboardInterrupt("client disconnected")

            try:
                self.engine.run(session_id, message, on_event=send, interactive=True)
            except KeyboardInterrupt:
                self.log_message("client disconnected mid-stream")
            except Exception as exc:  # keep the stream contract even on crash
                self.log_message("chat failed: %s", exc)
                traceback.print_exc()
                send({"type": "error", "message": "internal error (see server logs)"})
                send(
                    {
                        "type": "final",
                        "session_id": session_id,
                        "text": "",
                        "error": "internal error",
                    }
                )
            finally:
                try:
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
        finally:
            lock.release()

    # -------------------------------------------------------------- static
    def _serve_static(self, path: str) -> None:
        rel = posixpath.normpath(unquote(path)).lstrip("/")
        if rel in ("", "."):
            rel = "index.html"
        if rel not in STATIC_FILES:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        target = (self.cfg.web_dir / rel).resolve()
        if not target.is_file():
            self.log_message("missing UI asset: %s", target)
            self._send(500, b"UI asset missing (see server logs)", "text/plain; charset=utf-8")
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or target.suffix in (".js", ".css", ".svg"):
            content_type += "; charset=utf-8"
        self._send(200, target.read_bytes(), content_type)


def make_server(cfg: Config, engine: Engine | None = None) -> ThreadingHTTPServer:
    """Create (but do not start) the HTTP server."""
    cfg.ensure_dirs()
    handler = type("BoundAgentHandler", (AgentHandler,), {})
    handler.cfg = cfg
    handler.engine = engine or Engine(cfg)
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parents[2]
    env_path = Path(os.environ.get("AGENT_ENV_FILE", root / ".env"))
    if "--check" in argv:
        cfg = Config.from_env(env_path)
        problems = cfg.validate()
        print(json.dumps({"config": cfg.redacted(), "problems": problems}, indent=2))
        return 1 if problems else 0

    cfg = Config.from_env(env_path)
    if not cfg.web_dir.is_dir():
        cfg.web_dir = root / "web"
    cfg.ensure_dirs()
    problems = cfg.validate()
    for problem in problems:
        sys.stderr.write(f"[agent] config warning: {problem}\n")

    httpd = make_server(cfg, Engine(cfg))
    host, port = httpd.server_address[0], httpd.server_address[1]
    print(f"[agent] model={cfg.model} workspace={cfg.workspace}")
    print(f"[agent] listening on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[agent] shutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
