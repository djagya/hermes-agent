"""Live-gateway test guard: refuse Hermes-repo test execution inside the running gateway.

On 2026-09-17 a release card ran the Hermes suite with the terminal tool inside
Danil's live s6 gateway container; ``tests/docker`` lifecycle tests SIGTERM'd the
real ``gateway-default`` service three times during user work. Prompt-level
policy could not hold: specialized profiles are isolated islands, profile
configs drift, and memory/skill prose is advisory text a model may ignore.

This module is the durable, path-aware enforcement the task demands. Like
:mod:`tools.self_repo_guard` it is a pure classifier plugged into the shared
pre-execution choke points:

* ``terminal_tool_guards._pre_exec_block`` — every ``terminal()`` call,
  foreground AND background;
* ``tools.approval.check_execute_code_guard`` — whole-script ``execute_code``
  approval, mirroring the ``cron.lifecycle_guard`` pattern there.

An important asymmetry versus ``self_repo_guard``: that guard is Windows-only
because a git *mutation* of the running checkout only corrupts a process whose
modules are memory-mapped from the files being rewritten. A test run has no
such bound — ``pytest tests/docker/`` reaches the live s6 service tree from any
checkout and from any interpreter — so THIS guard keys on the live supervised
gateway (``tools.process_registry._is_supervised_gateway_process``) plus the
repository identity of the command's workdir, and is active on every platform
and every backend that can reach the host filesystem (local and containerized
backends with host bind mounts).

The two closed gates:
  A. The command invokes a Hermes test runner (``pytest``/``python -m pytest``/
     ``unittest``/``make test``/``scripts/run_tests.sh``/``run_tests_parallel.py``,
     under ``sudo``/``env``/``timeout``/... wrappers and inside ``sh -c`` payloads),
     AND the command's own target path — an explicit path argument, the
     terminal ``workdir``, or the command cwd — resolves into a Hermes checkout
     (the running source root, its ``.worktrees/*``, or any checkout whose
     ``pyproject.toml`` project name is ``hermes-agent``).
  B. The command invokes a Hermes-specific runner that only exists inside a
     Hermes checkout (``scripts/run_tests.sh``, ``scripts/run_tests_parallel.py``)
     but its target cannot be established (script unreadable, cwd vanished,
     unresolvable ``..``): block. Fail closed, per the task contract, while
     ordinary ``pytest`` in an unresolvable cwd still runs.

Everything is pure string/filesystem work — no subprocesses, no shell. A guard
crash must never take the terminal tool down (#77780): callers wrap
``classify_live_test_run`` in try/except and fail CLOSED, and this module's own
unavoidable failure paths (unreadable runner script, unresolvable paths)
return ``("blocked", ...)`` for Hermes-specific runners.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

from tools.approval_detection import (
    _bash_exec_payload,
    _command_parser_limit_exceeded,
    _deobfuscate_shell_word_for_detection,
    _iter_shell_command_starts,
    _normalize_command_for_detection,
    _read_shell_word,
)
from tools.shell_heredoc import strip_inert_heredoc_bodies

logger = logging.getLogger("tools.approval")

# Classification verdicts. CALLERS MAP THESE TO ACTIONS: "blocked" must block
# execution; "allowed" must not. No other module may invent a verdict name.
VERDICT_BLOCKED = "blocked"
VERDICT_ALLOWED = "allowed"

# A run_tests.sh argument that names a path (or pytest node) to run; bare
# flags (-q, --tb=long, -k pattern is NOT here: -k's value selects tests) and
# runner options (paths/--slice/--file-timeout/-j/--jobs) are not targets.
_RUN_TESTS_TARGET_ARGS = frozenset({"paths", "--slice", "--file-timeout"})

_TEST_RUNNER_WORDS = frozenset({"pytest", "py.test", "tox", "nox", "unittest"})

_TEST_FILE_BASENAME_RE = re.compile(
    r"^(?:test_[^/\\]*|[^/\\]*_test)\.(?:py|ts|tsx|js|mjs|cjs)$")

# Wrappers whose first non-option positional (or post-``--`` token) is the
# wrapped program: normalized spellings are peeled so ``env pytest`` does not
# read as "pytest as an argument of env". Values: index bump past consumed
# option arguments (option attached form already handled by the = split).
_WRAPPER_WORDS = frozenset({
    "sudo", "env", "exec", "nohup", "setsid", "time", "nice", "stdbuf",
    "ionice", "command", "builtin", "timeout", "chrt", "taskset"})
_WRAPPER_OPTIONS_WITH_ARG = {
    "sudo": {"-C", "--chdir", "-c", "--close-from", "-g", "--group", "-h",
             "--host", "-p", "--prompt", "-u", "--user", "-T", "--command-timeout"},
    "env": {"-a", "--argv0", "-C", "--chdir", "-S", "--split-string", "-u", "--unset"},
    "exec": {"-a"}, "nice": {"-n", "--adjustment"},
    "time": {"-f", "--format", "-o", "--output"},
    "timeout": {"-k", "--kill-after", "-s", "--signal"},
    "stdbuf": {"-e", "--error", "-i", "--input", "-o", "--output"},
    "ionice": {"-c", "--class", "-n", "--classdata"},
}
# Positional operands each wrapper consumes BEFORE the wrapped program
# (`timeout 300 pytest`, `chroot /srv sh`, `taskset -c 0,1 pytest`).
_WRAPPER_POSITIONAL_ARGS = {"chroot": 1, "chrt": 1, "taskset": 1, "timeout": 1}
_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "ksh", "zsh"})
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")

_PYTHON_INVOKER_RE = re.compile(r"p(?:y)?thon[23]?(?:\.\d+)*|pypy[3]?(?:\.\d+)*")
_PYTHON_INVOKER_NAMES = frozenset({"python", "python3", "pypy", "pypy3"})


def _is_python_invoker(name: str) -> bool:
    """python/python3/python3.13/pypy spellings (basename already applied)."""
    return name in _PYTHON_INVOKER_NAMES or bool(_PYTHON_INVOKER_RE.fullmatch(name))


def _executable_name(word: str) -> str:
    name = os.path.basename(word.replace("\\", "/")).removesuffix(".exe").lower()
    # A command-substituted program word (`pytest` --version) EXECUTES the
    # inner command and feeds the shell its output: when the backticks wrap a
    # bare name, that inner name is the executable this classifier cares about.
    if name.startswith("`") and name.endswith("`") and len(name) >= 2:
        inner = name[1:-1]
        if inner and re.fullmatch(r"[A-Za-z0-9_./-]+", inner):
            return inner.lower()
    return name


def _consume_options(words: list[str], start: int,
                     options_with_arg: "set[str] | dict[str, set[str]] | None" = None) -> int:
    """Index of the first non-option token at/after *start* (``--`` ends options).
    Separate-argument option values (``env -u FOO``) are skipped so ``FOO`` is
    not mistaken for the wrapped program. *options_with_arg* is the option set
    of the CURRENT wrapper (a plain set), not the wrapper table."""
    options = options_with_arg or set()
    index = start
    while index < len(words) and words[index].startswith("-") and words[index] != "-":
        if words[index] == "--":
            return index + 1
        option = words[index].split("=", 1)[0]
        if option in options:
            index += 2
        else:
            index += 1
    return index


def _words_at(command: str, start: int, limit: int = 64) -> list[str]:
    """Deobfuscated words of the simple command beginning at *start*."""
    words: list[str] = []
    cursor = start
    for _ in range(limit):
        word_start, word_end, raw_word = _read_shell_word(command, cursor)
        if word_start == word_end or (words and "\n" in command[cursor:word_start]):
            break
        words.append(_deobfuscate_shell_word_for_detection(raw_word))
        cursor = word_end
    return words


def _split_lead(words: list[str]) -> tuple[list[str], list[str]]:
    """Split leading VAR=value assignments off; returns (assignments, rest)."""
    index = 0
    while index < len(words) and _ASSIGNMENT_RE.fullmatch(words[index]):
        index += 1
    return words[:index], words[index:]


def _strip_lead_assignments(words: list[str]) -> list[str]:
    """Same as :func:`_split_lead` but the assignments are dropped (a bare
    env-wrapped command's words: ``['env', 'CI=1', 'bash', ...]`` peels to
    ``['CI=1', 'bash', ...]`` whose leading assignments belong to ``env``,
    not to the wrapped program)."""
    return _split_lead(words)[1]


def _peel_wrappers(words: list[str]) -> list[str]:
    """Peel wrapper executables (sudo/env/timeout/...), their option arguments
    (``-u root``) and their non-option operand arguments (``timeout 300``), so
    the wrapped program's name and arguments can be classified."""
    rest = words
    for _ in range(8):
        rest = _strip_lead_assignments(rest)
        if not rest:
            return rest
        name = _executable_name(rest[0])
        if name not in _WRAPPER_WORDS:
            return rest
        index = _consume_options(rest, 1, _WRAPPER_OPTIONS_WITH_ARG.get(name, set()))
        # Positional operands the wrapper itself consumes before the wrapped
        # program: `timeout 300 pytest` runs pytest; `chroot DIR ...` too.
        positionals = _WRAPPER_POSITIONAL_ARGS.get(name, 0)
        while index < len(rest) and positionals:
            index += 1
            positionals -= 1
        if index >= len(rest):
            return []
        rest = rest[index:]
    return rest


