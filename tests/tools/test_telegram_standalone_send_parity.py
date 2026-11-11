"""The standalone Telegram sender (``hermes send``, the ``send_message`` tool, standalone cron
delivery) makes the gateway adapter's outbound decisions for the same text: the push-notification
mode, the invisible-only guard, the ``rich_messages`` fast-path, and a MarkdownV2 retry before
plain text when Bot API HTML rejects a tag. The ``telegram`` package is stubbed; the bot records
every call, so the assertions read what would reach the Bot API."""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class BadRequest(Exception):
    """python-telegram-bot's ``BadRequest`` (matched by class name when PTB is absent)."""


class TimedOut(Exception):
    """python-telegram-bot's ``TimedOut``: the request left, the reply never came."""


class NetworkError(Exception):
    """python-telegram-bot's ``NetworkError``: what a 5xx answer raises (matched by class name)."""


def _html_5xx(status: int, phrase: str) -> NetworkError:
    """PTB's shape for a 5xx whose body is a fronting proxy's HTML page, not Telegram's JSON."""
    try:
        try:
            raise ValueError("Invalid server response")
        except ValueError as parse_error:
            raise NetworkError(f"{phrase} ({status}). Parsing the server response "
                               f"b'<html><h1>{status}</h1></html>' failed") from parse_error
    except NetworkError as exc:
        return exc


def _install_telegram_mock(monkeypatch: pytest.MonkeyPatch, bot) -> None:
    parse_mode = SimpleNamespace(MARKDOWN_V2="MarkdownV2", HTML="HTML")
    constants_mod = SimpleNamespace(ParseMode=parse_mode)
    telegram_mod = SimpleNamespace(Bot=MagicMock(return_value=bot), MessageEntity=lambda **kw: SimpleNamespace(**kw),
                                   InputMediaPhoto=lambda **kw: SimpleNamespace(**kw), constants=constants_mod)
    monkeypatch.setitem(sys.modules, "telegram", telegram_mod)
    monkeypatch.setitem(sys.modules, "telegram.constants", constants_mod)


