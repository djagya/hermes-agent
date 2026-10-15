"""Bounded public Markdown photo transport; all API calls mocked."""
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from tests.gateway.test_telegram_rich_messages import _make_adapter
from telegram.error import BadRequest, TimedOut

PHOTO = "https://example.org/item.jpg?size=600"
CONTENT = f"## First\n\n![first]({PHOTO})\n\nFirst description\n\n## Second\n\n![second](https://example.org/second.webp)\n\nSecond description"
KNIFE_URLS = [
    "https://assets.katogroup.eu/i/katogroup/MC-0192C_01_mcusta?%24product-image%24=&fmt=jpg&h=434&w=652",
    "https://assets.katogroup.eu/i/katogroup/MC163D_01_mcusta?%24product-image%24=&fmt=jpg&h=434&w=652",
    "https://www.coutellerie-tourangelle.com/images/imagecache/400x400/jpg/mcu193c-mcusta-folder-green-and-black-mcu193c.webp",
]
KNIFE_CONTENT = '\n\n'.join(
    f'## Knife {index}\n\n![knife {index}]({url})\n\nDescription {index}; SKU `MC-193C`'
    for index, url in enumerate(KNIFE_URLS)
)


def event():
    return MessageEvent(text="products", message_type=MessageType.TEXT,
                        source=SessionSource(platform=Platform.TELEGRAM, chat_id="111", chat_type="dm"),
                        message_id="2")


def test_native_markdown_preserves_order_and_schema():
    from pathlib import Path
    import plugins.platforms.telegram.adapter as module
    assert Path(module.__file__).resolve().parents[3] == Path(__file__).resolve().parents[2]
    adapter = _make_adapter()
    payload = adapter._rich_message_payload(CONTENT)
    assert "media" not in payload
    markdown = payload["markdown"]
    positions = [markdown.index(part) for part in ["## First", PHOTO, "First description",
                                                   "## Second", "https://example.org/second.webp", "Second description"]]
    assert positions == sorted(positions)
    assert adapter.preserves_inline_images(CONTENT)


@pytest.mark.parametrize("content", [
    f"![only]({PHOTO})", f"![only]({PHOTO})\n![two]({PHOTO})",
    CONTENT + "\nMEDIA:/tmp/output.html", CONTENT + "\nMEDIA:/tmp/audio.mp3",
    CONTENT + "\nMEDIA:/tmp/video.mp4", CONTENT + "\n[[as_document]]",
    CONTENT + "\n![video](https://example.org/movie.mp4)",
    "text ![gif](https://example.org/moving.gif)",
    f"text `![code]({PHOTO})`", "text ![local](file:///tmp/a.png)",
])
def test_exclusions(content):
    adapter = _make_adapter()
    assert not adapter.preserves_inline_images(content)
    assert not adapter._inline_photos(content)
    assert "media" not in adapter._rich_message_payload(content)


@pytest.mark.asyncio
async def test_send_and_final_edit_preserve_native_photos_and_preview_controls(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setattr(adapter, "_record_rich_sent", AsyncMock())
    assert (await adapter.send("111", CONTENT)).success
    assert PHOTO in adapter._bot.do_api_request.call_args.kwargs["api_kwargs"]["rich_message"]["markdown"]
    adapter._bot.do_api_request.reset_mock()
    assert (await adapter.edit_message("111", "123", CONTENT, finalize=True)).success
    assert adapter._bot.do_api_request.call_args.args[0] == "editMessageText"
    assert PHOTO in adapter._bot.do_api_request.call_args.kwargs["api_kwargs"]["rich_message"]["markdown"]
    adapter._bot.do_api_request.reset_mock()
    await adapter.edit_message("111", "123", CONTENT, finalize=False)
    adapter._bot.do_api_request.assert_not_called()
    await adapter._try_send_rich_draft("111", 1, CONTENT, None)
    assert "media" not in adapter._bot.do_api_request.call_args.kwargs["api_kwargs"]["rich_message"]
    assert not adapter._should_attempt_rich(CONTENT, {"expect_edits": True})


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected_images", [(None, 0), (BadRequest("invalid rich media"), 2),
                                                      (TimedOut("timed out"), 0),
                                                      (TypeError("no endpoint"), 2)])
