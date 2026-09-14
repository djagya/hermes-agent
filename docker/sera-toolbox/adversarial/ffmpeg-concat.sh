#!/usr/bin/env bash
# Concat/HTTP inputs must not reach the network.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
printf "file 'http://example.com/x.mp4'\n" > list.txt

set +e
ffmpeg -nostdin -f concat -safe 0 -protocol_whitelist file,http,https,tcp,tls \
  -i list.txt -f null - >out 2>err
status=$?
set -e
if [ "$status" -eq 0 ]; then
  echo "FAIL ffmpeg concat fetched HTTP" >&2
  cat err >&2 || true
  exit 1
fi
echo "OK ffmpeg concat/HTTP blocked"
