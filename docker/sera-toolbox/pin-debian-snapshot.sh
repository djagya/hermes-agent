#!/bin/sh
# Freeze apt on a dated Debian snapshot. Requires DEBIAN_SNAPSHOT.
set -eu
snap="${DEBIAN_SNAPSHOT:?DEBIAN_SNAPSHOT unset}"
# Official Debian images delete apt caches after every RUN. That fights
# BuildKit cache mounts; drop it so snapshot metadata can persist.
rm -f /etc/apt/apt.conf.d/docker-clean
printf 'Acquire::Check-Valid-Until "false";\nAcquire::Retries "3";\n' \
  > /etc/apt/apt.conf.d/99snapshot
rm -f /etc/apt/sources.list
rm -f /etc/apt/sources.list.d/debian.sources
# HTTP on purpose: debian-slim has no ca-certificates, so the first
# apt-get update cannot speak HTTPS to snapshot.debian.org (CI saw
# SSL certificate verify failed, then "Unable to locate package").
# Release files stay GPG-signed via debian-archive-keyring. Debian
# documents http:// as the supported fallback.
cat > /etc/apt/sources.list.d/debian.sources <<EOF
Types: deb
URIs: http://snapshot.debian.org/archive/debian/${snap}/
Suites: trixie
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
