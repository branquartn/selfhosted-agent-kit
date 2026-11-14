"""End-to-end HTTP API tests (real server on an ephemeral port)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from agent.engine import Engine
from agent.llm import StreamResult
from agent.server import make_server

AUTH = {"Authorization": "Bearer secret-token"}


@pytest.fixture()
def server(cfg, fake_llm):
    llm = fake_llm([StreamResult(content="Hello from the test model.")])
    cfg.port = 0
    httpd = make_server(cfg, Engine(cfg, llm=llm))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, cfg
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def request(base, path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_healthz_is_public(server):
    base, _ = server
    status, body = request(base, "/healthz")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_api_requires_token(server):
    base, _ = server
    status, body = request(base, "/api/config")
    assert status == 401
    assert json.loads(body)["error"] == "unauthorized"


def test_api_accepts_valid_token(server):
    base, cfg = server
    status, body = request(base, "/api/config", headers=AUTH)
    assert status == 200
    payload = json.loads(body)
    assert payload["model"] == cfg.model
    assert payload["api_key_set"] is True
    assert "api_key" not in payload


def test_x_agent_token_header_also_works(server):
    base, _ = server
    status, _ = request(base, "/api/config", headers={"X-Agent-Token": "secret-token"})
    assert status == 200


def test_wrong_token_rejected(server):
    base, _ = server
    status, _ = request(base, "/api/config", headers={"Authorization": "Bearer nope"})
    assert status == 401


def test_index_is_served(server):
    base, _ = server
    status, body = request(base, "/")
    assert status == 200
    assert "SelfHosted Agent" in body


def test_unknown_static_path_is_404(server):
    base, _ = server
    status, _ = request(base, "/etc/passwd")
    assert status == 404


def test_chat_streams_sse(server):
    base, _ = server
    status, body = request(base, "/api/chat", "POST", {"message": "hello"}, AUTH)
    assert status == 200
    events = [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:") and "[DONE]" not in line]
    types = [event["type"] for event in events]
    assert types[-1] == "final"
    final = events[-1]
    assert "Hello from the test model." in final["text"]
    assert final["session_id"]


def test_chat_rejects_empty_message(server):
    base, _ = server
    status, body = request(base, "/api/chat", "POST", {"message": "  "}, AUTH)
    assert status == 400


def test_chat_rejects_bad_session_id(server):
    base, _ = server
    status, _ = request(base, "/api/chat", "POST", {"message": "hi", "session_id": "../x"}, AUTH)
    assert status == 400


def test_session_lifecycle(server):
    base, _ = server
    status, body = request(base, "/api/sessions", "POST", {}, AUTH)
    assert status == 201
    session_id = json.loads(body)["id"]

    status, body = request(base, "/api/sessions", headers=AUTH)
    assert status == 200
    assert any(item["id"] == session_id for item in json.loads(body)["sessions"])

    status, body = request(base, f"/api/sessions/{session_id}", headers=AUTH)
    assert status == 200
    assert json.loads(body)["id"] == session_id

    status, _ = request(base, f"/api/sessions/{session_id}", "DELETE", headers=AUTH)
    assert status == 200
    status, _ = request(base, f"/api/sessions/{session_id}", headers=AUTH)
    assert status == 404


def test_memory_api(server):
    base, _ = server
    status, body = request(
        base,
        "/api/memory",
        "POST",
        {"name": "api-memory", "content": "stored through HTTP", "type": "project", "description": "test"},
        AUTH,
    )
    assert status == 201
    assert json.loads(body)["name"] == "api-memory"

    status, body = request(base, "/api/memory", headers=AUTH)
    assert status == 200
    names = [item["name"] for item in json.loads(body)["memories"]]
    assert "api-memory" in names

    status, _ = request(base, "/api/memory/api-memory", "DELETE", headers=AUTH)
    assert status == 200
    status, _ = request(base, "/api/memory/api-memory", "DELETE", headers=AUTH)
    assert status == 404


def test_invalid_json_body(server):
    base, _ = server
    req = urllib.request.Request(
        base + "/api/memory",
        data=b"{not json",
        method="POST",
        headers={"Content-Type": "application/json", **AUTH},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_message_too_long_is_rejected(server):
    base, _ = server
    status, body = request(base, "/api/chat", "POST", {"message": "x" * 32_001}, AUTH)
    assert status == 400
    assert "too long" in body


def test_session_lock_prevents_parallel_streams():
    from agent.server import _session_lock

    lock = _session_lock("shared")
    assert lock.acquire(blocking=False) is True
    assert lock.acquire(blocking=False) is False
    lock.release()
    assert lock.acquire(blocking=False) is True
    lock.release()
