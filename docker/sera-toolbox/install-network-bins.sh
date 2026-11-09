#!/usr/bin/env bash
# Checksum-pinned network CLIs. Not sandboxed (need net + creds).
# gitleaks and tirith are no longer baked (D15): tirith is dead since config
# v50, and leak scans run on the host with their own pinned gitleaks.
# himalaya stays a host overlay: live binary is v2.0.0 with
# +gmail +msgraph +vendored (custom feature build), not stock pimalaya.
set -euo pipefail

arch="${TARGETARCH:-amd64}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
# install-wrappers.sh may already have replaced PATH unzip/7z with the
# Bubblewrap sandbox. That wrapper refuses absolute /tmp paths and
# needs bwrap, which the builder may not have. These archives are
# checksum-pinned; extract them with real binutils.
PATH="/usr/bin:/bin:${PATH}"
export PATH

fetch() {
  local url="$1" dest="$2" sha="$3"
  curl -fsSL --retry 3 -o "$dest" "$url"
  echo "${sha}  ${dest}" | sha256sum -c -
}

case "$arch" in
  amd64)
    gh_url="https://github.com/cli/cli/releases/download/v2.103.0/gh_2.103.0_linux_amd64.tar.gz"
    gh_sha="fdd768eb70f6409f80fcfa9f3a1c0adf0fc98d827ab3ce0bed2ac22762c5d786"
    op_url="https://cache.agilebits.com/dist/1P/op2/pkg/v2.41.1/op_linux_amd64_v2.41.1.zip"
    op_sha="e52476700cba9202ac7469bdfb2648ecdbf3438ac85822010f77230139e74966"
    ;;
  arm64)
    gh_url="https://github.com/cli/cli/releases/download/v2.103.0/gh_2.103.0_linux_arm64.tar.gz"
    gh_sha="7f322df34e624d14f5ec0c4acb121fd6dfa14392fdbbd1066aa8e0a05ac3721e"
    op_url="https://cache.agilebits.com/dist/1P/op2/pkg/v2.41.1/op_linux_arm64_v2.41.1.zip"
    op_sha="89abd60c17d5c614066c20e6abed816449eb018c5e0679ed63d57071c7bee85e"
    ;;
  *)
    echo "unsupported TARGETARCH=${arch}" >&2
    exit 1
    ;;
esac

fetch "$gh_url" "$tmp/gh.tgz" "$gh_sha"
tar -C "$tmp" -xzf "$tmp/gh.tgz"
install -m 0755 "$tmp"/gh_*/bin/gh /usr/local/bin/gh

fetch "$op_url" "$tmp/op.zip" "$op_sha"
unzip -qo "$tmp/op.zip" -d "$tmp/op"
install -m 0755 "$tmp/op/op" /usr/local/bin/op

gh --version
op --version
