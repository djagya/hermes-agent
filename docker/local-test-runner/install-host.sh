#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STATE_ROOT=${HERMES_TEST_STATE_ROOT:-}
[[ -n "$STATE_ROOT" ]] || {
  printf '%s\n' \
    'HERMES_TEST_STATE_ROOT is required and must be the host path that maps' \
    'to gateway /opt/data/test-runner (for the monolith deployment:' \
    'HERMES_TEST_STATE_ROOT=/home/dlz/monolith/data/hermes/test-runner)' >&2
  exit 1
}
[[ "$STATE_ROOT" == /* ]] || {
  printf 'HERMES_TEST_STATE_ROOT must be an absolute host path: %s\n' "$STATE_ROOT" >&2
  exit 1
}
SPOOL="$STATE_ROOT/spool"
export HERMES_TEST_SPOOL_HOST="$SPOOL"

command -v docker >/dev/null 2>&1 || {
  printf 'docker is required on the host\n' >&2
  exit 1
}
docker info >/dev/null 2>&1 || {
  printf 'Docker daemon is not reachable; run this script on the Docker host\n' >&2
  exit 1
}

mkdir -p "$SPOOL/requests" "$SPOOL/results"
chmod 700 "$STATE_ROOT" "$SPOOL" "$SPOOL/requests" "$SPOOL/results"

printf 'Building and starting isolated Hermes test runner...\n'
docker compose -p hermes-local-tests -f "$HERE/compose.yml" up -d --build runner

for _ in $(seq 1 60); do
  status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' hermes-test-runner 2>/dev/null || true)
  [[ "$status" == healthy ]] && break
  sleep 2
done
[[ "${status:-}" == healthy ]] || {
  docker logs --tail 100 hermes-test-runner >&2 || true
  printf 'runner failed its health gate\n' >&2
  exit 1
}

python3 - <<'PY'
import json
import os
from pathlib import Path
import subprocess

info = json.loads(subprocess.check_output(
    ['docker', 'inspect', 'hermes-test-runner'], text=True
))[0]
mounts = {m['Destination'] for m in info.get('Mounts', [])}
forbidden = {'/opt/data', '/opt/hermes', '/run/service', '/var/run/docker.sock'}
assert mounts == {'/spool'}, f'unexpected mounts: {sorted(mounts)}'
assert not (mounts & forbidden), f'forbidden mount exposed: {sorted(mounts & forbidden)}'
spool_mount = next(m for m in info['Mounts'] if m['Destination'] == '/spool')
expected_spool = str(Path(os.environ['HERMES_TEST_SPOOL_HOST']).resolve())
actual_spool = str(Path(spool_mount['Source']).resolve())
assert actual_spool == expected_spool, (
    f'runner spool source mismatch: expected {expected_spool}, got {actual_spool}'
)
assert info['HostConfig']['NetworkMode'] == 'none'
assert info['HostConfig']['ReadonlyRootfs'] is True
assert info['HostConfig']['Privileged'] is False
assert info['HostConfig'].get('PidsLimit') == 768
print('isolation-ok: explicit spool-only mount, network=none, read-only root, unprivileged')
PY

printf 'Runner healthy. Gateway was not recreated or restarted.\n'
printf 'Use inside Hermes: hermes-test tests/path/file.py::Class::test_name\n'
