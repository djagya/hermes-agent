"""scripts/ci/run_job_locally.py: the local gate must mirror CI semantics it relies on."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "run_job_locally", ROOT / "scripts/ci/run_job_locally.py"
)
rjl = importlib.util.module_from_spec(spec)
sys.modules["run_job_locally"] = rjl
spec.loader.exec_module(rjl)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    return tmp_path


def ctx_for(repo, env=None):
    return rjl.Context(
        {"env": env or {"IMAGE_NAME": "ghcr.io/x/y"}}, "release/test", repo
    )


def test_render_and_conditions(repo):
    ctx = ctx_for(repo)
    assert ctx.render("${{ env.IMAGE_NAME }}:test") == "ghcr.io/x/y:test"
    assert ctx.render("${{ github.ref_name }}") == "release/test"
    assert ctx.render("${{ github.event.before }}") == "0" * 40
    ctx.steps["buildx"] = {"outcome": "failure", "outputs": {}}
    assert ctx.condition("steps.buildx.outcome == 'failure'") is True
    assert ctx.condition(None) is True
    ctx.failed = True
    assert ctx.condition(None) is False
    assert ctx.condition("always() && steps.buildx.outcome != 'skipped'") is True
    with pytest.raises(rjl.StepError):
        ctx.condition("contains(github.ref, 'x')")
    with pytest.raises(rjl.StepError):
        ctx.render("${{ format('{0}', 1) }}")


def test_run_step_outputs_and_env_flow(repo):
    workflow = {
        "env": {"A": "1"},
        "jobs": {
            "j": {
                "steps": [
                    {
                        "id": "v",
                        "run": 'echo "base=0.21.5" >> "$GITHUB_OUTPUT"; echo "FROM_STEP=yes" >> "$GITHUB_ENV"',
                    },
                    {
                        "run": 'test "${{ steps.v.outputs.base }}" = 0.21.5 && test "$FROM_STEP" = yes && test "$A" = 1'
                    },
                    {"name": "fails", "run": "false"},
                    {"name": "after failure", "run": "true"},
                    {"name": "always", "if": "always()", "run": "true"},
                ]
            }
        },
    }
    ctx = rjl.Context(workflow, "release/test", repo)
    results = rjl.run_job(workflow, "j", ctx, repo, repo / ".tools")
    assert [r[1] for r in results] == [
        "success",
        "success",
        "failure",
        "skipped",
        "success",
    ]


def test_unsupported_action_fails_loudly(repo):
    workflow = {"jobs": {"j": {"steps": [{"uses": "some/unknown-action@v1"}]}}}
    ctx = rjl.Context(workflow, "r", repo)
    assert rjl.run_job(workflow, "j", ctx, repo, repo / ".tools")[0][1] == "failure"


def test_build_push_translation_never_pushes(repo, monkeypatch):
    ctx = ctx_for(repo)  # before patching: Context reads HEAD with git
    calls = []
    monkeypatch.setattr(
        rjl.subprocess,
        "run",
        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    rjl.build_push(
        {
            "context": ".",
            "file": "Dockerfile",
            "target": "test",
            "load": True,
            "platforms": "linux/amd64",
            "tags": "${{ env.IMAGE_NAME }}:test",
            "build-args": "A=1\nB=2",
            "cache-from": "type=gha",
        },
        ctx,
        repo,
    )
    cmd = calls[0]
    assert (
        cmd[:3] == ["docker", "buildx", "build"]
        and "--load" in cmd
        and "--push" not in cmd
    )
    assert cmd[cmd.index("--tag") + 1] == "ghcr.io/x/y:test"
    assert ["--build-arg", "A=1"] == cmd[
        cmd.index("--build-arg") : cmd.index("--build-arg") + 2
    ]
    assert not any("gha" in c for c in cmd)
    with pytest.raises(rjl.StepError, match="refusing to push"):
        rjl.build_push({"push": True}, ctx, repo)


def test_real_workflow_uses_only_supported_steps():
    """Every step in the fork's gated jobs must be runnable locally, or the gate lies."""
    yaml = pytest.importorskip("yaml")

    wf = yaml.safe_load((ROOT / ".github/workflows/fork-release-image.yml").read_text())
    supported = rjl.SKIPPED_ACTIONS + (
        "docker/build-push-action@",
        "astral-sh/setup-uv@",
    )
    for job in ("lint", "build-test"):
        for step in wf["jobs"][job]["steps"]:
            uses = step.get("uses")
            assert (
                "run" in step
                or uses == "./.github/actions/retry"
                or uses.startswith(supported)
            ), (job, step.get("name"))
        assert rjl.unsupported_steps(wf["jobs"][job]["steps"], ROOT) == [], job


