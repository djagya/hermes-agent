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
  ./<composite action>        executed: its ``runs.steps`` run in order with its
                              inputs (declared defaults + ``with:``), its own
                              ``steps`` context, ``github.action_path`` and
                              outputs (e.g. ./.github/actions/setup-pm installs
                              the pinned PM toolchain as in CI; ./.github/actions/retry)
  actions/cache[/restore]     a cache miss (no outputs): the steps after it
                              take CI's cold path; nothing is ever saved
  ./.github/actions/setup-pm/prune
                              skipped (prunes the uv cache before actions/cache
                              saves it; the gate never saves a cache)
Anything else fails loudly: an unsupported step is not a passed step.

A ``continue-on-error`` step may fail without failing the job, as in CI. Job
``if:`` is honoured, and a job whose ``needs`` did not succeed is skipped. A
reusable workflow's ``on.workflow_call.inputs`` take their declared defaults;
``--input NAME=VALUE`` supplies what the caller would pass (lint.yml needs
``--input event_name=push``). RUNNER_TEMP is per job, as on GitHub.

Usage::

    python3 scripts/ci/run_job_locally.py [--workflow W] [--job J ...]
        [--ref-name BRANCH] [--tools-dir DIR] [--input NAME=VALUE ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
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
        self,
        workflow: dict,
        ref_name: str,
        workspace: Path,
        before: str = "0" * 40,
        inputs: dict[str, str] | None = None,
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
            "action_path": "",  # set while a composite action's steps run
        }
        self.env: dict[str, str] = {
            k: str(v) for k, v in (workflow.get("env") or {}).items()
        }
        self.inputs: dict[str, object] = workflow_inputs(workflow, inputs or {})
        self.steps: dict[str, dict] = {}
        self.containerd_store = False
        self.failed = False
        self.skipped = False  # the job's own `if:` was false
        self.job = ""
        self.before = before
        self.path_prepend: list[str] = []
        self.container: str | None = (
            None  # runner-image container id; None = host shell
        )
        self.container_path = ""
        self.tmp_root: Path | None = None
        self.runner_temp: Path | None = None  # per job, as on GitHub

    def runner_os(self) -> str:
        if self.container:
            return "Linux"
        return {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, "Linux")

    def lookup(self, path: str) -> object:
        parts = path.split(".")
        if parts[:2] == ["github", "event"]:
            # A local gate stands in for a push: `before` is the remote tip being moved.
            return {"before": self.before}.get(parts[2] if len(parts) > 2 else "", "")
        if parts[0] == "github" and len(parts) == 2:
            return str(self.github.get(parts[1], ""))
        if parts[0] == "env" and len(parts) == 2:
            return self.env.get(parts[1], "")
        if parts[0] == "inputs" and len(parts) == 2:
            if parts[1] not in self.inputs:
                raise StepError(
                    f"inputs.{parts[1]} has no value (a required workflow_call input:"
                    f" pass --input {parts[1]}=VALUE)"
                )
            return self.inputs[parts[1]]
        if parts[0] == "steps" and len(parts) >= 3:
            step = self.steps.get(parts[1], {})
            if parts[2] == "outputs" and len(parts) == 4:
                return step.get("outputs", {}).get(parts[3], "")
            return str(step.get(parts[2], ""))
        if parts == ["runner", "temp"]:
            return str(self.runner_temp or tempfile.gettempdir())
        if parts == ["runner", "os"]:
            return self.runner_os()
        raise StepError(f"unsupported expression path: {path}")

    def value(self, expr: str) -> object:
        """Evaluate a GitHub expression: literals, context paths, ``== != ! && ||``,
        parentheses and the status functions, with GitHub's short-circuit and
        coercion rules. Any other function fails loudly, but only if evaluated."""
        toks = _tokens(expr)
        pos = 0

        def peek() -> tuple[str, str] | None:
            return toks[pos] if pos < len(toks) else None

        def take(want: str | None = None) -> tuple[str, str]:
            nonlocal pos
            tok = peek()
            if tok is None or (want is not None and tok != ("op", want)):
                raise StepError(f"unsupported expression: {expr}")
            pos += 1
            return tok

        def primary():
            kind, text = take()
            if kind == "str":
                s = text[1:-1].replace("''", "'")
                return lambda: s
            if kind == "num":
                n = float(text)
                return lambda: n
            if (kind, text) == ("op", "("):
                inner = or_()
                take(")")
                return inner
            if kind != "name":
                raise StepError(f"unsupported expression: {expr}")
            if peek() == ("op", "("):
                take("(")
                args = []
                if peek() != ("op", ")"):
                    args.append(or_())
                    while peek() == ("op", ","):
                        take(",")
                        args.append(or_())
                take(")")
                if text in STATUS_FUNCTIONS and not args:
                    return lambda: self._status(text)

                def unsupported():
                    raise StepError(f"unsupported function: {text}()")

                return unsupported
            literal = {"true": True, "false": False, "null": None}
            if text in literal:
                return lambda: literal[text]
            return lambda: self.lookup(text)

        def unary():
            if peek() == ("op", "!"):
                take("!")
                inner = unary()
                return lambda: not _truthy(inner())
            return primary()

        def compare():
            left = unary()
            if peek() in (("op", "=="), ("op", "!=")):
                op = take()[1]
                right = unary()
                return lambda: _equal(left(), right()) == (op == "==")
            return left

        def chain(sub, op: str, stop_when: bool):
            parts = [sub()]
            while peek() == ("op", op):
                take(op)
                parts.append(sub())
            if len(parts) == 1:
                return parts[0]

            def run():
                result = None
                for part in parts:
                    result = part()
                    if _truthy(result) == stop_when:
                        break
                return result

            return run

        def and_():
            return chain(compare, "&&", False)

        def or_():
            return chain(and_, "||", True)

        thunk = or_()
        if pos != len(toks):
            raise StepError(f"unsupported expression: {expr}")
        return thunk()

    def _status(self, name: str) -> bool:
        return {
            "always": True,
            "success": not self.failed,
            "failure": self.failed,
            "cancelled": False,
        }[name]

    def evaluate(self, expr: str) -> str:
        return _to_str(self.value(expr))

    def render(self, value: object) -> str:
        return EXPR.sub(lambda m: self.evaluate(m.group(1)), str(value))

    def condition(self, cond: object) -> bool:
        """``if:``; without a status function GitHub implies ``success() && (...)``."""
        if cond is None:
            return not self.failed
        if isinstance(cond, bool):
            return cond and not self.failed
        text = EXPR.sub(lambda m: m.group(1), str(cond)).strip()
        result = _truthy(self.value(text))
        uses_status = re.search(r"\b(always|failure|success|cancelled)\s*\(", text)
        return result if uses_status else (result and not self.failed)


