#!/usr/bin/env bash
# Wrap must not persist caller secrets into receipts or tool output.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
printf 'hello\n' > in.txt
export OP_SERVICE_ACCOUNT_TOKEN=canary-token-do-not-leak
export GH_TOKEN=canary-token-do-not-leak
export HERMES_HOME=/opt/data
export SERA_SANDBOX_OUT="$tmp/out"
mkdir -p "$tmp/out"
file in.txt >stdout 2>stderr

if ! grep -q 'env -i' /opt/hermes/docker/sera-toolbox/wrap; then
  echo "FAIL wrap missing env -i" >&2
  exit 1
fi
if grep -R -F "canary-token-do-not-leak" . /tmp/sera-bwrap.*.json >/dev/null 2>&1; then
  echo "FAIL secret leaked into wrap outputs or receipt" >&2
  exit 1
fi
echo "OK secret-env not in wrap outputs"
