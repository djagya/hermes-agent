#!/usr/bin/env sh
# Cheap compose HEALTHCHECK. No PRAGMA quick_check.
set -eu

gateway_stat_ok() {
  # s6-svstat: "up (pid 347 pgid 347) 2618 seconds"
  case "$1" in
    up\ \(pid\ [0-9]*) return 0 ;;
    *) return 1 ;;
  esac
}

if [ "${1:-}" = "--self-test" ]; then
  gateway_stat_ok "up (pid 347 pgid 347) 2618 seconds" || exit 1
  gateway_stat_ok "down (exitcode 0) 10 seconds, normally up" && exit 1
  gateway_stat_ok "down (not started yet)" && exit 1
  exit 0
fi

home="${HERMES_HOME:-/opt/data}"

if [ -f "$home/ESTOP" ]; then
  echo "healthcheck: ESTOP set" >&2
  exit 1
fi
if [ -f "$home/.migration-in-progress" ]; then
  echo "healthcheck: migration in progress" >&2
  exit 1
fi
if [ -f "$home/.migration-skipped" ]; then
  echo "healthcheck: migration skipped (HERMES_SKIP_CONFIG_MIGRATION)" >&2
  exit 1
fi
if [ -f "$home/.skills-sync-skipped" ]; then
  echo "healthcheck: skills sync skipped (HERMES_SKIP_SKILLS_SYNC)" >&2
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

# If the default gateway slot exists, it must be up with a PID.
# s6-overlay always has /run/service — do not treat a missing slot as
# unhealthy after curl already passed (CLI / slot not registered yet).
if [ -e /run/service/gateway-default ]; then
  st="$(/command/s6-svstat /run/service/gateway-default)" || {
    echo "healthcheck: s6-svstat failed on gateway-default" >&2
    exit 1
  }
  if ! gateway_stat_ok "$st"; then
    echo "healthcheck: gateway-default not up: $st" >&2
    exit 1
  fi
fi
