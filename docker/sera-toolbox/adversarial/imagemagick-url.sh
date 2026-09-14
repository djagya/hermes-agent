#!/usr/bin/env bash
# ImageMagick URL/HTTP/HTTPS coders must stay disabled.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"

set +e
identify 'https://example.com/x.png' >out 2>err
status=$?
set -e
if [ "$status" -eq 0 ]; then
  echo "FAIL identify accepted a URL coder input" >&2
  cat err >&2 || true
  exit 1
fi
if [ -s out.png ]; then
  echo "FAIL identify wrote a fetched raster" >&2
  exit 1
fi
echo "OK identify URL coder refused (exit=$status)"
