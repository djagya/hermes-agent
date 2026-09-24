"""Durable-source handoff contract for large state-bearing payloads.

Spec (truncation-integrity, section E): large payloads cross role boundaries either as
an immutable artifact with a SHA-256/length contract plus read-back verification, or as
a reference to a durable destination — never as an in-context preview. A preview is
never the contract.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union

PathLike = Union[str, Path]

# Digest contract extraction: accepts "sha256 <hex>", "sha-256: <hex>", "SHA256`<hex>`"
# and bare 64-hex digests in free text.
_DIGEST_LINE_RE = re.compile(r"(?i)(?:sha[-_ 0-9a-f]{0,10}[:`\s]*|[`'\"]?)([0-9a-f]{64})(?:[`'\"]?)")
_ARTIFACT_REF_RE = re.compile(r"`([^`]+)`")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_bytes(data: bytes, *, sha256: Optional[str] = None, length: Optional[int] = None) -> Dict[str, Any]:
    """Digest/length check for a payload read back from a durable destination."""
    actual_sha = sha256_bytes(data)
    checks: Dict[str, Any] = {"sha256": actual_sha, "length": len(data), "intact": True}
    if sha256:
        expected = (sha256 or "").strip().lower()
        checks["intact"] = bool(expected) and actual_sha == expected
        checks["expected_sha256"] = expected
    if length is not None:
        checks["length_match"] = len(data) == int(length)
        checks["intact"] = checks["intact"] and checks["length_match"]
        checks["expected_length"] = int(length)
    return checks


def verify_spec_digest(path: PathLike, expected_sha256: str) -> Dict[str, Any]:
    """Read a canonical artifact and verify its SHA-256 contract. Raises ``ValueError``
    on any mismatch so callers fail closed before treating the artifact as authority."""
    data = Path(path).read_bytes()
    checks = verify_bytes(data, sha256=expected_sha256)
    if not checks["intact"]:
        raise ValueError(
            f"Digest mismatch for {path}: expected {checks.get('expected_sha256')}, "
            f"got {checks['sha256']} ({checks['length']} bytes). Refusing to treat "
            "this artifact as authoritative."
        )
    return checks


def read_contracted_text(path: PathLike, expected_sha256: str) -> str:
    """Digest-verified read of a canonical text artifact (e.g. a task spec)."""
    verify_spec_digest(path, expected_sha256)
    return Path(path).read_text(encoding="utf-8")


def write_artifact(path: PathLike, data: Union[str, bytes]) -> Dict[str, Any]:
    """Write an immutable handoff artifact and return its digest contract."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    p.write_bytes(payload)
    return {"path": str(p), "sha256": sha256_bytes(payload), "length": len(payload)}


def extract_digest_contract(text: str) -> Optional[Dict[str, Any]]:
    """Pull a ``sha256`` digest (and optional byte count) out of free text, so a
    receiver can verify a referenced artifact. First 64-hex digest wins; a bare
    64-hex run matches too (contract text usually labels it, but not always)."""
    match = _DIGEST_LINE_RE.search(text or "")
    if not match:
        return None
    contract: Dict[str, Any] = {"sha256": match.group(1).lower()}
    length_match = re.search(r"(?i)(?:^|\s|`)(\d{3,})\s*bytes", text)
    if length_match:
        contract["length"] = int(length_match.group(1))
    return contract


def contract_text(payload: str, artifact_path: PathLike) -> str:
    """Digest-anchored card body for a large payload (Kanban-operations guidance,
    spec section E): write the payload to an immutable artifact, then reference it by
    exact artifact + SHA-256 + length instead of inlining the payload. Receivers
    MUST read the artifact and verify the digest; the body itself is not the contract.
    """
    digest = sha256_text(payload)
    return (
        "Large payload delivered by artifact, not inline: read the file and verify its "
        f"SHA-256 digest before acting on it — artifact: `{artifact_path}`; "
        f"sha256: {digest}; length: {len(payload.encode('utf-8', errors='replace'))} bytes. "
        "The canonical contract is the artifact bytes; a preview or quote is never "
        "authoritative."
    )


def referenced_artifact_path(text: str) -> Optional[str]:
    """First backticked artifact path from a ``contract_text`` body, for read-back."""
    match = _ARTIFACT_REF_RE.search(text or "")
    return match.group(1) if match else None
