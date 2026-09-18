"""Live-gateway Hermes-test guard: contract tests.

Each test asserts a CONTRACT between the classifier and a piece of execution
context (command, workdir, cwd, gateway state) — never a frozen list of
commands or roots. The blocking invariant: inside a supervised gateway,
a test runner aimed at a Hermes checkout is blocked; the same runner aimed
elsewhere is allowed; a Hermes-specific runner with NO resolvable target
fails closed.

These tests never execute the suite: they only call the classifier and the
terminal-tool choke point against synthetic fixtures, with the supervised-
gateway probe monkeypatched. Safe to run anywhere, including (ironically)
inside the gateway — nothing here spawns pytest or touches /run/service.
"""

import json
import subprocess
from pathlib import Path

import pytest

from tools import live_gateway_test_guard as guard
from tools.live_gateway_test_guard import (
    VERDICT_ALLOWED,
    VERDICT_BLOCKED,
    classify_live_test_run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def gateway_on(monkeypatch):
    """Pretend this process is the live supervised gateway."""
    import tools.process_registry as pr

    monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: True)


@pytest.fixture
def gateway_off(monkeypatch):
    import tools.process_registry as pr

    monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: False)


@pytest.fixture
def other_repo(tmp_path):
    """A non-Hermes git repo whose pyproject does NOT name hermes-agent."""
    root = tmp_path / "some-project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "pyproject.toml").write_text('[project]\nname = "some-project"\n',
                                         encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Gate A: runner + Hermes target
# ---------------------------------------------------------------------------


class TestBlocksHermesTestRunsInGateway:
    @pytest.mark.parametrize(
        "command",
        [
            "pytest",
            "pytest -q",
            "python -m pytest",
            "python3 -m pytest tests/tools/test_x.py",
            "python -m unittest discover tests",
            "make test",
            "scripts/run_tests.sh",
            "scripts/run_tests.sh -q",
            "./scripts/run_tests.sh",
            "env CI=1 bash scripts/run_tests.sh tests/tools/",
            "timeout 300 pytest tests/",
            "sudo pytest",
            "sh -c 'pytest tests/agent'",
            "pytest || true",
            "pytest | tee log",
            "pytest tests/ ; make test",  # compound
        ],
    )
    def test_runner_with_repo_cwd_blocked(self, command):
        verdict, _ = classify_live_test_run(command, cwd=str(REPO_ROOT))
        assert verdict == VERDICT_BLOCKED

    @pytest.mark.parametrize(
        "command, cwd",
        [
            # Package/project runners must not stop wrapper peeling: the
            # inner runner is still aimed at this checkout.
            ("uv run pytest", "REPO"),
            ("uv run pytest tests/docker", "REPO"),
            ("uvx pytest", "REPO"),
            ("uvx pytest tests/docker", "REPO"),
            ("uv tool run pytest tests/", "REPO"),
            ("uv run --frozen pytest tests/", "REPO"),
            ("poetry run pytest", "REPO"),
            ("poetry run python -m pytest tests/tools/test_x.py", "REPO"),
            ("pipx run pytest", "REPO"),
            ("pdm run pytest", "REPO"),
            ("hatch run pytest", "REPO"),
            ("hatch run +py=3.12 pytest", "REPO"),
            ("pipenv run pytest tests/", "REPO"),
            ("uv run scripts/run_tests.sh -q", "REPO"),
            ("sudo -u root uv run pytest", "REPO"),
            # Option operands name the repo even from a foreign cwd.
            ("uv --directory {repo} run pytest", "OTHER"),
            ("uv --directory={repo} run pytest", "OTHER"),
            ("uv run --directory {repo} pytest", "OTHER"),
            ("poetry -C {repo} run pytest", "OTHER"),
            # make: -C/--directory operands are the run's repo, not targets.
            ("make -C {repo} test", "OTHER"),
            ("make --directory={repo} test", "OTHER"),
            ("make -C {repo} ci", "OTHER"),
            ("make -C {repo} -j4 test", "OTHER"),
        ],
    )
    def test_package_runner_and_make_directory_blocked(self, command, cwd):
        anchored = command.replace("{repo}", str(REPO_ROOT))
        verdict, _ = classify_live_test_run(
            anchored, cwd=str(REPO_ROOT) if cwd == "REPO" else "/tmp")
        assert verdict == VERDICT_BLOCKED

    @pytest.mark.parametrize(
        "command",
        [
            # The incident-shaped bypass: runner + explicit relative path arg
            # issued AFTER a cd into the checkout. The arg must resolve
            # against the cd-derived cwd, not the stale session cwd.
            f"cd {REPO_ROOT} && scripts/run_tests.sh tests/docker",
            f"cd {REPO_ROOT} && pytest tests/docker",
            f"cd {REPO_ROOT} && pytest",  # bare runner/pytest after cd
            # Variable-carried cd targets (assignment lead + $VAR/${VAR}).
            f'REPO={REPO_ROOT}; cd "$REPO" && pytest tests/docker/',
            f"REPO={REPO_ROOT}; cd $REPO && pytest tests/docker/",
            f"REPO={REPO_ROOT}; cd ${{REPO}} && pytest",
            # Subshell group.
            f"(cd {REPO_ROOT} && pytest)",
        ],
    )
    def test_cd_into_repo_then_runner_blocked(self, command):
        verdict, _ = classify_live_test_run(command, cwd="/opt/data/other-project")
        assert verdict == VERDICT_BLOCKED

    def test_any_resolvable_hermes_target_blocks(self):
        """A bogus explicit path arg must not wash out a Hermes workdir:
        the sweep blocks when ANY candidate target resolves into a
        Hermes checkout (rootdir/conftest come from the checkout)."""
        verdict, _ = classify_live_test_run(
            "pytest -c /tmp/other/pyproject.toml", workdir=str(REPO_ROOT),
            cwd="/opt/data/other-project")
        assert verdict == VERDICT_BLOCKED

    def test_classifier_ignores_ambient_getcwd(self, monkeypatch):
        """No os.getcwd() fallback: with cwd=None the verdict must not
        depend on the process's own working directory."""
        import os

        monkeypatch.setattr(os, "getcwd", lambda: str(REPO_ROOT))
        verdict, _ = classify_live_test_run("pytest tests/")
        assert verdict == VERDICT_ALLOWED

    def test_workdir_pointing_into_repo_blocked(self):
        verdict, _ = classify_live_test_run("pytest", workdir=str(REPO_ROOT))
        assert verdict == VERDICT_BLOCKED

    def test_explicit_repo_path_blocked_from_anywhere(self, tmp_path):
        verdict, _ = classify_live_test_run(
            f"pytest {REPO_ROOT}/tests", cwd=str(tmp_path))
        assert verdict == VERDICT_BLOCKED

    def test_canonical_root_inside_repo_blocked(self):
        verdict, _ = classify_live_test_run(
            "pytest tests/docker/", cwd=str(REPO_ROOT / "scripts"))
        assert verdict == VERDICT_BLOCKED

    def test_cd_into_repo_then_pytest_blocked(self):
        verdict, _ = classify_live_test_run(
            f"cd {REPO_ROOT} && pytest", cwd=None)
        assert verdict == VERDICT_BLOCKED