def test_real_workflow_build_inputs_are_all_translated():
    """An unknown build-push/setup-uv input would build or set up differently than CI."""
    yaml = pytest.importorskip("yaml")

    wf = yaml.safe_load((ROOT / ".github/workflows/fork-release-image.yml").read_text())
    known_with = {
        "docker/build-push-action@": rjl.BUILD_PUSH_KEYS | rjl.BUILD_PUSH_IGNORED,
        "astral-sh/setup-uv@": rjl.SETUP_UV_KEYS,
    }
    for job in ("lint", "build-test"):
        for step in wf["jobs"][job]["steps"]:
            for prefix, keys in known_with.items():
                if (step.get("uses") or "").startswith(prefix):
                    assert set(step.get("with") or {}) <= keys, (job, step.get("name"))


def test_unterminated_output_block_fails(repo):
    workflow = {
        "jobs": {
            "j": {
                "steps": [
                    {
                        "id": "v",
                        "run": 'printf "base<<EOF\\n0.21.5\\n" >> "$GITHUB_OUTPUT"',
                    },
                ]
            }
        }
    }
    ctx = rjl.Context(workflow, "r", repo)
    assert rjl.run_job(workflow, "j", ctx, repo, repo / ".tools")[0][1] == "failure"


def test_unknown_build_push_input_fails_loudly(repo):
    ctx = ctx_for(repo)
    with pytest.raises(rjl.StepError, match="unsupported with: secrets"):
        rjl.build_push({"context": ".", "secrets": "x"}, ctx, repo)


def test_publish_promotes_the_image_build_test_tested():
    """publish must push build-test's image, not a rebuild the gate never saw."""
    yaml = pytest.importorskip("yaml")

    wf = yaml.safe_load((ROOT / ".github/workflows/fork-release-image.yml").read_text())
    build, publish = wf["jobs"]["build-test"], wf["jobs"]["publish"]
    builds = [
        s
        for s in build["steps"]
        if (s.get("uses") or "").startswith("docker/build-push-action@")
    ]
    assert [s["with"]["target"] for s in builds] == ["runtime"]
    assert not any(
        (s.get("uses") or "").startswith("docker/build-push-action@")
        for s in publish["steps"]
    )
    uploads = {
        s["with"]["name"]
        for s in build["steps"]
        if (s.get("uses") or "").startswith("actions/upload-artifact@")
    }
    downloads = [
        s["with"]["name"]
        for s in publish["steps"]
        if (s.get("uses") or "").startswith("actions/download-artifact@")
    ]
    assert downloads and set(downloads) <= uploads
    output = build["outputs"]["config-digest"]
    step_id = output.split("steps.", 1)[1].split(".", 1)[0]
    assert step_id in {s.get("id") for s in build["steps"]}
    assert "needs.build-test.outputs.config-digest" in yaml.safe_dump(publish)


