# SelfHosted Agent Kit — Demo

A small, readable, **zero-dependency** AI agent you run on your own machine:
streaming web chat, OpenAI-compatible provider (DeepSeek, OpenAI, Groq, OpenRouter,
Ollama, vLLM, LM Studio), real tool calling, sessions, token-protected API.

This is the **demo edition**: it shows how the agent loop, the streaming client,
the sandbox and the HTTP/SSE server work. It can explore a workspace
(`list_dir`, `read_file`). The full version adds:

| | Demo | Full |
|---|:-:|:-:|
| Streaming chat UI, sessions, token-protected API | ✅ | ✅ |
| Any OpenAI-compatible provider | ✅ | ✅ |
| Read tools (`list_dir`, `read_file`) | ✅ | ✅ |
| Write files, run shell commands, text search | – | ✅ |
| **Action approval** (Allow / Refuse before every write or command) | – | ✅ |
| Long-term memory (one Markdown file per memory) | – | ✅ |
| Docs (architecture, FR quick start), changelog, commercial licence | – | ✅ |
| Updates and e-mail support | – | ✅ |

**Full version:** <LINK-TO-STORE>

## Quick start (2 minutes)

```bash
cp .env.example .env     # set LLM_API_KEY and AGENT_TOKEN
./install.sh && ./run.sh # then open http://127.0.0.1:8800 and paste your token
```

```bash
python3 -m pytest        # tests, no network, no API key needed
python3 scripts/smoke.py # end-to-end check against a fake provider
```

Requires Python 3.10+. No `pip install` needed to run it.

## Layout

```
src/agent/   config.py · llm.py (streaming client) · engine.py (agent loop)
             tools.py (sandboxed tools) · server.py (HTTP + SSE) · memory.py (stub)
web/         vanilla HTML/CSS/JS, no build step
tests/       pytest suite
```

Licence: see `LICENSE` (evaluation / personal, non-commercial).