def _operator_before(command: str, start: int) -> str | None:
    """The list operator (or newline) immediately preceding a command start."""
    head = command[:start].rstrip()
    for tail in (head[-2:], head[-1:]):
        if tail in {"&&", "||", ";", "|", "&", "(", "{"}:
            return tail
    return "\n" if "\n" in command[len(head):start] else None


_CD_TARGET_RE = re.compile(r"^(?:cd|pushd)$")


def _cd_target(args: list[str], cwd: Optional[Path]) -> Optional[Path]:
    """Directory a ``cd``/``pushd`` would land in (existing dirs only), else None."""
    index = _consume_options(args, 0)
    if index >= len(args) or args[index] == "-":
        return None
    target = _normalize_path(args[index], cwd or Path("/"))
    if target is not None and target.is_absolute() and target.is_dir():
        return target
    return None


def _iter_simple_commands(command: str, depth: int = 0,
                          cwd: Optional[Path] = None):
    """Yield ``(words, scoped_cwd)`` for every simple command, recursing into
    ``sh -c`` payloads. ``scoped_cwd`` tracks ``cd``/``pushd`` across ``&&``,
    ``;`` and newline separators so ``cd <repo> && pytest`` is judged against
    the repo directory, not the stale session cwd."""
    if depth > 4:
        return
    starts = sorted(set(_iter_shell_command_starts(command)))
    scoped_cwd = cwd
    pending_cd: Optional[Path] = None
    for start in starts:
        if pending_cd is not None and _operator_before(command, start) in {"&&", ";", "\n"}:
            scoped_cwd = pending_cd
        pending_cd = None
        words = _words_at(command, start)
        _, rest = _split_lead(words)
        if not rest:
            continue
        yield rest, scoped_cwd
        name = _executable_name(rest[0])
        if name in {"cd", "pushd"}:
            pending_cd = _cd_target(rest[1:], scoped_cwd)
            continue
        if name in _SHELL_EXECUTABLES:
            has_c, payload = _bash_exec_payload(rest[1:])
            if has_c and payload:
                yield from _iter_simple_commands(payload, depth + 1, scoped_cwd)


