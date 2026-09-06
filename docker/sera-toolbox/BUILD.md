# Building the Sera toolbox image

Matches CI in `.github/workflows/fork-release-image.yml`. Published
target is `runtime`. There is no separate `test` stage — smoke is an
entrypoint on `runtime`.

```bash
# Local load (amd64). Pass the same args CI uses.
docker buildx build \
  --target runtime \
  --load \
  --platform linux/amd64 \
  --build-arg HERMES_GIT_SHA="$(git rev-parse HEAD)" \
  --build-arg HERMES_IMAGE_NAME=ghcr.io/djagya/hermes-agent \
  --build-arg HERMES_BUILD_REF="$(git rev-parse --abbrev-ref HEAD)" \
  --build-arg DEBIAN_SNAPSHOT=20260508T000000Z \
  -t ghcr.io/djagya/hermes-agent:local \
  -f Dockerfile \
  .

# Toolbox + golden + adversarial (needs the monolith seccomp profile).
docker run --rm --network none \
  --security-opt seccomp=docker/sera-toolbox/seccomp-bwrap.json \
  --entrypoint /opt/hermes/docker/sera-toolbox/smoke.sh \
  ghcr.io/djagya/hermes-agent:local
```

Builder stage keeps compilers. Do not publish `builder`.

Reproducible-build experiment (two clean builds, compare IDs) is
`workflow_dispatch` only: `.github/workflows/reproducible-build.yml`.
Exact digest match is not a release blocker.
