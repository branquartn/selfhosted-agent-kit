"""Conversation storage and the agent loop.

The loop is deliberately small and readable: one model round, zero or more
tool calls, repeat until the model answers without tools or the step budget is
exhausted. Every step emits a structured event so the web UI (or any SSE
client) can render progress live.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import tools as tools_mod
from .config import Config
from .llm import LLMClient, LLMError, StreamResult
from .memory import MemoryStore

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_STORED_MESSAGES = 400

EventHandler = Callable[[dict[str, Any]], None]

DEFAULT_SYSTEM_PROMPT = """You are a self-hosted coding and operations agent running on the user's own machine.

Working rules:
- Inspect before you change anything: read the current file or run a read-only command first.
- Use the tools you are given; never invent file contents or command output.
- Make minimal, focused edits and state clearly which files you touched.
- Answer in the user's language, concisely, and lead with the result.
- If a tool returns an `error:` line, read it and adapt instead of retrying blindly.
- Never print secrets, tokens or private keys, even if you read them.

Workspace root: {workspace}
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def valid_session_id(value: str) -> bool:
    return bool(value) and bool(SESSION_ID_RE.match(value))


class SessionStore:
    """JSON-file persistence for conversations (one file per session)."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, session_id: str) -> Path:
        if not valid_session_id(session_id):
            raise ValueError(f"invalid session id: {session_id!r}")
        return self.directory / f"{session_id}.json"

    def create(self, title: str = "") -> dict[str, Any]:
        self.directory.mkdir(parents=True, exist_ok=True)
        session = {
            "id": uuid.uuid4().hex[:16],
            "title": title or "New conversation",
            "created": _now(),
            "updated": _now(),
            "messages": [],
        }
        self.save(session)
        return session

    def save(self, session: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        session["updated"] = _now()
        messages = list(session.get("messages") or [])
        if len(messages) > MAX_STORED_MESSAGES:
            messages = messages[-MAX_STORED_MESSAGES:]
        # A `tool` message must follow the assistant message that requested it,
        # otherwise the next request is rejected by the provider (400). Drop the
        # orphans created by truncation, at both ends.
        while messages and messages[0].get("role") == "tool":
            messages.pop(0)
        while messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
            messages.pop()
        session["messages"] = messages
        target = self._path(session["id"])
        fd, tmp_name = tempfile.mkstemp(dir=self.directory, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(session, ensure_ascii=False, indent=1))
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def load(self, session_id: str) -> dict[str, Any] | None:
        path = self._path(session_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def list(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.directory.is_dir():
            return items
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            items.append(
                {
                    "id": data.get("id", path.stem),
                    "title": data.get("title", "New conversation"),
                    "updated": data.get("updated", ""),
                    "messages": len(data.get("messages") or []),
                }
            )
        return sorted(items, key=lambda item: item.get("updated", ""), reverse=True)

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if path.is_file():
            path.unlink()
            return True
        return False


RISKY_TOOLS = frozenset({"run_shell", "write_file"})


class Approvals:
    """Pending user confirmations, resolved from a second HTTP request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, list[Any]] = {}

    def wait(self, call_id: str, timeout: float) -> bool:
        event = threading.Event()
        slot: list[Any] = [event, False]
        with self._lock:
            self._pending[call_id] = slot
        try:
            event.wait(timeout)
            return bool(slot[1])
        finally:
            with self._lock:
                self._pending.pop(call_id, None)

    def resolve(self, call_id: str, ok: bool) -> bool:
        with self._lock:
            slot = self._pending.get(call_id)
        if slot is None:
            return False
        slot[1] = bool(ok)
        slot[0].set()
        return True


