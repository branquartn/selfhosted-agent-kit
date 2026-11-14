"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent.config import Config  # noqa: E402
from agent.llm import StreamResult  # noqa: E402


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    workspace = tmp_path / "workspace"
    data_dir = tmp_path / "var"
    workspace.mkdir()
    data_dir.mkdir()
    config = Config(
        api_key="test-key",
        model="test-model",
        token="secret-token",
        workspace=workspace,
        data_dir=data_dir,
        web_dir=ROOT / "web",
        max_steps=4,
        max_tool_chars=4000,
        shell_timeout=20,
    )
    config.ensure_dirs()
    return config


class FakeLLM:
    """Scripted stand-in for :class:`agent.llm.LLMClient`."""

    def __init__(self, script: list[StreamResult]) -> None:
        self.script = list(script)
        self.calls: list[list[dict]] = []

    def stream_chat(self, messages, tools=None, on_event=None):
        self.calls.append(messages)
        if not self.script:
            raise AssertionError("FakeLLM ran out of scripted responses")
        result = self.script.pop(0)
        if on_event and result.content:
            for chunk in result.content.split(" "):
                on_event("text", chunk + " ")
        return result

    def complete(self, messages, tools=None):  # pragma: no cover - symmetry
        return self.stream_chat(messages, tools)


@pytest.fixture()
def fake_llm():
    return FakeLLM