# ---------------------------------------------------------------------------
# Gate B: preservation of ordinary local tests outside the Hermes repo
# ---------------------------------------------------------------------------


class TestAllowsNonHermesTestRuns:
    def test_pytest_in_other_repo_allowed(self, other_repo):
        verdict, _ = classify_live_test_run("pytest", cwd=str(other_repo))
        assert verdict == VERDICT_ALLOWED

    def test_module_pytest_in_other_repo_allowed(self, other_repo):
        verdict, _ = classify_live_test_run(
            "python -m pytest tests/", cwd=str(other_repo))
        assert verdict == VERDICT_ALLOWED

    def test_unittest_in_other_repo_allowed(self, other_repo):
        verdict, _ = classify_live_test_run(
            "python -m unittest", cwd=str(other_repo))
        assert verdict == VERDICT_ALLOWED

    def test_make_test_in_other_repo_allowed(self, other_repo):
        verdict, _ = classify_live_test_run("make test", cwd=str(other_repo))
        assert verdict == VERDICT_ALLOWED

    @pytest.mark.parametrize(
        "command",
        [
            # The same package-runner invocations stay allowed when they are
            # NOT aimed at a Hermes checkout — ordinary local testing must
            # not regress.
            "uv run pytest",
            "uvx pytest",
            "uvx pytest tests/",
            "uv tool run pytest tests/",
            "uv run --frozen pytest tests/",
            "poetry run pytest",
            "poetry run python -m pytest tests/",
            "pipx run pytest",
            "pdm run pytest",
            "hatch run pytest",
            "hatch run +py=3.12 pytest",
            "pipenv run pytest tests/",
            "make -C {repo} test",
            "make --directory={repo} test",
        ],
    )
    def test_package_runners_in_other_repo_allowed(self, other_repo, command):
        verdict, _ = classify_live_test_run(
            command.replace("{repo}", str(other_repo)), cwd=str(other_repo))
        assert verdict == VERDICT_ALLOWED

    def test_uv_run_pytest_with_unknown_cwd_allowed(self):
        """No cwd/workdir at all: even a wrapped pytest has no established
        Hermes identity and must not fail closed (only Hermes-SPECIFIC
        runners do)."""
        verdict, _ = classify_live_test_run("uv run pytest tests/")
        assert verdict == VERDICT_ALLOWED

    def test_npm_test_in_other_repo_allowed(self, other_repo):
        verdict, _ = classify_live_test_run("npm test", cwd=str(other_repo))
        assert verdict == VERDICT_ALLOWED

    def test_unresolvable_plain_pytest_allowed(self):
        """No cwd/workdir at all: plain pytest has no established Hermes
        identity and must NOT fail closed (only Hermes-SPECIFIC runners do)."""
        verdict, _ = classify_live_test_run("pytest tests/")
        assert verdict == VERDICT_ALLOWED

    def test_non_runner_commands_in_repo_allowed(self):
        for command in ("git status", "ls tests/", "cat scripts/run_tests.sh",
                        "echo pytest", "grep -r pytest ."):
            verdict, _ = classify_live_test_run(command, cwd=str(REPO_ROOT))
            assert verdict == VERDICT_ALLOWED, command


