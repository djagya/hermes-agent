#!/bin/sh
# s6-overlay shim. The real logic lives in docker/stage2-hook.sh, invoked
# by /etc/cont-init.d/01-hermes-setup (installed by the Dockerfile). This
# file exists so external references to docker/entrypoint.sh still work,
# but it's no longer the image ENTRYPOINT — entrypoint-dispatch.sh is.
#
# Plan 5c: when someone still invokes this path, keep the pre-s6 contract
# (bootstrap, then exec CMD). Same sequence as the non-PID-1 fallback in
# entrypoint-dispatch.sh. Do not exec stage2 with the CMD args — stage2
# is bootstrap only.
#
# Deprecation: migrate wrappers to the image ENTRYPOINT. This shim stays
# one more release so those wrappers keep running the requested command.
set -e
echo "[hermes] WARNING: docker/entrypoint.sh is a deprecated shim under " \
    "s6-overlay. The container's real ENTRYPOINT is " \
    "entrypoint-dispatch.sh (which delegates to /init + main-wrapper.sh " \
    "when PID 1). This shim runs the stage2 hook and then execs " \
    "main-wrapper.sh so the CMD still runs. Drop the override." >&2
export PATH="/command:/package/admin/s6/command:${PATH}"
/opt/hermes/docker/stage2-hook.sh
exec /opt/hermes/docker/main-wrapper.sh "$@"
