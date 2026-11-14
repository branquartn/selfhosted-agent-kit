# Architecture

```
browser ──HTTP/SSE──> server.py ──> engine.py ──> llm.py ──HTTPS──> provider
                          │            │
                          │            ├──> tools.py  ──> workspace/ (sandboxed)
                          │            └──> memory.py ──> var/memory/*.md
                          └──> var/sessions/*.json
```

## Modules

| Module | Lines | Responsibility |
|---|---|---|
| `config.py` | ~200 | `.env` loading, dataclass config, validation, redaction |
| `llm.py` | ~230 | OpenAI-compatible streaming client, tool-call reassembly |
| `tools.py` | ~330 | 7 tools + path sandbox + shell guard-rails + schemas |
| `memory.py` | ~180 | Markdown file-per-memory store with front matter |
| `engine.py` | ~290 | agent loop (`run` → `_loop` → `_finish`), session store, trimming |
| `server.py` | ~280 | HTTP routes, SSE, token auth, static files |

## Request lifecycle

1. `POST /api/chat {session_id, message}` → token check → SSE headers.
2. `Engine.run()` loads (or creates) the session, appends the user message.
3. For each step:
   - build `[system] + trimmed(history)`, where the system prompt carries the
     workspace path and the memory index;
   - `LLMClient.stream_chat()` forwards `text` / `reasoning` deltas immediately
     through `on_event` → SSE → browser;
   - if the model returned `tool_calls`, each one is executed by `tools.execute()`
     and appended as a `role: tool` message (errors included, as text, so the
     model can recover);
   - otherwise the loop ends.
4. The session is persisted and a `final` event closes the stream.

## Design decisions

- **Stdlib only.** `urllib` + `ThreadingHTTPServer` cover the whole product. No
  dependency drift, no CVE feed to follow, no venv required.
- **Sandbox at the tool boundary.** Paths are resolved (`Path.resolve()`) and
  compared against the workspace root, so `../`, absolute paths and symlinks are
  all rejected in one place.
- **Tool errors are data.** A failing tool returns `error: …` instead of raising:
  agents recover far better from readable feedback than from exceptions.
- **Memory as files.** One fact per Markdown file with a description index keeps
  context cost low and lets a human audit exactly what the agent remembers.
- **Sessions as JSON.** Simple, greppable, portable. Swap `SessionStore` for a
  database if you need concurrency beyond a handful of users.
- **One lock per conversation.** Two tabs on *different* chats stream in parallel;
  a second message on the *same* conversation gets `409` immediately instead of
  queueing silently behind a long generation.
- **Persist before propagating.** `run()` catches a disconnecting client
  (`KeyboardInterrupt` from the SSE writer), saves the partial transcript, then
  re-raises: no lost history on a closed tab.

## Extension points

- **New tool** — add a handler `_tool_x(cfg, args) -> str` and register it in
  `_HANDLERS` + `TOOL_SCHEMAS` (`tools.py`). Keep the return value short and
  text-only.
- **Different model** — `LLMClient` is provider-agnostic; set `LLM_BASE_URL`,
  `LLM_API_KEY`, `LLM_MODEL`.
- **New transport** — `engine.run(..., on_event=callable)` is transport-neutral:
  wrap it in WebSocket, CLI or a queue consumer without touching the loop.
- **Multi-user** — add a `user_id` to the session/memory paths and derive the
  workspace from it; the config object is the only place that knows about paths.
