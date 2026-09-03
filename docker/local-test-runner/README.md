# Local isolated Hermes test runner

This sidecar lets the live Hermes gateway test changed local source without
executing the test process in the production container.

## Boundary

- The gateway writes a sanitized tar snapshot and request metadata to
  `/opt/data/test-runner/spool`.
- The runner mounts only that spool at `/spool`.
- It does **not** mount `/opt/data`, `/opt/hermes`, `/run/service`, or the Docker
  socket.
- The runner has its own PID/filesystem namespaces, a read-only root,
  `network_mode: none`, all capabilities dropped, and `no-new-privileges`.
- Resource ceiling: 4 CPUs, 8 GiB RAM, 768 PIDs; the full-suite runner uses two
  internal workers.
- `/work` and `/tmp` are private tmpfs mounts and are destroyed with the
  container.
- Runtime dependency drift is fail-closed: if the request's `uv.lock` hash does
  not match the image, rebuild the runner before executing tests.

A worktree, temporary `HERMES_HOME`, or mock is not treated as isolation.

## One-time host activation

Run on the Docker host, not inside the Hermes container:

```bash
/home/dlz/monolith/data/hermes/hermes-agent/docker/local-test-runner/install-host.sh
```

The installer builds and starts only `hermes-test-runner`; it neither recreates
nor restarts the gateway. It verifies the effective mounts, network mode,
read-only root, privilege state, PID limit, and heartbeat before succeeding.

## Use from Hermes

```bash
hermes-test --doctor
hermes-test tests/tools/test_skill_manager_tool.py::TestValidateName::test_valid_names
hermes-test tests/tools/test_skill_manager_tool.py
hermes-test --full
```

The launcher snapshots tracked and non-ignored untracked files, so dirty changed
code is exercised. It excludes Git metadata, virtual environments, caches,
`node_modules`, and live `.env*` files. Symlinks, oversized files, oversized
snapshots, invalid targets, stale heartbeats, checksum mismatches, and dependency
drift are refused.

The sidecar independently repeats all validation. The launcher is not a trust
boundary.

## Expected limitations

- Runtime network is disabled. Tests that genuinely require external services
  belong in a separately authorized integration environment.
- Docker-in-Docker tests cannot access the host daemon and may skip or fail.
  Never repair that by mounting `/var/run/docker.sock`.
- Results remain under `/opt/data/test-runner/spool/results` for seven days.

## Rebuild after dependency changes

Rerun the host installer. It rebuilds the image against the current
`pyproject.toml` and `uv.lock`, then replaces only the runner sidecar.

## Rollback

On the Docker host:

```bash
cd /home/dlz/monolith/data/hermes/hermes-agent
docker compose -p hermes-local-tests \
  -f docker/local-test-runner/compose.yml down --remove-orphans
```

Then remove `/opt/data/.local/bin/hermes-test` inside Hermes. The spool may be
retained for audit or deleted separately after reviewing results. Gateway state
is untouched by either operation.
