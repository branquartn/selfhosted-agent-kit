"""Persistent memory: one Markdown file per memory, reloaded every turn.

Layout (inside ``AGENT_DATA_DIR``)::

    var/memory/
        project-acme.md
        user-preferences.md

Each file carries a tiny front-matter block so the index can be injected in the
system prompt while the body is only loaded on demand::

    ---
    name: user-preferences
    type: user
    description: How the user likes answers formatted
    updated: 2026-01-31T09:12:00Z
    ---
    Markdown body...

File-per-memory keeps writes atomic and diffs readable, and lets the operator
grep/backup the whole agent state with plain tools.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_TYPES = ("user", "feedback", "project", "reference")
MAX_CONTENT_CHARS = 20_000
MAX_MEMORIES = 200
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class MemoryError(RuntimeError):
    """Raised on invalid memory operations."""


def slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return slug[:64] or "memory"


@dataclass
class Memory:
    """A single durable memory."""

    name: str
    type: str
    description: str
    content: str
    updated: str

    def to_markdown(self) -> str:
        description = " ".join(self.description.split())
        return (
            "---\n"
            f"name: {self.name}\n"
            f"type: {self.type}\n"
            f"description: {description}\n"
            f"updated: {self.updated}\n"
            "---\n"
            f"{self.content.rstrip()}\n"
        )

    @classmethod
    def from_markdown(cls, text: str, fallback_name: str = "memory") -> "Memory":
        meta: dict[str, str] = {}
        body = text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) == 3:
                for line in parts[1].strip().splitlines():
                    if ":" in line:
                        key, _, value = line.partition(":")
                        meta[key.strip().lower()] = value.strip()
                body = parts[2].lstrip("\n")
        name = slugify(meta.get("name", fallback_name))
        mem_type = meta.get("type", "reference").lower()
        if mem_type not in ALLOWED_TYPES:
            mem_type = "reference"
        return cls(
            name=name,
            type=mem_type,
            description=meta.get("description", "")[:300],
            content=body.rstrip(),
            updated=meta.get("updated", ""),
        )


class MemoryStore:
    """CRUD over the memory directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    # ------------------------------------------------------------- plumbing
    def _path(self, name: str) -> Path:
        return self.directory / f"{slugify(name)}.md"

    def ensure(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------- read
    def all(self) -> list[Memory]:
        if not self.directory.is_dir():
            return []
        memories: list[Memory] = []
        for path in sorted(self.directory.glob("*.md")):
            try:
                mem = Memory.from_markdown(
                    path.read_text(encoding="utf-8"), fallback_name=path.stem
                )
            except OSError:
                continue
            mem.name = mem.name or slugify(path.stem)
            memories.append(mem)
        return memories

    def get(self, name: str) -> Memory | None:
        path = self._path(name)
        if not path.is_file():
            return None
        return Memory.from_markdown(path.read_text(encoding="utf-8"), fallback_name=name)

    def index_text(self) -> str:
        """Compact listing injected in the system prompt."""
        memories = self.all()
        if not memories:
            return ""
        lines = [f"- **{m.name}** ({m.type}) — {m.description or 'no description'}" for m in memories]
        return "\n".join(lines)

    # ---------------------------------------------------------------- write
    def save(
        self,
        name: str,
        content: str,
        mem_type: str = "reference",
        description: str = "",
    ) -> Memory:
        self.ensure()
        content = (content or "").strip()
        if not content:
            raise MemoryError("content must not be empty")
        if mem_type not in ALLOWED_TYPES:
            raise MemoryError(f"type must be one of {', '.join(ALLOWED_TYPES)}")
        if len(content) > MAX_CONTENT_CHARS:
            raise MemoryError(f"content too long (max {MAX_CONTENT_CHARS} chars)")
        if len(self.all()) >= MAX_MEMORIES and not self._path(name).exists():
            raise MemoryError(f"too many memories (max {MAX_MEMORIES})")

        existing = self.get(name)
        memory = Memory(
            name=slugify(name),
            type=mem_type,
            description=description or (existing.description if existing else "") or content[:120],
            content=content,
            updated=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        target = self._path(name)
        fd, tmp_name = tempfile.mkstemp(dir=self.directory, prefix=".tmp-", suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(memory.to_markdown())
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return memory

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if path.is_file():
            path.unlink()
            return True
        return False
