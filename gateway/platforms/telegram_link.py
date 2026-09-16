"""Telegram deep-link rendering from persisted delivery coordinates.

Presentation seam, platform-neutral core: the rest of Hermes never builds a
messaging URL itself; it hands a stored receipt (``messages.platform_delivery``
JSON, written only after a confirmed platform ACK) to
:func:`telegram_message_link` and renders ``None`` as "no link available".

Telegram deep-link grammar (what each shape needs):
- private supergroup with forum topics enabled: ``https://t.me/c/<internal>/<thread>/<id>``
- private supergroup/group without topics:      ``https://t.me/c/<internal>/<id>``
- public-username chat (supergroup or a bot DM): ``https://t.me/<handle>/<id>``

The private ``/c/`` form uses the SIGNED 64-bit chat id minus the ``-100`` flag
prefix; a chat id that does not carry it is a non-supergroup shape the ``/c/``
grammar cannot express, so without a public handle there is no link. Anything
underdetermined — missing coordinates, empty ids, a non-Telegram receipt,
a reserved username like ``c`` or a bare handle with no name — renders ``None``.
A link is never invented from partial data.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_PRIVATE_CHAT_FLAG = "-100"
# ``t.me/c/…`` is the private-access grammar; a chat literally named "c" would
# collide with it (Telegram reserves the username, we stay consistent with that).
_UNUSABLE_HANDLES = {"c"}


def _clean_str(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def _internal_chat_id(chat_id: str) -> Optional[str]:
    """/c/ grammar's numeric id: the signed supergroup id minus its ``-100`` flag."""
    if not chat_id.startswith(_PRIVATE_CHAT_FLAG):
        return None
    digits = chat_id[len(_PRIVATE_CHAT_FLAG):]
    return digits if digits.isdigit() else None


def telegram_message_link(receipt: Optional[Dict[str, Any]]) -> Optional[str]:
    """The t.me link for a stored delivery receipt, or ``None`` when the receipt
    does not carry enough truth to build one."""
    if not isinstance(receipt, dict):
        return None
    if receipt.get("platform") != "telegram":
        return None
    message_ids = receipt.get("message_ids") or []
    if not message_ids:
        return None
    primary = _clean_str(message_ids[-1])
    chat_id = _clean_str(receipt.get("chat_id"))
    if primary is None or chat_id is None:
        return None

    handle = _clean_str(receipt.get("chat_handle"))
    if handle is not None:
        handle = handle.lstrip("@")
        if handle.lower() in _UNUSABLE_HANDLES or not handle:
            return None
        return f"https://t.me/{handle}/{primary}"

    internal = _internal_chat_id(chat_id)
    if internal is None:
        return None
    thread_id = _clean_str(receipt.get("thread_id"))
    if thread_id and thread_id.isdigit():
        return f"https://t.me/c/{internal}/{thread_id}/{primary}"
    return f"https://t.me/c/{internal}/{primary}"
