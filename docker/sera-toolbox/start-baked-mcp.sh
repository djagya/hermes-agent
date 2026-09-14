#!/usr/bin/env bash
# Start a baked MCP package with the npm registry blocked.
# Does not use npx: npx pkg@ver still consults the registry even with
# --no-install (ECONNREFUSED on 127.0.0.1:9 in smoke).
# Usage:
#   start-baked-mcp.sh [--exec] <name> <pkg@ver>
# --exec  exec the baked bin (identity hooks). Default: 5s smoke.
set -euo pipefail

do_exec=0
if [ "${1:-}" = "--exec" ]; then
  do_exec=1
  shift
fi

name="${1:?name}"
pkg="${2:?pkg@version}"

export NPM_CONFIG_REGISTRY="http://127.0.0.1:9"
export npm_config_registry="http://127.0.0.1:9"
export npm_config_offline=true
export NPM_CONFIG_OFFLINE=true
export npm_config_fetch_retries=0
export npm_config_update_notifier=false

bare="${pkg%@*}"
ver="${pkg##*@}"
if [ "$bare" = "$pkg" ] || [ "$ver" = "$pkg" ]; then
  echo "BAD $name: expected pkg@ver, got $pkg" >&2
  exit 1
fi

mod="/usr/local/lib/node_modules/$bare"
if [ ! -f "$mod/package.json" ]; then
  echo "BAD $name: not baked at $mod" >&2
  exit 1
fi

got="$(node -e "console.log(require('$mod/package.json').version)")"
if [ "$got" != "$ver" ]; then
  echo "BAD $name: baked $got want $ver" >&2
  exit 1
fi

bin="$(node -e "
const path = require('path');
const p = require('$mod/package.json');
const b = p.bin;
const rel = typeof b === 'string' ? b : b[Object.keys(b || {})[0]];
if (!rel) { process.exit(2); }
process.stdout.write(path.join('$mod', rel));
")"
if [ ! -f "$bin" ]; then
  echo "BAD $name: bin missing ($bin)" >&2
  exit 1
fi

if [ "$do_exec" = 1 ]; then
  exec node "$bin"
fi

log="$(mktemp)"
trap 'rm -f "$log"' EXIT
set +e
timeout 5 node "$bin" </dev/null >"$log" 2>&1
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

if ! grep -qiE 'npm ERR!' "$log"; then
  echo "OK $name baked resolve rc=$rc (registry blocked)"
  exit 0
fi

echo "BAD $name hook start rc=$rc: $(cat "$log")" >&2
exit 1
