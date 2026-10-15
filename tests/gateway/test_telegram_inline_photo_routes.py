"""Ordinary and queued finals share authored photo order; transport is synthetic."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, TimedOut

from gateway.platforms.base import SendResult
from gateway.platforms.base_response import final_text_metadata
from gateway.run import GatewayRunner
from tests.gateway.test_telegram_inline_photos import KNIFE_URLS, event
from tests.gateway.test_telegram_rich_messages import _make_adapter

# Public image URLs only; descriptions and labels are deliberately synthetic.
PRODUCT_URLS = [
    "https://www.metmo.co.uk/cdn/shop/files/Pocket-driver-productpage-Hero-HD.jpg?v=1754468260",
    "https://thumbs.static-thomann.de/thumb/padthumb600x600/pics/bdb/_43/431590/12905228_800.jpg",
    "https://www.kaweco-pen.com/media/a1/28/59/1738853687/Kaweco-Brass-Sport-FP-s.webp?ts=1738853687",
    "https://siebenstein-spiele.com/cdn/shop/files/Huzzle_Cast_Infinity.png?v=1743674973",
    "https://d2j6dbq0eux0bg.cloudfront.net/images/22582377/3300831411.jpg",
    "https://www.fotoimpex.de/shop/images/products/main/63755.jpg",
    "https://cdn.shopify.com/s/files/1/0627/7051/2130/products/GreenX32-491946.jpg?v=1751255664",
    "https://thumbs.static-thomann.de/thumb/padthumb600x600/pics/bdb/_59/597998/19405816_800.jpg",
]


def response(urls):
    return '\n\n'.join(
        f'## Item {i}\n\n![image {i}]({url})\n\nDescription {i}; SKU `sample`'
        for i, url in enumerate(urls))


def prepared_adapter(monkeypatch):
    adapter = _make_adapter()
    adapter.config.typing_indicator = False
    adapter._get_human_delay = lambda: 0
    adapter._stop_typing_refresh = AsyncMock()
    adapter.send_multiple_images = AsyncMock(return_value=SendResult(success=True))
    monkeypatch.setattr(adapter, '_record_rich_sent', AsyncMock())
    monkeypatch.setattr(adapter, '_wants_auto_tts', lambda *args: False)

    async def ledgered(evt, key, text, metadata, **kwargs):
        return await adapter.send(evt.source.chat_id, text, metadata=metadata), adapter

    monkeypatch.setattr(adapter, 'send_final_ledgered', ledgered)
    return adapter


async def ordinary_final(adapter, content):
    adapter.set_message_handler(AsyncMock(return_value=content))
    await adapter._process_message_background(event(), 'photo-route-test')


@pytest.mark.asyncio
@pytest.mark.parametrize('urls', [PRODUCT_URLS, KNIFE_URLS])
async def test_ordinary_and_queued_finals_have_identical_native_photo_order(monkeypatch, urls):
    adapter = prepared_adapter(monkeypatch)
    content = response(urls)
    await ordinary_final(adapter, content)
    adapter._bot.do_api_request.assert_awaited_once()
    ordinary = adapter._bot.do_api_request.call_args.kwargs['api_kwargs']['rich_message']
    adapter._bot.do_api_request.reset_mock()

    runner = object.__new__(GatewayRunner)
    delivered = await runner._deliver_queued_first_response(
        content, source=event().source, adapter=adapter, metadata={}, event_message_id='2')
    assert delivered
    adapter._bot.do_api_request.assert_awaited_once()
    queued = adapter._bot.do_api_request.call_args.kwargs['api_kwargs']['rich_message']
    assert ordinary == queued
    assert 'media' not in ordinary
    positions = [ordinary['markdown'].index(part) for i, url in enumerate(urls)
                 for part in (f'## Item {i}', f'![image {i}]({url})', f'Description {i}')]
    assert positions == sorted(positions)
    adapter.send_multiple_images.assert_not_called()
    adapter._bot.send_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [BadRequest('invalid rich media'), TypeError('no endpoint'),
                                     TimedOut('timed out')])
async def test_eight_photos_fallback_once_or_no_replay_on_uncertain_send(monkeypatch, failure):
    adapter = prepared_adapter(monkeypatch)
    adapter._bot.do_api_request.side_effect = failure
    await ordinary_final(adapter, response(PRODUCT_URLS))
    adapter._bot.do_api_request.assert_awaited_once()
    if isinstance(failure, TimedOut):
        adapter.send_multiple_images.assert_not_called()
        adapter._bot.send_message.assert_not_called()
    else:
        adapter.send_multiple_images.assert_awaited_once()
        assert adapter.send_multiple_images.call_args.kwargs['images'] == [
            (url, f'image {i}') for i, url in enumerate(PRODUCT_URLS)]
        adapter._bot.send_message.assert_awaited_once()
        text = adapter._bot.send_message.call_args.kwargs['text']
        assert all(f'Description {i}' in text for i in range(len(PRODUCT_URLS)))
        assert '![' not in text
        assert not any(url in text for url in PRODUCT_URLS)


@pytest.mark.asyncio
async def test_capability_latched_off_after_extraction_retains_single_fallback(monkeypatch):
    adapter = prepared_adapter(monkeypatch)
    original = adapter._extract_response_content

    async def extract(*args, **kwargs):
        extracted = await original(*args, **kwargs)
        adapter._rich_send_disabled = True
        return extracted

    monkeypatch.setattr(adapter, '_extract_response_content', extract)
    await ordinary_final(adapter, response(PRODUCT_URLS))
    adapter._bot.do_api_request.assert_not_called()
    adapter.send_multiple_images.assert_awaited_once()
    assert adapter.send_multiple_images.call_args.kwargs['images'] == [
        (url, f'image {i}') for i, url in enumerate(PRODUCT_URLS)]
    assert '![' not in adapter._bot.send_message.call_args.kwargs['text']


def test_text_transport_metadata_does_not_mutate_attachment_metadata():
    adapter = _make_adapter()
    metadata = {'thread_id': 'topic', 'notify': True}
    extracted = SimpleNamespace(text_content='Plain reply', images=[], media_files=[], local_files=[],
                                force_document_attachments=False)
    assert final_text_metadata(adapter, extracted, metadata) is metadata
    extracted.media_files = [('speech.ogg', True)]
    assert final_text_metadata(adapter, extracted, metadata) == {
        **metadata, 'legacy_attachment_delivery': True}
    assert metadata == {'thread_id': 'topic', 'notify': True}
    extracted.media_files = []
    extracted.images = [(PRODUCT_URLS[0], 'image 0')]
    extracted.text_content = response(PRODUCT_URLS[:1])
    assert final_text_metadata(adapter, extracted, metadata) == {
        **metadata, 'inline_images_fallback': True}
    assert metadata == {'thread_id': 'topic', 'notify': True}
