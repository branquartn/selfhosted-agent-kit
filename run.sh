#!/usr/bin/env bash
# Launch the agent. No dependencies, no virtualenv required.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export AGENT_ENV_FILE="${AGENT_ENV_FILE:-$ROOT/.env}"

exec python3 -m agent.server "$@"
