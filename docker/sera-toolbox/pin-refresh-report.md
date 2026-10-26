# Pin refresh

Review-only. No unattended merge, publish, or deploy.
This job does not rebuild. Size, CVE policy, and SBOM path
below are the last accepted `image-budget.json` baseline.
Do not merge until `fork-release-image` on this branch stays
green (measure-image-budget, Trivy fixable CRITICAL/HIGH,
named SPDX/CycloneDX artifact `hermes-agent-sbom`).

## Last accepted image (size / CVE / SBOM)

- digest: `sha256:d63fc55532a760f7981baa6de696d44ed8dd778c60f0aa707f703c3f7ca24c30`
- git: `e3391400e6c04dc30c0bebe2c797f0f07ca0ef09`
- measured_image_bytes: `4494315525`
- max_image_bytes: `5170000000` (slack `675684475`)
- max_largest_layer_bytes: `2500000000`
- max_fixable_critical: `0`
- max_fixable_high: `0`
- SBOM: CI artifact `hermes-agent-sbom` on that green run
  (not baked under `/etc/hermes`).
- smoke: golden + adversarial in the same job (fail-closed).

## Accepted budget

```json
{
  "accepted_digest": "sha256:d63fc55532a760f7981baa6de696d44ed8dd778c60f0aa707f703c3f7ca24c30",
  "accepted_git_sha": "e3391400e6c04dc30c0bebe2c797f0f07ca0ef09",
  "du_margin_ratio": 0.01,
  "max_cold_help_ms": 60000,
  "max_fixable_critical": 0,
  "max_fixable_high": 0,
  "max_idle_rss_kb": 1048576,
  "max_image_bytes": 5170000000,
  "max_largest_layer_bytes": 2500000000,
  "max_offline_cache_write_bytes": 67108864,
  "max_shutdown_ms": 90000,
  "max_warm_help_ms": 30000,
  "measured_image_bytes": 4494315525,
  "note": "Last published B.3 digest e3391400e / sha256:d63fc555. CI run 34107241626 measured 4494315525 bytes (4286 MiB) on the CI-built image. max_image_bytes started as first-B 4341e0fd plus 10% slack (4943659885). Raised to 4960000000 after the 20260928 snapshot upgrades of util-linux, gzip, libpcre2, and libsqlite3 measured 4947449996 in CI. Raised to 5170000000 for the v0.22.2 sync (about +5%): the v0.21.7 release image measured 4919117397 bytes in CI, 41 MB under the old limit. CI builds --target runtime and measures docker image inspect .Size on overlay2 (uncompressed layer bytes). On a containerd-store host (the box gate) .Size adds the compressed blobs, about 1.45x, so measure-image-budget.sh measures du -sxb / inside the image instead; on the v0.21.7 release image that read 4890601244, 0.58% below CI. du_margin_ratio lowers max_image_bytes by 1% for a du measurement so a box pass implies a CI pass. Trivy still fails the job on any fixable CRITICAL/HIGH.",
  "schema": 1
}
```

## Old vs new

| pin | current | latest | notes |
|---|---|---|---|
| debian:13.4 | `sha256:e2d08da6f42ef4b09b165d55528a12727aeed8240dc9edf888e3ec07e10ef9da` | `sha256:e2d08da6f42ef4b09b165d55528a12727aeed8240dc9edf888e3ec07e10ef9da` | same [release](https://hub.docker.com/_/debian) |
| node:26-bookworm-slim | `` | `sha256:28147155d47dc31abe0a46e0d12b59b2fb3d0a2dcc5dc65fce76a074180b16fe` | bump [release](https://hub.docker.com/_/node) |
| s6-overlay | `3.2.3.0` | `3.2.3.2` | bump [release](https://github.com/just-containers/s6-overlay/releases/tag/v3.2.3.2) |
| gh | `2.101.0` | `2.103.2` | bump [release](https://github.com/cli/cli/releases/tag/v2.103.2) |
| gitleaks | `8.30.1` | `8.30.1` | same [release](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1) |
| tirith | `0.3.3` | `0.4.2` | bump [release](https://github.com/sheeki03/tirith/releases/tag/v0.4.2) |
| op | `2.39.0` | `2.39.0` | same [release](https://app-updates.agilebits.com/product_history/CLI2) |
| uv image | `@` | `0.13.5-python3.13-trixie@sha256:11f0329b64916f56cfdf532194564aa993b959568f8806fe55b76503ef3c2c54` | bump [release](https://github.com/astral-sh/uv/releases/tag/0.13.5) |
| sqlite-autoconf | `3530400` | `3530400` | same [release](https://sqlite.org/download.html) |
| DEBIAN_SNAPSHOT | `20261017T204620Z` | `20261017T204620Z` | same [release](https://snapshot.debian.org/) |
| himalaya | `b1f6dece32c3` | `2.2.1` | bump [release](https://github.com/pimalaya/himalaya/releases/tag/v2.2.1) |
| weasyprint | `` | `70.0` | bump [release](https://pypi.org/project/weasyprint/70.0/) |
| python-docx | `` | `1.2.0` | bump [release](https://pypi.org/project/python-docx/1.2.0/) |
| openpyxl | `` | `3.1.5` | bump [release](https://pypi.org/project/openpyxl/3.1.5/) |
| yt-dlp | `` | `2026.10.21` | bump [release](https://pypi.org/project/yt-dlp/2026.10.21/) |
| clickup-mcp | `1.8.0` | `1.9.0` | bump [release](https://www.npmjs.com/package/@hauptsache.net/clickup-mcp/v/1.9.0) |
| caldav-mcp | `0.10.0` | `0.10.3` | bump [release](https://www.npmjs.com/package/caldav-mcp/v/0.10.3) |

## Applied on this branch

- s6-overlay 3.2.3.0 -> 3.2.3.2
- gh 2.101.0 -> 2.103.2
- tirith 0.3.3 -> 0.4.2

