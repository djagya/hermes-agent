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
    --build-arg DEBIAN_SNAPSHOT=20260905T000000Z \
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

ClickUp 1.8.0 and `caldav-mcp` 0.10.0 are baked. Live hooks stay
`npx -y @pinned` until this image is the box pin; then switch to
`--no-install`. Todoist stays `mcp-remote` until native URL-only
OAuth keeps `/mcp`. Do not switch that transport in this image.

Reproducible-build experiment (two clean builds, compare IDs) is
`workflow_dispatch` only: `.github/workflows/reproducible-build.yml`.
Exact digest match is not a release blocker.
