#!/usr/bin/env bash
# SelfHosted Agent Kit — installer.
#
#   ./install.sh              # local install: .env, dirs, token, dependency check
#   sudo ./install.sh --systemd   # + install and enable a hardened systemd service
#
# Idempotent: safe to re-run. Never overwrites an existing .env.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT/.env"
SERVICE="selfhosted-agent"
SERVICE_USER="selfhosted-agent"
WITH_SYSTEMD=0

for arg in "$@"; do
  case "$arg" in
    --systemd) WITH_SYSTEMD=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -9
      exit 0
      ;;
    *)
      echo "unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- prerequisites
command -v python3 >/dev/null 2>&1 || die "python3 is required"
PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)')"
[ "$PY_OK" = "1" ] || die "Python 3.10+ is required (found $(python3 -V 2>&1))"
say "Python $(python3 -c 'import platform; print(platform.python_version())') detected"

# ----------------------------------------------------------------------- .env
if [ -f "$ENV_FILE" ]; then
  say ".env already exists — leaving it untouched"
else
  cp "$ROOT/.env.example" "$ENV_FILE"
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  python3 - "$ENV_FILE" "$TOKEN" <<'PY'
import sys
from pathlib import Path

path, token = Path(sys.argv[1]), sys.argv[2]
text = path.read_text(encoding="utf-8").replace("AGENT_TOKEN=replace-me-too", f"AGENT_TOKEN={token}")
path.write_text(text, encoding="utf-8")
PY
  chmod 600 "$ENV_FILE"
  say "created .env with a freshly generated AGENT_TOKEN"
fi

# ---------------------------------------------------------------------- layout
mkdir -p "$ROOT/var/sessions" "$ROOT/var/memory" "$ROOT/workspace"
say "runtime directories ready (var/, workspace/)"

# A copied .env may still hold the placeholder token: rotate it.
if grep -q '^AGENT_TOKEN=replace-me-too' "$ENV_FILE"; then
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  python3 - "$ENV_FILE" "$TOKEN" <<'PY'
import sys
from pathlib import Path

path, token = Path(sys.argv[1]), sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines()
lines = [
    f"AGENT_TOKEN={token}" if line.startswith("AGENT_TOKEN=replace-me-too") else line
    for line in lines
]
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
  chmod 600 "$ENV_FILE"
  say "replaced the placeholder AGENT_TOKEN with a generated one"
fi

if grep -q '^LLM_API_KEY=replace-me' "$ENV_FILE"; then
  warn "LLM_API_KEY still has the placeholder value — edit $ENV_FILE before starting"
fi

# -------------------------------------------------------------------- systemd
if [ "$WITH_SYSTEMD" = "1" ]; then
  [ "$(id -u)" = "0" ] || die "--systemd must be run as root (sudo ./install.sh --systemd)"
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found"

  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home "$ROOT" --shell /usr/sbin/nologin "$SERVICE_USER"
    say "created system user $SERVICE_USER"
  fi

  chown -R "$SERVICE_USER":"$SERVICE_USER" "$ROOT/var" "$ROOT/workspace"
  chown root:"$SERVICE_USER" "$ENV_FILE"
  chmod 640 "$ENV_FILE"

  sed -e "s|__ROOT__|$ROOT|g" \
      -e "s|__USER__|$SERVICE_USER|g" \
      "$ROOT/deploy/selfhosted-agent.service" > "/etc/systemd/system/$SERVICE.service"

  systemctl daemon-reload
  systemctl enable "$SERVICE.service"
  say "installed /etc/systemd/system/$SERVICE.service"
  say "start it with: sudo systemctl start $SERVICE"
else
  chmod +x "$ROOT/run.sh" 2>/dev/null || true
fi

# ---------------------------------------------------------------------- hints
cat <<EOF

Next steps
----------
1. Edit    : \$EDITOR $ENV_FILE      (set LLM_API_KEY and LLM_MODEL)
2. Run     : ./run.sh               → http://127.0.0.1:8800
3. Token   : \$(grep '^AGENT_TOKEN=' $ENV_FILE | cut -d= -f2-)
4. Tests   : python3 -m pytest
EOF
