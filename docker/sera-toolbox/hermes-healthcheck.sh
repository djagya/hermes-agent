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

curl -fsS --max-time 4 http://127.0.0.1:8642/health >/dev/null

if [ -e /run/service/gateway-default ]; then
  /command/s6-svstat /run/service/gateway-default >/dev/null
fi
