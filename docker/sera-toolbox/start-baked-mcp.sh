#!/usr/bin/env bash
# Start a baked MCP package with the npm registry blocked.
# Usage: start-baked-mcp.sh <name> <pkg@ver>
set -euo pipefail

name="${1:?name}"
pkg="${2:?pkg@version}"
log="$(mktemp)"
trap 'rm -f "$log"' EXIT

export NPM_CONFIG_REGISTRY="http://127.0.0.1:9"
export npm_config_registry="http://127.0.0.1:9"
export npm_config_fetch_retries=0
export npm_config_update_notifier=false

set +e
timeout 5 npx --no-install "$pkg" </dev/null >"$log" 2>&1
rc=$?
set -e

if grep -qiE 'ECONNREFUSED|ENOTFOUND|EAI_AGAIN|E404|not found:|npm ERR! network|npm ERR! code' "$log"; then
  echo "BAD $name registry/missing: $(cat "$log")" >&2
  exit 1
fi

case "$rc" in
  0|124)
    echo "OK $name hook start rc=$rc (registry blocked)"
    exit 0
    ;;
esac

# Package resolved; server rejected empty stdin or missing creds.
if ! grep -qiE 'npm ERR!' "$log"; then
  echo "OK $name baked resolve rc=$rc (registry blocked)"
  exit 0
fi

echo "BAD $name hook start rc=$rc: $(cat "$log")" >&2
exit 1
