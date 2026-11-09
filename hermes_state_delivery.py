"""dlz: outbound delivery receipts (``messages.platform_delivery``), a fork column upstream does not have.
Split out of ``hermes_state_messages`` to keep it under the code-health size target and bound onto SessionDB
through ``SessionMessagesMixin``; built on its ``_write_rowcount`` primitive."""

from __future__ import annotations

import json
from typing import Any, Optional


class SessionDeliveryReceiptMixin:
    """Write-once platform delivery receipt on one durable assistant row."""

    def set_message_delivery(
        self, session_id: str, row_id: int, delivery: Optional[dict[str, Any]]
    ) -> int:
        """Persist the outbound delivery receipt onto ONE known durable assistant row.

        Row-addressed like :meth:`set_message_api_content`: the caller passes the
        ``_row_id`` stamped on the live message dict at persist time, so the receipt
        cannot drift onto a neighbouring row. Idempotent and write-once: a row that
        already carries a receipt is left untouched (a redelivery sweep re-reporting
        success must never overwrite the first confirmed receipt with a different
        one). ``active = 1`` and ``role = 'assistant'`` stay as guards so an archived
        or user-role row is never silently branded. Returns rows changed (0 = refused
        or already recorded).

        ``delivery`` is the canonical receipt dict — platform, chat_id, thread_id,
        message_ids (send order, primary LAST), chat_kind/chat_handle when known,
        delivered_at (epoch seconds) — encoded as ``platform_delivery`` JSON. Only a
        CONFIRMED platform ACK may call this; pending/attempting/failed sends do not.
        """
        if not session_id or isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0:
            return 0
        if not isinstance(delivery, dict) or not delivery:
            return 0
        encoded = json.dumps(delivery, ensure_ascii=False, sort_keys=True)
        return self._write_rowcount(
            "UPDATE messages SET platform_delivery = ? WHERE id = ? AND session_id = ? "
            "AND role = 'assistant' AND active = 1 AND platform_delivery IS NULL",
            (encoded, row_id, session_id))
