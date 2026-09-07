import asyncio

from cron.scheduler import (
    _extract_media_caption_map,
    _requires_native_image_album,
    _send_media_via_adapter,
)


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


def test_media_only_image_result_requires_native_album(tmp_path):
    first = tmp_path / "one.png"
    second = tmp_path / "two.webp"

    assert _requires_native_image_album(
        [(str(first), False), (str(second), False)], ""
    ) is True
    assert _requires_native_image_album(
        [(str(first), False), (str(second), False)], "visible text"
    ) is False


def test_required_native_album_never_falls_back_to_individual_images(
    tmp_path, monkeypatch
):
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    individual_calls = []

    class Adapter:
        async def send_multiple_images(self, **kwargs):
            raise RuntimeError("sendMediaGroup unavailable")

        async def send_image_file(self, **kwargs):
            individual_calls.append(kwargs)

    class Future:
        def __init__(self, coro):
            self.coro = coro

        def result(self, timeout):
            return asyncio.run(self.coro)

    monkeypatch.setattr(
        "agent.async_utils.safe_schedule_threadsafe",
        lambda coro, loop: Future(coro),
    )

    errors = _send_media_via_adapter(
        Adapter(),
        "chat-1",
        [(str(first), False), (str(second), False)],
        {"_require_native_media_group": True},
        object(),
        {"id": "job-1"},
        platform="telegram",
    )

    assert len(errors) == 1
    assert "sendMediaGroup unavailable" in errors[0]
    assert individual_calls == []