# ---------------------------------------------------------------------------
# Fail closed: Hermes-specific runner with no resolvable target
# ---------------------------------------------------------------------------


class TestFailsClosedForHermesRunners:
    def test_run_tests_sh_without_target_blocked(self):
        """scripts/run_tests.sh only exists inside a Hermes checkout; if the
        guard cannot establish where it would run, it must refuse."""
        verdict, reason = classify_live_test_run("scripts/run_tests.sh",
                                                 cwd=None, workdir=None)
        assert verdict == VERDICT_BLOCKED
        assert "could not be established" in reason

    def test_run_tests_parallel_without_target_blocked(self):
        verdict, _ = classify_live_test_run(
            "python scripts/run_tests_parallel.py", cwd=None, workdir=None)
        assert verdict == VERDICT_BLOCKED


# ---------------------------------------------------------------------------
# Terminal choke point wiring
# ---------------------------------------------------------------------------


class TestTerminalChokePoint:
    def test_gateway_blocks_before_spawn(self, gateway_on):
        from tools.terminal_tool_guards import live_gateway_test_block

        blocked = live_gateway_test_block(
            command="scripts/run_tests.sh", env=None, env_type="local",
            cwd=str(REPO_ROOT), workdir=None, session_key="t")
        assert blocked is not None
        result = json.loads(blocked)
        assert result["status"] == "blocked"
        assert result["exit_code"] == 1
        assert "GitHub CI" in result["error"] or "CI" in result["error"]

    def test_non_gateway_never_blocks(self, gateway_off):
        from tools.terminal_tool_guards import live_gateway_test_block

        assert live_gateway_test_block(
            command="pytest", env=None, env_type="local",
            cwd=str(REPO_ROOT), workdir=None, session_key="t") is None

    def test_background_path_shares_the_block(self, gateway_on, monkeypatch):
        """terminal_tool._pre_exec_block runs BEFORE the background/foreground
        split, so background=true cannot bypass the guard."""
        from tools.terminal_tool import _pre_exec_block
        from tools.terminal_tool_guards import live_gateway_test_block

        monkeypatch.setattr(
            "tools.terminal_tool.live_gateway_test_block", live_gateway_test_block)
        with pytest.raises(Exception) as excinfo:
            _pre_exec_block(
                "pytest", env=None, env_type="local", cwd=str(REPO_ROOT),
                workdir=None, session_key="t")
        assert "Blocked" in str(excinfo.value)

    def test_guard_error_fails_closed(self, gateway_on, monkeypatch):
        from tools.terminal_tool_guards import live_gateway_test_block

        monkeypatch.setattr(
            guard, "classify_live_test_run",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        blocked = live_gateway_test_block(
            command="pytest", env=None, env_type="local",
            cwd=str(REPO_ROOT), workdir=None, session_key="t")
        assert blocked is not None
        assert json.loads(blocked)["status"] == "blocked"


# ---------------------------------------------------------------------------
# execute_code choke point
# ---------------------------------------------------------------------------


class TestExecuteCodeChokePoint:
    def test_script_spawning_pytest_in_repo_blocked(self, gateway_on, monkeypatch):
        monkeypatch.setattr(guard, "_script_child_cwd", lambda: str(REPO_ROOT))
        verdict, _ = guard.check_live_gateway_test_script(
            "import os\nos.system('python -m pytest tests/docker')")
        assert verdict == VERDICT_BLOCKED

    def test_script_spawning_hermes_runner_blocked(self, gateway_on, monkeypatch):
        monkeypatch.setattr(guard, "_script_child_cwd", lambda: str(REPO_ROOT))
        verdict, _ = guard.check_live_gateway_test_script(
            "import subprocess\nsubprocess.run(['bash', 'scripts/run_tests.sh', '-q'])")
        assert verdict == VERDICT_BLOCKED

    def test_script_list_form_pytest_blocked(self, gateway_on, monkeypatch):
        """List-form argv carries the runner as separately quoted words;
        the shell regex cannot see it. Dequoted exact-word matching must."""
        monkeypatch.setattr(guard, "_script_child_cwd", lambda: str(REPO_ROOT))
        verdict, _ = guard.check_live_gateway_test_script(
            'import subprocess\nsubprocess.run(["python", "-m", "pytest", "tests/docker"])')
        assert verdict == VERDICT_BLOCKED

    def test_script_pytest_cov_not_a_hit(self, gateway_on, monkeypatch):
        """'pytest-cov' is not a pytest invocation (exact-word match)."""
        monkeypatch.setattr(guard, "_script_child_cwd", lambda: str(REPO_ROOT))
        verdict, _ = guard.check_live_gateway_test_script(
            "import subprocess\nsubprocess.run(['pytest-cov', '--version'])")
        assert verdict == VERDICT_ALLOWED

    def test_script_comment_only_pytest_allowed(self, gateway_on, monkeypatch):
        monkeypatch.setattr(guard, "_script_child_cwd", lambda: str(REPO_ROOT))
        verdict, _ = guard.check_live_gateway_test_script(
            "# pytest\nprint('hello')")
        assert verdict == VERDICT_ALLOWED

    def test_mention_without_spawn_allowed(self, gateway_on, monkeypatch):
        monkeypatch.setattr(guard, "_script_child_cwd", lambda: str(REPO_ROOT))
        verdict, _ = guard.check_live_gateway_test_script(
            "print('the runner is scripts/run_tests.sh')")
        assert verdict == VERDICT_ALLOWED

    def test_innocuous_script_allowed(self, gateway_on, monkeypatch):
        monkeypatch.setattr(guard, "_script_child_cwd", lambda: str(REPO_ROOT))
        verdict, _ = guard.check_live_gateway_test_script(
            "import numpy\nprint(numpy.__version__)")
        assert verdict == VERDICT_ALLOWED

    def test_non_gateway_script_allowed_even_for_pytest(self, gateway_off):
        verdict, _ = guard.check_live_gateway_test_script("os.system('pytest')")
        assert verdict == VERDICT_ALLOWED

    def test_check_execute_code_guard_integrated(self, gateway_on, monkeypatch):
        """tools.approval.check_execute_code_guard must route through the
        guard before any approval flow can approve the script."""
        from tools import approval

        monkeypatch.setattr(guard, "_script_child_cwd", lambda: str(REPO_ROOT))
        result = approval.check_execute_code_guard(
            "import os\nos.system('scripts/run_tests.sh')", "local")
        assert result["approved"] is False
        assert "gateway" in result.get("message", "").lower()


# ---------------------------------------------------------------------------
# Config bridge: profile templates cannot drift from DEFAULT_CONFIG
# ---------------------------------------------------------------------------


class TestProfileDriftGuard:
    def test_default_config_declares_guard_key(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["terminal"]["block_live_gateway_tests"] is True

    def test_bridge_map_covers_guard_key(self):
        """set_config_value bridges terminal.* through TERMINAL_CONFIG_ENV_MAP;
        a missing key means `hermes config set terminal.X` silently no-ops."""
        from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP

        assert TERMINAL_CONFIG_ENV_MAP.get("block_live_gateway_tests") == \
            "TERMINAL_BLOCK_LIVE_GATEWAY_TESTS"

    def test_scope_projection_covers_guard_key(self):
        """terminal_scope.build_profile_terminal_scope projects DEFAULT_CONFIG
       ['terminal'] into the per-profile scope; a missing projection means a
        multiplexed secondary profile never sees the key."""
        import inspect

        from tools import terminal_scope

        source = inspect.getsource(terminal_scope)
        assert "_TOOL_LEVEL_DEFAULTS" in source  # the projection table exists
        from tools.terminal_scope import _TOOL_LEVEL_DEFAULTS

        assert "block_live_gateway_tests" in _TOOL_LEVEL_DEFAULTS


# ---------------------------------------------------------------------------
# Purity: the classifier never spawns anything
# ---------------------------------------------------------------------------


def test_classifier_never_spawns():
    """A guard that shells out would itself be a live-gateway hazard: the
    module must never import subprocess or CALL os.system/popen-style
    executors (detection-table STRINGS naming them are data, not calls —
    AST keeps the two apart, unlike substring matching)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(guard))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "subprocess" not in imported
    assert not any(name in imported for name in ("pty", "sh"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"system", "popen", "Popen", "spawn", "spawnl"}:
                pytest.fail(f"guard performs a process-spawning call: ast line {node.lineno}")


def test_parser_budget_failure_closed():
    """Over-budget commands are refused rather than waved through."""
    huge = "pytest " + "a" * 200_000
    verdict, _ = classify_live_test_run(huge, cwd=str(REPO_ROOT))
    assert verdict == VERDICT_BLOCKED
