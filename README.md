# SelfHosted Agent Kit

A complete, self-hosted AI agent you can run on your own server in **two minutes**:
streaming web chat, real tool use (files, shell, search), long-term memory, session
history and a token-protected API — in ~2 000 lines of readable Python with
**zero third-party dependencies**.

Works with **any OpenAI-compatible provider**: DeepSeek, OpenAI, Groq, Together,
OpenRouter, vLLM, LM Studio, Ollama.

---

## Why this one

| | SelfHosted Agent Kit | Typical "agent starter" repo |
|---|---|---|
| Dependencies | **none** (stdlib only) | LangChain + 40 transitive packages |
| Install time | ~10 s | minutes, version conflicts |
| Web UI included | **yes**, streaming, tool cards, memory viewer | usually a CLI |
| Tool use | files, shell (allow-list), grep, memory | varies |
| Long-term memory | **file-per-memory Markdown**, human-editable | opaque vector DB |
| Node backend needed | **no** | often yes |
| Provider lock-in | none | SDK-specific |
| Tests | 91 passing, no network needed | rarely |
| Licence | **MIT** | MIT |

## Features

- **Streaming chat UI** — tokens appear as they are generated, with collapsible tool cards, reasoning output (DeepSeek/`reasoning_content`), session sidebar and a memory browser.
- **Real agent loop** — the model calls tools, reads the results, retries on errors, up to `AGENT_MAX_STEPS`.
- **7 built-in tools** — `list_dir`, `read_file`, `write_file`, `search_text`, `run_shell`, `memory_save`, `memory_list`.
- **Sandboxed file access** — every path is resolved and refused if it escapes the workspace (symlink attacks included).
- **Long-term memory** — one Markdown file per fact, injected as an index every turn; readable by a human and greppable by the agent.
- **Sessions** — persisted as JSON, resumable, deletable through the UI or the API.
- **Token auth** — `Bearer` or `X-Agent-Token` on every `/api/` route, constant-time comparison.
- **Deploy anywhere** — systemd, Docker, bare Python 3.10+.

## Quickstart

```bash
# 1. Unpack, then run the installer (creates .env, dirs, token, optional systemd unit)
./install.sh

# 2. Edit the two required values
$EDITOR .env          # LLM_API_KEY=... and LLM_MODEL=...  (AGENT_TOKEN is generated)

# 3. Run
./run.sh              # → http://127.0.0.1:8800
```

Open the URL, paste the `AGENT_TOKEN` from `.env`, start talking.
The agent's file operations happen inside `./workspace/`.

### One-liner alternative (no installer)

```bash
cp .env.example .env && chmod 600 .env
python3 -c "import secrets;print('AGENT_TOKEN='+secrets.token_urlsafe(32))" >> .env
python3 -m venv .venv && . .venv/bin/activate   # optional, zero deps anyway
PYTHONPATH=src python3 -m agent.server
```

### Docker

```bash
cp .env.example .env && $EDITOR .env
docker compose up -d
```

## Configuration

Everything is environment-driven (see `.env.example`). The essentials:

| Variable | Default | Purpose |
|---|---|---|
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI-compatible endpoint |
| `LLM_API_KEY` | — | Provider key (can be empty for local Ollama) |
| `LLM_MODEL` | `deepseek-chat` | Model name as the provider spells it |
| `AGENT_TOKEN` | — | **Required.** API secret |
| `AGENT_HOST` / `AGENT_PORT` | `127.0.0.1:8800` | Bind address |
| `AGENT_WORKSPACE` | `./workspace` | File-tool root (sandbox) |
| `AGENT_DATA_DIR` | `./var` | Sessions + memories |
| `AGENT_MAX_STEPS` | `20` | Tool-call budget per turn |
| `AGENT_SHELL_ENABLED` | `true` | Disable shell entirely with `false` |
| `AGENT_APPROVAL` | `ask` | `ask` = the web UI asks before every file write / shell command; `auto` = run directly |
| `AGENT_APPROVAL_TIMEOUT` | `300` | Seconds to wait for your answer (no answer = refused) |
| `AGENT_SHELL_ALLOW` | *(empty)* | Comma list of allowed executables |
| `AGENT_SYSTEM_PROMPT` | *(built-in)* | Override the persona |

Check a deployment without starting it:

```bash
PYTHONPATH=src python3 -m agent.server --check
```

## API

All `/api/` routes require `Authorization: Bearer $AGENT_TOKEN`.

