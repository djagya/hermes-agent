#!/usr/bin/env sh
# Cheap compose HEALTHCHECK. No PRAGMA quick_check.
set -eu

home="${HERMES_HOME:-/opt/data}"

if [ -f "$home/ESTOP" ]; then
  echo "healthcheck: ESTOP set" >&2
  exit 1
fi
if [ -f "$home/.migration-in-progress" ]; then
  echo "healthcheck: migration in progress" >&2
  exit 1
fi

avail_kb="$(df -Pk "$home" 2>/dev/null | awk 'NR==2 {print $4}')"
if [ -z "${avail_kb:-}" ] || [ "$avail_kb" -lt 65536 ]; then
  echo "healthcheck: $home has ${avail_kb:-?}KB free" >&2
  exit 1
fi
if [ ! -w "$home" ]; then
  echo "healthcheck: $home is not writable" >&2
  exit 1
fi

if [ -f "$home/state.db" ]; then
  python3 -c "import sqlite3, sys; sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True).close()" \
    "$home/state.db" >/dev/null
fi

curl -fsS --max-time 4 http://127.0.0.1:8642/health >/dev/null

if [ -e /run/service/gateway-default ]; then
  /command/s6-svstat /run/service/gateway-default >/dev/null
fi