async def test_real_base_final_route_no_duplicate_attachments(monkeypatch, failure, expected_images):
    adapter = _make_adapter()
    adapter.config.typing_indicator = False
    adapter._get_human_delay = lambda: 0
    monkeypatch.setattr(adapter, "_record_rich_sent", AsyncMock())
    monkeypatch.setattr(adapter, "_wants_auto_tts", lambda *args: False)
    adapter.send_multiple_images = AsyncMock(return_value=SendResult(success=True))
    adapter._stop_typing_refresh = AsyncMock()
    if failure:
        adapter._bot.do_api_request.side_effect = failure
    async def handler(_event):
        return CONTENT
    adapter.set_message_handler(handler)
    # Real base final ledger seam, with storage isolated out and send transport real.
    async def ledgered(evt, key, text, metadata, **kwargs):
        return await adapter.send(evt.source.chat_id, text, metadata=metadata), adapter
    monkeypatch.setattr(adapter, "send_final_ledgered", ledgered)
    await adapter._process_message_background(event(), "inline-photo-test")
    if expected_images:
        assert len(adapter.send_multiple_images.call_args.kwargs["images"]) == expected_images
        sent_text = adapter._bot.send_message.call_args.kwargs["text"]
        assert "example.org" not in sent_text
        assert "First description" in sent_text
    else:
        adapter.send_multiple_images.assert_not_called()
    if failure is None:
        adapter._bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_extraction_keeps_fallback_candidates_and_opt_out():
    adapter = _make_adapter()
    extracted = await adapter._extract_response_content(CONTENT, event(), "test", is_ephemeral_response=False)
    assert extracted.text_content == CONTENT
    assert len(extracted.images) == 2
    adapter._rich_messages_enabled = False
    extracted = await adapter._extract_response_content(CONTENT, event(), "test", is_ephemeral_response=False)
    assert "![" not in extracted.text_content
    assert len(extracted.images) == 2


@pytest.mark.asyncio
async def test_image_only_remains_native_album(monkeypatch):
    adapter = _make_adapter()
    adapter.config.typing_indicator = False
    adapter._get_human_delay = lambda: 0
    adapter._stop_typing_refresh = AsyncMock()
    adapter.send_multiple_images = AsyncMock(return_value=SendResult(success=True))
    async def handler(_event):
        return f"![one]({PHOTO})\n![two]({PHOTO})"
    adapter.set_message_handler(handler)
    await adapter._process_message_background(event(), "album-test")
    adapter._bot.do_api_request.assert_not_called()
    adapter._bot.send_message.assert_not_called()
    assert len(adapter.send_multiple_images.call_args.kwargs["images"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("extension,sender", [("html", "send_document"), ("mp4", "send_video"),
                                              ("mp3", "send_voice")])
async def test_mixed_nonimage_media_uses_legacy_partition(tmp_path, monkeypatch, extension, sender):
    from gateway.platforms import base
    artifact = tmp_path / f"artifact.{extension}"
    artifact.write_bytes(b"synthetic-media")
    monkeypatch.setattr(base, "MEDIA_DELIVERY_SAFE_ROOTS", (tmp_path,))
    monkeypatch.setattr(base, "_media_delivery_allowed_roots", lambda: [tmp_path])
    adapter = _make_adapter()
    adapter.config.typing_indicator = False
    adapter._get_human_delay = lambda: 0
    adapter._stop_typing_refresh = AsyncMock()
    adapter.send_multiple_images = AsyncMock(return_value=SendResult(success=True))
    monkeypatch.setattr(adapter, sender, AsyncMock(return_value=SendResult(success=True)))
    monkeypatch.setattr(adapter, "_wants_auto_tts", lambda *args: False)
    async def handler(_event):
        return CONTENT + f"\nMEDIA:{artifact}\n\n| Name | Price |\n|---|---|\n| item | 1 |"
    async def ledgered(evt, key, text, metadata, **kwargs):
        return await adapter.send(evt.source.chat_id, text, metadata=metadata), adapter
    monkeypatch.setattr(adapter, "send_final_ledgered", ledgered)
    adapter.set_message_handler(handler)
    await adapter._process_message_background(event(), "mixed-media-test")
    adapter._bot.do_api_request.assert_not_called()
    assert len(adapter.send_multiple_images.call_args.kwargs["images"]) == 2
    getattr(adapter, sender).assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, BadRequest('invalid rich media'), TypeError('no endpoint')])