def test_store_dependent_step_is_advisory_only_on_containerd_hosts(repo):
    step = {"name": "Record image size budget", "run": "exit 1"}
    workflow = {"jobs": {"j": {"steps": [step]}}}

    ctx = rjl.Context(workflow, "r", repo)
    ctx.containerd_store = True
    assert rjl.run_job(workflow, "j", ctx, repo, repo / ".tools")[0][1] == "advisory"
    assert not ctx.failed

    ctx = rjl.Context(workflow, "r", repo)
    ctx.containerd_store = False
    assert rjl.run_job(workflow, "j", ctx, repo, repo / ".tools")[0][1] == "failure"
    assert ctx.failed

    other = {"jobs": {"j": {"steps": [{"name": "Unit tests", "run": "exit 1"}]}}}
    ctx = rjl.Context(other, "r", repo)
    ctx.containerd_store = True
    assert rjl.run_job(other, "j", ctx, repo, repo / ".tools")[0][1] == "failure"
    assert ctx.failed


def test_expressions_follow_github_semantics(repo):
    ctx = ctx_for(repo)
    ctx.inputs = {"toolchain": "python", "cache": "true", "extras": "", "flag": True}
    ctx.steps["a"] = {"outputs": {"p": ""}}
    ctx.steps["b"] = {"outputs": {"p": "/b/python"}}
    # || / && return operands; the losing side of a short-circuit is never evaluated
    assert ctx.render("${{ steps.a.outputs.p || steps.b.outputs.p }}") == "/b/python"
    assert (
        ctx.condition("inputs.toolchain != 'python' && hashFiles(inputs.x) == ''")
        is False
    )
    assert ctx.condition("inputs.extras != '' || inputs.cache == 'TRUE'") is True
    assert ctx.condition("!(inputs.extras == '')") is False
    assert ctx.condition("inputs.flag == true && inputs.cache == 'true'") is True
    assert ctx.render("${{ inputs.flag }}") == "true"
    with pytest.raises(rjl.StepError, match="hashFiles"):
        ctx.condition("hashFiles('x') == ''")


def test_workflow_call_inputs_default_and_job_if(repo):
    wf = {
        True: {
            "workflow_call": {
                "inputs": {
                    "event_name": {"type": "string", "required": True},
                    "strict": {"type": "boolean", "default": True},
                }
            }
        },
        "jobs": {
            "pr-only": {
                "if": "inputs.event_name == 'pull_request'",
                "steps": [{"run": "false"}],
            },
            "blocking": {
                "if": "inputs.strict",
                "steps": [
                    {
                        "name": "advisory",
                        "if": "github.event_name == 'pull_request'",
                        "run": "false",
                    },
                    {"name": "check", "run": "true"},
                ],
            },
        },
    }
    ctx = rjl.Context(wf, "release/x", repo, inputs={"event_name": "push"})
    assert rjl.run_job(wf, "pr-only", ctx, repo, repo / ".tools") == [
        ("(job if)", "skipped", 0.0)
    ]
    ctx = rjl.Context(wf, "release/x", repo, inputs={"event_name": "push"})
    results = rjl.run_job(wf, "blocking", ctx, repo, repo / ".tools")
    assert [(n, o) for n, o, _ in results] == [
        ("advisory", "skipped"),
        ("check", "success"),
    ]
    # A required input the caller did not pass fails loudly, never renders ''.
    ctx = rjl.Context(wf, "release/x", repo)
    assert rjl.run_job(wf, "pr-only", ctx, repo, repo / ".tools")[0][1] == "failure"
    with pytest.raises(rjl.StepError, match="--input event_name="):
        ctx.render("${{ inputs.event_name }}")
    with pytest.raises(rjl.StepError, match="no input nope"):
        rjl.Context(wf, "release/x", repo, inputs={"nope": "1"})


def _write_action(repo, rel, text):
    path = repo / rel / "action.yml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