# ---- target path resolution ----------------------------------------------------------------


def _normalize_path(value: str, base: Path) -> Optional[Path]:
    """Expand and resolve *value* against *base*; None when it stays relative
    or the filesystem walk cannot complete (fail-closed caller decision)."""
    raw = os.path.expanduser(value)
    if not raw.strip():
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return Path(os.path.normpath(str(candidate)))
    except (OSError, RuntimeError, ValueError):
        return None


def _resolve_run_target(explicit: str | None, workdir: str | None,
                        cwd: str | None) -> tuple[Optional[Path], bool]:
    """``(target, resolved)`` — the directory a test run would exercise and
    whether identity could actually be established.

    ``explicit`` (a path argument of the runner) wins: ``pytest`` inside a
    non-Hermes cwd that says ``pytest /opt/data/hermes-agent/tests`` runs
    Hermes tests. ``cwd`` is probed last because a session cwd often outlives
    the directory it named.

    Identity is established ONLY by a value anchored on this filesystem: a
    bare relative target (``pytest tests/`` with no workdir/cwd at all) has no
    anchor, so it reports unresolved rather than claiming a bogus directory —
    plain pytest stays allowed (no fail-closed) while Hermes-specific runners
    fail closed on it.
    """
    for value, anchor in ((explicit, workdir or cwd),
                          (workdir, None),
                          (cwd, None)):
        if not value:
            continue
        base = Path(os.path.expanduser(anchor)) if anchor else Path("/")
        target = _normalize_path(str(value), base)
        if target is None:
            continue
        if not target.is_absolute():
            continue
        if target.exists():
            return target, True
        # A named test file may not exist yet (dry invocation); its nearest
        # existing ancestor still fixes identity.
        probe = target
        while len(probe.parts) > 1:
            probe = probe.parent
            if probe.exists():
                return probe, True
    return None, False