async def test_exact_prior_knife_selection_final_route(monkeypatch, failure):
    adapter = _make_adapter()
    adapter.config.typing_indicator = False
    adapter._get_human_delay = lambda: 0
    adapter._stop_typing_refresh = AsyncMock()
    adapter.send_multiple_images = AsyncMock(return_value=SendResult(success=True))
    monkeypatch.setattr(adapter, '_record_rich_sent', AsyncMock())
    monkeypatch.setattr(adapter, '_wants_auto_tts', lambda *args: False)
    if failure:
        adapter._bot.do_api_request.side_effect = failure
    async def handler(_event):
        return KNIFE_CONTENT
    async def ledgered(evt, key, text, metadata, **kwargs):
        return await adapter.send(evt.source.chat_id, text, metadata=metadata), adapter
    monkeypatch.setattr(adapter, 'send_final_ledgered', ledgered)
    adapter.set_message_handler(handler)
    await adapter._process_message_background(event(), 'prior-knife-selection')
    adapter._bot.do_api_request.assert_awaited_once()
    payload = adapter._bot.do_api_request.call_args.kwargs['api_kwargs']['rich_message']
    assert 'media' not in payload
    markdown = payload['markdown']
    positions = [markdown.index(part) for i in range(3)
                 for part in (f'## Knife {i}', KNIFE_URLS[i], f'Description {i}')]
    assert positions == sorted(positions)
    assert all(url in markdown for url in KNIFE_URLS)
    if failure:
        adapter.send_multiple_images.assert_awaited_once()
        assert adapter.send_multiple_images.call_args.kwargs['images'] == [
            (url, f'knife {i}') for i, url in enumerate(KNIFE_URLS)]
        adapter._bot.send_message.assert_awaited_once()
        text = adapter._bot.send_message.call_args.kwargs['text']
        assert all(f'Description {i}' in text for i in range(3))
        assert 'katogroup' not in text and 'coutellerie' not in text
        assert '![' not in text
    else:
        adapter.send_multiple_images.assert_not_called()
        adapter._bot.send_message.assert_not_called()


@pytest.mark.parametrize('key', ['fmt', 'format'])
@pytest.mark.parametrize('fmt', ['jpg', 'jpeg', 'png', 'webp', 'JPG'])
def test_extensionless_formats_share_fallback_criterion(key, fmt):
    adapter = _make_adapter()
    url = f'https://unlisted-cdn.example/image?id=42&{key}={fmt}'
    content = f'SKU `MC-193C`\n\n![item]({url})\n\nDescription'
    assert adapter.preserves_inline_images(content)
    assert url in adapter._rich_message_payload(content)['markdown']
    images, cleaned = adapter.extract_images(content)
    assert images == [(url, 'item')]
    assert url not in cleaned and '`MC-193C`' in cleaned


@pytest.mark.parametrize('url', [
    'https://cdn.example/image?fmt=gif', 'https://cdn.example/image?format=svg',
    'https://cdn.example/image?other=jpg', 'https://cdn.example/image?fmt=jpg-extra',
    'https://user:password@cdn.example/image?fmt=jpg',
])
def test_no_inline_photo_ownership_for_nonphoto_format_or_credentials(url):
    adapter = _make_adapter()
    assert not adapter.preserves_inline_images(f'Description ![item]({url})')


@pytest.mark.parametrize('wrapper', ['`{}`', '``{}``', '```markdown\n{}\n```',
                                     '~~~markdown\n{}\n~~~', '```markdown\n{}'])
@pytest.mark.asyncio
async def test_code_image_literals_never_compile_or_deliver(monkeypatch, wrapper):
    adapter = _make_adapter()
    literal = wrapper.format(f'![literal]({KNIFE_URLS[0]})')
    code_only = 'Example:\n\n' + literal
    assert not adapter.preserves_inline_images(code_only)
    assert 'media' not in adapter._rich_message_payload(code_only)
    assert adapter.extract_images(code_only) == ([], code_only)
    # Code next to real photos neither disables them nor becomes a fallback attachment.
    content = KNIFE_CONTENT + '\n\n' + literal
    payload = adapter._rich_message_payload(content)
    assert 'media' not in payload
    assert adapter.preserves_inline_images(content)
    # Existing rich newline normalization may add hard breaks to tilde/open fences;
    # the literal photo itself must remain unbound.
    assert f'![literal]({KNIFE_URLS[0]})' in payload['markdown']
    images, cleaned = adapter.extract_images(content)
    assert images == [(url, f'knife {i}') for i, url in enumerate(KNIFE_URLS)]
    assert literal in cleaned
    adapter.config.typing_indicator = False
    adapter._get_human_delay = lambda: 0
    adapter._stop_typing_refresh = AsyncMock()
    adapter.send_multiple_images = AsyncMock(return_value=SendResult(success=True))
    monkeypatch.setattr(adapter, '_record_rich_sent', AsyncMock())
    monkeypatch.setattr(adapter, '_wants_auto_tts', lambda *args: False)
    adapter._bot.do_api_request.side_effect = BadRequest('invalid rich media')
    async def handler(_event):
        return content
    async def ledgered(evt, key, text, metadata, **kwargs):
        return await adapter.send(evt.source.chat_id, text, metadata=metadata), adapter
    monkeypatch.setattr(adapter, 'send_final_ledgered', ledgered)
    adapter.set_message_handler(handler)
    await adapter._process_message_background(event(), 'code-photo-test')
    adapter.send_multiple_images.assert_awaited_once()
    assert adapter.send_multiple_images.call_args.kwargs['images'] == images
