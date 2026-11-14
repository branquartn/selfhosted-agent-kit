"""Persistent memory — available in the full version only.

This demo ships a no-op stub so the rest of the code keeps working. The full
SelfHosted Agent Kit stores one Markdown file per memory and injects the index
in the system prompt every turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class MemoryError(RuntimeError):
    """Raised by the full version when a memory operation is invalid."""


class MemoryStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def all(self) -> list[Any]:
        return []

    def index_text(self) -> str:
        return ""

    def save(self, **kwargs: Any) -> Any:
        raise MemoryError("memory is part of the full version")

    def delete(self, name: str) -> bool:
        return False
