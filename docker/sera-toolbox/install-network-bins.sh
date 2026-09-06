#!/usr/bin/env bash
# Checksum-pinned network CLIs. Not sandboxed (need net + creds).
# himalaya/op stay host overlays: the live himalaya is a custom feature
# build, not the stock pimalaya tarball.
set -euo pipefail

arch="${TARGETARCH:-amd64}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fetch() {
  local url="$1" dest="$2" sha="$3"
  curl -fsSL --retry 3 -o "$dest" "$url"
  echo "${sha}  ${dest}" | sha256sum -c -
}

case "$arch" in
  amd64)
    gh_url="https://github.com/cli/cli/releases/download/v2.97.0/gh_2.97.0_linux_amd64.tar.gz"
    gh_sha="a2c9b8497e1f85b1ad0dfcb78b5a622e098801b8e461e459e88e1ee12f018112"
    gl_url="https://github.com/gitleaks/gitleaks/releases/download/v8.28.0/gitleaks_8.28.0_linux_x64.tar.gz"
    gl_sha="a65b5253807a68ac0cafa4414031fd740aeb55f54fb7e55f386acb52e6a840eb"
    ti_url="https://github.com/sheeki03/tirith/releases/download/v0.3.3/tirith-x86_64-unknown-linux-gnu.tar.gz"
    ti_sha="6cdbe35e8f9ccf42e70ad95b501c93cd218ac18201c3df958d54f6ba0d995ce2"
    ;;
  arm64)
    gh_url="https://github.com/cli/cli/releases/download/v2.97.0/gh_2.97.0_linux_arm64.tar.gz"
    gh_sha="73ea440ecad9c9e284429997ee6f93577bc6f7bc6fba357ef62c53ad8fb641a5"
    gl_url="https://github.com/gitleaks/gitleaks/releases/download/v8.28.0/gitleaks_8.28.0_linux_arm64.tar.gz"
    gl_sha="eff65261156100e5d94a6b3dec313d532fddfe19ae1590bf7a2b4f2699128356"
    ti_url="https://github.com/sheeki03/tirith/releases/download/v0.3.3/tirith-aarch64-unknown-linux-gnu.tar.gz"
    ti_sha="c784233083003a6a1533db9ebba30b1a7bb7cefaa239db6ca121598b384cca1a"
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

gh --version
gitleaks version
tirith --version
