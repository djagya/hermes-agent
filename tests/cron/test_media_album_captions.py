import asyncio

from cron.scheduler import _extract_media_caption_map, _send_media_via_adapter


def test_extract_media_caption_map_binds_caption_to_preceding_media(tmp_path):
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    content = (
        f"MEDIA:{first}\nCAPTION: first frame\n"
        f"MEDIA:{second}\nCAPTION: second frame\n"
        "visible footer"
    )

    captions, cleaned = _extract_media_caption_map(content)

    assert captions == {
        str(first): "first frame",
        str(second): "second frame",
    }
    assert "CAPTION:" not in cleaned
    assert "visible footer" in cleaned


def test_telegram_images_use_one_native_album_with_per_path_captions(
    tmp_path, monkeypatch
):
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    captured = []

    class Adapter:
        async def send_multiple_images(self, **kwargs):
            captured.append(kwargs)

    class Future:
        def result(self, timeout):
            return None

    def schedule(coro, loop):
        asyncio.run(coro)
        return Future()

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", schedule)

    errors = _send_media_via_adapter(
        Adapter(),
        "chat-1",
        [(str(first), False), (str(second), False)],
        {"thread_id": "7"},
        object(),
        {"id": "job-1"},
        platform="telegram",
        captions_by_path={str(first): "one", str(second): "two"},
    )

    assert errors == []
    assert captured == [
        {
            "chat_id": "chat-1",
            "images": [
                (first.resolve().as_uri(), "one"),
                (second.resolve().as_uri(), "two"),
            ],
            "metadata": {"thread_id": "7"},
        }
    ]