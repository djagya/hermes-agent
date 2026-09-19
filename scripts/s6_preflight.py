#!/usr/bin/env python3
"""Refuse to run the Hermes test suite inside a live s6 gateway container.

Running ``scripts/run_tests.sh`` inside a live s6-supervised gateway container
let the suite's Docker lifecycle tests signal the host's supervised services.
This preflight is the small fail-fast layer in the canonical test runners: it
runs before pytest import, test discovery or any child process, and exits with
a stable code when the host looks like a live gateway box.

Decision table (``evaluate`` — pure, dependency-injectable):

    multiple ``gateway-*`` services, or a ``dashboard`` service  -> refuse
        (a strong live topology; the isolated-execution opt-in below can
        never override it)

    any ``gateway-*`` service + unreadable/invalid PID 1 state  -> refuse
        (fail closed: a live gateway service tree is proven, the init check
        is not)

    PID 1 is ``s6-svscan`` + a ``gateway-*`` service             -> refuse
        unless the isolated-execution contract is met:
            ``HERMES_TEST_ISOLATED`` is exactly ``1``, and
            ``CI`` or ``HERMES_TEST_IMAGE`` (the repository CI image marker)
            is set
        A disposable s6 container running the suite against an injected
        image opts in this way; an ordinary image container (s6 init, a
        single ``gateway-default`` slot, no opt-in) refuses.

    everything else (normal dev machines and GitHub runners, including
    hosts where ``/proc`` or ``/run/service`` do not exist)       -> allow

This is an accidental-safety guard, not a same-UID security sandbox: it only
reads PID 1's comm and ``/run/service`` directory names, and a determined
same-UID process can spoof both. Detection logic lives only here; both
runners import this module (the shell runner executes it directly), so there
is no duplicated heuristic in shell or Python.

Stdlib-only and import-safe (no side effects at import time).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

#: comm of the s6 supervisor init — the "this is an s6 container" half of the
#: live-context contract.
S6_INIT_COMM = "s6-svscan"

#: Stable non-zero refusal code (EX_CONFIG — "something about this host's
#: configuration makes running the suite here wrong", not a test failure).
EXIT_REFUSED = 78

ALLOW_NON_S6 = "non-s6 host"
ALLOW_S6_NO_SERVICES = "s6 init without gateway services"
ALLOW_ISOLATED_OPTIN = "isolated execution contract accepted"
REFUSE_STRONG_TOPOLOGY = (
    "live gateway topology: multiple gateway services or a dashboard service"
)
REFUSE_LIVE_GATEWAY = "live s6 gateway container"
REFUSE_UNREADABLE_PID_WITH_SERVICES = (
    "gateway services present but pid 1 state unreadable"
)

_REFUSAL_MESSAGE = (
    "refusing to run tests: {reason}.\n"
    "Run tests via GitHub Actions CI or an isolated container "
    "(HERMES_TEST_ISOLATED=1 with CI=1 or HERMES_TEST_IMAGE set); "
    "see scripts/s6_preflight.py.\n"
)


@dataclass(frozen=True)
class Decision:
    """Outcome of the preflight: run the suite, or refuse with a reason."""

    allowed: bool
    reason: str


def _present(env: Mapping[str, str], key: str) -> bool:
    """True when ``key`` is set to a non-empty value (marker semantics)."""
    return bool(env.get(key, "").strip())


def _isolated_opt_in(env: Mapping[str, str]) -> bool:
    """True when the explicit isolated-execution contract is fully met.

    Both halves are required: the deliberate ``HERMES_TEST_ISOLATED=1``
    opt-in (exactly ``1`` — ``0``, ``true`` or any other value does not
    count, so a defaulted-to-off switch can never masquerade as an
    opt-in) AND a CI context (``CI`` or the canonical ``HERMES_TEST_IMAGE``
    marker used by the repository's docker jobs). Neither value alone,
    and no other variable, enables it.
    """
    return env.get("HERMES_TEST_ISOLATED", "").strip() == "1" and (
        _present(env, "CI") or _present(env, "HERMES_TEST_IMAGE")
    )


def evaluate(
    pid1_comm: Optional[str],
    service_names: Iterable[str],
    *,
    env: Mapping[str, str],
) -> Decision:
    """Decide allow/refuse from injected probe values (see module docstring)."""
    names = list(service_names)
    gateways = [n for n in names if n.startswith("gateway-")]
    has_dashboard = "dashboard" in names

    # Strong live topology refuses first and unconditionally: it is never a
    # legitimate shape for a disposable test container, so the opt-in must
    # not be able to override it.
    if len(gateways) >= 2 or has_dashboard:
        return Decision(False, REFUSE_STRONG_TOPOLOGY)

    if not gateways:
        if pid1_comm == S6_INIT_COMM:
            return Decision(True, ALLOW_S6_NO_SERVICES)
        return Decision(True, ALLOW_NON_S6)

    # Exactly one gateway service: the live-context contract needs the s6
    # init half too. An unreadable PID 1 next to a proven gateway tree fails
    # closed; a proven non-s6 init (dev box, odd mount) allows.
    if pid1_comm is None:
        return Decision(False, REFUSE_UNREADABLE_PID_WITH_SERVICES)
    if pid1_comm != S6_INIT_COMM:
        return Decision(True, ALLOW_NON_S6)
    if _isolated_opt_in(env):
        return Decision(True, ALLOW_ISOLATED_OPTIN)
    return Decision(False, REFUSE_LIVE_GATEWAY)


def _read_pid1_comm(path: str = "/proc/1/comm") -> Optional[str]:
    """PID 1's comm, or None when unreadable/invalid (fails closed above)."""
    try:
        comm = open(path, encoding="utf-8", errors="replace").read().strip()
    except OSError:
        return None
    return comm or None


def _list_service_names(path: str = "/run/service") -> Sequence[str]:
    """Supervised service directory names, or [] when the tree is absent."""
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def main(env: Optional[Mapping[str, str]] = None) -> int:
    """Probe the real host; return 0 to proceed or exit 78 with a reason."""
    decision = evaluate(
        _read_pid1_comm(),
        _list_service_names(),
        env=os.environ if env is None else env,
    )
    if decision.allowed:
        return 0
    print(_REFUSAL_MESSAGE.format(reason=decision.reason), file=sys.stderr)
    raise SystemExit(EXIT_REFUSED)


def preflight_or_die() -> None:
    """Entry point for the Python runner: raise SystemExit(78) on refusal."""
    main()


if __name__ == "__main__":
    sys.exit(main())
