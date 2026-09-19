"""Invariant tests for the canonical runners' s6 live-gateway preflight.

Regression root: running ``scripts/run_tests.sh`` inside a live
s6-supervised gateway container let the suite's Docker lifecycle tests
signal the host's supervised services. The preflight must refuse that
context before pytest import, discovery or any child process, while
never affecting normal dev machines or GitHub Actions runners.

Everything here is hermetic: the decision core gets injected probe
values, and the runner-ordering tests use PATH-shim interpreters that
never signal a real process. The shims carry real shebangs (they are
exec'd directly — without one the kernel hands them to the shell via
ENOEXEC, which cannot run Python). The handoff test uses a delegating
shim that re-execs the real interpreter found on PATH with its own
directory excluded — excluding itself is what prevents the shim from
recursing into itself. Private host-specific denylists stay
out-of-tree by design; see the message-hygiene test below.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_FILE = REPO_ROOT / "scripts" / "s6_preflight.py"
RUNNER_FILE = REPO_ROOT / "scripts" / "run_tests_parallel.py"
RUN_TESTS_SH = REPO_ROOT / "scripts" / "run_tests.sh"

#: Marker the ordering tests watch for; printed by the shim interpreters
#: on every invocation so a test can prove *when* an interpreter was
#: reached relative to the runner's other work.
S6_GUARD_MARKER = "s6_preflight_probe"


def _load_guard():
    spec = importlib.util.spec_from_file_location("s6_preflight", GUARD_FILE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass creation resolves its own module
    spec.loader.exec_module(mod)
    return mod


def _services(root: Path, names: tuple[str, ...]) -> Path:
    service_dir = root / "run" / "service"
    for name in names:
        (service_dir / name).mkdir(parents=True, exist_ok=True)
    return service_dir


# ---------------------------------------------------------------------------
# Decision table (evaluate, fully injected)
# ---------------------------------------------------------------------------


def test_non_s6_pid1_allows() -> None:
    guard = _load_guard()
    d = guard.evaluate("systemd", ["cron", "dbus"], env={})
    assert d.allowed and d.reason == guard.ALLOW_NON_S6


def test_s6_without_gateway_services_allows_without_live_claim() -> None:
    guard = _load_guard()
    d = guard.evaluate("s6-svscan", [], env={})
    assert d.allowed and d.reason == guard.ALLOW_S6_NO_SERVICES


@pytest.mark.parametrize("env", [{}, {"HERMES_TEST_ISOLATED": "1"}, {"CI": "1"}])
def test_s6_with_one_gateway_and_no_full_optin_refuses(env: dict) -> None:
    guard = _load_guard()
    d = guard.evaluate("s6-svscan", ["gateway-default"], env=env)
    assert not d.allowed and d.reason == guard.REFUSE_LIVE_GATEWAY


@pytest.mark.parametrize(
    "marker",
    [
        {"HERMES_TEST_ISOLATED": "1", "CI": "1"},
        {"HERMES_TEST_ISOLATED": "1", "HERMES_TEST_IMAGE": "x:y"},
    ],
)
def test_s6_with_one_gateway_and_full_optin_allows(marker: dict) -> None:
    guard = _load_guard()
    d = guard.evaluate("s6-svscan", ["gateway-default"], env=marker)
    assert d.allowed and d.reason == guard.ALLOW_ISOLATED_OPTIN


@pytest.mark.parametrize("value", ["0", "true", ""])
def test_isolated_optin_requires_exactly_one(value: str) -> None:
    """The opt-in switch is ``HERMES_TEST_ISOLATED=1`` — nothing else counts.

    A defaulted-to-off value (``0``) or a prose value (``true``) must never
    masquerade as the deliberate opt-in; CI/IMAGE alone is only the second
    half and cannot satisfy the contract by itself.
    """
    guard = _load_guard()
    for env in (
        {"HERMES_TEST_ISOLATED": value, "CI": "1"},
        {"HERMES_TEST_ISOLATED": value, "HERMES_TEST_IMAGE": "x:y"},
    ):
        d = guard.evaluate("s6-svscan", ["gateway-default"], env=env)
        assert not d.allowed and d.reason == guard.REFUSE_LIVE_GATEWAY


@pytest.mark.parametrize(
    "names", [("gateway-default", "gateway-ci"), ("gateway-default", "dashboard")]
)
def test_strong_live_topology_refuses_even_with_optin(names: tuple[str, ...]) -> None:
    guard = _load_guard()
    d = guard.evaluate("s6-svscan", names, env={"HERMES_TEST_ISOLATED": "1", "CI": "1"})
    assert not d.allowed and d.reason == guard.REFUSE_STRONG_TOPOLOGY


def test_non_s6_init_with_one_gateway_allows() -> None:
    guard = _load_guard()
    d = guard.evaluate("systemd", ["gateway-default"], env={})
    assert d.allowed and d.reason == guard.ALLOW_NON_S6


def test_unreadable_pid1_with_gateway_service_fails_closed() -> None:
    guard = _load_guard()
    d = guard.evaluate(
        None, ["gateway-default"], env={"HERMES_TEST_ISOLATED": "1", "CI": "1"}
    )
    assert not d.allowed and d.reason == guard.REFUSE_UNREADABLE_PID_WITH_SERVICES


def test_gateway_glob_matches_prefix_only() -> None:
    guard = _load_guard()
    d = guard.evaluate("s6-svscan", ["gatewayish"], env={})
    assert d.allowed


# ---------------------------------------------------------------------------
# Stable refusal: exit code, stderr route, allow path silent
# ---------------------------------------------------------------------------


def test_refusal_exits_78_with_route_on_stderr_and_allow_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    guard = _load_guard()
    service_dir = _services(tmp_path, ("gateway-default",))
    assert guard._list_service_names(service_dir) == [
        "gateway-default"
    ]  # fixture shape
    monkeypatch.setattr(guard, "_read_pid1_comm", lambda *a, **k: "s6-svscan")
    monkeypatch.setattr(
        guard, "_list_service_names", lambda *a, **k: ["gateway-default"]
    )

    with pytest.raises(SystemExit) as exc:
        guard.main(env={})
    assert exc.value.code == 78 == guard.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "GitHub Actions CI or an isolated container" in err
    assert "HERMES_TEST_ISOLATED=1" in err

    monkeypatch.setattr(guard, "_list_service_names", lambda *a, **k: [])
    assert guard.main(env={}) == 0
    assert capsys.readouterr().err == ""


def test_preflight_probe_failures_fail_closed_only_with_services(
    tmp_path: Path,
) -> None:
    guard = _load_guard()
    assert guard._read_pid1_comm(tmp_path / "missing" / "comm") is None
    assert guard._list_service_names(tmp_path / "missing") == []
    d = guard.evaluate(None, ["gateway-default"], env={})
    assert not d.allowed


# ---------------------------------------------------------------------------
# Ordering: both runners invoke the guard before any test work
# ---------------------------------------------------------------------------

# Refusing shim: prints the marker, refuses every direct invocation. Makes
# the shell-runner ordering proof hermetic and identical on dev machines,
# CI runners and live gateway boxes (the run must die at the preflight
# regardless of whether the real guard would refuse that particular host).
#
# Both shims are ``#!/bin/sh`` scripts, NOT ``#!/usr/bin/env python3``:
# under the shim-first PATH, an ``env python3`` shebang resolves to the
# shim itself, so the kernel re-processes the shebang on every exec and
# the process loops forever without ever reaching the shim's code.
_REFUSING_SHIM = "#!/bin/sh\necho %s >&2\nexit 78\n" % S6_GUARD_MARKER

# Delegating shim: prints the marker, then re-execs the invocation with the
# real interpreter — found on PATH with the shim's own directory excluded
# (keeping it, or an ``env python3`` shebang, makes the shim find and
# re-exec itself). Lets a test run the REAL guard and the REAL runner
# end-to-end under a shimmed ``python3``.
_DELEGATING_SHIM = (
    textwrap.dedent(
        """
    #!/bin/sh
    echo %s >&2
    here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 78
    real=""
    oldIFS=$IFS
    IFS=:
    for d in $PATH; do
        IFS=$oldIFS
        [ -n "$d" ] || d=.
        [ "$d" = "$here" ] && continue
        if [ -x "$d/python3" ]; then
            real="$d/python3"
            break
        fi
    done
    IFS=$oldIFS
    [ -n "$real" ] || exit 78
    exec "$real" "$@"
    """
    )
    # strip("\n"), not strip(): the shim is exec'd DIRECTLY, so its first
    # line must be the shebang. dedent keeps the leading newline; a bare
    # strip() would hide the defect while embedding a leading-blank-line
    # trap for anyone reformatting the literal.
    .strip("\n")
    % S6_GUARD_MARKER
)


def _path_shim(tmp_path: Path, source: str, name: str = "python3") -> Path:
    bin_dir = tmp_path / "shim-bin"
    bin_dir.mkdir()
    shim = bin_dir / name
    shim.write_text(source, encoding="utf-8")
    for bit in (0o100, 0o010, 0o001):
        shim.chmod(shim.stat().st_mode | bit)
    return bin_dir


def _shimmed_path(shim_bin: Path) -> str:
    """Shim first, then the real system dirs — ``bash`` and the delegated
    real interpreter must remain resolvable under the overridden PATH."""
    return os.pathsep.join(
        [str(shim_bin)] + [d for d in ("/usr/bin", "/bin") if Path(d).is_dir()]
    )


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only probe: bash + PATH shim"
)
def test_shell_runner_invokes_preflight_before_test_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusal precedes the test command even with profile HOME cleared.

    The refusing shim intercepts the runner's interpreter probes, so only
    the marker can appear on stderr — the guard's own refusal message is
    asserted in-process by the exit-code test above. The shim environment
    has no ``~/.hermes`` pytest plugin and no venv, so a pass here also
    proves the guard cannot be bypassed by clearing profile
    HOME/PYTEST_PLUGINS.
    """
    shim_bin = _path_shim(tmp_path, _REFUSING_SHIM)
    monkeypatch.setenv("PATH", _shimmed_path(shim_bin))
    # bash must be resolved BEFORE the env override: subprocess resolves the
    # executable against the provided env's PATH, which is shim-first.
    bash = shutil.which("bash")
    assert bash is not None
    proc = subprocess.run(
        [bash, str(RUN_TESTS_SH)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": _shimmed_path(shim_bin),
            "HOME": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    assert proc.returncode == 78
    assert S6_GUARD_MARKER in proc.stderr  # guard ran (intercepted shim)
    assert (
        "▶ running per-file parallel test suite" not in proc.stdout
    )  # test command never ran
    assert "Discovered" not in proc.stdout  # discovery never happened


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only probe: sibling bash shim"
)
def test_direct_parallel_runner_invokes_preflight_before_discovery(
    tmp_path: Path,
) -> None:
    """A bare ``python run_tests_parallel.py`` cannot bypass the guard.

    Invokes the real runner entry point with a refusing ``_run_s6_preflight``
    patched in (that function is, by construction, the guard invocation):
    the refusal must surface before discovery or any pytest subprocess, on
    any host — including ordinary CI runners, where the real guard allows.

    POSIX-only like the sibling runner probe in ``test_run_tests_parallel.py``
    (the injection itself is cross-platform; the pair shares this file's
    bash shim semantics and is reviewed together).
    """
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_probe.py").write_text(
        "def test_smoke():\n    assert True\n", encoding="utf-8"
    )
    inject = (
        "import sys\n"
        f"sys.path.insert(0, {str(RUNNER_FILE.parent)!r})\n"
        "import run_tests_parallel as runner\n"
        "def _refusing():\n"
        "    print('s6 preflight stub: refusing', file=sys.stderr)\n"
        "    raise SystemExit(78)\n"
        "runner._run_s6_preflight = _refusing\n"
        "raise SystemExit(runner.main())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", inject, "--paths", str(tests_dir)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 78, (proc.stdout, proc.stderr)
    assert "s6 preflight stub: refusing" in proc.stderr
    assert "Discovered" not in proc.stdout  # discovery never happened
    assert "passed" not in proc.stdout  # no test executed


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only probe: bash + PATH shim"
)
def test_isolated_optin_survives_hermetic_env_handoff(tmp_path: Path) -> None:
    """The §2 opt-in must hold through BOTH guard evaluations on one host.

    ``run_tests.sh`` evaluates the guard in the outer shell, then re-execs
    the parallel runner under a hermetic ``env -i`` where the guard
    evaluates again. If the handoff stripped either half of the opt-in
    contract (``HERMES_TEST_ISOLATED``, ``CI``), the inner guard would
    re-refuse a container the outer guard allowed. The two halves are
    proven together:

    * the traced ``env -i`` exec line — the exact handoff boundary —
      carries ``HERMES_TEST_ISOLATED=1`` and ``CI=1`` and not a control
      var the forward allowlist does not name;
    * the real inner runner then runs to its bounded ``--generate-slices``
      exit (discovery output, JSON matrix, rc 0) under the delivered
      environment. On an ordinary dev box the inner guard allows either
      way (no s6 services), so the delivery proof is the exec line; on a
      disposable s6 container the inner guard re-refuses unless the
      delivery happened, so there the runner output alone is the proof.

    The delegating PATH shim lets the REAL guard evaluate in the outer
    shell. Skips only where that real preflight refuses this host even
    with the full opt-in — i.e. on the live-gateway boxes the guard
    exists for.
    """
    guard = _load_guard()
    real = guard.evaluate(
        guard._read_pid1_comm(),
        guard._list_service_names(),
        env={"HERMES_TEST_ISOLATED": "1", "CI": "1"},
    )
    if not real.allowed:
        pytest.skip(
            "host is a live s6 gateway box (real preflight refuses with the "
            "full opt-in); the run_tests.sh handoff contract runs in isolated CI"
        )

    # Guarantees the runner's venv probe succeeds on any host (repo venvs
    # win when present and pytest-capable; this stub only fills the gap),
    # so the exec line is always reached and the test never depends on
    # host venv layout.
    real_python = shutil.which("python3")
    assert real_python is not None
    stub_pkg = tmp_path / "stub-pytest" / "pytest"
    stub_pkg.mkdir(parents=True)
    (stub_pkg / "__init__.py").write_text("", encoding="utf-8")
    stub_bin = tmp_path / ".hermes" / "hermes-agent" / "venv" / "bin"
    stub_bin.mkdir(parents=True)
    (stub_bin.parent / "activate").write_text("# stub venv (runner probe)\n")
    stub_python = stub_bin / "python"
    stub_python.write_text(
        f'#!/bin/sh\nexport PYTHONPATH="{stub_pkg.parent}"\nexec "{real_python}" "$@"\n'
    )
    stub_python.chmod(stub_python.stat().st_mode | 0o111)

    shim_bin = _path_shim(tmp_path, _DELEGATING_SHIM)
    # bash must be resolved BEFORE the env override (shim-first PATH).
    bash = shutil.which("bash")
    assert bash is not None
    proc = subprocess.run(
        [bash, "-x", str(RUN_TESTS_SH), "--generate-slices", "2"],
        capture_output=True,
        text=True,
        timeout=180,
        env={
            "PATH": _shimmed_path(shim_bin),
            "HOME": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1",
            # Opt-in under test:
            "HERMES_TEST_ISOLATED": "1",
            "CI": "1",
            # Negative control: NOT in run_tests.sh's forward allowlist, so
            # its absence from the exec line proves the allowlist (not trace
            # noise) is what we are reading.
            "HERMES_S6_HANDOFF_PROBE": "outer-only",
        },
    )
    assert proc.returncode == 0, (proc.stdout[-2000:], proc.stderr[-2000:])
    assert "refusing to run tests" not in proc.stderr  # outer guard allowed

    # The handoff boundary: exactly one exec of the parallel runner, with
    # both opt-in halves forwarded and the control var stripped.
    exec_lines = [
        line
        for line in proc.stderr.splitlines()
        if "env -i" in line and "run_tests_parallel.py" in line
    ]
    assert len(exec_lines) == 1, (
        f"expected exactly one traced exec of the parallel runner, "
        f"got {len(exec_lines)}:\n" + "\n".join(exec_lines)
    )
    assert "HERMES_TEST_ISOLATED=1" in exec_lines[0]
    assert "CI=1" in exec_lines[0]
    assert "HERMES_S6_HANDOFF_PROBE" not in exec_lines[0]
    assert "TZ=UTC" in exec_lines[0]  # hermetic base intact alongside the opt-in

    # The real inner runner ran under the delivered env: guard allowed,
    # discovery executed, bounded --generate-slices exit.
    assert "Discovered" in proc.stdout
    assert '"slice"' in proc.stdout


# ---------------------------------------------------------------------------
# Message hygiene (public surface; the private-token denylist itself is
# out-of-tree by design — embedding it here would leak exactly what it
# checks for. The positive route/message assertions live in the exit-code
# test above; this pins the remaining host-specific shape.)
# ---------------------------------------------------------------------------


def test_refusal_message_has_no_host_specific_paths() -> None:
    """The refusal names CI/isolated execution, never a host-specific path.

    Every path in the message is repo-relative or explicitly relative;
    an absolute path would leak deployment layout into public errors.
    """
    guard = _load_guard()
    for reason in (
        guard.REFUSE_LIVE_GATEWAY,
        guard.REFUSE_STRONG_TOPOLOGY,
        guard.REFUSE_UNREADABLE_PID_WITH_SERVICES,
    ):
        rendered = guard._REFUSAL_MESSAGE.format(reason=reason)
        for line in rendered.splitlines():
            assert not line.lstrip().startswith("/"), (
                f"absolute path in refusal message: {line!r}"
            )
        assert "GitHub Actions CI" in rendered
        assert "HERMES_TEST_ISOLATED=1" in rendered