| Method | Route | Description |
|---|---|---|
| `GET` | `/healthz` | Liveness (public) |
| `GET` | `/api/config` | Redacted runtime config |
| `POST` | `/api/chat` | SSE stream: `{"session_id": "…", "message": "…"}` — `409` if that conversation is already streaming, `400` above 32 000 chars |
| `GET` | `/api/sessions` | List conversations |
| `POST` | `/api/sessions` | Create a conversation |
| `GET` | `/api/sessions/<id>` | Load a conversation |
| `DELETE` | `/api/sessions/<id>` | Delete a conversation |
| `GET` | `/api/memory` | List memories |
| `POST` | `/api/memory` | Save `{name, content, type, description}` |
| `DELETE` | `/api/memory/<name>` | Delete a memory |

SSE event types: `step`, `reasoning`, `text`, `tool_call`, `tool_result`,
`usage`, `warning`, `error`, `final`.

```bash
curl -N http://127.0.0.1:8800/api/chat \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message":"list the files in the workspace"}'
```

## Production deployment

### systemd (recommended for a VPS)

```bash
sudo ./install.sh --systemd      # installs /etc/systemd/system/selfhosted-agent.service
sudo systemctl enable --now selfhosted-agent
journalctl -u selfhosted-agent -f
```

The unit runs as a dedicated unprivileged user, with `ProtectSystem=strict`,
`NoNewPrivileges` and a writable `var/` + `workspace/` only. `ProtectHome=read-only`
is used on purpose: installing under `/home` (the default) must stay readable, while
`ProtectHome=yes` would hide it from `ReadWritePaths` and break the unit. Installing
under `/opt/agent` is still the cleanest choice.

### Reverse proxy

Put Caddy or nginx in front for TLS, and keep `AGENT_HOST=127.0.0.1`:

```caddyfile
agent.example.com {
    reverse_proxy 127.0.0.1:8800
}
```

Streaming needs buffering off — the server already sends `X-Accel-Buffering: no`
and closes the stream cleanly, so nginx works with the default config.

## Security model (read this)

- The **workspace sandbox protects against mistakes, not against a hostile model.**
  Tool calls are validated, shell patterns are screened, but a determined prompt
  injection can still do whatever the agent's UNIX user may do.
- Therefore: run it as an **unprivileged user**, ideally in a container, and never
  give it access to `~/.ssh`, cloud credentials or your whole home directory.
- Use a strong `AGENT_TOKEN`, expose it through TLS only, and rotate the token if it leaks.
- Memories and sessions land in `var/` — treat them as sensitive; back them up, don't publish them.
- `run_shell` is powerful by design. Set `AGENT_SHELL_ALLOW` to a short list if the
  agent only needs a few commands.

## Customising

| Goal | Where |
|---|---|
| Change the persona / rules | `AGENT_SYSTEM_PROMPT` in `.env`, or `DEFAULT_SYSTEM_PROMPT` in `src/agent/engine.py` |
| Add a tool | write a handler in `src/agent/tools.py`, register it in `_HANDLERS` and `TOOL_SCHEMAS` |
| Change the model at runtime | `src/agent/llm.py` — one class, one method |
| Restyle the UI | `web/chat.css` (CSS variables at the top) |
| Swap JSON sessions for a DB | `SessionStore` in `src/agent/engine.py` (`create/save/load/list/delete`) |

## Tests

```bash
python3 -m pytest            # 91 tests, no network, no API key needed
python3 scripts/smoke.py     # end-to-end: fake provider + real server + SSE chat
```

## Layout

```
.
├── .env.example
├── install.sh            # setup + optional systemd install
├── run.sh                # dev/prod launcher
├── Dockerfile
├── docker-compose.yml
├── deploy/
│   └── selfhosted-agent.service
├── src/agent/
│   ├── config.py         # env/.env → Config, validation
│   ├── llm.py            # OpenAI-compatible streaming client (urllib)
│   ├── tools.py          # 7 sandboxed tools
│   ├── memory.py         # Markdown file-per-memory store
│   ├── engine.py         # agent loop + session store
│   └── server.py         # HTTP API + SSE + static UI
├── web/                  # index.html, chat.js, chat.css (no build step)
└── tests/                # pytest suite
```

## Licence

MIT — see [`LICENSE`](LICENSE). Free for personal and commercial use. Issues and pull
requests are welcome; there is no paid support.

---

*Version 1.1.1 — see [CHANGELOG.md](CHANGELOG.md).*