@pytest.fixture
def standalone(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A temp HERMES_HOME with ``config.yaml`` content of the test's choosing, plus a recording bot."""
    for var in ("TELEGRAM_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY",
                "all_proxy", "HERMES_TELEGRAM_NOTIFICATIONS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: None)
    monkeypatch.setattr("gateway.platforms.base._detect_macos_system_proxy", lambda: None)
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
    bot.send_document = AsyncMock(return_value=SimpleNamespace(message_id=8))
    bot.do_api_request = AsyncMock(return_value={"message_id": 9})
    _install_telegram_mock(monkeypatch, bot)

    def run(message, *, config="", extra=None, notify=None, media_files=None, thread_id=None):
        from gateway.config import Platform, PlatformConfig
        from tools.send_message_tool import _send_to_platform
        (tmp_path / "config.yaml").write_text(config, encoding="utf-8")
        pconfig = PlatformConfig(enabled=True, token="tok", extra=extra or {})
        return asyncio.run(_send_to_platform(Platform.TELEGRAM, pconfig, "123", message, notify=notify,
                                             media_files=media_files, thread_id=thread_id))
    return SimpleNamespace(bot=bot, run=run, home=tmp_path)


def test_notifications_mode_reaches_every_standalone_send(standalone) -> None:
    """dlz: a standalone send (the ``send_message`` tool, ``hermes send``, cron's standalone lane) is a
    deliverable like the gateway's final reply, so under ``important`` (default) it rings unless the
    caller passes ``notify=False`` (cron's ``cron.delivery.notify: false``), text and media alike; the
    fork's standalone sender always rang. ``notifications: all`` in config.yaml rings regardless."""
    doc = standalone.home / "report.txt"
    doc.write_text("x", encoding="utf-8")
    assert standalone.run("status: still syncing")["success"] is True
    assert "disable_notification" not in standalone.bot.send_message.await_args.kwargs
    assert standalone.run("report attached", media_files=[(str(doc), False)])["success"] is True
    assert "disable_notification" not in standalone.bot.send_document.await_args.kwargs

    standalone.run("quiet status", notify=False)
    assert standalone.bot.send_message.await_args.kwargs["disable_notification"] is True
    assert standalone.run("quiet report", notify=False, media_files=[(str(doc), False)])["success"] is True
    assert standalone.bot.send_document.await_args.kwargs["disable_notification"] is True

    standalone.run("daily brief", notify=True)
    assert "disable_notification" not in standalone.bot.send_message.await_args.kwargs

    standalone.run("deploy finished", config="display:\n  platforms:\n    telegram:\n      notifications: all\n")
    assert "disable_notification" not in standalone.bot.send_message.await_args.kwargs


def test_invisible_only_text_is_never_delivered(standalone) -> None:
    result = standalone.run("\u200b\u2060\ufeff \u00ad")
    assert "error" in result
    standalone.bot.send_message.assert_not_awaited()


def test_rich_messages_opt_in_sends_tables_through_send_rich_message(standalone) -> None:
    table = "| Service | Port |\n|---|---:|\n| oMLX | 8000 |"
    result = standalone.run(table, extra={"rich_messages": True})
    assert result == {"success": True, "platform": "telegram", "chat_id": "123", "message_id": "9"}
    method, = standalone.bot.do_api_request.await_args.args
    payload = standalone.bot.do_api_request.await_args.kwargs["api_kwargs"]
    # dlz: an unmarked standalone send rings (see test_notifications_mode_reaches_every_standalone_send).
    assert (method, payload["rich_message"]["markdown"], payload.get("disable_notification")) == (
        "sendRichMessage", table, None)
    standalone.bot.send_message.assert_not_awaited()

    standalone.run(table)  # rich_messages off (the default): legacy MarkdownV2, table flattened
    assert standalone.bot.do_api_request.await_count == 1
    assert standalone.bot.send_message.await_args.kwargs["parse_mode"] == "MarkdownV2"


def test_rejected_html_retries_as_markdown_before_plain_text(standalone) -> None:
    """One unsupported tag beside a supported one fails the HTML parse; the Markdown still renders."""
    async def send_message(**kwargs):
        if kwargs.get("parse_mode") == "HTML":
            raise BadRequest("Bad Request: can't parse entities: unsupported start tag \"details\"")
        return SimpleNamespace(message_id=7)
    standalone.bot.send_message = AsyncMock(side_effect=send_message)

    result = standalone.run("<b>Quiz</b>\n\n- **one**\n\n<details><summary>Answers</summary>a</details>")

    assert result["success"] is True
    modes = [c.kwargs["parse_mode"] for c in standalone.bot.send_message.await_args_list]
    assert modes == ["HTML", "MarkdownV2"]
    assert "*one*" in standalone.bot.send_message.await_args.kwargs["text"]


def test_photo_batches_go_as_albums_and_mixed_batches_stay_per_file(standalone) -> None:
    """Two or more still photos ride ``sendMediaGroup`` (10 per album, in order); a batch with a
    non-photo keeps one upload per file."""
    standalone.bot.send_media_group = AsyncMock(return_value=[SimpleNamespace(message_id=11)])
    standalone.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=12))
    photos = []
    for i in range(12):
        photo = standalone.home / f"p{i}.png"
        photo.write_bytes(b"\x89PNG\r\n\x1a\n")
        photos.append((str(photo), False))

    assert standalone.run("", media_files=photos)["success"] is True
    albums = [call.kwargs for call in standalone.bot.send_media_group.await_args_list]
    assert [len(kw["media"]) for kw in albums] == [10, 2]
    assert all("disable_notification" not in kw for kw in albums)  # dlz: an unmarked standalone send rings
    standalone.bot.send_photo.assert_not_awaited()

    doc = standalone.home / "notes.txt"
    doc.write_text("x", encoding="utf-8")
    assert standalone.run("", media_files=photos[:2] + [(str(doc), False)])["success"] is True
    assert standalone.bot.send_media_group.await_count == 2
    assert standalone.bot.send_photo.await_count == 2


def _receipts(kwargs):
    """dlz: Telegram's confirmed album answer: one Message per photo, one shared ``media_group_id``."""
    return [SimpleNamespace(message_id=11 + i, media_group_id="g") for i in range(len(kwargs["media"]))]


