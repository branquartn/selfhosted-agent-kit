"""Tool definitions and execution.

Every tool is sandboxed to ``AGENT_WORKSPACE``: paths are resolved and rejected
when they escape the root (including through symlinks). ``run_shell`` is opt-out
(``AGENT_SHELL_ENABLED=false``) and can be restricted to an allow-list of
executables with ``AGENT_SHELL_ALLOW=git,python3,ls``.

The guard-rails here are *not* a security boundary against a hostile model —
run the agent as an unprivileged user in a container if you need that.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .config import Config

MAX_READ_BYTES = 200_000
MAX_SEARCH_HITS = 200
DANGEROUS_PATTERNS = (
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=/dev/zero of=/dev/",
    ":(){",
    "chmod -R 777 /",
    "shutdown",
    "reboot",
    "> /dev/sda",
)


class ToolError(RuntimeError):
    """Raised when a tool call is invalid or refused."""


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories at a path relative to the workspace root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, defaults to '.'"},
                    "recursive": {"type": "boolean", "default": False},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {"type": "integer", "minimum": 1, "default": 400},
                },
                "required": ["path"],
            },
        },
    },
]


def _resolve(cfg: Config, rel_path: str) -> Path:
    """Resolve ``rel_path`` inside the workspace or refuse."""
    root = cfg.workspace.resolve()
    candidate = (root / (rel_path or ".")).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError as exc:  # pragma: no cover - exotic filesystem errors
        raise ToolError(f"cannot resolve {rel_path!r}: {exc}") from exc
    if resolved != root and root not in resolved.parents:
        raise ToolError(f"path escapes the workspace root: {rel_path!r}")
    return resolved


def _truncate(text: str, limit: int, label: str = "output") -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [{label} truncated, {len(text)} chars total]"


def _tool_list_dir(cfg: Config, args: dict[str, Any]) -> str:
    target = _resolve(cfg, args.get("path") or ".")
    if not target.is_dir():
        raise ToolError(f"not a directory: {args.get('path')!r}")
    recursive = bool(args.get("recursive"))
    lines: list[str] = []
    entries = target.rglob("*") if recursive else target.iterdir()
    for entry in entries:
        try:
            rel = entry.relative_to(cfg.workspace.resolve())
        except ValueError:
            continue
        suffix = "/" if entry.is_dir() else f" ({entry.stat().st_size} B)"
        lines.append(f"{rel}{suffix}")
        if len(lines) >= 500:
            lines.append("... (listing truncated at 500 entries)")
            break
    return "\n".join(lines) or "(empty)"


def _tool_read_file(cfg: Config, args: dict[str, Any]) -> str:
    target = _resolve(cfg, args.get("path") or "")
    if not target.is_file():
        raise ToolError(f"no such file: {args.get('path')!r}")
    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise ToolError(f"file too large ({size} B > {MAX_READ_BYTES} B): use search_text")
    offset = max(0, int(args.get("offset") or 0))
    limit = max(1, int(args.get("limit") or 400))
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    chunk = lines[offset : offset + limit]
    return "\n".join(chunk) if chunk else "(no content in that range)"












_HANDLERS = {
    "list_dir": _tool_list_dir,
    "read_file": _tool_read_file,
}

TOOL_NAMES = tuple(_HANDLERS)


def execute(cfg: Config, name: str, raw_args: str | dict[str, Any]) -> str:
    """Run a tool by name and return its textual result.

    Errors are returned as strings (prefixed with ``error:``) instead of being
    raised, so the model can read the failure and retry — the classic agent
    loop contract.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return f"error: unknown tool {name!r} (available: {', '.join(TOOL_NAMES)})"
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError as exc:
            return f"error: invalid JSON arguments for {name}: {exc}"
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        return f"error: arguments for {name} must be a JSON object"
    if not isinstance(args, dict):
        return f"error: arguments for {name} must be a JSON object"
    try:
        return _truncate(handler(cfg, args), cfg.max_tool_chars, "tool output")
    except ToolError as exc:
        return f"error: {exc}"
    except OSError as exc:
        return f"error: {exc}"
    except Exception as exc:  # noqa: BLE001 - a tool must never break the loop
        return f"error: {type(exc).__name__}: {exc}"
