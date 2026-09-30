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
