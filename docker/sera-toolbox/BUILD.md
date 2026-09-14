# Building the Sera toolbox image

Matches CI in `.github/workflows/fork-release-image.yml`. Published
target is `runtime`. `--target test` is runtime plus
`docker/sera-toolbox/fixtures/` (golden text fixtures). CI
`build-test` loads `test`; `publish` pushes `runtime`.

```bash
# Local load (amd64). Pass the same args CI uses.
# Smoke/golden: --target test. Pin/publish: --target runtime.
docker buildx build \
    --target test \
    --load \
    --platform linux/amd64 \
    --build-arg HERMES_GIT_SHA="$(git rev-parse HEAD)" \
    --build-arg HERMES_IMAGE_NAME=ghcr.io/djagya/hermes-agent \
    --build-arg HERMES_BUILD_REF="$(git rev-parse --abbrev-ref HEAD)" \
    --build-arg DEBIAN_SNAPSHOT=20260907T000000Z \
    -t ghcr.io/djagya/hermes-agent:local \
    -f Dockerfile \
    .

# Toolbox + golden + adversarial. Ubuntu 24.04 needs userns +
# apparmor=unconfined (docker-default denies mount). Seccomp must
# allow clone/clone3 (Docker default blocks CLONE_NEWUSER / clone3).
# Never privileged or seccomp=unconfined.
#   sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
docker run --rm --network none \
  --security-opt seccomp=docker/sera-toolbox/seccomp-bwrap.json \
  --security-opt apparmor=unconfined \
  --entrypoint /opt/hermes/docker/sera-toolbox/smoke.sh \
  ghcr.io/djagya/hermes-agent:local
```

Builder stage keeps compilers. Do not publish `builder`.

BuildKit cache mounts (CI and the command above). They must not leak
into the published `runtime` layer:

- apt: `Dockerfile` `RUN --mount=type=cache,target=/var/cache/apt`
  and `/var/lib/apt` on the snapshot + toolbox apt lines
- npm: `target=/root/.npm` on `npm ci` and the web/`ui-tui` build
- uv: `target=/root/.cache/uv` on `uv sync` and later `uv pip`

Runtime writes go to `/opt/data/cache/npm` (`NPM_CONFIG_CACHE`) and
`/opt/data/cache/uv`. Those are bind-mount state, not image layers.

Managed scope (`/etc/hermes/config.yaml`) seeds
`memory.write_approval` (off), `skills.write_approval` (on), and
`approvals.cron_mode` (deny) when those leaves are missing from user
`config.yaml`. `hermes config set` writes the user file and wins;
unset falls back to the seed. Residual (deferred): no command allowlist
leaf — v0.21 has no supported managed-config key for it; do not
invent precedence. `approvals.mode` / `deny` and
`telegram.allowed_chats` stay in user config.

ClickUp 1.8.0 and `caldav-mcp` 0.10.0 are baked. `start-baked-mcp.sh`
execs the baked bin (no `npx` — `npx pkg@ver` still hits the
registry). `smoke.sh` starts both with the registry blocked
(`http://127.0.0.1:9`). iCloud is `caldav-mcp`; there is no
icloud-named package.
Live identity already uses native URL-only Todoist
(`https://ai.todoist.net/mcp`). Do not put `mcp-remote` back.

`disk-gate.sh --check` refuses tight disk and unopenable existing
`*.db` files (`SELECT 1` only; no `PRAGMA quick_check`). Archive
wrappers inspect zip and tar members (`check-archive-members.py`).
Debian snapshot date (`DEBIAN_SNAPSHOT`) is the apt version lock —
no floating sid, no per-package `pkg=ver` pins (those rot on every
snapshot bump). Login shells source `/etc/profile.d/hermes-path.sh`
so PATH prefixes match image ENV (`/opt/hermes/bin`, venv,
`/command`) and `umask 002`. Compose may still put
`/opt/data/.local/bin` first until overlay retirement.
CI also runs `adversarial/tar-slip.sh`,
`adversarial/imagemagick-url.sh`, and
`adversarial/office-macro.sh` (VBA/AutoOpen + external OLE;
MacroSecurityLevel 3, no LibreOffice `--safe-mode`).
`check-archive-members.py` applies the zip bomb ratio to tar
and to `7z` via the origin/libexec binary (never PATH `7z`).
`tar.zst` listing uses origin `bsdtar` (never PATH wrap).
CI runs `adversarial/high-ratio-zstd.sh`.
Wrap caps output bytes and bounds tmpfs (`--size`, default
256 MiB). Receipt is content-free (tool, real path, limits,
input/output hashes, exit, elapsed). CI runs
`adversarial/high-ratio-7z.sh`. Wrap adds
`--rlimit-cpu` inside bwrap (not parent `ulimit -t`).
`hermes-image-doctor --full` runs `golden-smoke.sh` when
fixtures exist (test image); published runtime skips.
GNU `tar` stays unwrapped so boot
scripts keep `/usr/bin/tar`; `bsdtar` and `7z` are the sandboxed
extractors. Wrap must not `ulimit -u` in the parent — that is
per-UID and starves forks on a busy hermes box; the cap stays
inside bwrap. Stored golden fixtures include `golden.heif`, `golden.zip.zst`,
and `golden.7z` (smoke still does a relative-path `7z a`
round-trip). Lint runs
`tests/sera_toolbox`. SPDX/CycloneDX stay CI artifacts attached
to the digest, not files under `/etc/hermes`.

Reproducible-build experiment (two clean builds, compare IDs) is
`workflow_dispatch` only: `.github/workflows/reproducible-build.yml`.
Exact digest match is not a release blocker.
