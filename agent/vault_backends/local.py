"""Local Fernet vault as a login backend (the always-on default)."""

from __future__ import annotations

from typing import Dict, List, Optional

from agent.vault_backends.base import LoginBackend
from agent.vault_store import VaultItemMeta

# Keys of a local login secret that are never recovery codes (review E, 11-18).
_NOT_CODE_KEYS = frozenset({"password", "otp_secret", "identifier", "identifier_type", "email", "phone", "username"})


def _store():
    # Late import: tests and callers patch ``agent.vault_store.get_vault_store``; binding it here
    # at call time keeps that facade name the single seam.
    from agent.vault_store import get_vault_store
    return get_vault_store()


class LocalLoginBackend(LoginBackend):
    name = "local"
    display_name = "Hermes vault"
    prefix = "vault_"
    payment_revisions = True  # keyed digest of the stored record

    def list_items(self) -> list[VaultItemMeta]:
        return _store().list_items()

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        return _store().get_meta(handle)

    def resolve_password(self, handle: str) -> str:
        return str(_store().resolve_secret(handle).get("password") or "")

    def resolve_otp(self, handle: str) -> Optional[str]:
        from agent.vault_store import totp_now
        seed = str(_store().resolve_secret(handle).get("otp_secret") or "")
        return totp_now(seed) if seed else None

    def resolve_recovery_codes(self, handle: str, field: str) -> list[str]:
        """D-0365: codes stored on the item under a key named like ``field`` ("recovery_codes"). The password,
        the TOTP seed and the identifier keys are never read as codes."""
        from agent.vault_recovery_codes import pick_code_values

        secret = _store().resolve_secret(handle)
        return pick_code_values(field, ((str(k), "", str(v), str(k) in _NOT_CODE_KEYS) for k, v in secret.items()))

    def resolve_secret(self, handle: str) -> dict[str, str]:
        return {k: str(v) for k, v in _store().resolve_secret(handle).items()}

    def resolve_payment_secret(self, handle: str) -> tuple[dict[str, str], Optional[str]]:
        secret, revision = _store().resolve_secret_with_revision(handle)
        return {k: str(v) for k, v in secret.items()}, revision
