"""dlz: an album chunk is confirmed only by Telegram's exact receipts (fork ``_send_telegram_photo_album``).

``sendMediaGroup`` answers one Message per photo, all sharing one non-null ``media_group_id``. A short,
empty, split or id-less answer proves nothing about what is in the chat: the chunk is reported
ambiguous (it may be posted) and is never resent photo by photo. Only confirmed receipts give an id.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _adapter(answer):
    import telegram
    telegram.InputMediaPhoto = MagicMock(side_effect=lambda media, caption=None: {"media": media})
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._bot = MagicMock()
    adapter._bot.send_media_group = AsyncMock(return_value=answer)
    adapter.send_image = AsyncMock()
    adapter.send_image_file = AsyncMock()
    return adapter


_PHOTOS = [(f"https://x.com/{i}.png", "") for i in range(2)]


@pytest.mark.asyncio
async def test_confirmed_album_reports_the_last_receipt_id():
    adapter = _adapter([SimpleNamespace(message_id=5, media_group_id="g"),
                        SimpleNamespace(message_id=6, media_group_id="g")])
    result = await adapter.send_multiple_images("1", _PHOTOS)
    assert result.success is True
    assert result.message_id == "6"
    assert not (result.raw_response or {}).get("ambiguous")


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    None, [], True,
    [SimpleNamespace(message_id=5, media_group_id="g")],                                     # short
    [SimpleNamespace(message_id=5, media_group_id="g"), SimpleNamespace(message_id=6, media_group_id="h")],
    [SimpleNamespace(message_id=5, media_group_id=None), SimpleNamespace(message_id=6, media_group_id=None)],
], ids=["none", "empty", "not-a-list", "short", "split-group", "no-group-id"])
async def test_malformed_album_ack_is_uncertain_and_never_resent(answer):
    adapter = _adapter(answer)
    result = await adapter.send_multiple_images("1", _PHOTOS)
    # Uncertain, not failed: it may be in the chat, so no caller may resend it.
    assert result.success is True
    assert result.raw_response["ambiguous"] is True
    assert result.message_id is None
    assert adapter._bot.send_media_group.await_count == 1
    adapter.send_image.assert_not_awaited()
    adapter.send_image_file.assert_not_awaited()