def hermes_checkout_root(path: Path, depth: int = 0) -> Optional[Path]:
    """Nearest ancestor of *path* that is a Hermes checkout, else None.

    A checkout is the running source root, anything under a ``.worktrees``
    directory of the running root (kanban worktrees have no ``.git`` of their
    own until hydrated), or a directory whose ``pyproject.toml`` declares
    ``name = "hermes-agent"``. Depth-bounded so a path near the filesystem
    root cannot turn into a long walk on the hot terminal path.
    """
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError):
        resolved = path
    probe = resolved
    for _ in range(min(depth, 8) if depth else 8):
        if _is_hermes_checkout(probe):
            return probe
        if probe.parent == probe:
            return None
        probe = probe.parent
    return None


def _is_hermes_checkout(path: Path) -> bool:
    running = _running_source_root()
    if running is not None:
        try:
            if path == running or path.is_relative_to(running):
                return True
        except (OSError, RuntimeError, ValueError):
            pass
    pyproject = path / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        with open(pyproject, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if re.fullmatch(r"\s*name\s*=\s*[\"']hermes-agent[\"']\s*", line):
                    return True
    except OSError:
        return False
    return False


def _running_source_root() -> Optional[Path]:
    """The checkout backing this process (``tools.self_repo_guard`` shape)."""
    try:
        from tools.self_repo_guard import get_running_source_root
        return get_running_source_root()
    except Exception:
        return None


def canonical_hermes_roots() -> list[Path]:
    """Existing canonical Hermes roots for THIS installation: the running
    source checkout plus its ``.worktrees/*`` siblings. Deployment-anchored —
    never a hardcoded ``/opt/data`` path and never one profile's HOME."""
    roots: list[Path] = []
    running = _running_source_root()
    if running is not None:
        roots.append(running)
        worktrees = running / ".worktrees"
        try:
            if worktrees.is_dir():
                roots.extend(child for child in sorted(worktrees.iterdir()) if child.is_dir())
        except OSError:
            pass
    return roots


def _target_is_hermes(target: Optional[Path]) -> bool:
    return target is not None and hermes_checkout_root(target) is not None


# ---- runner classification -----------------------------------------------------------------


def _classify_runner(words: list[str]) -> tuple[str, Optional[str]]:
    """``(kind, explicit_target)`` for one simple command's words.

    kind is ``""`` (not a test runner), ``"pytest"`` (pytest-family / unittest
    against the cwd or an explicit path), ``"make_test"``, ``"npm_test"``, or
    ``"hermes_runner"`` (scripts/run_tests.sh + run_tests_parallel.py:
    Hermes-anchored by NAME).
    """
    peeled = _peel_wrappers(words)
    if not peeled:
        return "", None
    name = _executable_name(peeled[0])
    args = peeled[1:]

    # A shell interpreter runs its first positional script operand: when that
    # operand's NAME is Hermes-anchored (run_tests.sh), the interpreter is
    # just a delivery mechanism (`env CI=1 bash scripts/run_tests.sh ...`).
    if name in _SHELL_EXECUTABLES:
        index = _consume_options(args, 0)
        if index < len(args) and not _ASSIGNMENT_RE.fullmatch(args[index]):
            operand = args[index]
            operand_name = _executable_name(operand)
            if operand_name.endswith(".sh") and "run_tests" in operand_name:
                return "hermes_runner", _run_tests_sh_target(args[index + 1:])
            if "run_tests_parallel" in operand_name:
                return "hermes_runner", _runner_target_arg(args[index + 1:])
        has_c, payload = _bash_exec_payload(args)
        if not has_c:
            return "", None

    if _is_python_invoker(name) and args:
        module = args[0].split("=", 1)[0]
        if module == "-m" and len(args) >= 2:
            invoked = _executable_name(args[1])
            if invoked in _TEST_RUNNER_WORDS:
                return "pytest", _runner_target_arg(args[2:])
            return "", None
        # python <script.py>: when the script's NAME pins it to the Hermes
        # suite (run_tests_parallel.py), the interpreter is just delivery.
        script_name = _executable_name(args[0])
        if "run_tests_parallel" in script_name:
            return "hermes_runner", _runner_target_arg(args[1:])
        return "", None

    if name in _TEST_RUNNER_WORDS:
        return "pytest", _runner_target_arg(args)

    if name == "make":
        first = next((a for a in args if not a.startswith("-")), "")
        if first.split("=", 1)[0] in {"test", "ci"}:
            return "make_test", None
        return "", None

    if name == "npm" or name == "pnpm" or name == "yarn" or name == "bun":
        # `npm test` / `npm run test` in a Hermes checkout drives vitest over
        # tests that shell out to the python suite (run_tests.sh).
        if name == "npm" and args and args[0] == "test":
            return "npm_test", None
        if len(args) >= 2 and args[0] == "run" and args[1].split("=", 1)[0] == "test":
            return "npm_test", None
        return "", None

    if name in {"scripts/run_tests.sh", "run_tests.sh"} or (
            name.endswith(".sh") and "run_tests" in name):
        return "hermes_runner", _run_tests_sh_target(args)

    if name == "run_tests_parallel.py" or (name.endswith(".py") and "run_tests_parallel" in name):
        return "hermes_runner", _runner_target_arg(args)

    # A referenced script is scanned only by name when it lives inside a Hermes
    # checkout and is itself resolvable (callers pass resolved text separately).
    return "", None


def _runner_target_arg(args: list[str]) -> Optional[str]:
    """First path-looking positional argument of a pytest/unittest invocation."""
    for arg in args:
        if arg == "--":
            continue
        if arg.startswith("-"):
            continue
        if "/" in arg or "\\" in arg or arg.endswith((".py", ".ts", ".tsx")) or _TEST_FILE_BASENAME_RE.match(arg):
            return arg
        # A bare `tests` or `tests/agent` positional is still a path target.
        if arg in {"tests", "tests/"} or arg.startswith(("tests/", "tests\\")):
            return arg
        return None
    return None


def _run_tests_sh_target(args: list[str]) -> Optional[str]:
    """The path target of ``scripts/run_tests.sh`` from its own option grammar:
    positional paths override discovery; --paths/--slice/--file-timeout carry
    theirs; -j/--jobs do not."""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if not arg.startswith("-"):
            return arg
        option = arg.split("=", 1)[0]
        if option in _RUN_TESTS_TARGET_ARGS:
            if "=" in arg:
                return arg.split("=", 1)[1]
            if index + 1 < len(args):
                return args[index + 1]
            return None
        index += 2 if option in {"-j", "--jobs"} and "=" not in arg else 1
    positional = next((a for a in args[index:] if not a.startswith("-")), None)
    return positional


def _classify_runner_script(words: list[str], base: Path) -> tuple[str, Optional[str]]:
    """Classify ``<script> <args>`` where *words[0]* names an existing file.

    Only NAME-anchored Hermes runners (``run_tests*``) are claimed here: an
    existing file whose name is a known runner word (``/usr/bin/python3``) is
    NOT enough — its arguments decide (``python3 -m pytest``), so fall back to
    the generic classifier.
    """
    path = _normalize_path(words[0], base)
    if path is not None and "run_tests" in path.name:
        return "hermes_runner", _run_tests_sh_target(words[1:])
    return _classify_runner(words)


# ---- public classifier ---------------------------------------------------------------------


def classify_live_test_run(
    command: str,
    *,
    cwd: str | None = None,
    workdir: str | None = None,
) -> tuple[str, str]:
    """Classify whether *command* would run Hermes tests against this box.

    Returns ``(verdict, reason)``. ``verdict`` is :data:`VERDICT_BLOCKED` when
    the command must not run inside the live gateway, :data:`VERDICT_ALLOWED`
    when it may. ``reason`` is a human explanation for the block message and
    audit logs. Pure: no subprocesses, no writes, no network.

    Gates (both required for a block unless identity is unresolvable):

    1. **Runner**: the command (after wrapper peeling, quote/ANSI/IFS
       normalization, ``sh -c`` recursion, heredoc-inert masking) invokes
       pytest/py.test/unittest/tox/nox, ``make test``, ``npm test``, or a
       Hermes-specific runner (``scripts/run_tests.sh``,
       ``run_tests_parallel.py``, or any referenced script whose NAME carries
       ``run_tests``).
    2. **Target**: the run's target directory — the runner's explicit path
       argument, else the terminal ``workdir``, else ``cwd`` — resolves into a
       Hermes checkout. When a Hermes-SPECIFIC runner is invoked and no target
       can be established, the verdict is blocked (fail closed); ordinary
       pytest with no resolvable target stays allowed.
    """
    if not command or not command.strip():
        return VERDICT_ALLOWED, ""
    if _command_parser_limit_exceeded(command):
        return VERDICT_BLOCKED, (
            "command exceeds the safe parsing budget; refusing to evaluate a "
            "possible Hermes test run inside the live gateway")
    # Mask provably-inert heredoc bodies (quoted, terminated, non-shell
    # consumer) so their text cannot fake a runner; everything else stays raw.
    masked = strip_inert_heredoc_bodies(command)
    normalized = _normalize_command_for_detection(masked)

    explicit_candidates: list[str] = []
    kinds: set[str] = set()
    # A cd-derived target is authoritative: the operator pointed the runner at
    # that directory IN the command. It outranks the ambient workdir/cwd.
    cd_targets: list[Path] = []
    session_cwd = Path(os.path.expanduser(cwd)) if cwd else None
    for words, scoped_cwd in _iter_simple_commands(normalized, cwd=session_cwd):
        if scoped_cwd is not None and scoped_cwd != session_cwd:
            cd_targets.append(scoped_cwd)
        first = words[0] if words else ""
        base = scoped_cwd or session_cwd or Path(os.getcwd())
        if _words_name_existing_script(first, base):
            kind, explicit = _classify_runner_script(words, base)
        else:
            kind, explicit = _classify_runner(words)
        if kind:
            kinds.add(kind)
            if explicit:
                explicit_candidates.append(explicit)

    if not kinds:
        return VERDICT_ALLOWED, ""

    target, resolved = _resolve_run_target(
        explicit_candidates[0] if explicit_candidates else None, workdir, cwd)
    if target is None and cd_targets:
        target, resolved = cd_targets[0], True

    if _target_is_hermes(target):
        return VERDICT_BLOCKED, (
            "test runner targets the Hermes checkout at "
            f"{target} (command cwd/workdir resolved inside the live gateway)")

    if "hermes_runner" in kinds and not resolved:
        return VERDICT_BLOCKED, (
            "Hermes-specific test runner invoked but its target directory "
            "could not be established; failing closed inside the live gateway")

    return VERDICT_ALLOWED, ""


def _words_name_existing_script(word: str, base: Path) -> bool:
    path = _normalize_path(word, base)
    return path is not None and path.is_file()


def check_live_gateway_test_script(code: str) -> tuple[str, str]:
    """execute_code choke point adapter for a Python script body.

    A script can spawn the suite via ``os.system``/``subprocess`` without ever
    passing through the terminal tool, so the execute_code guard mirrors it
    (same relationship as the cron lifecycle guard). Detection is two-layer:
    the shell classifier over the raw script text (a script that happens to
    BUILD a shell string) plus a Python-source scan for the canonical spawn
    shapes — ``python -m pytest`` / bare ``pytest`` calls and the
    Hermes-specific runner scripts passed to ``os.system``/``subprocess``.
    Gated on the supervised gateway. Never raises.
    """
    try:
        from tools.process_registry import _is_supervised_gateway_process
        if not _is_supervised_gateway_process():
            return VERDICT_ALLOWED, ""
    except Exception:
        return VERDICT_ALLOWED, ""
    try:
        child_cwd = _script_child_cwd()
        # When the body parses as Python, the Python-source scan is
        # authoritative: shell-level word peeling misreads `print('pytest')`
        # as a pytest run. Only unparseable text (a script assembling a shell
        # string) falls back to the shell classifier.
        try:
            import ast as _ast
            _ast.parse(code)
        except (SyntaxError, ValueError):
            verdict, reason = classify_live_test_run(code, cwd=child_cwd)
            if verdict == VERDICT_BLOCKED:
                return verdict, reason
        return _python_source_test_hit(code, child_cwd)
    except Exception as exc:
        logger.warning("live-gateway execute_code test guard failed; blocking: %s",
                       exc, exc_info=True)
        return VERDICT_BLOCKED, f"guard error: {type(exc).__name__}: {exc}"


_PY_SPAWN_FUNCS = frozenset({
    "os.system", "os.popen", "subprocess.run", "subprocess.Popen",
    "subprocess.check_call", "subprocess.check_output", "subprocess.call",
    "subprocess.getoutput", "subprocess.getstatusoutput"})
_PY_TEST_CALL_RE = re.compile(
    r"\b(?:python3?(?:\.\d+)*\s+-m\s+)?pytest\b")
_PY_UNittest_RE = re.compile(r"\bunittest\b")
_PY_RUNNER_RE = re.compile(r"run_tests(?:_parallel)?\.py|run_tests\.sh")
_MAKE_TEST_RE = re.compile(r"\bmake\s+(?:test|ci)\b")
_MAKE_ARG_TEST_RE = re.compile(r"[\"'](?:test|ci)[\"']")


def _python_source_test_hit(code: str, child_cwd: str) -> tuple[str, str]:
    """Scan a Python script for subprocess shapes that would run the Hermes
    suite from the kernel's cwd. Deliberately shallow: it pairs a spawn
    function name (or a spawn-bearing AST) with a runner/pytest token, then
    resolves the target through the same repo-identity gate as the shell
    path. An AST parse failure falls back to the regex pair over raw text.
    """
    import ast as _ast

    spawn_spans: list[tuple[int, int]] = []
    try:
        tree = _ast.parse(code)
        for node in _ast.walk(tree):
            target_ok = False
            if isinstance(node, _ast.Call):
                func = node.func
                dotted = ""
                while isinstance(func, _ast.Attribute):
                    dotted = f".{func.attr}{dotted}"
                    func = func.value
                if isinstance(func, _ast.Name):
                    dotted = f"{func.id}{dotted}"
                target_ok = dotted in _PY_SPAWN_FUNCS or dotted.split(".")[-1] in {
                    "system", "popen", "run", "Popen", "call", "check_call",
                    "check_output", "getoutput", "getstatusoutput"}
            if target_ok and hasattr(node, "lineno"):
                spawn_spans.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
    except SyntaxError:
        pass

    def _line_has_spawn(line: str) -> bool:
        return any(
            f"{name}(" in line.replace(" ", "") for name in
            ("os.system", "os.popen", "subprocess.run", "subprocess.Popen",
             "subprocess.call", "subprocess.check_call", "subprocess.check_output",
             "subprocess.getoutput", "subprocess.getstatusoutput")
        ) or bool(re.search(r"\b(?:system|popen|run|Popen|call|check_call|check_output)\s*\(",
                            line))

    lines = code.splitlines()
    for index, line in enumerate(lines):
        # The pytest token must ride a spawn shape (same line, or the line is
        # inside a multi-line spawn call). A string literal or comment mention
        # with no spawn nearby stays allowed.
        spawn_here = _line_has_spawn(line) or _in_spawn_span(spawn_spans, index + 1)
        comment_only = line.lstrip().startswith("#")
        if not spawn_here or comment_only:
            continue
        if _PY_TEST_CALL_RE.search(line):
            hit = "pytest invocation"
        elif _PY_RUNNER_RE.search(line):
            hit = "Hermes-specific runner script"
        elif _MAKE_TEST_RE.search(line) or (
                "'make'" in line or '"make"' in line) and _MAKE_ARG_TEST_RE.search(line):
            hit = "make test invocation"
        elif _PY_UNittest_RE.search(line) and "python" in line:
            hit = "unittest invocation"
        else:
            continue
        if _cwd_is_hermes(child_cwd):
            return VERDICT_BLOCKED, (
                f"{hit} in execute_code script targets the Hermes checkout at "
                f"{child_cwd}")
        # An explicit repo path inside the spawn argument also blocks.
        for match in re.finditer(r"[\"']([^\"']*/hermes-agent[^\"']*)[\"']", line):
            if _cwd_is_hermes(match.group(1)):
                return VERDICT_BLOCKED, (
                    f"{hit} in execute_code script targets the Hermes checkout at "
                    f"{match.group(1)}")
    return VERDICT_ALLOWED, ""


def _in_spawn_span(spans: list[tuple[int, int]], lineno: int) -> bool:
    return any(start <= lineno <= end for start, end in spans)


def _cwd_is_hermes(path: str | None) -> bool:
    if not path:
        return False
    try:
        target = Path(path)
        return target.exists() and hermes_checkout_root(target) is not None
    except (OSError, RuntimeError, ValueError):
        return False


def _script_child_cwd() -> str:
    """The cwd an execute_code session kernel would use in project mode."""
    try:
        from tools.code_execution_env import _resolve_child_cwd
        return _resolve_child_cwd("project", "", task_id="") or ""
    except Exception:
        return ""


def hermes_live_test_block(
    *,
    command: str,
    env_type: str,
    cwd: str,
    workdir: Optional[str],
    env: Optional[object] = None,
) -> Optional[str]:
    """Terminal choke point adapter: the finished blocked-result JSON, or None.

    Gated on the live supervised gateway (``_is_supervised_gateway_process``):
    a CLI or unsupervised run may execute Hermes tests locally — CI parity
    instructions tell developers to do exactly that — while the gateway
    process, which serves Danil's production s6 tree, must never spawn the
    suite. Applies on local AND container-with-host-mount backends: the
    incident path was a local run inside the gateway container.

    Never raises: a classification failure blocks (fail closed) with a
    diagnostic, because an unevaluable command near the live gateway is not
    provably safe (#76762 contract).
    """
    try:
        from tools.process_registry import _is_supervised_gateway_process
        if not _is_supervised_gateway_process():
            return None
    except Exception:
        return None

    # Operator kill-switch: terminal.block_live_gateway_tests (default on).
    # Read scope-aware so a multiplexed secondary profile's own config wins.
    try:
        from tools.terminal_tool_config import _tenv_bool
        if not _tenv_bool("TERMINAL_BLOCK_LIVE_GATEWAY_TESTS", "true"):
            return None
    except Exception:
        pass  # scope/config unreadable: keep the guard armed

    from tools.terminal_tool_guards import _blocked_json

    try:
        verdict, reason = classify_live_test_run(command, cwd=cwd, workdir=workdir)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("live-gateway test guard failed; blocking: %s", exc, exc_info=True)
        verdict, reason = VERDICT_BLOCKED, f"guard error: {type(exc).__name__}: {exc}"
    if verdict != VERDICT_BLOCKED:
        return None
    logger.warning("Blocked Hermes test run inside live gateway (command: %s)",
                   command[:200])
    return _blocked_json(
        "Blocked: this command would run the Hermes test suite inside the live "
        f"gateway container ({reason}). Tests in the Hermes repo signal "
        "lifecycle handlers (s6/service trees) and have SIGTERM'd the running "
        "gateway-default service. Run Hermes tests through exact-head GitHub "
        "CI or a separate disposable container with isolated PID/service "
        "namespaces — never inside this gateway. Local test runs stay "
        "available for non-Hermes projects.",
        "blocked",
    )