STATUS_FUNCTIONS = ("always", "success", "failure", "cancelled")
TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+(?:\.\d+)?)(?![\w.])"
    r"|(?P<op>==|!=|&&|\|\||[!(),])"
    r"|(?P<name>[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*))"
)


def _tokens(expr: str) -> list[tuple[str, str]]:
    out, pos, text = [], 0, expr.rstrip()
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m or m.lastgroup is None:
            raise StepError(f"unsupported expression: {expr}")
        out.append((m.lastgroup, m.group(m.lastgroup)))
        pos = m.end()
    return out


def _truthy(value: object) -> bool:
    if isinstance(value, float):
        return not (value == 0 or math.isnan(value))
    return bool(value)  # '' / None / False are falsy, as on GitHub


def _number(value: object) -> float:
    if value is None or isinstance(value, (bool, int, float)):
        return float(value or 0)
    text = str(value).strip()
    try:
        return float(text) if text else 0.0
    except ValueError:
        return math.nan


def _equal(left: object, right: object) -> bool:
    """GitHub compares strings case-insensitively; mixed types compare as numbers."""
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    if type(left) is type(right):
        return left == right
    return _number(left) == _number(right)


def _to_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value)


def workflow_inputs(workflow: dict, given: dict[str, str]) -> dict[str, object]:
    """``inputs`` of a reusable workflow: what the caller passes (``--input``),
    else the declared default. A required input without either stays unset, so a
    reference to it fails loudly instead of rendering ''."""
    on = workflow.get("on", workflow.get(True))  # PyYAML reads a bare `on:` as True
    call = (on.get("workflow_call") if isinstance(on, dict) else None) or {}
    declared = call.get("inputs") or {}
    unknown = sorted(set(given) - set(declared))
    if unknown:
        raise StepError(f"workflow declares no input {', '.join(unknown)}")
    empty = {"boolean": False, "number": 0.0}
    out: dict[str, object] = {}
    for name, spec in declared.items():
        spec = spec or {}
        kind = spec.get("type", "string")
        if name in given:
            raw = given[name]
            out[name] = (
                raw.lower() == "true"
                if kind == "boolean"
                else _number(raw)
                if kind == "number"
                else raw
            )
        elif "default" in spec:
            out[name] = spec["default"]
        elif not spec.get("required"):
            out[name] = empty.get(kind, "")
    return out


