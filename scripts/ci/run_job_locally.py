#!/usr/bin/env python3
"""Run a GitHub Actions job from this repo's workflow on the local Docker host.

The fork's release image failed build-test nine times in a row during one
upgrade because each fix was verified by pushing. This runner executes the
workflow's own steps (``run:`` blocks verbatim, a small set of ``uses:``
actions translated) so the same gate runs before the push. It reads the
workflow file, so it never drifts from CI.

Supported ``uses:``:
  actions/checkout            skipped (run from a checkout with full history)
  docker/setup-buildx-action  skipped (the host's buildx builder is used)
  docker/build-push-action    ``docker buildx build`` with the same context,
                              file, target, platforms, tags, labels and
                              build-args; ``load`` honoured, never pushes,
                              no cache import/export
  actions/upload-artifact     skipped (publish's tested-image hand-off is CI-only;
                              the export step before it still runs)
  astral-sh/setup-uv          the pinned uv release, cached under --tools-dir
  ./.github/actions/retry     runs ``inputs.command`` (in working-directory)
Anything else fails loudly: an unsupported step is not a passed step.

Usage::

    python3 scripts/ci/run_job_locally.py [--workflow W] [--job J ...]
        [--ref-name BRANCH] [--tools-dir DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path


EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
SKIPPED_ACTIONS = (
    "actions/checkout@",
    "docker/setup-buildx-action@",
    "actions/upload-artifact@",
)


# Steps whose verdict depends on how the host's Docker stores images. GitHub
# runners use overlay2, where `docker image inspect .Size` is the uncompressed
# image; the containerd image store (Docker 29 default) reports compressed
# content (+ unpacked snapshots), so the same image measured 1.95 GB, 4.95 GB
# (CI) and 7.18 GB. On such a host a failure here is advisory; CI decides.
STORE_DEPENDENT_STEPS = ("Record image size budget",)


def host_uses_containerd_store() -> bool:
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{json .DriverStatus}}"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "io.containerd.snapshotter" in out


class StepError(RuntimeError):
    pass


class Context:
    def __init__(
        self, workflow: dict, ref_name: str, workspace: Path, before: str = "0" * 40
    ) -> None:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.github = {
            "sha": sha,
            "ref_name": ref_name,
            "workspace": str(workspace),
            "repository": "djagya/hermes-agent",
            "event_name": "push",
            "ref": f"refs/heads/{ref_name}",
        }
        self.env: dict[str, str] = {
            k: str(v) for k, v in (workflow.get("env") or {}).items()
        }
        self.steps: dict[str, dict] = {}
        self.containerd_store = False
        self.failed = False
        self.before = before
        self.path_prepend: list[str] = []
        self.container: str | None = (
            None  # runner-image container id; None = host shell
        )
        self.container_path = ""
        self.tmp_root: Path | None = None

    def lookup(self, path: str) -> str:
        parts = path.split(".")
        if parts[:2] == ["github", "event"]:
            # A local gate stands in for a push: `before` is the remote tip being moved.
            return {"before": self.before}.get(parts[2] if len(parts) > 2 else "", "")
        if parts[0] == "github":
            return str(self.github.get(parts[1], ""))
        if parts[0] == "env":
            return self.env.get(parts[1], "")
        if parts[0] == "steps" and len(parts) >= 3:
            step = self.steps.get(parts[1], {})
            if parts[2] == "outputs" and len(parts) == 4:
                return step.get("outputs", {}).get(parts[3], "")
            return str(step.get(parts[2], ""))
        if parts[0] == "runner" and parts[1] == "temp":
            return tempfile.gettempdir()
        raise StepError(f"unsupported expression path: {path}")

    def evaluate(self, expr: str) -> str:
        expr = expr.strip()
        if re.fullmatch(r"[A-Za-z_][\w.\-]*", expr):
            return self.lookup(expr)
        raise StepError(f"unsupported expression: {expr}")

    def render(self, value: object) -> str:
        return EXPR.sub(lambda m: self.evaluate(m.group(1)), str(value))

    def condition(self, cond: str | None) -> bool:
        """``if:`` for the shapes these workflows use: always(), success(), failure(),
        ``steps.X.outcome ==/!= 'v'`` joined by && / ||."""
        if cond is None:
            return not self.failed
        text = EXPR.sub(lambda m: m.group(1), str(cond)).strip()
        uses_status = re.search(r"\b(always|failure|success|cancelled)\(\)", text)

        def atom(m: re.Match) -> str:
            left, op, right = m.group(1), m.group(2), m.group(3)
            value = self.evaluate(left)
            return repr((value == right) if op == "==" else (value != right))

        py = re.sub(r"([\w.\-]+)\s*(==|!=)\s*'([^']*)'", atom, text)
        py = (
            py
            .replace("always()", "True")
            .replace("success()", repr(not self.failed))
            .replace("failure()", repr(self.failed))
            .replace("cancelled()", "False")
            .replace("&&", " and ")
            .replace("||", " or ")
            .replace("!", " not ")
        )
        if not re.fullmatch(r"(?:\s|True|False|and|or|not|\(|\))*", py):
            raise StepError(f"unsupported if: {cond}")
        result = bool(eval(py, {"__builtins__": {}}, {}))  # noqa: S307 — sanitized above
        return result if uses_status else (result and not self.failed)


def _read_kv_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^([^=<]+)<<(.+)$", line)
        if m:
            key, delim, buf = m.group(1), m.group(2), []
            i += 1
            while i < len(lines) and lines[i] != delim:
                buf.append(lines[i])
                i += 1
            if i >= len(lines):  # GitHub fails the step on an unterminated delimiter
                raise StepError(
                    f"unterminated {delim!r} block for {key!r} in {path.name}"
                )
            out[key] = "\n".join(buf)
        elif "=" in line:
            key, value = line.split("=", 1)
            out[key] = value
        i += 1
    return out


def run_shell(
    script: str, env: dict[str, str], cwd: Path, ctx: Context, step_id: str | None
) -> None:
    with tempfile.TemporaryDirectory(prefix="gate-step-", dir=ctx.tmp_root) as tmp:
        tmpd = Path(tmp)
        out, genv = tmpd / "output", tmpd / "env"
        out.touch()
        genv.touch()
        script_file = tmpd / "step.sh"
        script_file.write_text(script, encoding="utf-8")
        step_env = {
            **ctx.env,
            **env,
            "GITHUB_OUTPUT": str(out),
            "GITHUB_ENV": str(genv),
            "GITHUB_STEP_SUMMARY": str(tmpd / "summary"),
            "GITHUB_SHA": ctx.github["sha"],
            "GITHUB_REF_NAME": ctx.github["ref_name"],
            "GITHUB_WORKSPACE": ctx.github["workspace"],
            "GITHUB_REPOSITORY": ctx.github["repository"],
            "GITHUB_EVENT_NAME": ctx.github["event_name"],
            "CI": "true",
            "RUNNER_TEMP": tmp,
            # Same path on host and in the container: temp dirs a step bind-mounts into a
            # test container resolve, and nothing lands in the gate host's /tmp.
            "TMPDIR": tmp,
        }
        shell = ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script_file)]
        shim = tmpd / "shim"  # in both modes: a job must never get real sudo here
        shim.mkdir(exist_ok=True)
        (shim / "sudo").write_text(SUDO_SHIM, encoding="utf-8")
        (shim / "sudo").chmod(0o755)
        if ctx.container:
            # Runner-image parity: the step sees the image's tools, not the host's.
            step_env["PATH"] = ":".join([
                str(shim),
                *ctx.path_prepend,
                "/tmp/gate-home/.local/bin",
                ctx.container_path,
            ])
            cmd = ["docker", "exec", "-i", "--user", step_user(), "-w", str(cwd)]
            for key, value in step_env.items():
                cmd += ["-e", f"{key}={value}"]
            proc = subprocess.run([*cmd, ctx.container, *shell])
        else:
            step_env["PATH"] = ":".join([
                str(shim),
                *ctx.path_prepend,
                os.environ["PATH"],
            ])
            proc = subprocess.run(shell, cwd=cwd, env={**os.environ, **step_env})
        ctx.env.update(_read_kv_file(genv))
        if step_id:
            ctx.steps.setdefault(step_id, {})["outputs"] = _read_kv_file(out)
        if proc.returncode != 0:
            raise StepError(f"exit {proc.returncode}")


# `with:` keys a translation understands. Cache keys are deliberately ignored (no GHA cache
# locally, and they never change image contents); any other key fails the step, because
# silently dropping it could build a different image than CI does.
BUILD_PUSH_KEYS = frozenset({
    "context",
    "file",
    "target",
    "platforms",
    "load",
    "push",
    "tags",
    "labels",
    "build-args",
})
BUILD_PUSH_IGNORED = frozenset({"cache-from", "cache-to"})
SETUP_UV_KEYS = frozenset({"version", "enable-cache"})


def _check_with(
    uses: str, with_: dict, known: frozenset, ignored: frozenset = frozenset()
) -> None:
    unknown = sorted(set(with_) - known - ignored)
    if unknown:
        raise StepError(f"{uses.split('@')[0]}: unsupported with: {', '.join(unknown)}")


def build_push(with_: dict, ctx: Context, cwd: Path) -> None:
    _check_with("docker/build-push-action", with_, BUILD_PUSH_KEYS, BUILD_PUSH_IGNORED)
    w = {k: ctx.render(v) for k, v in with_.items()}
    if w.get("push", "false").lower() == "true":
        raise StepError("refusing to push from a local gate")
    cmd = [
        "docker",
        "buildx",
        "build",
        "--progress=plain",
        "--file",
        w.get("file", "Dockerfile"),
        w.get("context", "."),
    ]
    if w.get("target"):
        cmd += ["--target", w["target"]]
    if w.get("platforms"):
        cmd += ["--platform", w["platforms"]]
    if w.get("load", "false").lower() == "true":
        cmd += ["--load"]
    for key, flag in (
        ("tags", "--tag"),
        ("labels", "--label"),
        ("build-args", "--build-arg"),
    ):
        for line in (w.get(key) or "").splitlines():
            if line.strip():
                cmd += [flag, line.strip()]
    print("+ " + " ".join(cmd), flush=True)
    if subprocess.run(cmd, cwd=cwd, env={**os.environ, **ctx.env}).returncode != 0:
        raise StepError("docker buildx build failed")


def setup_uv(with_: dict, ctx: Context, tools: Path) -> None:
    _check_with("astral-sh/setup-uv", with_, SETUP_UV_KEYS)
    version = ctx.render(with_.get("version", "")).strip()
    if not version:
        raise StepError("setup-uv without a pinned version")
    dest = tools / f"uv-{version}"
    if not (dest / "uv").exists():
        url = (
            f"https://github.com/astral-sh/uv/releases/download/{version}/"
            "uv-x86_64-unknown-linux-gnu.tar.gz"
        )
        dest.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".tgz", dir=tools) as tgz:
            urllib.request.urlretrieve(url, tgz.name)  # noqa: S310 — fixed https URL
            # The binary runs with docker-socket access: verify it like setup-uv does.
            with urllib.request.urlopen(url + ".sha256") as resp:  # noqa: S310
                want = resp.read().decode().split()[0]
            got = hashlib.sha256(Path(tgz.name).read_bytes()).hexdigest()
            if got != want:
                raise StepError(f"uv {version} checksum mismatch: {got} != {want}")
            with tarfile.open(tgz.name) as tar:
                for member in tar.getmembers():
                    if member.isfile() and Path(member.name).name in ("uv", "uvx"):
                        member.name = Path(member.name).name
                        tar.extract(member, dest)
    ctx.path_prepend.insert(0, str(dest))


def run_job(
    workflow: dict, job_name: str, ctx: Context, workspace: Path, tools: Path
) -> list[tuple]:
    job = workflow["jobs"][job_name]
    ctx.env.update({k: ctx.render(v) for k, v in (job.get("env") or {}).items()})
    results = []
    for step in job.get("steps", []):
        name = step.get("name") or step.get("uses") or "step"
        sid = step.get("id")
        if not ctx.condition(step.get("if")):
            results.append((name, "skipped", 0.0))
            if sid:
                ctx.steps[sid] = {"outcome": "skipped", "outputs": {}}
            continue
        start = time.monotonic()
        print(f"\n=== {job_name}: {name}", flush=True)
        env = {k: ctx.render(v) for k, v in (step.get("env") or {}).items()}
        cwd = workspace / ctx.render(step.get("working-directory", "."))
        uses = step.get("uses", "")
        with_ = step.get("with") or {}
        try:
            if "run" in step:
                run_shell(ctx.render(step["run"]), env, cwd, ctx, sid)
            elif uses.startswith(SKIPPED_ACTIONS):
                print(f"(skipped locally: {uses.split('@')[0]})")
            elif uses.startswith("docker/build-push-action@"):
                build_push(with_, ctx, workspace)
            elif uses.startswith("astral-sh/setup-uv@"):
                setup_uv(with_, ctx, tools)
            elif uses == "./.github/actions/retry":
                wd = workspace / ctx.render(with_.get("working-directory", "."))
                attempts = int(ctx.render(with_.get("attempts", 1)) or 1)
                for attempt in range(1, attempts + 1):
                    try:
                        run_shell(ctx.render(with_["command"]), env, wd, ctx, sid)
                        break
                    except StepError:
                        if attempt == attempts:
                            raise
                        time.sleep(float(ctx.render(with_.get("delay", 5)) or 5))
            else:
                raise StepError(f"unsupported action {uses}")
            outcome = "success"
        except StepError as exc:
            outcome = "failure"
            print(f"!!! {name}: {exc}", flush=True)
            if name in STORE_DEPENDENT_STEPS and ctx.containerd_store:
                outcome = "advisory"
                print(
                    f"!!! {name}: advisory on this host (containerd image store"
                    " measures differently from CI's overlay2); CI decides",
                    flush=True,
                )
            elif not step.get("continue-on-error"):
                ctx.failed = True
        if sid:
            ctx.steps.setdefault(sid, {"outputs": {}})["outcome"] = outcome
        results.append((name, outcome, time.monotonic() - start))
    return results


# GitHub runners have passwordless sudo; the gate host must never be reconfigured
# by a job. This shim satisfies `sudo sysctl -w key=value` only when the host
# already has that value (e.g. the box's persistent bwrap userns settings) and
# refuses every other sudo use, so a step needing real root fails loudly.
SUDO_SHIM = """#!/bin/sh
if [ "$1" = sysctl ] && [ "$2" = -w ] && [ $# -eq 3 ]; then
  key=${3%%=*}; want=${3#*=}
  have=$(sysctl -n "$key" 2>/dev/null) || { echo "gate sudo: cannot read $key" >&2; exit 1; }
  [ "$have" = "$want" ] && { echo "$key = $have (already set on the gate host)"; exit 0; }
  echo "gate sudo: host has $key=$have, job wants $want; set it on the host" >&2; exit 1
fi
echo "gate sudo: refusing '$*' (a local gate never runs root commands)" >&2
exit 1
"""

# Tools GitHub's ubuntu-latest ships that the act runner image lacks. Add one here
# when a step fails locally only because of a missing binary.
RUNNER_EXTRA_PACKAGES = ("sqlite3", "zstd")


def start_container(image: str, workspace: Path, tools: Path) -> str:
    """One long-lived runner container per job (state persists across steps as on
    GitHub), same paths as the host so bind mounts in docker tests resolve. Started
    as root to add RUNNER_EXTRA_PACKAGES; steps then run as the caller's uid with
    the docker socket's gid (see ``step_user``) so files stay owned by the host user."""
    sock = Path("/var/run/docker.sock")
    # A worktree's .git points into its common git dir (which may borrow objects
    # through alternates): mount those at the same paths or git fails in the steps.
    common = Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    shared = [common]
    alternates = common / "objects" / "info" / "alternates"
    if alternates.exists():
        shared += [
            Path(x)
            for x in alternates.read_text(encoding="utf-8-sig").splitlines()
            if x.strip()
        ]
    extra: list[str] = []
    for path in shared:
        if not str(path).startswith(f"{workspace}/"):
            extra += ["-v", f"{path}:{path}"]
    cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--label",
        "fork-gate=1",
        # The gate host may be production: cap the job container. Image builds run in the
        # host's buildkitd and test containers in dockerd, outside these limits.
        "--cpuset-cpus",
        _half_the_cpus(),
        "--memory",
        "8g",
        "--pids-limit",
        "4096",
        *extra,
        "-e",
        "HOME=/tmp/gate-home",
        # GitHub's ubuntu-latest allows `pip install --user` and has ~/.local/bin on PATH.
        "-e",
        "PIP_BREAK_SYSTEM_PACKAGES=1",
        "-v",
        f"{sock}:{sock}",
        "-v",
        f"{workspace}:{workspace}",
        "-v",
        f"{tools}:{tools}",
        "-w",
        str(workspace),
        "--entrypoint",
        "sleep",
        image,
        "infinity",
    ]
    cid = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()
    try:
        _setup_container(cid)
    except BaseException:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
        raise
    return cid


def _half_the_cpus() -> str:
    n = max(1, (os.cpu_count() or 2) // 2)
    return f"0-{n - 1}"


def _setup_container(cid: str) -> None:
    setup = (
        "set -e; mkdir -p /tmp/gate-home && chmod 1777 /tmp/gate-home; "
        "apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive "
        f"apt-get install -y -qq --no-install-recommends {' '.join(RUNNER_EXTRA_PACKAGES)} >/dev/null"
    )
    subprocess.run(["docker", "exec", cid, "bash", "-c", setup], check=True)


def step_user() -> str:
    return f"{os.getuid()}:{Path('/var/run/docker.sock').stat().st_gid}"  # windows-footgun: ok — Linux gate host only (runs jobs in a Linux runner container)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workflow", default=".github/workflows/fork-release-image.yml"
    )
    parser.add_argument("--job", action="append", dest="jobs")
    parser.add_argument("--ref-name", default="release/local-gate")
    parser.add_argument("--tools-dir", default=".gate-tools")
    parser.add_argument(
        "--before",
        default="0" * 40,
        help="remote tip the push would move (github.event.before); zeros for a new branch",
    )
    parser.add_argument(
        "--container",
        help="runner image (e.g. the act ubuntu image, digest-pinned): run: steps execute "
        "inside it with the host Docker socket and identical workspace/tools paths",
    )
    args = parser.parse_args(argv)

    import yaml  # PyYAML: present on the gate host (python3-yaml), not a fork dependency

    workspace = Path.cwd()
    workflow = yaml.safe_load(
        (workspace / args.workflow).read_text(encoding="utf-8-sig")
    )
    jobs = args.jobs or [j for j in ("lint", "build-test") if j in workflow["jobs"]]
    tools = Path(args.tools_dir).resolve()
    tools.mkdir(parents=True, exist_ok=True)
    summary = []
    for job in jobs:
        ctx = Context(workflow, args.ref_name, workspace, args.before)
        ctx.tmp_root = tools
        ctx.containerd_store = host_uses_containerd_store()
        try:
            if args.container:
                ctx.container = start_container(args.container, workspace, tools)
                ctx.container_path = subprocess.run(
                    ["docker", "exec", ctx.container, "printenv", "PATH"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            summary += [
                (job, *r) for r in run_job(workflow, job, ctx, workspace, tools)
            ]
        finally:
            if ctx.container:
                subprocess.run(
                    ["docker", "rm", "-f", ctx.container], capture_output=True
                )
        if ctx.failed:
            break
    print("\n=== local gate summary")
    for job, name, outcome, secs in summary:
        print(f"{outcome:8s} {secs:7.1f}s  {job}: {name}")
    failed = [s for s in summary if s[2] == "failure"]
    print(
        json.dumps({
            "result": "FAIL" if failed else "PASS",
            "failed": [f"{j}: {n}" for j, n, _, _ in failed],
        })
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
