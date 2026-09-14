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
- max_image_bytes: `4943659885` (slack `449344360`)
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
  "max_cold_help_ms": 60000,
  "max_fixable_critical": 0,
  "max_fixable_high": 0,
  "max_idle_rss_kb": 1048576,
  "max_image_bytes": 4943659885,
  "max_largest_layer_bytes": 2500000000,
  "max_offline_cache_write_bytes": 67108864,
  "max_shutdown_ms": 90000,
  "max_warm_help_ms": 30000,
  "measured_image_bytes": 4494315525,
  "note": "Last published B.3 digest e3391400e / sha256:d63fc555. CI run 34107241626 measured 4494315525 bytes (4286 MiB) on the test-stage image. max_image_bytes is first-B 4341e0fd plus 10% slack. Live B.2 unpacked size on the box is smaller (docker inspect Size) because layer accounting differs from the CI test target. Trivy still fails the job on any fixable CRITICAL/HIGH.",
  "schema": 1
}
```

## Old vs new

| pin | current | latest | notes |
|---|---|---|---|
| debian:13.4 | `sha256:e2d08da6f42ef4b09b165d55528a12727aeed8240dc9edf888e3ec07e10ef9da` | `sha256:e2d08da6f42ef4b09b165d55528a12727aeed8240dc9edf888e3ec07e10ef9da` | same [release](https://hub.docker.com/_/debian) |
| node:26-bookworm-slim | `sha256:9e6f9357d371591e32ab6f2d8a26d63bdd0d17c29eee3f4f3e7e454d9634bf73` | `sha256:cd9f682fa2885cd1056e830424764158570061c59736a1da836bc3d73df095ae` | bump [release](https://hub.docker.com/_/node) |
| s6-overlay | `3.2.3.0` | `3.2.3.2` | bump [release](https://github.com/just-containers/s6-overlay/releases/tag/v3.2.3.2) |
| gh | `2.100.0` | `2.100.0` | same [release](https://github.com/cli/cli/releases/tag/v2.100.0) |
| gitleaks | `8.30.1` | `8.30.1` | same [release](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1) |
| tirith | `0.3.3` | `0.4.2` | bump [release](https://github.com/sheeki03/tirith/releases/tag/v0.4.2) |
| op | `2.35.0` | `2.35.0` | same [release](https://app-updates.agilebits.com/product_history/CLI2) |
| uv image | `0.11.6-python3.13-trixie@sha256:b3c543b6c4f23a5f2df22866bd7857e5d304b67a564f4feab6ac22044dde719b` | `0.12.13-python3.13-trixie@sha256:8add4f333ff97df36c18f42e8082301a2829ccf0cef995538a7d9af0ed0b4183` | bump [release](https://github.com/astral-sh/uv/releases/tag/0.12.13) |
| sqlite-autoconf | `3530400` | `3530400` | same [release](https://sqlite.org/download.html) |
| DEBIAN_SNAPSHOT | `20260907T000000Z` | `20260907T000000Z` | same [release](https://snapshot.debian.org/) |
| himalaya | `b1f6dece32c3` | `2.1.0` | bump [release](https://github.com/pimalaya/himalaya/releases/tag/v2.1.0) |
| weasyprint | `69.0` | `70.0` | bump [release](https://pypi.org/project/weasyprint/70.0/) |
| python-docx | `1.2.0` | `1.2.0` | same [release](https://pypi.org/project/python-docx/1.2.0/) |
| openpyxl | `3.1.5` | `3.1.5` | same [release](https://pypi.org/project/openpyxl/3.1.5/) |
| yt-dlp | `2026.08.19` | `2026.8.19` | bump [release](https://pypi.org/project/yt-dlp/2026.8.19/) |
| clickup-mcp | `1.8.0` | `1.9.0` | bump [release](https://www.npmjs.com/package/@hauptsache.net/clickup-mcp/v/1.9.0) |
| caldav-mcp | `0.10.0` | `0.10.0` | same [release](https://www.npmjs.com/package/caldav-mcp/v/0.10.0) |

## Applied on this branch

- node digest x1
- s6-overlay 3.2.3.0 -> 3.2.3.2
- tirith 0.3.3 -> 0.4.2
- uv 0.11.6-python3.13-trixie -> 0.12.13-python3.13-trixie

