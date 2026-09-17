"""Receipt/ordering for pending-voice follow-ups (pending-voice-delivery fork candidate).

Red-capable scenarios on top of the cherry-picked per-clip single-flight STT
(``gateway/pending_audio.py``): a queued voice message must never hold a finished
turn's answer hostage, and a voice follow-up that requested an interrupt must
suppress the finished turn's stale final. Real production handlers and drain paths
run here; external I/O (adapter transport, STT provider, agent) is faked on a temp
``HERMES_HOME``.
"""
import asyncio
import threading
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext

try:
    from gateway.pending_audio import PendingAudioClip
except ImportError:  # pre-candidate baseline: the per-clip module does not exist yet
    PendingAudioClip = None


def _clip_file(tmp_path, name="clip-a"):
    path = tmp_path / f"{name}.ogg"
    path.write_bytes(b"OggSfixture")
    return str(path)


@pytest.fixture
def runner_env(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    adapter_pending = {}
    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(success=True)),
        edit_message=AsyncMock(return_value=SimpleNamespace(success=True)),
        send_typing=AsyncMock(),
        # Base-adapter contract used by the drain: pop the pending event for a session.
        get_pending_message=lambda key: adapter_pending.pop(key, None),
        extract_media=lambda response: ([], response),
        extract_local_files=lambda text: ([], text),
        _background_tasks=set(),
        _pending_messages=adapter_pending,
        _post_delivery_callbacks={},
    )
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(stt_enabled=True, stt_echo_transcripts=True)
    runner._draining = False
    runner.__dict__["_sessions"] = {}
    monkeypatch.setattr(runner, "_adapter_for_source", lambda source: adapter, raising=False)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(runner, "_refresh_agent_cache_message_count", _noop, raising=False)
    yield runner, adapter


class _FakeAgent:
    """Minimal running-agent stand-in for the interrupt/receipt paths."""

    def __init__(self):
        self.interrupts = []

    def get_activity_summary(self):
        return {"max_iterations": 0, "api_call_count": 0, "current_tool": None}

    def interrupt(self, message=None, **kwargs):
        self.interrupts.append(message)
        return True


def _blocking_stt(monkeypatch, transcript):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def transcribe(path, *args):
        calls.append(path)
        entered.set()
        assert release.wait(20), "test did not release STT"
        return {"success": True, "transcript": transcript, "provider": "fixture"}

    monkeypatch.setattr("tools.transcription_tools.transcribe_audio", transcribe)
    monkeypatch.setattr(
        "tools.transcription_tools.transcribe_audio_local_fallback",
        lambda path: {"success": False, "error": "fixture fallback"})
    return entered, release, calls


def _voice_event(source, clip):
    return MessageEvent(
        text="", message_type=MessageType.VOICE, source=source,
        media_urls=[clip], media_types=["audio/ogg"])


def _turn_ctx(source, result):
    return TurnContext(
        source=source, session_key="conversation", session_id="s1", history=result.get("messages"),
        stream_consumer_holder=[None], streaming_tts_consumer_holder=[None],
        result_holder=[result])


@pytest.mark.asyncio
async def test_queue_delivers_first_answer_before_slow_voice_stt(runner_env, monkeypatch, tmp_path):
    """A queued voice clip must not delay delivery of the finished turn's answer.

    Production order (``TurnRunner._run_agent``): ``_run_agent_drain_pending`` fully
    returns before ``_run_agent_queued_followup`` delivers the first response — so a
    drain that awaits STT in front of delivery holds the answer for the whole clip
    (the reported incident: ~5.5 minutes). RED on base: the drain blocks inside STT.
    """
    runner, adapter = runner_env
    entered, release, calls = _blocking_stt(monkeypatch, "slow voice request")

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm")
    voice = _voice_event(source, _clip_file(tmp_path))
    result = {"final_response": "first answer", "messages": [], "interrupted": False}
    adapter._pending_messages["conversation"] = voice

    drain = asyncio.ensure_future(
        runner._run_agent_drain_pending(result, adapter, source, "conversation"))
    try:
        pending_event, pending = await asyncio.wait_for(drain, 5)
    except asyncio.TimeoutError:
        release.set()  # unblock the fixture's STT thread before failing
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
        raise
    assert pending_event is voice
    # Caption-less: the pending text is the media placeholder carrier; the turn joins the
    # transcript via the event in _prepare_inbound_message_text.
    assert pending.startswith("[User sent audio:")

    turn_ctx = _turn_ctx(source, result)
    await runner._run_agent_deliver_first_response(turn_ctx, adapter, None, result, None)
    # The finished turn's answer is out while the clip's STT is still unresolved.
    assert entered.is_set() and not release.is_set()
    assert adapter.send.await_count == 1
    assert adapter.send.await_args.args[1] == "first answer"

    release.set()
    await asyncio.gather(*adapter._background_tasks)
    # Nothing lost: the transcript echo still lands exactly once.
    delivered = [call.args[1] for call in adapter.send.await_args_list]
    assert Counter(delivered) == Counter({"first answer": 1, '🎙️ "slow voice request"': 1})
    assert calls == [voice.media_urls[0]]  # exactly one STT invocation for the clip