def _input_str(value: object) -> str:
    """Action inputs are strings on GitHub: YAML ``true`` arrives as 'true'."""
    return _to_str(value)


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
        out, genv, gpath = tmpd / "output", tmpd / "env", tmpd / "path"
        for command_file in (out, genv, gpath):
            command_file.touch()
        script_file = tmpd / "step.sh"
        script_file.write_text(script, encoding="utf-8")
        step_env = {
            **ctx.env,
            **env,
            "GITHUB_OUTPUT": str(out),
            "GITHUB_ENV": str(genv),
            "GITHUB_PATH": str(gpath),
            "GITHUB_STEP_SUMMARY": str(tmpd / "summary"),
            "GITHUB_SHA": ctx.github["sha"],
            "GITHUB_REF_NAME": ctx.github["ref_name"],
            "GITHUB_WORKSPACE": ctx.github["workspace"],
            "GITHUB_REPOSITORY": ctx.github["repository"],
            "GITHUB_EVENT_NAME": ctx.github["event_name"],
            "CI": "true",
            "RUNNER_OS": ctx.runner_os(),
            "RUNNER_TEMP": str(ctx.runner_temp or tmp),
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
                # no-tmp: ok — path inside the disposable Linux runner container, not on the gate host
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
        # GitHub prepends each GITHUB_PATH line in order: the last line ends up first.
        for line in gpath.read_text(encoding="utf-8-sig").splitlines():
            if line.strip():
                ctx.path_prepend.insert(0, line.strip())
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


TRANSLATED_ACTIONS = ("docker/build-push-action@", "astral-sh/setup-uv@")
# A cache step is a miss locally (no outputs, nothing restored or saved), so the
# steps after it take CI's cold path; cache contents never change a verdict.
CACHE_ACTIONS = ("actions/cache@", "actions/cache/restore@")
# Local actions a gate need not run, with the reason printed in the log.
SKIPPED_LOCAL_ACTIONS = {
    "./.github/actions/setup-pm/prune": "prunes the uv cache before actions/cache"
    " saves it; the gate never saves a cache",
}


def load_action(workspace: Path, uses: str) -> tuple[Path, dict]:
    """A local composite action (``uses: ./path``) and its parsed action.yml."""
    import yaml  # PyYAML: see main()

    path = Path(os.path.normpath(workspace / uses))
    for name in ("action.yml", "action.yaml"):
        if (path / name).is_file():
            action = yaml.safe_load((path / name).read_text(encoding="utf-8-sig"))
            break
    else:
        raise StepError(f"unsupported action {uses} (no action.yml)")
    using = (action.get("runs") or {}).get("using")
    if using != "composite":
        raise StepError(f"unsupported action {uses} (runs.using: {using})")
    return path, action


def unsupported_steps(steps: list, workspace: Path) -> list[str]:
    """Steps this runner cannot execute, recursing into local composite actions."""
    bad = []
    for step in steps:
        uses = step.get("uses") or ""
        if "run" in step:
            if step.get("shell", "bash") != "bash":
                bad.append(f"{step.get('name')}: shell {step['shell']}")
        elif uses in SKIPPED_LOCAL_ACTIONS or uses.startswith(
            SKIPPED_ACTIONS + CACHE_ACTIONS + TRANSLATED_ACTIONS
        ):
            continue
        elif uses.startswith("./"):
            try:
                _, action = load_action(workspace, uses)
            except StepError as exc:
                bad.append(str(exc))
                continue
            bad += [
                f"{uses} > {b}"
                for b in unsupported_steps(action["runs"].get("steps") or [], workspace)
            ]
        else:
            bad.append(uses or str(step.get("name")))
    return bad


def run_composite(
    uses: str,
    with_: dict,
    env: dict[str, str],
    ctx: Context,
    workspace: Path,
    tools: Path,
    label: str,
    results: list[tuple],
    sid: str | None,
    advisory: bool,
) -> None:
    """Run a local composite action's steps with its own inputs/steps scope."""
    path, action = load_action(workspace, uses)
    declared = action.get("inputs") or {}
    unknown = sorted(set(with_) - set(declared))
    if unknown:
        raise StepError(f"{uses}: unsupported with: {', '.join(unknown)}")
    inputs: dict[str, object] = {
        k: _input_str((spec or {}).get("default", "")) for k, spec in declared.items()
    }
    inputs.update({k: ctx.render(_input_str(v)) for k, v in with_.items()})
    saved = (ctx.inputs, ctx.steps, ctx.github["action_path"], ctx.failed)
    ctx.inputs, ctx.steps, ctx.github["action_path"] = inputs, {}, str(path)
    ctx.failed = False  # inside the action, success() is the action's own status
    inner: list[tuple] = []
    outputs: dict[str, str] = {}
    try:
        run_steps(
            action["runs"].get("steps") or [],
            ctx,
            workspace,
            tools,
            label,
            inner,
            {**env, "GITHUB_ACTION_PATH": str(path)},
        )
        failed = ctx.failed
        if sid and not failed:
            outputs = {
                k: ctx.render((spec or {}).get("value", ""))
                for k, spec in (action.get("outputs") or {}).items()
            }
    finally:
        ctx.inputs, ctx.steps, ctx.github["action_path"], ctx.failed = saved
        # A continue-on-error caller makes the action's failures advisory too.
        results += [
            (n, "advisory" if advisory and o == "failure" else o, t)
            for n, o, t in inner
        ]
    if sid:
        ctx.steps[sid] = {"outputs": outputs}
    if failed:
        raise StepError(f"{uses}: a step failed")


def run_steps(
    steps: list,
    ctx: Context,
    workspace: Path,
    tools: Path,
    label: str,
    results: list[tuple],
    outer_env: dict[str, str] | None = None,
) -> None:
    for step in steps:
        name = label + (
            step.get("name") or step.get("uses") or step.get("id") or "step"
        )
        sid = step.get("id")
        start = time.monotonic()
        try:
            run = ctx.condition(step.get("if"))
        except StepError as exc:
            print(f"!!! {ctx.job}: {name}: if: {exc}", flush=True)
            ctx.failed = True
            results.append((name, "failure", 0.0))
            continue
        if not run:
            results.append((name, "skipped", 0.0))
            if sid:
                ctx.steps[sid] = {
                    "outcome": "skipped",
                    "conclusion": "skipped",
                    "outputs": {},
                }
            continue
        print(f"\n=== {ctx.job}: {name}", flush=True)
        coe = step.get("continue-on-error", False)
        advisory = coe if isinstance(coe, bool) else ctx.render(coe).strip() == "true"
        uses = step.get("uses", "")
        with_ = step.get("with") or {}
        try:
            env = {
                **(outer_env or {}),
                **{k: ctx.render(v) for k, v in (step.get("env") or {}).items()},
            }
            cwd = workspace / ctx.render(step.get("working-directory", "."))
            if "run" in step:
                if step.get("shell", "bash") != "bash":
                    raise StepError(f"unsupported shell {step['shell']}")
                run_shell(ctx.render(step["run"]), env, cwd, ctx, sid)
            elif uses.startswith(SKIPPED_ACTIONS):
                print(f"(skipped locally: {uses.split('@')[0]})")
            elif uses in SKIPPED_LOCAL_ACTIONS:
                print(f"(skipped locally: {uses}: {SKIPPED_LOCAL_ACTIONS[uses]})")
            elif uses.startswith(CACHE_ACTIONS):
                print(
                    f"(cache miss locally: {uses.split('@')[0]}; later steps run cold)"
                )
                if sid:
                    ctx.steps[sid] = {"outputs": {}}
            elif uses.startswith("docker/build-push-action@"):
                build_push(with_, ctx, workspace)
            elif uses.startswith("astral-sh/setup-uv@"):
                setup_uv(with_, ctx, tools)
            elif uses.startswith("./"):
                run_composite(
                    uses,
                    with_,
                    env,
                    ctx,
                    workspace,
                    tools,
                    name + " > ",
                    results,
                    sid,
                    advisory,
                )
            else:
                raise StepError(f"unsupported action {uses}")
            outcome = shown = "success"
        except StepError as exc:
            outcome = shown = "failure"
            print(f"!!! {name}: {exc}", flush=True)
            if name in STORE_DEPENDENT_STEPS and ctx.containerd_store:
                outcome = shown = "advisory"
                print(
                    f"!!! {name}: advisory on this host (containerd image store"
                    " measures differently from CI's overlay2); CI decides",
                    flush=True,
                )
            elif advisory:
                shown = "advisory"  # outcome stays failure; the job goes on, as in CI
                print(f"!!! {name}: continue-on-error, the job goes on", flush=True)
            else:
                ctx.failed = True
        if sid:
            ctx.steps.setdefault(sid, {"outputs": {}}).update(
                outcome=outcome,
                conclusion="failure" if shown == "failure" else "success",
            )
        results.append((name, shown, time.monotonic() - start))


def run_job(
    workflow: dict, job_name: str, ctx: Context, workspace: Path, tools: Path
) -> list[tuple]:
    job = workflow["jobs"][job_name]
    ctx.job = job_name
    try:
        if "uses" in job:
            raise StepError(
                f"reusable workflow call {job['uses']}: run its jobs directly"
            )
        if not ctx.condition(job.get("if")):
            print(f"\n=== {job_name}: skipped (if: {job['if']})", flush=True)
            ctx.skipped = True
            return [("(job if)", "skipped", 0.0)]
        ctx.env.update({k: ctx.render(v) for k, v in (job.get("env") or {}).items()})
    except StepError as exc:
        print(f"!!! {job_name}: {exc}", flush=True)
        ctx.failed = True
        return [("(job)", "failure", 0.0)]
    results: list[tuple] = []
    ctx.runner_temp = Path(tempfile.mkdtemp(prefix="gate-job-", dir=ctx.tmp_root))
    try:
        run_steps(job.get("steps", []), ctx, workspace, tools, "", results)
    finally:
        shutil.rmtree(ctx.runner_temp, ignore_errors=True)
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
        # no-tmp: ok — path inside the disposable Linux runner container, not on the gate host
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
        # no-tmp: ok — path inside the disposable Linux runner container, not on the gate host
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
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="workflow_call input the caller would pass (declared defaults apply "
        "otherwise), e.g. event_name=push for lint.yml",
    )
    args = parser.parse_args(argv)
    if any("=" not in item for item in args.input):
        parser.error("--input takes NAME=VALUE")
    given = dict(item.split("=", 1) for item in args.input)

    import yaml  # PyYAML: present on the gate host (python3-yaml), not a fork dependency

    workspace = Path.cwd()
    workflow = yaml.safe_load(
        (workspace / args.workflow).read_text(encoding="utf-8-sig")
    )
    jobs = args.jobs or [j for j in ("lint", "build-test") if j in workflow["jobs"]]
    if not jobs:
        parser.error(f"{args.workflow} has no lint/build-test job: pass --job")
    missing = [j for j in jobs if j not in workflow["jobs"]]
    if missing:
        parser.error(f"{args.workflow} has no job {', '.join(missing)}")
    try:
        workflow_inputs(workflow, given)
    except StepError as exc:
        parser.error(str(exc))
    tools = Path(args.tools_dir).resolve()
    tools.mkdir(parents=True, exist_ok=True)
    summary = []
    blocked: set[str] = set()  # jobs that failed or were skipped: dependents skip
    failed_jobs = []
    for job in jobs:
        needs = workflow["jobs"][job].get("needs") or []
        needs = {needs} if isinstance(needs, str) else set(needs)
        if needs & blocked:
            print(f"\n=== {job}: skipped (needs {', '.join(sorted(needs & blocked))})")
            summary.append((job, "(needs)", "skipped", 0.0))
            blocked.add(job)
            continue
        ctx = Context(workflow, args.ref_name, workspace, args.before, given)
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
        if ctx.failed or ctx.skipped:
            blocked.add(job)
        if ctx.failed:
            failed_jobs.append(job)
    print("\n=== local gate summary")
    for job, name, outcome, secs in summary:
        print(f"{outcome:8s} {secs:7.1f}s  {job}: {name}")
    failed = [s for s in summary if s[2] == "failure"]
    print(
        json.dumps({
            "result": "FAIL" if failed_jobs else "PASS",
            "failed": [f"{j}: {n}" for j, n, _, _ in failed],
        })
    )
    return 1 if failed_jobs else 0


if __name__ == "__main__":
    sys.exit(main())
