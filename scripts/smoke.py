"""End-to-end smoke test for SelfHosted Agent Kit.

Starts a *fake* OpenAI-compatible provider (streaming SSE) plus the real agent
server, then drives one chat turn that forces a tool call. No API key, no
external network — verifies that config, prompting, tool execution, session
persistence and SSE plumbing work together.

    python3 scripts/smoke.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "smoke-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeProvider(BaseHTTPRequestHandler):
    """Streams scripted OpenAI responses: tool call first, then a final answer."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the output clean
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        messages = request.get("messages") or []
        has_tool_result = any(message.get("role") == "tool" for message in messages)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send(chunk: dict) -> None:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()

        if not has_tool_result:
            # Ask for a tool call, fragmented across chunks like a real provider.
            send(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_smoke",
                                        "function": {
                                            "name": "write_file",
                                            "arguments": '{"path": "smoke.txt", "content": ',
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            )
            send(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": '"hello from smoke"}'}}
                                ]
                            }
                        }
                    ]
                }
            )
        else:
            for chunk in ("The file ", "smoke.txt ", "now exists."):
                send({"choices": [{"delta": {"content": chunk}}]})
            send({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 42}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def wait_for(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"timeout waiting for {url}")


def main() -> int:
    workdir = ROOT / ".smoke"
    shutil.rmtree(workdir, ignore_errors=True)
    (workdir / "workspace").mkdir(parents=True)

    provider_port = free_port()
    provider = ThreadingHTTPServer(("127.0.0.1", provider_port), FakeProvider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()

    agent_port = free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "LLM_BASE_URL": f"http://127.0.0.1:{provider_port}/v1",
        "LLM_API_KEY": "smoke-key",
        "LLM_MODEL": "smoke-model",
        "AGENT_TOKEN": TOKEN,
        "AGENT_HOST": "127.0.0.1",
        "AGENT_PORT": str(agent_port),
        "AGENT_DATA_DIR": str(workdir / "var"),
        "AGENT_WORKSPACE": str(workdir / "workspace"),
        "AGENT_WEB_DIR": str(ROOT / "web"),
        "AGENT_MAX_STEPS": "4",
        "AGENT_APPROVAL": "auto",
    }
    agent = subprocess.Popen(
        [sys.executable, "-m", "agent.server"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{agent_port}"
    failures: list[str] = []
    try:
        wait_for(f"{base}/healthz")

        # 1. healthz + auth
        request = urllib.request.Request(f"{base}/api/config")
        try:
            urllib.request.urlopen(request, timeout=5)
            failures.append("unauthenticated /api/config was allowed")
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                failures.append(f"expected 401 without token, got {exc.code}")

        request = urllib.request.Request(
            f"{base}/api/config", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            config = json.loads(response.read())
        if config["model"] != "smoke-model":
            failures.append("config endpoint did not report the configured model")

        # 2. one chat turn that must trigger a tool call
        payload = json.dumps({"message": "create smoke.txt"}).encode()
        request = urllib.request.Request(
            f"{base}/api/chat",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
        events = [
            json.loads(line[5:])
            for line in body.splitlines()
            if line.startswith("data:") and "[DONE]" not in line
        ]
        types = [event["type"] for event in events]
        for expected in ("tool_call", "tool_result", "final"):
            if expected not in types:
                failures.append(f"missing SSE event {expected!r} (got {types})")
        final = events[-1]
        if "smoke.txt" not in (final.get("text") or ""):
            failures.append(f"unexpected final text: {final.get('text')!r}")

        # 3. the tool actually wrote the file inside the workspace
        created = workdir / "workspace" / "smoke.txt"
        if not created.is_file() or "hello from smoke" not in created.read_text():
            failures.append("write_file tool did not create workspace/smoke.txt")

        # 4. the session was persisted and is listed
        request = urllib.request.Request(
            f"{base}/api/sessions", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            sessions = json.loads(response.read())["sessions"]
        if not any(item["id"] == final["session_id"] for item in sessions):
            failures.append("session was not persisted")

        # 5. static UI is served
        with urllib.request.urlopen(f"{base}/", timeout=5) as response:
            if "SelfHosted Agent" not in response.read().decode():
                failures.append("index.html not served")

    finally:
        agent.terminate()
        try:
            agent.wait(timeout=10)
        except subprocess.TimeoutExpired:
            agent.kill()
        provider.shutdown()
        provider.server_close()
        shutil.rmtree(workdir, ignore_errors=True)

    if failures:
        print("SMOKE FAILED")
        for failure in failures:
            print(" -", failure)
        return 1
    print("SMOKE OK — healthz, auth, SSE chat, tool call, session, static UI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