def test_composite_action_runs_its_steps_with_inputs_path_and_outputs(repo):
    pytest.importorskip("yaml")
    _write_action(
        repo,
        ".github/actions/tool",
        """
inputs:
  word:
    default: fallback
  enabled:
    default: 'false'
  optional:
    default: ''
outputs:
  bin:
    value: ${{ steps.missing.outputs.bin || steps.install.outputs.bin }}
runs:
  using: composite
  steps:
    - name: install
      id: install
      shell: bash
      env:
        _WORD: ${{ inputs.word }}
        _HERE: ${{ github.action_path }}
      run: |
        set -euo pipefail
        test "$_HERE" = "$GITHUB_ACTION_PATH" && test -f "$_HERE/action.yml"
        mkdir -p "$RUNNER_TEMP/bin"
        { echo '#!/bin/sh'; echo "echo $_WORD"; } > "$RUNNER_TEMP/bin/tool"
        chmod +x "$RUNNER_TEMP/bin/tool"
        echo "$RUNNER_TEMP/bin" >> "$GITHUB_PATH"
        echo "TOOL_HOME=$RUNNER_TEMP" >> "$GITHUB_ENV"
        echo "bin=$RUNNER_TEMP/bin/tool" >> "$GITHUB_OUTPUT"
    - id: cache
      if: inputs.enabled == 'true'
      uses: actions/cache@0000000000000000000000000000000000000000
      with:
        key: ${{ hashFiles('never-rendered') }}
    - name: cold path after a miss
      if: steps.cache.outputs.cache-hit != 'true'
      shell: bash
      run: test "$(tool)" = "${{ inputs.word }}"
    - name: short-circuited
      if: inputs.optional != '' && hashFiles(inputs.optional) == ''
      shell: bash
      run: exit 1
""",
    )
    workflow = {
        "jobs": {
            "j": {
                "steps": [
                    {
                        "id": "tool",
                        "uses": "./.github/actions/tool",
                        "with": {"word": "hello", "enabled": True},
                    },
                    {
                        "name": "consumer",
                        "run": 'test "$(tool)" = hello && test -d "$TOOL_HOME"'
                        ' && test "${{ steps.tool.outputs.bin }}" = "$TOOL_HOME/bin/tool"',
                    },
                ]
            }
        }
    }
    ctx = rjl.Context(workflow, "r", repo)
    results = rjl.run_job(workflow, "j", ctx, repo, repo / ".tools")
    assert [(n, o) for n, o, _ in results] == [
        ("./.github/actions/tool > install", "success"),
        (
            "./.github/actions/tool > actions/cache@0000000000000000000000000000000000000000",
            "success",
        ),
        ("./.github/actions/tool > cold path after a miss", "success"),
        ("./.github/actions/tool > short-circuited", "skipped"),
        ("./.github/actions/tool", "success"),
        ("consumer", "success"),
    ], results
    assert not ctx.failed
    # RUNNER_TEMP lives for the job (steps share it) and is removed after it.
    assert not Path(ctx.runner_temp).exists()


def test_composite_failure_and_continue_on_error(repo):
    pytest.importorskip("yaml")
    _write_action(
        repo,
        ".github/actions/broken",
        """
runs:
  using: composite
  steps:
    - {name: boom, shell: bash, run: exit 3}
    - {name: after, shell: bash, run: 'true'}
""",
    )
    step = {"name": "advisory", "uses": "./.github/actions/broken"}
    workflow = {
        "jobs": {"j": {"steps": [{**step, "continue-on-error": True}, {"run": "true"}]}}
    }
    ctx = rjl.Context(workflow, "r", repo)
    results = rjl.run_job(workflow, "j", ctx, repo, repo / ".tools")
    assert [o for _, o, _ in results] == ["advisory", "skipped", "advisory", "success"]
    assert not ctx.failed

    workflow = {"jobs": {"j": {"steps": [step, {"name": "next", "run": "true"}]}}}
    ctx = rjl.Context(workflow, "r", repo)
    results = rjl.run_job(workflow, "j", ctx, repo, repo / ".tools")
    assert [o for _, o, _ in results] == ["failure", "skipped", "failure", "skipped"]
    assert ctx.failed

    _write_action(repo, ".github/actions/js", "runs:\n  using: node24\n  main: x.js\n")
    assert rjl.unsupported_steps([{"uses": "./.github/actions/js"}], repo) == [
        "unsupported action ./.github/actions/js (runs.using: node24)"
    ]