def _photos(home, n):
    paths = []
    for i in range(n):
        photo = home / f"p{i}.png"
        photo.write_bytes(b"\x89PNG\r\n\x1a\n")
        paths.append((str(photo), False))
    return paths


def test_album_with_a_lost_reply_is_never_resent(standalone) -> None:
    """Telegram may already have posted the album: no per-photo copy, success with a warning."""
    standalone.bot.send_media_group = AsyncMock(side_effect=TimedOut("Timed out"))
    standalone.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=12))

    result = standalone.run("", media_files=_photos(standalone.home, 2))

    assert result["success"] is True
    assert "not resent" in " ".join(result["warnings"])
    assert standalone.bot.send_media_group.await_count == 1
    standalone.bot.send_photo.assert_not_awaited()


def test_refused_album_falls_back_per_photo_and_reports_each_failure(standalone) -> None:
    standalone.bot.send_media_group = AsyncMock(side_effect=BadRequest("Bad Request: group send failed"))

    async def send_photo(**kwargs):
        if kwargs["photo"].name.endswith("p1.png"):
            raise BadRequest("Bad Request: wrong file identifier")
        return SimpleNamespace(message_id=12)
    standalone.bot.send_photo = AsyncMock(side_effect=send_photo)

    result = standalone.run("", media_files=_photos(standalone.home, 2))

    assert result["success"] is True
    assert standalone.bot.send_photo.await_count == 2
    assert [w for w in result["warnings"] if "p1.png" in w], result["warnings"]


def test_refused_first_album_keeps_the_photos_in_order(standalone) -> None:
    """12 photos, the first album refused and the second accepted: the user sees p0..p11 in the
    order given, never the later album ahead of the refused one's per-photo fallback."""
    delivered = []

    async def send_media_group(**kwargs):
        names = [os.path.basename(item.media.name) for item in kwargs["media"]]
        if names[0] == "p0.png":
            raise BadRequest("Bad Request: group send failed")
        delivered.extend(names)
        return [SimpleNamespace(message_id=11)]

    async def send_photo(**kwargs):
        delivered.append(os.path.basename(kwargs["photo"].name))
        return SimpleNamespace(message_id=12)
    standalone.bot.send_media_group = AsyncMock(side_effect=send_media_group)
    standalone.bot.send_photo = AsyncMock(side_effect=send_photo)

    result = standalone.run("", media_files=_photos(standalone.home, 12))

    assert result["success"] is True
    assert delivered == [f"p{i}.png" for i in range(12)]


def test_album_to_a_stale_topic_retries_once_without_the_thread(standalone) -> None:
    """A dead private topic still gets the album into the chat, as one album, never photo by photo.
    The route steps (same topic, its native id, then the chat root) are the adapter's DM-topic retry."""
    calls = []

    async def send_media_group(**kwargs):
        calls.append(kwargs)
        if kwargs.get("message_thread_id") or kwargs.get("direct_messages_topic_id"):
            raise BadRequest("Bad Request: message thread not found")
        return _receipts(kwargs)
    standalone.bot.send_media_group = AsyncMock(side_effect=send_media_group)
    standalone.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=12))

    result = standalone.run("", media_files=_photos(standalone.home, 2), thread_id="77")

    assert result["success"] is True and "warnings" not in result
    assert calls[0].get("message_thread_id") == 77
    assert not calls[-1].get("message_thread_id") and not calls[-1].get("direct_messages_topic_id")
    standalone.bot.send_photo.assert_not_awaited()


def test_photo_that_cannot_be_read_is_reported_and_the_rest_go_as_the_album(standalone) -> None:
    """A photo that exists but cannot be opened (here a directory named like one) is left out of
    the album and reported by name; the readable photos still go as one album."""
    standalone.bot.send_media_group = AsyncMock(return_value=[SimpleNamespace(message_id=11)])
    photos = _photos(standalone.home, 3)
    unreadable = standalone.home / "p1.png"
    unreadable.unlink()
    unreadable.mkdir()

    result = standalone.run("", media_files=photos)

    assert result["success"] is True
    assert len(standalone.bot.send_media_group.await_args.kwargs["media"]) == 2
    assert [w for w in result.get("warnings", ()) if "p1.png" in w], result

