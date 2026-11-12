"""dlz: a photo set that must land as native Telegram albums or not at all.

Cron's live lane marks a media-only image set ``_require_native_media_group`` (``cron/scheduler_delivery``).
For it ``TelegramAdapter.send_multiple_images`` uses this sender instead of upstream's
``telegram_album.send_photo_album``: every chunk is one ``sendMediaGroup`` whose receipts must number
the photos sent and share one ``media_group_id``. A missing file, any send error (including a lost
reply) or a receipt mismatch ends the batch with ``success=False``; nothing is ever re-sent photo by
photo, so the caller can report the album as failed instead of a loose pile of photos.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, Optional
from urllib.parse import unquote

from gateway.platforms.base import SendResult

logger = logging.getLogger(__name__)

ALBUM_LIMIT = 10  # Telegram's media-group cap


async def send_native_album(adapter: Any, chat_id: str, photos: list[tuple], metadata: Optional[dict],
                            human_delay: float, input_media: Any) -> SendResult:
    """Send ``(url, alt)`` photos as albums of up to ten, failing closed on the first problem."""
    from plugins.platforms.telegram.adapter import _redact_telegram_error_text as redact

    chunks = [photos[i:i + ALBUM_LIMIT] for i in range(0, len(photos), ALBUM_LIMIT)]
    delivered = False
    for chunk_idx, chunk in enumerate(chunks):
        if human_delay > 0 and chunk_idx > 0:
            await asyncio.sleep(human_delay)
        with contextlib.ExitStack() as stack:
            media: list[Any] = []
            opened: list[Any] = []
            for image_url, alt_text in chunk:
                source: Any = image_url
                if image_url.startswith("file://"):
                    local_path = unquote(image_url[7:])
                    if not os.path.exists(local_path):
                        return SendResult(success=False, error=f"missing image: {local_path}")
                    if compressed := adapter._compress_image_to_jpeg(local_path):
                        stack.callback(lambda p=compressed: os.path.exists(p) and os.remove(p))
                        local_path = compressed
                    try:
                        source = stack.enter_context(open(local_path, "rb"))  # noqa: ASYNC230 -- streamed to the upload
                    except OSError as exc:
                        return SendResult(success=False, error=redact(exc))
                    opened.append(source)
                media.append(input_media(media=source, caption=adapter._caption_1024(alt_text)))

            def reset() -> None:
                for fh in opened:
                    with contextlib.suppress(Exception):
                        fh.seek(0)

            logger.info("[%s] Sending native media group of %d photo(s) (chunk %d/%d)",
                        adapter.name, len(media), chunk_idx + 1, len(chunks))
            _, send_kwargs = adapter._media_send_kwargs(chat_id, None, metadata)
            try:
                sent = await adapter._send_with_dm_topic_reply_anchor_retry(
                    adapter._bot.send_media_group, {**send_kwargs, "media": media}, metadata, "media group",
                    reset_media=reset)
            except Exception as exc:
                return SendResult(success=False, error=redact(exc))
            receipts = list(sent or [])
            if len(receipts) != len(media):
                return SendResult(success=False, error=(
                    f"Telegram album receipt count mismatch: expected {len(media)}, got {len(receipts)}"))
            group_ids = {getattr(receipt, "media_group_id", None) for receipt in receipts}
            if None in group_ids or len(group_ids) != 1:
                return SendResult(success=False, error="Telegram album receipts did not share one media_group_id")
            delivered = True
    return SendResult(success=delivered, error=None if delivered else "all images failed to send")
