"""Standalone Telegram formatting selection and readable parse-error fallbacks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import utf16_len
from tools.send_message_senders import _send_telegram, _telegram_format


@pytest.mark.parametrize(
    ("message", "expects_html"),
    [
        ("**Bank check passed**\nReply `approve <KEYS>`", False),
        ("Replace <KEYS> before submitting", False),
        ("Use `<b>literal</b>` here", False),
        ("```html\n<a href=\"https://example.com\">literal</a>\n```", False),
        ("**ordinary Markdown** and [link](https://example.com)", False),
        ("Unsupported <blink>tag</blink>", False),
        ("<b>Bold</b> <a href=\"https://example.com\">Docs</a> <code>x</code>", True),
        ("**Markdown stays literal** beside <b>deliberate HTML</b>", True),
    ],
)
def test_telegram_format_selects_html_only_for_supported_tags_outside_markdown_code(
    message: str, expects_html: bool
) -> None:
    from telegram.constants import ParseMode

    formatted, parse_mode, has_html = _telegram_format(message)

    assert has_html is expects_html
    assert parse_mode == (ParseMode.HTML if expects_html else ParseMode.MARKDOWN_V2)
    if expects_html:
        assert formatted == message


def test_standalone_telegram_payloads_use_readable_fallbacks_and_utf16_chunks(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from telegram.constants import ParseMode

    import tools.send_message_senders as senders

    html = (
        '<b>Report &amp; details</b> — '
        '<a href="https://example.com/report">Docs</a> approve <KEYS>'
    )

    text_bot = MagicMock()
    text_bot.send_message = AsyncMock(
        side_effect=[Exception("Can't parse entities: unsupported start tag keys"), SimpleNamespace(message_id=1)]
    )
    monkeypatch.setattr(senders, "_telegram_bot", lambda _token: text_bot)

    result = asyncio.run(_send_telegram("token", "123", html))

    assert result["success"] is True
    assert [call.kwargs["parse_mode"] for call in text_bot.send_message.await_args_list] == [
        ParseMode.HTML,
        None,
    ]
    assert text_bot.send_message.await_args_list[1].kwargs["text"] == (
        "Report & details — Docs (https://example.com/report) approve <KEYS>"
    )

    image = tmp_path / "image.png"
    image.write_bytes(b"not-a-real-image")
    caption_bot = MagicMock()
    caption_bot.send_message = AsyncMock()
    caption_bot.send_photo = AsyncMock(
        side_effect=[Exception("Can't parse caption entities"), SimpleNamespace(message_id=2)]
    )
    monkeypatch.setattr(senders, "_telegram_bot", lambda _token: caption_bot)

    result = asyncio.run(_send_telegram("token", "123", html, media_files=[(str(image), False)]))

    assert result["success"] is True
    assert [call.kwargs["parse_mode"] for call in caption_bot.send_photo.await_args_list] == [
        ParseMode.HTML,
        None,
    ]
    assert caption_bot.send_photo.await_args_list[1].kwargs["caption"] == (
        "Report & details — Docs (https://example.com/report) approve <KEYS>"
    )

    chunks_bot = MagicMock()
    chunks_bot.send_message = AsyncMock(
        side_effect=lambda **_kwargs: SimpleNamespace(message_id=3)
    )
    monkeypatch.setattr(senders, "_telegram_bot", lambda _token: chunks_bot)
    financial_markdown = "**Bank check passed**\nReply `approve <KEYS>` 🚀\n" * 120

    result = asyncio.run(_send_telegram("token", "123", financial_markdown))

    assert result["success"] is True
    assert chunks_bot.send_message.await_count >= 2
    assert all(
        call.kwargs["parse_mode"] == ParseMode.MARKDOWN_V2
        for call in chunks_bot.send_message.await_args_list
    )
    assert all(utf16_len(call.kwargs["text"]) <= 4096 for call in chunks_bot.send_message.await_args_list)
