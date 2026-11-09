"""``VaultStore._read_all`` must surface every decryptable-but-malformed vault
file as ``VaultError`` so callers that catch ``VaultError`` (the ``hermes vault``
commands, ``vault.*`` RPC handlers) get the designed error path instead of a raw
traceback. Corruption arrives via partial-write survivors, manual edits of the
envelope, or format drift."""

import re

import pytest

from agent.vault_store import VaultError, VaultStore


def _corrupt(store: VaultStore, payload: bytes) -> None:
    store._vault_path.write_bytes(store._fernet().encrypt(payload))


def _store(tmp_path) -> VaultStore:
    store = VaultStore(tmp_path / "vault")
    store.add_item(
        "login",
        "Example login",
        {"identifier_type": "email", "identifier": "u@example.com", "password": "pw"},
        origin="https://example.com",
    )
    return store


@pytest.mark.parametrize(
    "op",
    ["list_items", "get_meta", "remove_item", "resolve_secret", "add_item"],
)
def test_corrupt_vault_raises_vault_error_on_every_reader(tmp_path, op):
    """Every ``_read_all`` consumer must see the same VaultError contract so
    callers that catch VaultError all get the designed error path."""
    store = _store(tmp_path)
    _corrupt(store, b"{not json")
    call = {
        "list_items": lambda: store.list_items(),
        "get_meta": lambda: store.get_meta("vault_x"),
        "remove_item": lambda: store.remove_item("vault_x"),
        "resolve_secret": lambda: store.resolve_secret("vault_x"),
        "add_item": lambda: store.add_item(
            "login",
            "x",
            {"identifier_type": "email", "identifier": "u", "password": "p"},
            origin="https://x.example",
        ),
    }[op]
    with pytest.raises(VaultError):
        call()


def test_a_payment_record_revision_follows_the_stored_record(tmp_path):
    """Design v2 A8 (local store): meta and the secret read report the same revision; a record rewritten
    under the same id (restore, manual edit) gets a new one. The revision is a keyed digest, not the card."""
    store = VaultStore(tmp_path / "vault")
    card = {"card_number": "4242424242424242", "exp_month": "07", "exp_year": "2035", "cvc": "837"}
    meta = store.add_item("payment", "Synthetic card", card)
    secret, revision = store.resolve_secret_with_revision(meta.id)
    assert revision == meta.revision == store.get_meta(meta.id).revision and secret["cvc"] == "837"
    assert re.fullmatch(r"local:[0-9a-f]{32}", revision)  # keyed digest only, never card digits
    records = store._read_all()
    records[0]["secret"]["cvc"] = "838"
    store._write_all(records)
    assert store.get_meta(meta.id).revision != meta.revision