@pytest.mark.parametrize("exc", [TimedOut("Timed out"), NetworkError("Gateway Timeout (504)"),
                                 _html_5xx(502, "Bad Gateway"), _html_5xx(504, "Gateway Timeout")],
                         ids=["timed-out", "504", "502-html", "504-html"])
def test_album_that_may_be_in_the_chat_is_never_resent(standalone, exc) -> None:
    """A timeout, a 504 or an HTML 5xx page answering sendMediaGroup may follow a posted album: one
    request, no per-photo copy, success with the "not resent" warning (one classifier with the text
    lane)."""
    standalone.bot.send_media_group = AsyncMock(side_effect=exc)
    standalone.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=12))

    result = standalone.run("", media_files=_photos(standalone.home, 2))

    assert result["success"] is True
    assert "not resent" in " ".join(result["warnings"])
    assert standalone.bot.send_media_group.await_count == 1
    standalone.bot.send_photo.assert_not_awaited()


def test_album_on_telegrams_json_5xx_resends_the_same_album(standalone, monkeypatch) -> None:
    """Telegram's own JSON 502 (PTB's unchained ``NetworkError('Bad Gateway')``) means the album was
    not posted: the same sendMediaGroup goes again after the text lane's backoff, never photo by
    photo, and the files are re-read from the start."""
    sleep = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", sleep)
    uploads = []

    async def send_media_group(**kwargs):
        uploads.append([item.media.read() for item in kwargs["media"]])
        if len(uploads) == 1:
            raise NetworkError("Bad Gateway")
        return _receipts(kwargs)
    standalone.bot.send_media_group = AsyncMock(side_effect=send_media_group)
    standalone.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=12))

    result = standalone.run("", media_files=_photos(standalone.home, 2))

    assert result["success"] is True and "warnings" not in result
    assert uploads[0] == uploads[1] and len(uploads) == 2 and uploads[0][0]
    standalone.bot.send_photo.assert_not_awaited()
    sleep.assert_awaited_once_with(1.0)


