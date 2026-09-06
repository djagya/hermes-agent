#!/usr/bin/env python3
"""Build a review-only pin-refresh report and optionally apply bumps.

Never merges, publishes, or deploys. Lookups that fail stay in the
report as errors; --apply skips those pins.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
NET_BINS = ROOT / "docker/sera-toolbox/install-network-bins.sh"
UA = "djagya-hermes-agent-pin-refresh/1"


def fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def fetch_text(url: str) -> str:
    return fetch(url).decode("utf-8", "replace")


def gh_latest_tag(repo: str) -> tuple[str, str]:
    payload = json.loads(fetch_text(f"https://api.github.com/repos/{repo}/releases/latest"))
    tag = str(payload.get("tag_name") or "").lstrip("v")
    html = str(payload.get("html_url") or f"https://github.com/{repo}/releases")
    if not tag:
        raise RuntimeError(f"no latest tag for {repo}")
    return tag, html


def dockerhub_digest(image: str, tag: str) -> str:
    url = f"https://hub.docker.com/v2/namespaces/library/repositories/{image}/tags/{tag}"
    payload = json.loads(fetch_text(url))
    digest = str(payload.get("digest") or "")
    if not digest.startswith("sha256:"):
        raise RuntimeError(f"no digest for {image}:{tag}")
    return digest


def sha256_url(url: str) -> str:
    data = fetch(url, timeout=120)
    return hashlib.sha256(data).hexdigest()


def parse_dockerfile() -> dict[str, str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    def arg(name: str) -> str:
        m = re.search(rf"^ARG {re.escape(name)}=(.+)$", text, re.M)
        return m.group(1).strip() if m else ""

    debian = re.search(r"FROM debian:13\.4@(sha256:[0-9a-f]+)", text)
    node = re.search(r"FROM node:26-bookworm-slim@(sha256:[0-9a-f]+)", text)
    uv = re.search(r"FROM ghcr.io/astral-sh/uv:([^@\s]+)@(sha256:[0-9a-f]+)", text)
    return {
        "debian_digest": debian.group(1) if debian else "",
        "node_digest": node.group(1) if node else "",
        "uv_tag": uv.group(1) if uv else "",
        "uv_digest": uv.group(2) if uv else "",
        "s6": arg("S6_OVERLAY_VERSION"),
        "s6_noarch": arg("S6_OVERLAY_NOARCH_SHA256"),
        "s6_amd64": arg("S6_OVERLAY_X86_64_SHA256"),
        "s6_arm64": arg("S6_OVERLAY_AARCH64_SHA256"),
        "s6_symlinks": arg("S6_OVERLAY_SYMLINKS_SHA256"),
        "sqlite": arg("SQLITE_AUTOCONF_VERSION"),
        "snapshot": arg("DEBIAN_SNAPSHOT"),
    }


def parse_net_bins() -> dict[str, str]:
    text = NET_BINS.read_text(encoding="utf-8")
    gh = re.search(r"cli/releases/download/v([0-9.]+)/", text)
    gl = re.search(r"gitleaks/releases/download/v([0-9.]+)/", text)
    return {
        "gh": gh.group(1) if gh else "",
        "gitleaks": gl.group(1) if gl else "",
    }


def replace_all(path: Path, old: str, new: str) -> int:
    if not old or old == new:
        return 0
    text = path.read_text(encoding="utf-8")
    if old not in text:
        return 0
    path.write_text(text.replace(old, new), encoding="utf-8")
    return text.count(old)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    pins = parse_dockerfile()
    bins = parse_net_bins()
    rows: list[tuple[str, str, str, str]] = []
    errors: list[str] = []
    applied: list[str] = []

    def row(name: str, old: str, new: str, link: str) -> None:
        rows.append((name, old, new or "(lookup failed)", link))

    try:
        debian_new = dockerhub_digest("debian", "13.4")
        row("debian:13.4", pins["debian_digest"], debian_new, "https://hub.docker.com/_/debian")
        if args.apply and debian_new and debian_new != pins["debian_digest"]:
            n = replace_all(DOCKERFILE, pins["debian_digest"], debian_new)
            if n:
                applied.append(f"debian digest x{n}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"debian: {exc}")
        row("debian:13.4", pins["debian_digest"], "", "")

    try:
        node_new = dockerhub_digest("node", "26-bookworm-slim")
        row("node:26-bookworm-slim", pins["node_digest"], node_new, "https://hub.docker.com/_/node")
        if args.apply and node_new and node_new != pins["node_digest"]:
            n = replace_all(DOCKERFILE, pins["node_digest"], node_new)
            if n:
                applied.append(f"node digest x{n}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"node: {exc}")
        row("node:26-bookworm-slim", pins["node_digest"], "", "")

    try:
        s6_new, s6_url = gh_latest_tag("just-containers/s6-overlay")
        row("s6-overlay", pins["s6"], s6_new, s6_url)
        if args.apply and s6_new and s6_new != pins["s6"]:
            base = f"https://github.com/just-containers/s6-overlay/releases/download/v{s6_new}"
            noarch = sha256_url(f"{base}/s6-overlay-noarch.tar.xz")
            amd64 = sha256_url(f"{base}/s6-overlay-x86_64.tar.xz")
            arm64 = sha256_url(f"{base}/s6-overlay-aarch64.tar.xz")
            sym = sha256_url(f"{base}/s6-overlay-symlinks-noarch.tar.xz")
            text = DOCKERFILE.read_text(encoding="utf-8")
            text = text.replace(f"S6_OVERLAY_VERSION={pins['s6']}", f"S6_OVERLAY_VERSION={s6_new}")
            text = text.replace(pins["s6_noarch"], noarch)
            text = text.replace(pins["s6_amd64"], amd64)
            text = text.replace(pins["s6_arm64"], arm64)
            text = text.replace(pins["s6_symlinks"], sym)
            DOCKERFILE.write_text(text, encoding="utf-8")
            applied.append(f"s6-overlay {pins['s6']} -> {s6_new}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"s6: {exc}")
        row("s6-overlay", pins["s6"], "", "https://github.com/just-containers/s6-overlay/releases")

    try:
        gh_new, gh_url = gh_latest_tag("cli/cli")
        row("gh", bins["gh"], gh_new, gh_url)
        if args.apply and gh_new and gh_new != bins["gh"]:
            amd = sha256_url(
                f"https://github.com/cli/cli/releases/download/v{gh_new}/gh_{gh_new}_linux_amd64.tar.gz"
            )
            arm = sha256_url(
                f"https://github.com/cli/cli/releases/download/v{gh_new}/gh_{gh_new}_linux_arm64.tar.gz"
            )
            text = NET_BINS.read_text(encoding="utf-8")
            text = text.replace(f"v{bins['gh']}/gh_{bins['gh']}_", f"v{gh_new}/gh_{gh_new}_")
            # After URL rewrite, sha is still old. Patch amd64 then arm64.
            text = re.sub(
                rf'(gh_url="https://github.com/cli/cli/releases/download/v{re.escape(gh_new)}/gh_{re.escape(gh_new)}_linux_amd64.tar.gz"\n    gh_sha=")[0-9a-f]+(")',
                rf"\g<1>{amd}\2",
                text,
                count=1,
            )
            text = re.sub(
                rf'(gh_url="https://github.com/cli/cli/releases/download/v{re.escape(gh_new)}/gh_{re.escape(gh_new)}_linux_arm64.tar.gz"\n    gh_sha=")[0-9a-f]+(")',
                rf"\g<1>{arm}\2",
                text,
                count=1,
            )
            NET_BINS.write_text(text, encoding="utf-8")
            applied.append(f"gh {bins['gh']} -> {gh_new}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"gh: {exc}")
        row("gh", bins["gh"], "", "https://github.com/cli/cli/releases")

    try:
        gl_new, gl_url = gh_latest_tag("gitleaks/gitleaks")
        row("gitleaks", bins["gitleaks"], gl_new, gl_url)
        if args.apply and gl_new and gl_new != bins["gitleaks"]:
            amd = sha256_url(
                f"https://github.com/gitleaks/gitleaks/releases/download/v{gl_new}/gitleaks_{gl_new}_linux_x64.tar.gz"
            )
            arm = sha256_url(
                f"https://github.com/gitleaks/gitleaks/releases/download/v{gl_new}/gitleaks_{gl_new}_linux_arm64.tar.gz"
            )
            text = NET_BINS.read_text(encoding="utf-8")
            text = text.replace(f"v{bins['gitleaks']}/gitleaks_{bins['gitleaks']}_", f"v{gl_new}/gitleaks_{gl_new}_")
            text = re.sub(
                rf'(gl_url="https://github.com/gitleaks/gitleaks/releases/download/v{re.escape(gl_new)}/gitleaks_{re.escape(gl_new)}_linux_x64.tar.gz"\n    gl_sha=")[0-9a-f]+(")',
                rf"\g<1>{amd}\2",
                text,
                count=1,
            )
            text = re.sub(
                rf'(gl_url="https://github.com/gitleaks/gitleaks/releases/download/v{re.escape(gl_new)}/gitleaks_{re.escape(gl_new)}_linux_arm64.tar.gz"\n    gl_sha=")[0-9a-f]+(")',
                rf"\g<1>{arm}\2",
                text,
                count=1,
            )
            NET_BINS.write_text(text, encoding="utf-8")
            applied.append(f"gitleaks {bins['gitleaks']} -> {gl_new}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"gitleaks: {exc}")
        row("gitleaks", bins["gitleaks"], "", "https://github.com/gitleaks/gitleaks/releases")

    row("uv image tag", pins["uv_tag"], pins["uv_tag"], "https://github.com/astral-sh/uv/pkgs/container/uv")
    row("sqlite-autoconf", pins["sqlite"], pins["sqlite"], "https://sqlite.org/download.html")
    row("DEBIAN_SNAPSHOT", pins["snapshot"], pins["snapshot"], "https://snapshot.debian.org/")

    budget = (ROOT / "docker/sera-toolbox/image-budget.json").read_text(encoding="utf-8")
    lines = [
        "# Pin refresh",
        "",
        "Review-only. No unattended merge, publish, or deploy.",
        "CI on this branch must fill SBOM/CVE delta and image-size",
        "delta before anyone merges.",
        "",
        "## Accepted budget",
        "",
        "```json",
        budget.rstrip(),
        "```",
        "",
        "## Old vs new",
        "",
        "| pin | current | latest | notes |",
        "|---|---|---|---|",
    ]
    for name, old, new, link in rows:
        status = "same" if old and old == new else ("bump" if new and not new.startswith("(") else "lookup")
        ref = f"[release]({link})" if link else ""
        lines.append(f"| {name} | `{old}` | `{new}` | {status} {ref} |".rstrip())
    lines.append("")
    if applied:
        lines.append("## Applied on this branch")
        lines.append("")
        for item in applied:
            lines.append(f"- {item}")
        lines.append("")
    if errors:
        lines.append("## Lookup errors")
        lines.append("")
        for err in errors:
            lines.append(f"- {err}")
        lines.append("")
    Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.report} applied={applied or 'none'} errors={len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
