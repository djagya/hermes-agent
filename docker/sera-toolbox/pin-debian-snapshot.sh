#!/bin/sh
# Freeze apt on a dated Debian snapshot. Requires DEBIAN_SNAPSHOT.
set -eu
snap="${DEBIAN_SNAPSHOT:?DEBIAN_SNAPSHOT unset}"
printf 'Acquire::Check-Valid-Until "false";\nAcquire::Retries "3";\n' \
  > /etc/apt/apt.conf.d/99snapshot
rm -f /etc/apt/sources.list
rm -f /etc/apt/sources.list.d/debian.sources
cat > /etc/apt/sources.list.d/debian.sources <<EOF
Types: deb
URIs: https://snapshot.debian.org/archive/debian/${snap}/
Suites: trixie
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