def test_lint_workflow_blocking_jobs_are_supported():
    """The jobs PRs into fork main require must run locally, setup-pm included."""
    yaml = pytest.importorskip("yaml")

    wf = yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text(encoding="utf-8"))
    for job in ("windows-footguns", "ruff-blocking"):
        steps = wf["jobs"][job]["steps"]
        assert rjl.unsupported_steps(steps, ROOT) == [], job
        assert "./.github/actions/setup-pm" in [s.get("uses") for s in steps], job


def test_setup_pm_under_lint_inputs_runs_the_pinned_toolchain_steps(repo, monkeypatch):
    """setup-pm as lint.yml calls it: prepare + install run, caches miss, prune skips,
    dependency steps stay off, and a push renders the job's PR-only steps skipped."""
    yaml = pytest.importorskip("yaml")

    wf = yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text(encoding="utf-8"))
    ran = []

    def fake_shell(script, env, cwd, ctx, sid):
        ran.append((sid, script, env))
        if sid:
            ctx.steps.setdefault(sid, {})["outputs"] = {
                "bootstrap-python": "/usr/bin/python3",
                "python-path": "/pm/python",
            }

    monkeypatch.setattr(rjl, "run_shell", fake_shell)
    ctx = rjl.Context(wf, "release/test", repo, inputs={"event_name": "push"})
    results = rjl.run_job(wf, "windows-footguns", ctx, ROOT, repo / ".tools")
    outcomes = {n: o for n, o, _ in results}
    pm = "./.github/actions/setup-pm > "
    assert outcomes[pm + "Read PM pins with the runner bootstrap Python"] == "success"
    assert outcomes[pm + "Install and verify tools through PM"] == "success"
    assert outcomes[pm + "Cache verified PM tools"] == "success"  # the miss
    assert outcomes[pm + "Register uv cache pruning"] == "success"  # skipped locally
    for off in (
        "Restore verified PM tools without saving",
        "Preserve and retrieve the pinned toolchain",
        "Cache uv dependency downloads and builds",
        "Require an npm dependency lock for caching",
        "Install the requested Python dependencies through PM",
    ):
        assert outcomes[pm + off] == "skipped", off
    assert outcomes["Profile-scope patterns on added lines (advisory)"] == "skipped"
    assert outcomes["Public-surface diff vs base (advisory)"] == "skipped"
    assert outcomes["Run footgun checker"] == "success"
    assert not ctx.failed
    prepare, install = ran[0], ran[1]
    assert prepare[0] == "prepare" and prepare[2]["_PM_TOOLCHAIN"] == "python"
    assert prepare[2]["_PM_ACTION"] == str(ROOT / ".github/actions/setup-pm")
    assert install[0] == "install" and install[2]["_PM_BOOTSTRAP"] == "/usr/bin/python3"
    assert [s for _, s, _ in ran[2:]][0] == "python scripts/check-windows-footguns.py --all"


def test_a_jobs_untracked_outputs_do_not_reach_the_next_job(repo, monkeypatch):
    """GitHub gives every job a fresh checkout. build-test's install-stamp.json once
    leaked into the lint lane, where PM took the checkout for a Docker install."""
    pytest.importorskip("yaml")
    (repo / ".gitignore").write_text("install-stamp.json\n", encoding="utf-8")
    (repo / "mine.txt").write_text("pre-existing, not the job's\n", encoding="utf-8")
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/w.yml").write_text(
        """
jobs:
  build:
    steps:
      - run: echo '{"distribution":"docker"}' > install-stamp.json && mkdir -p out/deep && touch out/deep/f
  lint:
    steps:
      - run: test ! -e install-stamp.json && test ! -e out && test -f mine.txt
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    argv = ["--workflow", ".github/workflows/w.yml", "--tools-dir", ".gate-tools"]
    assert rjl.main([*argv, "--job", "build", "--job", "lint"]) == 0
    # ...and across runner invocations (the box gate calls it once per workflow).
    assert rjl.main([*argv, "--job", "build"]) == 0
    assert not (repo / "install-stamp.json").exists()
    assert (repo / "mine.txt").exists() and (repo / ".gate-tools").is_dir()