class Engine:
    """Runs the model/tool loop."""

    def __init__(self, cfg: Config, llm: LLMClient | None = None) -> None:
        self.cfg = cfg
        self.llm = llm or LLMClient(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
        )
        self.approvals = Approvals()
        self.sessions = SessionStore(cfg.sessions_dir)
        self.memories = MemoryStore(cfg.memory_dir)

    # ------------------------------------------------------------- prompting
    def system_prompt(self) -> str:
        base = self.cfg.system_prompt or DEFAULT_SYSTEM_PROMPT.format(workspace=self.cfg.workspace)
        index = self.memories.index_text()
        if index:
            base += (
                "\n## Long-term memory index\n"
                "These memories are persisted between conversations. Save new durable facts "
                "with `memory_save`; reuse the same name to update one.\n"
                f"{index}\n"
            )
        return base

    def _trim(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop the oldest exchanges to stay under the context budget."""
        budget = self.cfg.context_chars
        trimmed = list(messages)
        while len(trimmed) > 2 and sum(len(json.dumps(m, ensure_ascii=False)) for m in trimmed) > budget:
            # Remove the oldest user/assistant pair, never a dangling tool result.
            trimmed.pop(1)
            while len(trimmed) > 1 and trimmed[1].get("role") == "tool":
                trimmed.pop(1)
        return trimmed

    # ------------------------------------------------------------- main loop
    def run(
        self,
        session_id: str,
        user_message: str,
        on_event: EventHandler | None = None,
        interactive: bool = False,
    ) -> dict[str, Any]:
        emit = on_event or (lambda event: None)
        if not valid_session_id(session_id):
            raise ValueError(f"invalid session id: {session_id!r}")
        session = self.sessions.load(session_id)
        if session is None:
            session = {
                "id": session_id,
                "title": user_message.strip().splitlines()[0][:60] or "New conversation",
                "created": _now(),
                "updated": _now(),
                "messages": [],
            }
        if not session.get("messages") and user_message:
            session["title"] = user_message.strip().splitlines()[0][:60] or "New conversation"

        history: list[dict[str, Any]] = list(session.get("messages") or [])
        history.append({"role": "user", "content": user_message})

        system_prompt = self.system_prompt()
        texts: list[str] = []
        tool_trace: list[dict[str, Any]] = []
        error: str | None = None
        steps = 0

        try:
            steps, error = self._loop(
                history, system_prompt, texts, tool_trace, emit, initial_error=error,
                interactive=interactive,
            )
        except (KeyboardInterrupt, GeneratorExit):
            # Streaming client went away: keep what we have, then let it bubble.
            self._finish(session, history, texts, tool_trace, "client disconnected", steps, lambda event: None)
            raise
        return self._finish(session, history, texts, tool_trace, error, steps, emit)

    def _loop(
        self,
        history: list[dict[str, Any]],
        system_prompt: str,
        texts: list[str],
        tool_trace: list[dict[str, Any]],
        emit: EventHandler,
        initial_error: str | None = None,
        interactive: bool = False,
    ) -> tuple[int, str | None]:
        """Run model/tool rounds until the model answers or the budget runs out."""
        error = initial_error
        steps = 0
        for step in range(1, self.cfg.max_steps + 1):
            steps = step
            emit({"type": "step", "step": step, "max": self.cfg.max_steps})
            messages = [{"role": "system", "content": system_prompt}] + self._trim(history)

            def forward(kind: str, chunk: str) -> None:
                emit({"type": kind, "delta": chunk})

            try:
                result: StreamResult = self.llm.stream_chat(
                    messages, tools_mod.TOOL_SCHEMAS, on_event=forward
                )
            except LLMError as exc:
                error = str(exc)
                emit({"type": "error", "message": error})
                break

            if result.content:
                texts.append(result.content)
            history.append(result.assistant_message())
            if result.usage:
                emit({"type": "usage", "usage": result.usage})

            if not result.wants_tools:
                break

            for call in result.tool_calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                raw_args = function.get("arguments") or "{}"
                call_id = call.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                emit({"type": "tool_call", "id": call_id, "name": name, "args": raw_args})
                if interactive and self.cfg.approval == "ask" and name in RISKY_TOOLS:
                    emit({"type": "approval", "id": call_id, "name": name, "args": raw_args})
                    allowed = self.approvals.wait(call_id, self.cfg.approval_timeout)
                    output = (
                        tools_mod.execute(self.cfg, name, raw_args)
                        if allowed
                        else "error: action refused by the user (or confirmation timed out)"
                    )
                else:
                    output = tools_mod.execute(self.cfg, name, raw_args)
                tool_trace.append({"name": name, "args": raw_args, "output": output[:4000]})
                emit(
                    {
                        "type": "tool_result",
                        "id": call_id,
                        "name": name,
                        "output": output,
                        "is_error": output.startswith("error:"),
                    }
                )
                history.append({"role": "tool", "tool_call_id": call_id, "content": output})
        else:
            emit(
                {
                    "type": "warning",
                    "message": f"step budget reached ({self.cfg.max_steps}), stopping",
                }
            )
        return steps, error

    def _finish(
        self,
        session: dict[str, Any],
        history: list[dict[str, Any]],
        texts: list[str],
        tool_trace: list[dict[str, Any]],
        error: str | None,
        steps: int,
        emit: EventHandler,
    ) -> dict[str, Any]:
        """Persist the session then emit the final event."""
        answer = "\n\n".join(part for part in texts if part.strip())
        session["messages"] = history
        try:
            self.sessions.save(session)
        except OSError as exc:  # disk full / permissions: keep the answer
            emit({"type": "warning", "message": f"could not persist session: {exc}"})

        payload = {
            "type": "final",
            "session_id": session["id"],
            "text": answer,
            "steps": steps,
            "tools": tool_trace,
            "error": error,
        }
        emit(payload)
        return payload
