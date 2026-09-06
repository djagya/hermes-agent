#!/bin/sh
# /opt/hermes/bin/mcp — keep `mcp` on PATH from resolving to the PyPI
# developer CLI. Image PATH puts this directory first. Exec through the
# hermes shim so docker-exec-as-root still drops to uid hermes.
set -e
umask 002
exec /opt/hermes/bin/hermes mcp "$@"
