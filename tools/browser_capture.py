"""Gateway side of the ``ux_capture`` helpers for ``browser_exec``.

When the submitted code names ``ux_capture``, :func:`prepare_exec` sets the
capture env (artifact root, payment-guard path, task) and prepends a loader
that imports ``tools/browser_capture_harness.py`` by path inside the harness
subprocess and binds ``ux_capture`` there. Code that does not name it runs
byte-identical.

The payment capture guard sees ``ux_capture`` as a capture token
(``tools.browser_payment_privacy._CAPTURE_TOKENS``), so a call on a slot that
held a card fill is refused before dispatch; the harness module re-checks the
same mark file before every capture.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

NAMESPACE = "ux_capture"
HARNESS_MODULE = Path(__file__).with_name("browser_capture_harness.py")

ROOT_ENV = "HERMES_UX_CAPTURE_ROOT"
GUARD_ENV = "HERMES_UX_CAPTURE_GUARD"
TASK_ENV = "HERMES_UX_CAPTURE_TASK"

_TASK_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

_PREAMBLE = """\
# hermes: ux capture helpers
def _hermes_ux_capture_bind():
    import importlib.util as _u
    _s = _u.spec_from_file_location("hermes_browser_capture_harness", {path!r})
    _m = _u.module_from_spec(_s)
    _s.loader.exec_module(_m)
    globals()["ux_capture"] = _m.bind(globals())
_hermes_ux_capture_bind()
del _hermes_ux_capture_bind
"""


def wants_capture_helpers(code: str) -> bool:
    return NAMESPACE in (code or "")


def artifacts_root() -> Path:
    """``$HERMES_HOME/artifacts/ux-capture``: runs, baselines, storyboards."""
    return Path(get_hermes_home()) / "artifacts" / "ux-capture"


def prepare_exec(code: str, env: dict, task_id: Optional[str]) -> str:
    """Return ``code`` with the ux_capture loader prepended when it is used; set its env."""
    if not wants_capture_helpers(code):
        return code
    from tools.browser_payment_privacy import _state_path

    root = artifacts_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    env[ROOT_ENV] = str(root)
    env[GUARD_ENV] = str(_state_path())
    env[TASK_ENV] = (_TASK_SAFE_RE.sub("_", str(task_id or "default"))[:80]) or "default"
    return _PREAMBLE.format(path=str(HARNESS_MODULE)) + code


_RESET_CODE = "# hermes: ux_capture reset after a failed or killed call\nux_capture.reset_slot()\n"


def reset_after_failed_exec(code: str, env: dict, dispatched: dict, cmd: list, run, task_id: Optional[str],
                            *, fenced: bool = False) -> None:
    """After a ux_capture call that timed out or exited non-zero, clear its overrides on the tab.

    Restores run in the harness process's ``finally``; a SIGKILL on timeout skips them while the
    detached daemon keeps the CDP session, so the slot would stay emulated, throttled, offline or
    screencasting. One short best-effort call on the same daemon (``BU_NAME``) undoes that.
    A browser behind the Bot Desktop human-control fence (``fenced``) is left to the next bind.
    """
    if fenced or not wants_capture_helpers(code):
        return
    proc = dispatched.get("proc")
    timed_out = "timed out" in str(dispatched.get("error_result") or "")
    if not timed_out and (proc is None or proc.returncode == 0):
        return
    try:
        run(cmd, prepare_exec(_RESET_CODE, env, task_id), env, 20)
    except Exception as exc:  # health: allow BLE001 -- best effort; the next ux_capture bind resets again
        logger.warning("ux_capture reset after failed call did not run: %s", exc)