@pytest.mark.parametrize("status,phrase", [(502, "Bad Gateway"), (504, "Gateway Timeout")])
def test_html_5xx_page_is_not_a_parse_error(standalone, monkeypatch, status, phrase) -> None:
    """PTB's text for an HTML 5xx body says "Parsing ... failed"; it may follow a posted message, so
    no plain-text copy and no retry. A real Bot API entity-parse refusal still falls back once."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    standalone.bot.send_message = AsyncMock(side_effect=_html_5xx(status, phrase))

    result = standalone.run("hello *world*")

    assert "error" in result
    assert [c.kwargs["parse_mode"] for c in standalone.bot.send_message.await_args_list] == ["MarkdownV2"]

    standalone.bot.send_message = AsyncMock(side_effect=[
        BadRequest("Bad Request: can't parse entities: character '!' is reserved"), SimpleNamespace(message_id=7)])
    assert standalone.run("hello *world*")["success"] is True
    assert [c.kwargs["parse_mode"] for c in standalone.bot.send_message.await_args_list] == ["MarkdownV2", None]


def test_a_sender_written_to_the_earlier_contract_still_delivers(standalone, monkeypatch) -> None:
    """A user plugin re-registering telegram with the documented ``standalone_sender_fn`` signature
    (no ``notify``) is reached through the real registry lookup and is never handed ``notify``; a
    sender taking ``**kwargs`` still gets it."""
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    entry = platform_registry.get("telegram")
    calls = []

    async def documented(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
        calls.append(message)
        return {"success": True, "message_id": "user-plugin"}

    monkeypatch.setattr(entry, "standalone_sender_fn", documented)
    assert standalone.run("daily brief", notify=True) == {"success": True, "message_id": "user-plugin"}

    async def open_kwargs(pconfig, chat_id, message, **kwargs):
        calls.append(kwargs.get("notify"))
        return {"success": True, "message_id": "kw"}

    monkeypatch.setattr(entry, "standalone_sender_fn", open_kwargs)
    assert standalone.run("daily brief", notify=True)["success"] is True
    assert calls == ["daily brief", True]


def test_an_unhashable_callable_sender_still_delivers(standalone, monkeypatch) -> None:
    """A ``standalone_sender_fn`` that is a callable object defining ``__eq__`` (so unhashable)
    is reached through the real registry lookup and still gets ``notify``."""
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import discover_plugins
    discover_plugins()

    class Sender:
        def __init__(self):
            self.calls = []

        def __eq__(self, other):
            return self is other

        async def __call__(self, pconfig, chat_id, message, *, thread_id=None, media_files=None,
                           force_document=False, notify=None):
            self.calls.append((message, notify))
            return {"success": True, "message_id": "obj"}

    sender = Sender()
    monkeypatch.setattr(platform_registry.get("telegram"), "standalone_sender_fn", sender)
    assert standalone.run("daily brief", notify=True) == {"success": True, "message_id": "obj"}
    assert sender.calls == [("daily brief", True)]


@pytest.mark.parametrize("answer", [[], [SimpleNamespace(message_id=11)],
                                    [SimpleNamespace(message_id=11, media_group_id=None),
                                     SimpleNamespace(message_id=12, media_group_id=None)]],
                         ids=["empty", "short", "no-group-id"])
def test_album_with_a_malformed_receipt_is_uncertain_and_never_resent(standalone, answer) -> None:
    """dlz: the fork's receipt check (exact count, one shared non-null ``media_group_id``) survives the
    move into the plugin's album sender: a malformed ack is a possibly-posted album, never a per-photo copy."""
    standalone.bot.send_media_group = AsyncMock(return_value=answer)
    standalone.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=13))

    result = standalone.run("", media_files=_photos(standalone.home, 2))

    assert result["success"] is True
    assert "not resent" in " ".join(result["warnings"])
    assert "message_id" not in result
    assert standalone.bot.send_media_group.await_count == 1
    standalone.bot.send_photo.assert_not_awaited()


def test_confirmed_album_reports_its_receipt_id(standalone) -> None:
    standalone.bot.send_media_group = AsyncMock(return_value=[
        SimpleNamespace(message_id=21, media_group_id="g"), SimpleNamespace(message_id=22, media_group_id="g")])
    result = standalone.run("", media_files=_photos(standalone.home, 2))
    assert result["success"] is True and result["message_id"] == "22"
    assert not result.get("warnings")


@pytest.mark.parametrize("fence", ["~~~", "~~~~", "  ~~~"])
def test_tag_inside_a_tilde_fence_is_an_example_not_html(standalone, fence) -> None:
    """dlz (fork ``_telegram_has_html``): a tilde-fenced block is Markdown code like a backtick one, so
    ``<b>literal</b>`` inside it never opts the whole message into HTML parse mode."""
    standalone.run(f"Example:\n{fence}html\n<b>literal</b>\n{fence.strip()}\n")
    assert standalone.bot.send_message.await_args.kwargs["parse_mode"] == "MarkdownV2"
    standalone.run("<b>real</b> bold")  # control: a tag outside code is still HTML
    assert standalone.bot.send_message.await_args.kwargs["parse_mode"] == "HTML"


def test_html_rejected_as_html_and_markdown_falls_back_to_readable_plain_text(standalone) -> None:
    """dlz (fork ``_telegram_html_to_plain``): the last-resort plain send carries no tags or entities;
    a link keeps its target, an unsupported angle literal stays readable."""
    async def send_message(**kwargs):
        if kwargs.get("parse_mode"):
            raise BadRequest("Bad Request: can't parse entities: unsupported start tag")
        return SimpleNamespace(message_id=7)
    standalone.bot.send_message = AsyncMock(side_effect=send_message)

    result = standalone.run('<b>Bold</b> &amp; <a href="https://x.example/p">docs</a> for List<int>')

    assert result["success"] is True
    last = standalone.bot.send_message.await_args.kwargs
    assert last["parse_mode"] is None
    assert last["text"] == "Bold & docs (https://x.example/p) for List<int>"
