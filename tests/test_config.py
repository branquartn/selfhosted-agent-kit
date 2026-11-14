"""Configuration loading tests."""

from __future__ import annotations

import os
from pathlib import Path

from agent.config import Config, load_env_file


def test_load_env_file_parses_quotes_and_comments(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n\nLLM_MODEL=quoted-model\nLLM_API_KEY=\"sk-abc\"\n"
        "AGENT_TOKEN='tok'\nexport AGENT_PORT=9999\n",
        encoding="utf-8",
    )
    for key in ("LLM_MODEL", "LLM_API_KEY", "AGENT_TOKEN", "AGENT_PORT"):
        monkeypatch.delenv(key, raising=False)
    load_env_file(env)
    assert os.environ["LLM_MODEL"] == "quoted-model"
    assert os.environ["LLM_API_KEY"] == "sk-abc"
    assert os.environ["AGENT_TOKEN"] == "tok"
    assert os.environ["AGENT_PORT"] == "9999"


def test_existing_env_wins_over_file(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("LLM_MODEL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("LLM_MODEL", "from-shell")
    load_env_file(env)
    assert os.environ["LLM_MODEL"] == "from-shell"


def test_missing_env_file_is_ignored(tmp_path: Path):
    load_env_file(tmp_path / "nope.env")  # must not raise


def test_from_env_builds_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("AGENT_TOKEN", "t")
    monkeypatch.setenv("AGENT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_PORT", "1234")
    monkeypatch.setenv("AGENT_MAX_STEPS", "7")
    monkeypatch.setenv("AGENT_SHELL_ENABLED", "false")
    monkeypatch.setenv("AGENT_SHELL_ALLOW", "git, ls")
    cfg = Config.from_env()
    assert cfg.api_key == "k"
    assert cfg.model == "m"
    assert cfg.port == 1234
    assert cfg.max_steps == 7
    assert cfg.shell_enabled is False
    assert cfg.shell_allow == ("git", "ls")
    assert cfg.workspace == (tmp_path / "workspace").resolve()
    assert cfg.data_dir == (tmp_path / "var").resolve()


def test_validate_reports_missing_secrets():
    cfg = Config(api_key="", token="", base_url="ftp://nope")
    problems = cfg.validate()
    assert any("LLM_API_KEY" in p for p in problems)
    assert any("AGENT_TOKEN" in p for p in problems)
    assert any("http" in p for p in problems)


def test_validate_accepts_complete_config():
    assert Config(api_key="k", token="t").validate() == []


def test_redacted_hides_secrets():
    payload = Config(api_key="secret", token="tok").redacted()
    assert "secret" not in str(payload.values())
    assert payload["api_key_set"] is True
    assert payload["token_set"] is True


def test_inline_comment_is_stripped(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AGENT_TOKEN=abc123  # generated\n", encoding="utf-8")
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    load_env_file(env)
    assert os.environ["AGENT_TOKEN"] == "abc123"
