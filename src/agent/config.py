"""Configuration loading.

Everything is driven by environment variables (optionally seeded from a `.env`
file) so the kit can run from a shell, systemd, Docker or a PaaS without code
changes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


def load_env_file(path: Path | str) -> None:
    """Load a minimal `.env` file into ``os.environ``.

    Existing environment variables win (``setdefault``) so operators can always
    override a file value from the shell. Supports ``#`` comments, blank lines,
    ``KEY=VALUE`` and single/double quoted values.
    """
    path = Path(path)
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif "#" in value:
            # strip an inline comment from an unquoted value
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if key:
            os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    return raw not in ("0", "false", "no", "off", "")


def _env_list(name: str) -> tuple[str, ...]:
    raw = _env(name, "")
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass
class Config:
    """Runtime configuration."""

    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    temperature: float = 0.3
    max_tokens: int = 4096

    # HTTP server
    host: str = "127.0.0.1"
    port: int = 8800
    token: str = ""

    # Agent loop
    system_prompt: str = ""
    max_steps: int = 20
    context_chars: int = 120_000
    max_tool_chars: int = 12_000

    # Storage
    workspace: Path = field(default_factory=lambda: Path.cwd())
    data_dir: Path = field(default_factory=lambda: Path("var"))
    web_dir: Path = field(default_factory=lambda: Path("web"))

    # Tools
    shell_enabled: bool = True
    shell_timeout: int = 60
    shell_allow: tuple[str, ...] = ()
    approval: str = "ask"  # "ask": confirm risky tools in the web UI; "auto": run them directly
    approval_timeout: int = 300

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memory"

    def ensure_dirs(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, env_path: Path | str | None = None) -> "Config":
        """Build a config from the environment (and optional `.env` file)."""
        if env_path is not None:
            load_env_file(env_path)

        root = Path(_env("AGENT_ROOT", ".")).expanduser().resolve()
        data_dir = Path(_env("AGENT_DATA_DIR", str(root / "var"))).expanduser()
        workspace = Path(_env("AGENT_WORKSPACE", str(root / "workspace"))).expanduser()
        web_dir = _env("AGENT_WEB_DIR", "")

        cfg = cls(
            base_url=_env("LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            api_key=_env("LLM_API_KEY"),
            model=_env("LLM_MODEL", DEFAULT_MODEL),
            temperature=_env_float("LLM_TEMPERATURE", 0.3),
            max_tokens=_env_int("LLM_MAX_TOKENS", 4096),
            host=_env("AGENT_HOST", "127.0.0.1"),
            port=_env_int("AGENT_PORT", 8800),
            token=_env("AGENT_TOKEN"),
            system_prompt=_env("AGENT_SYSTEM_PROMPT"),
            max_steps=_env_int("AGENT_MAX_STEPS", 20),
            context_chars=_env_int("AGENT_CONTEXT_CHARS", 120_000),
            max_tool_chars=_env_int("AGENT_MAX_TOOL_CHARS", 12_000),
            workspace=workspace,
            data_dir=data_dir,
            shell_enabled=_env_bool("AGENT_SHELL_ENABLED", True),
            shell_timeout=_env_int("AGENT_SHELL_TIMEOUT", 60),
            shell_allow=_env_list("AGENT_SHELL_ALLOW"),
            approval=_env("AGENT_APPROVAL", "ask").lower(),
            approval_timeout=_env_int("AGENT_APPROVAL_TIMEOUT", 300),
        )
        cfg.workspace = cfg.workspace.resolve()
        cfg.data_dir = cfg.data_dir.resolve()
        cfg.web_dir = (
            Path(web_dir).expanduser().resolve() if web_dir else root / "web"
        )
        return cfg

    def validate(self) -> list[str]:
        """Return a list of human-readable configuration problems."""
        problems: list[str] = []
        if not self.api_key:
            problems.append("LLM_API_KEY is empty: the agent cannot call the model")
        if not self.model:
            problems.append("LLM_MODEL is empty")
        if not self.base_url.startswith(("http://", "https://")):
            problems.append(f"LLM_BASE_URL must start with http(s):// (got {self.base_url!r})")
        if not self.token:
            problems.append("AGENT_TOKEN is empty: the API would be world-writable")
        if self.approval not in ("ask", "auto"):
            problems.append("AGENT_APPROVAL must be 'ask' or 'auto'")
        if self.max_steps < 1:
            problems.append("AGENT_MAX_STEPS must be >= 1")
        return problems

    def redacted(self) -> dict[str, object]:
        """Config snapshot safe to log or expose through an endpoint."""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "host": self.host,
            "port": self.port,
            "workspace": str(self.workspace),
            "data_dir": str(self.data_dir),
            "max_steps": self.max_steps,
            "shell_enabled": self.shell_enabled,
            "shell_allow": list(self.shell_allow),
            "approval": self.approval,
            "api_key_set": bool(self.api_key),
            "token_set": bool(self.token),
        }
