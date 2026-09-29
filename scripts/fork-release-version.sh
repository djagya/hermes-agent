#!/usr/bin/env bash
# Derive a fork release's version from upstream's release tags (fork-release-image.yml).
#
# The fork carries upstream code past its release line, so a hand-kept pyproject
# version goes stale: the upstream-sync release shipped all of v2026.9.24 (0.21.5)
# and more while still saying 0.21.4, and its image stamp carried no base version
# at all ("unknown" to plugins' requires_hermes). The base is the version the
# nearest upstream CalVer release tag in HEAD's history shipped (read from that
# tag's pyproject, the same rule as hermes_cli.version_info); the distance is the
# commit count past it.
#
# Prints GITHUB_OUTPUT lines: tag=, base=, distance=, display=.
# Exits 1 when no release tag is reachable, or with --check when the committed
# pyproject version differs from the derived base (the fix is printed).
set -euo pipefail

check=0
[ "${1:-}" = "--check" ] && check=1

tag="$(git describe --tags --abbrev=0 --match 'v2[0-9][0-9][0-9].*' HEAD 2>/dev/null || true)"
if [ -z "$tag" ]; then
  echo "ERROR: no upstream vYYYY.M.D release tag reachable from HEAD; fetch upstream tags first" >&2
  exit 1
fi
base="$(git show "$tag:pyproject.toml" | python3 -c 'import sys, tomllib; print(tomllib.load(sys.stdin.buffer)["project"]["version"])')"
if ! [[ "$base" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "ERROR: $tag pyproject version '$base' is not X.Y.Z" >&2
  exit 1
fi
distance="$(git rev-list --count "$tag..HEAD")"
short="$(git rev-parse --short=7 HEAD)"
display="$base"
[ "$distance" -gt 0 ] && display="$base+$distance.g$short"

if [ "$check" = 1 ]; then
  committed="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
  if [ "$committed" != "$base" ]; then
    echo "ERROR: pyproject.toml version $committed != $base (shipped by upstream $tag, the newest release in this tree)." >&2
    echo "       Set project.version and the hermes-agent entry in uv.lock to $base, then push." >&2
    exit 1
  fi
fi

printf 'tag=%s\nbase=%s\ndistance=%s\ndisplay=%s\n' "$tag" "$base" "$distance" "$display"
