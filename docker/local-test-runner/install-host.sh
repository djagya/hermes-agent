#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$HERE/../.." && pwd)
STATE_ROOT=$(CDPATH= cd -- "$REPO/.." && pwd)/test-runner
SPOOL="$STATE_ROOT/spool"

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
import subprocess

info = json.loads(subprocess.check_output(
    ['docker', 'inspect', 'hermes-test-runner'], text=True
))[0]
mounts = {m['Destination'] for m in info.get('Mounts', [])}
forbidden = {'/opt/data', '/opt/hermes', '/run/service', '/var/run/docker.sock'}
assert mounts == {'/spool'}, f'unexpected mounts: {sorted(mounts)}'
assert not (mounts & forbidden), f'forbidden mount exposed: {sorted(mounts & forbidden)}'
assert info['HostConfig']['NetworkMode'] == 'none'
assert info['HostConfig']['ReadonlyRootfs'] is True
assert info['HostConfig']['Privileged'] is False
assert info['HostConfig'].get('PidsLimit') == 768
print('isolation-ok: spool-only, network=none, read-only root, unprivileged')
PY

printf 'Runner healthy. Gateway was not recreated or restarted.\n'
printf 'Use inside Hermes: hermes-test tests/path/file.py::Class::test_name\n'
