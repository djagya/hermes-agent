#!/usr/bin/env bash
# Checksum-pinned network CLIs. Not sandboxed (need net + creds).
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
    gh_url="https://github.com/cli/cli/releases/download/v2.100.0/gh_2.100.0_linux_amd64.tar.gz"
    gh_sha="e4d4bb4498e8d007abe545b6568926793ace1b6447da598294a610018cb164be"
    gl_url="https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz"
    gl_sha="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
    ti_url="https://github.com/sheeki03/tirith/releases/download/v0.3.3/tirith-x86_64-unknown-linux-gnu.tar.gz"
    ti_sha="6cdbe35e8f9ccf42e70ad95b501c93cd218ac18201c3df958d54f6ba0d995ce2"
    op_url="https://cache.agilebits.com/dist/1P/op2/pkg/v2.35.0/op_linux_amd64_v2.35.0.zip"
    op_sha="4457ade59850b852c64c77164235b34dd0b984ef7826eb0ccd32f1fd78a2ceb7"
    ;;
  arm64)
    gh_url="https://github.com/cli/cli/releases/download/v2.100.0/gh_2.100.0_linux_arm64.tar.gz"
    gh_sha="ea4e7a581a32ccad6cc7923cb1576ac5859ba4b9a16ab22eb8f8a96e78e2e961"
    gl_url="https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_arm64.tar.gz"
    gl_sha="e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"
    ti_url="https://github.com/sheeki03/tirith/releases/download/v0.3.3/tirith-aarch64-unknown-linux-gnu.tar.gz"
    ti_sha="c784233083003a6a1533db9ebba30b1a7bb7cefaa239db6ca121598b384cca1a"
    op_url="https://cache.agilebits.com/dist/1P/op2/pkg/v2.35.0/op_linux_arm64_v2.35.0.zip"
    op_sha="28153b3e1b379cc117a2b8478fc29c73e4a391d0a9b7876c360d305e98390a78"
    ;;
  *)
    echo "unsupported TARGETARCH=${arch}" >&2
    exit 1
    ;;
esac

fetch "$gh_url" "$tmp/gh.tgz" "$gh_sha"
tar -C "$tmp" -xzf "$tmp/gh.tgz"
install -m 0755 "$tmp"/gh_*/bin/gh /usr/local/bin/gh

fetch "$gl_url" "$tmp/gitleaks.tgz" "$gl_sha"
tar -C "$tmp" -xzf "$tmp/gitleaks.tgz"
install -m 0755 "$tmp/gitleaks" /usr/local/bin/gitleaks

fetch "$ti_url" "$tmp/tirith.tgz" "$ti_sha"
tar -C "$tmp" -xzf "$tmp/tirith.tgz"
ti_bin="$(find "$tmp" -type f -name tirith | head -n 1)"
install -m 0755 "$ti_bin" /usr/local/bin/tirith

fetch "$op_url" "$tmp/op.zip" "$op_sha"
unzip -qo "$tmp/op.zip" -d "$tmp/op"
install -m 0755 "$tmp/op/op" /usr/local/bin/op

gh --version
gitleaks version
tirith --version
op --version
