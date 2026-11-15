"""Network-class run failures that never alert (owner decision 2026-11-15).

A host that loses DNS or its uplink for a minute fails every job that runs in it, and one ping
per job buries the real failures. A run whose error text carries one of the allow-listed
signatures below is recorded (incident ledger, failure type ``transient_network``) but does not
count toward the job's failure streak, so no alert threshold is reached, and delivers no failure
notice. Any other failure alerts exactly as before.

The match is on the error text, an allow-list of exact network phrases and exception names; a
signature is added here only after it showed up in a real run. An agent's own ``[CRON_FAILURE]``
verdict is prose about the job's subject and is never classified.
"""

from __future__ import annotations

import re
from typing import Any, Optional

TRANSIENT_NETWORK_FAILURE_TYPE = "transient_network"

_MAX_SCANNED_CHARS = 8000
_SIGNATURES = (
    # DNS
    "could not resolve host", "temporary failure in name resolution", "name or service not known",
    "nodename nor servname provided", "getaddrinfo failed", "failed to resolve",
    # transport
    "connection reset by peer", "connection refused", "connection aborted", "network is unreachable",
    "no route to host", "remote end closed connection", "server disconnected without sending a response",
    "tls handshake timeout", "ssl handshake timeout", "handshake operation timed out",
    "connection timed out", "connect timeout",
)
# Exception class names as they lead a ``Type: message`` error string (word-bounded).
_EXCEPTION_NAMES = (
    "ConnectError", "ConnectTimeout", "APIConnectionError", "ClientConnectorError",
    "ClientConnectorDNSError", "RemoteDisconnected", "NewConnectionError",
)
_NAME_RE = re.compile(r"\b(?:" + "|".join(_EXCEPTION_NAMES) + r")\b")


def is_transient_network_failure(error: Optional[str], *, agent_declared: bool = False) -> bool:
    """True when *error* names a DNS / connection-class failure from the allow-list."""
    if agent_declared or not error:
        return False
    text = str(error)[-_MAX_SCANNED_CHARS:]
    lowered = text.lower()
    return any(sig in lowered for sig in _SIGNATURES) or bool(_NAME_RE.search(text))


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
        logger.debug("Incident store unavailable for job %s (transient network): %s", job.get("id"), exc)
