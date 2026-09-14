#!/usr/bin/env bash
# Parsers must not reach the network even if asked.
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"

fail=0
if convert "https://example.com/x.png" out.png 2>err; then
  echo "FAIL convert fetched URL" >&2
  fail=1
else
  echo "OK convert URL blocked"
fi

if ffmpeg -nostdin -i "http://example.com/x.mp4" -f null - 2>err2; then
  echo "FAIL ffmpeg fetched URL" >&2
  fail=1
else
  echo "OK ffmpeg URL blocked"
fi

exit "$fail"
