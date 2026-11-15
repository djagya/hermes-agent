"""Network-class run failures that never alert (owner decision 2026-11-15).

A host that loses DNS or its uplink for a minute fails every job that runs in it, and one ping
per job buries the real failures. A run whose error text carries one of the allow-listed
signatures below is recorded (incident ledger, failure type ``transient_network``) but does not
count toward the job's failure streak, so no alert threshold is reached, and delivers no failure
notice. Any other failure alerts exactly as before.

The match is on the run's error line only, never on script stdout or earlier log chatter: the last
error-looking line of the error text (of the script's stderr when the error carries one), plus the
final line. An allow-list of exact network phrases and of exception names in exception-prefix
position (``httpx.ConnectError: ...``, not ``ImportError: cannot import name ConnectError``); a
signature is added here only after it showed up in a real run, and only when it cannot also be a
credential, git or build-tool message. An agent's own ``[CRON_FAILURE]`` verdict is prose about the
job's subject and is never classified.

This is the message half of the check. ``cron.network_probe`` is the other half: when the box
itself is offline, any failure in that window is network-class whatever its message. Neither half
silences a job for good: ``network_failing_since`` (stamped on the job, see ``cron.jobs``) ages out
after ``cron.failure_repeat_alert_hours`` and the failure then alerts normally.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

TRANSIENT_NETWORK_FAILURE_TYPE = "transient_network"

_MAX_SCANNED_CHARS = 8000
_SIGNATURES = (
    # DNS. Not "failed to resolve": that also reads "Failed to resolve a Nous inference API key",
    # git's "Failed to resolve 'HEAD'" and a bundler's "failed to resolve import".
    "could not resolve host", "temporary failure in name resolution", "name or service not known",
    "nodename nor servname provided", "getaddrinfo failed",
    # transport
    "connection reset by peer", "connection refused", "connection aborted", "network is unreachable",
    "no route to host", "remote end closed connection", "server disconnected without sending a response",
    "tls handshake timeout", "ssl handshake timeout", "handshake operation timed out",
    "connection timed out", "connect timeout",
)
# Exception class names, only where they lead the line as ``[pkg.mod.]Type:`` / ``Type(``, behind
# at most one ``WrapperError: `` prefix (the scheduler writes ``RuntimeError: <message>``).
_EXCEPTION_NAMES = (
    "ConnectError", "ConnectTimeout", "APIConnectionError", "ClientConnectorError",
    "ClientConnectorDNSError", "RemoteDisconnected", "NewConnectionError",
)
_NAME_RE = re.compile(r"^\s*(?:\w+(?:\.\w+)*:\s+)?(?:\w+\.)*(?:" + "|".join(_EXCEPTION_NAMES) + r")(?=\s*[:(])")
_STDERR_RE = re.compile(r"(?:^|\n)stderr:\n(.*?)(?:\nstdout:\n|\Z)", re.DOTALL)
_ERROR_LINE_RE = re.compile(
    r"error|fail|exception|traceback|denied|refused|unreachable|timed? ?out|fatal|exit", re.IGNORECASE)


def _error_lines(error: str) -> list[str]:
    """The lines that name the failing step. A script failure (``Script exited with code N`` then
    ``stderr:`` / ``stdout:``) contributes its stderr only; the script's stdout is its own report
    and never evidence of why the run failed."""
    match = _STDERR_RE.search(error)  # split first: a long stdout must not push stderr out of view
    if match:
        text = match.group(1)[-_MAX_SCANNED_CHARS:]
    elif error.lstrip().startswith("Script exited with code"):
        return []
    else:
        text = error[-_MAX_SCANNED_CHARS:]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    last_error = next((ln for ln in reversed(lines) if _ERROR_LINE_RE.search(ln)), lines[-1])
    return [last_error] if last_error == lines[-1] else [last_error, lines[-1]]


def is_transient_network_failure(error: Optional[str], *, agent_declared: bool = False) -> bool:
    """True when the run's error line names a DNS / connection-class failure from the allow-list."""
    if agent_declared or not error:
        return False
    for line in _error_lines(str(error)):
        if _NAME_RE.search(line) or any(sig in line.lower() for sig in _SIGNATURES):
            return True
    return False


def network_failing_too_long(job: dict[str, Any], hours: float) -> bool:
    """True once the job's network-class failures have run longer than *hours* (its
    ``network_failing_since`` stamp is older): from then on a failure alerts like any other, so a
    permanent breakage that merely looks like a network problem cannot stay silent for good."""
    since = job.get("network_failing_since")
    if not since:
        return False
    try:
        from cron.jobs import _elapsed_seconds, _ensure_aware
        from hermes_time import now as _hermes_now

        started = _ensure_aware(datetime.fromisoformat(str(since)))
        return _elapsed_seconds(_hermes_now(), started) >= hours * 3600
    except (TypeError, ValueError):
        return False


def record_transient_network_incident(job: dict[str, Any], error: Optional[str],
                                      output_file: Optional[Any] = None) -> None:
    """Best-effort ledger row for the suppressed failure; never raises into the run's bookkeeping."""
    import logging

    logger = logging.getLogger("cron.scheduler")
    logger.warning("Job '%s': transient network failure, no alert: %s", job.get("id"),
                   str(error or "")[:200])
    try:
        from cron.incidents import upsert_incident

        upsert_incident(job["id"], str(error or ""), job_name=job.get("name"),
                        failure_type=TRANSIENT_NETWORK_FAILURE_TYPE, output_file=output_file)
    except Exception as exc:
        logger.warning("Incident store unavailable for job %s (transient network): %s", job.get("id"), exc,
                       exc_info=True)
