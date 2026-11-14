"""Interactive approval of risky tools (write_file / run_shell)."""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from agent.engine import Engine
from agent.llm import StreamResult
from agent.server import make_server

AUTH = {"Authorization": "Bearer secret-token", "Content-Type": "application/json"}


def _call(name, args, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


@pytest.fixture()
def live(cfg, fake_llm):
    def build(script):
        cfg.port = 0
        cfg.approval = "ask"
        cfg.approval_timeout = 5
        httpd = make_server(cfg, Engine(cfg, llm=fake_llm(script)))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"

    made = []

    def factory(script):
        httpd, base = build(script)
        made.append(httpd)
        return base

    yield factory
    for httpd in made:
        httpd.shutdown()
        httpd.server_close()


def _stream_chat(base, message, on_event):
    req = urllib.request.Request(
        base + "/api/chat",
        data=json.dumps({"message": message}).encode(),
        headers=AUTH,
        method="POST",
    )
    events = []
    with urllib.request.urlopen(req, timeout=20) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            event = json.loads(line[6:])
            events.append(event)
            on_event(event)
    return events


def _approve(base, call_id, ok):
    req = urllib.request.Request(
        base + "/api/approve",
        data=json.dumps({"id": call_id, "ok": ok}).encode(),
        headers=AUTH,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _script(path="out.txt"):
    return [
        StreamResult(tool_calls=[_call("write_file", {"path": path, "content": "hi"})]),
        StreamResult(content="done"),
    ]


def test_write_waits_for_approval_then_runs(live, cfg):
    base = live(_script())
    events = _stream_chat(base, "write", lambda e: e["type"] == "approval" and _approve(base, e["id"], True))
    assert any(e["type"] == "approval" for e in events)
    assert (cfg.workspace / "out.txt").read_text() == "hi"


def test_refusal_blocks_the_action(live, cfg):
    base = live(_script())
    events = _stream_chat(base, "write", lambda e: e["type"] == "approval" and _approve(base, e["id"], False))
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["is_error"] and "refused" in result["output"]
    assert not (cfg.workspace / "out.txt").exists()


def test_timeout_counts_as_refusal(live, cfg):
    base = live(_script())
    cfg.approval_timeout = 1
    start = time.time()
    events = _stream_chat(base, "write", lambda e: None)
    assert time.time() - start < 4
    assert not (cfg.workspace / "out.txt").exists()
    assert next(e for e in events if e["type"] == "tool_result")["is_error"]


def test_read_only_tools_need_no_approval(live):
    base = live([StreamResult(tool_calls=[_call("list_dir", {"path": "."})]), StreamResult(content="ok")])
    events = _stream_chat(base, "look", lambda e: None)
    assert not any(e["type"] == "approval" for e in events)


def test_auto_mode_skips_approval(live, cfg):
    base = live(_script())
    cfg.approval = "auto"
    events = _stream_chat(base, "write", lambda e: None)
    assert not any(e["type"] == "approval" for e in events)
    assert (cfg.workspace / "out.txt").exists()


def test_approve_unknown_id_is_404(live):
    base = live([StreamResult(content="x")])
    req = urllib.request.Request(base + "/api/approve", data=b'{"id":"nope","ok":true}', headers=AUTH, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 404
