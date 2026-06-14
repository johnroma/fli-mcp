#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
FLI_ENV="${FLI_ENV:-$WORKSPACE_DIR/env-profiles/local.env}"

env_value() {
  local key="$1"
  if [[ ! -f "$FLI_ENV" ]]; then
    return 0
  fi

  awk -F= -v key="$key" '
    $0 !~ /^[[:space:]]*#/ && $1 == key {
      sub(/^[^=]*=/, "", $0)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0)
      gsub(/^["'\'']|["'\'']$/, "", $0)
      print $0
      exit
    }
  ' "$FLI_ENV"
}

export HOST="${FLI_MCP_HOST:-$(env_value FLI_MCP_HOST)}"
export PORT="${FLI_MCP_PORT:-$(env_value FLI_MCP_PORT)}"
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8000}"

cd "$SCRIPT_DIR"
exec "$SCRIPT_DIR/.venv/bin/python" -c "from fli.mcp._entry import run_http; run_http()"
