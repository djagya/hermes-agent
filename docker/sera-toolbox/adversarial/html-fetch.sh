#!/usr/bin/env bash
# WeasyPrint must not fetch remote CSS/images (url_fetcher + unshare-net).
set -euo pipefail

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
printf '%s\n' '<html><head>
<link rel="stylesheet" href="http://127.0.0.1:9/x.css">
</head><body><p>local-only</p>
<img src="http://127.0.0.1:9/x.png"></body></html>' > ext.html

set +e
weasyprint ext.html out.pdf >out 2>err
status=$?
set -e
if grep -qi '127.0.0.1' out.pdf 2>/dev/null; then
  echo "FAIL weasyprint embedded fetched host" >&2
  exit 1
fi
if grep -qi 'example.com' err out 2>/dev/null; then
  echo "FAIL weasyprint logged an external fetch" >&2
  cat err >&2 || true
  exit 1
fi
echo "OK weasyprint HTML fetch refused (exit=$status)"