@pytest.mark.asyncio
async def test_voice_followup_that_interrupted_suppresses_stale_final(runner_env, monkeypatch, tmp_path):
    """A queued voice follow-up that itself requested the interrupt must NOT deliver the
    finished turn's final: that answer predates the superseding message (the incident's
    obsolete missing-material request). RED on base: base delivers the stale final.
    """
    runner, adapter = runner_env

    calls = []

    def transcribe(path, *args):
        calls.append(path)
        return {"success": True, "transcript": "actual new question", "provider": "fixture"}

    monkeypatch.setattr("tools.transcription_tools.transcribe_audio", transcribe)
    monkeypatch.setattr(
        "tools.transcription_tools.transcribe_audio_local_fallback",
        lambda path: {"success": False, "error": "fixture fallback"})

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm")
    voice = _voice_event(source, _clip_file(tmp_path))
    voice._gateway_interrupt_requested = True  # stamped by the busy interrupt path
    # The clip finished transcribing by follow-up time (background claim owns the echo).
    voice._gateway_pending_stt_text = '"actual new question"'
    voice._gateway_pending_stt_transcripts = ["actual new question"]

    result = {
        "final_response": "please send the missing material",
        "messages": [], "interrupted": False,
    }
    turn_ctx = _turn_ctx(source, result)

    followup_messages = []

    async def fake_run_agent(**kwargs):
        followup_messages.append(kwargs.get("message"))
        return {"final_response": "fresh answer", "messages": [], "interrupted": False}

    monkeypatch.setattr(runner, "_run_agent", fake_run_agent)

    merged = await runner._run_agent_queued_followup(
        turn_ctx, adapter, "", voice, None, result, None)
    assert adapter.send.await_count == 0  # no stale final, no duplicate echo
    assert followup_messages == ['"actual new question"']
    assert merged.get("final_response") == "fresh answer"
    assert calls == []  # warm cache reused; no second transcription


@pytest.mark.asyncio
async def test_interrupt_receipt_then_drain_single_flight(runner_env, monkeypatch, tmp_path):
    """Interrupt receipt must be immediate (no STT wait) and the clip must transcribe
    exactly once across the interrupt handler, the drain and the follow-up turn.
    RED on base: the receipt handler blocks on STT before interrupt() returns.
    """
    runner, adapter = runner_env
    entered, release, calls = _blocking_stt(monkeypatch, "shared clip words")

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm")
    voice = _voice_event(source, _clip_file(tmp_path))
    agent = _FakeAgent()

    receipt = asyncio.ensure_future(
        runner._interrupt_running_agent_for_busy_event(voice, adapter, agent))
    try:
        await asyncio.wait_for(receipt, 5)
    except asyncio.TimeoutError:
        release.set()
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
        raise
    # Truthful receipt: the agent was interrupted with the caption immediately, while the
    # clip's STT is still unresolved (no false "interrupt complete with your words").
    assert entered.is_set() and not release.is_set()
    assert agent.interrupts == [""]
    assert voice._gateway_interrupt_requested is True

    # The interrupt path queued the event; the post-turn drain must not block on STT either.
    adapter._pending_messages["conversation"] = voice
    pending_event, pending = await asyncio.wait_for(
        runner._run_agent_drain_pending({"interrupted": True}, adapter, source, "conversation"), 5)
    assert pending_event is voice

    release.set()
    await asyncio.gather(*adapter._background_tasks)
    # One clip, one provider call, one echo, and a warm cache for the follow-up turn.
    assert calls == [voice.media_urls[0]]
    delivered = [call.args[1] for call in adapter.send.await_args_list]
    assert delivered == ['🎙️ "shared clip words"']
    message_text = await runner._prepare_profile_scoped_inbound_message_text(
        event=voice, source=source, history=[], session_key="conversation")
    assert "shared clip words" in message_text
    assert calls == [voice.media_urls[0]]  # join, not a second transcription
    assert len([call for call in adapter.send.await_args_list]) == 1


@pytest.mark.asyncio
async def test_drain_on_warm_cache_reuses_text_and_echoes_once(runner_env, monkeypatch, tmp_path):
    """A drained event whose clip already transcribed (interrupt path finished first)
    reuses the cached text, needs no new STT call, and echoes the transcript once."""
    if PendingAudioClip is None:
        pytest.skip("requires the per-clip single-flight module (candidate infra)")
    runner, adapter = runner_env
    calls = []

    def transcribe(path, *args):  # any call here is a failure of reuse
        calls.append(path)
        return {"success": True, "transcript": "warm words", "provider": "fixture"}

    import tools.transcription_tools as stt

    monkeypatch.setattr(stt, "transcribe_audio", transcribe)

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm")
    clip = _clip_file(tmp_path)
    voice = _voice_event(source, clip)
    loop = asyncio.get_running_loop()
    done_task = loop.create_future()
    done_task.set_result(('"warm words"', ["warm words"]))
    voice._gateway_pending_stt_clips = {clip: PendingAudioClip(task=done_task)}
    voice._gateway_pending_stt_text = '"warm words"'
    voice._gateway_pending_stt_transcripts = ["warm words"]
    adapter._pending_messages["conversation"] = voice

    pending_event, pending = await asyncio.wait_for(
        runner._run_agent_drain_pending(
            {"final_response": "done", "interrupted": False}, adapter, source, "conversation"), 5)
    assert pending == '"warm words"'
    await asyncio.gather(*adapter._background_tasks)
    assert calls == []
    delivered = [call.args[1] for call in adapter.send.await_args_list]
    assert delivered == ['🎙️ "warm words"']  # echo-once on the warm drain path
