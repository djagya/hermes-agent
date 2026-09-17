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


def _instant_stt(monkeypatch, transcript):
    """Transcribable immediately; the event exists only for symmetric unpacking."""
    calls = []

    def transcribe(path, *args):
        calls.append(path)
        return {"success": True, "transcript": transcript, "provider": "fixture"}

    monkeypatch.setattr("tools.transcription_tools.transcribe_audio", transcribe)
    monkeypatch.setattr(
        "tools.transcription_tools.transcribe_audio_local_fallback",
        lambda path: {"success": False, "error": "fixture fallback"})
    return threading.Event(), calls


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

    original_history = [
        {"role": "user", "content": "older task: what is the swap deadline"},
        {"role": "assistant", "content": "please send the missing material"},
    ]
    result = {
        "final_response": "please send the missing material",
        # The superseded turn's real history: the original question that must survive
        # into the follow-up turn (question + transcript preservation criterion).
        "messages": original_history,
        "interrupted": False,
    }
    turn_ctx = _turn_ctx(source, result)

    followup_calls = []

    async def fake_run_agent(**kwargs):
        followup_calls.append(kwargs)
        return {"final_response": "fresh answer", "messages": [], "interrupted": False}

    monkeypatch.setattr(runner, "_run_agent", fake_run_agent)

    merged = await runner._run_agent_queued_followup(
        turn_ctx, adapter, "", voice, None, result, None)
    assert adapter.send.await_count == 0  # no stale final, no duplicate echo
    assert [call["message"] for call in followup_calls] == ['"actual new question"']
    assert merged.get("final_response") == "fresh answer"
    assert calls == []  # warm cache reused; no second transcription
    # Question + transcript preservation: the follow-up turn runs with the FULL preserved
    # history — the original question survives beside the new transcript, not instead of it.
    assert len(followup_calls) == 1
    carried_history = followup_calls[0]["history"]
    assert [m["content"] for m in carried_history] == [
        "older task: what is the swap deadline", "please send the missing material",
    ]
    assert followup_calls[0]["message"].count("actual new question") == 1


@pytest.mark.asyncio
async def test_interrupt_receipt_then_drain_single_flight(runner_env, monkeypatch, tmp_path):
    """Interrupt receipt must be immediate (no STT wait) and the clip must transcribe
    exactly once across the interrupt handler, the busy merge route and the follow-up
    turn. RED on base: the receipt handler blocks on STT before interrupt() returns.
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

    # REAL busy queue route, in production order: the stamped event is merged into a
    # retained head event that has no flag of its own (a follow-up queued first). The
    # supersession semantics must transfer onto the RETAINED event — merge may keep the
    # OLDER event, so the flag must survive the merge, not live on the absorbed object.
    head = _voice_event(source, _clip_file(tmp_path, name="clip-head"))
    runner._queue_or_replace_pending_event("conversation", head)
    assert adapter._pending_messages["conversation"] is head
    from gateway.platforms.base import merge_pending_message_event
    merge_pending_message_event(adapter._pending_messages, "conversation", voice)
    assert adapter._pending_messages["conversation"] is head  # the OLDER event was retained
    assert head._gateway_interrupt_requested is True  # flag transferred by the merge
    assert Counter(head.media_urls) == Counter([head.media_urls[0], voice.media_urls[0]])

    # The post-turn drain must not block on STT either.
    pending_event, pending = await asyncio.wait_for(
        runner._run_agent_drain_pending({"interrupted": True}, adapter, source, "conversation"), 5)
    assert pending_event is head

    release.set()
    await asyncio.gather(*adapter._background_tasks)
    # One provider call per clip: the absorbed clip joins the shared in-flight task the
    # interrupt path started; the head clip transcribes exactly once.
    assert Counter(calls) == Counter([voice.media_urls[0], head.media_urls[0]])
    delivered = [call.args[1] for call in adapter.send.await_args_list]
    assert Counter(delivered) == Counter(['🎙️ "shared clip words"', '🎙️ "shared clip words"'])
    # Warm cache for the follow-up turn: both transcripts join the turn input, no new STT.
    message_text = await runner._prepare_profile_scoped_inbound_message_text(
        event=head, source=source, history=[], session_key="conversation")
    assert Counter(calls) == Counter([voice.media_urls[0], head.media_urls[0]])
    assert message_text.count("shared clip words") == 2
    assert adapter.send.await_count == 2  # the two echoes only; warm prep added none


@pytest.mark.asyncio
async def test_cold_inbound_voice_without_background_state(runner_env, monkeypatch, tmp_path):
    """Cold-path safety: an ordinary FIRST inbound voice event (no background STT task, no clip
    state — the scheduled-background wrapper has never run) must transcribe exactly once through
    the canonical entry point, hand the model a real string, and keep the caller-side transcript
    echo (the clip ledger makes it exactly-once). RED before the fixes: the prep indexed clip
    state that did not exist (KeyError); after the entry-point repair the echo was lost
    (send.await_count 0 — the transcript reached the model but never the chat).
    """
    runner, adapter = runner_env
    _unused_event, calls = _instant_stt(monkeypatch, "cold clip words")

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm")
    voice = _voice_event(source, _clip_file(tmp_path))
    voice.text = "please check this"  # caption on an ordinary cold voice note
    # No _gateway_pending_stt_* attrs, no clip entries: fully cold event.

    message_text = await asyncio.wait_for(
        runner._prepare_profile_scoped_inbound_message_text(
            event=voice, source=source, history=[], session_key="conversation"), 5)
    assert isinstance(message_text, str)  # string contract on the cold path
    assert "cold clip words" in message_text
    assert "please check this" in message_text  # caption preserved beside the transcript
    assert Counter(calls) == Counter([voice.media_urls[0]])
    # Cold inbound echoes once: the user can verify STT quality exactly as the base
    # inbound pipeline always did.
    delivered = [call.args[1] for call in adapter.send.await_args_list]
    assert delivered == ['🎙️ "cold clip words"']


@pytest.mark.asyncio
async def test_followup_prep_joins_inflight_clip_returns_string_once(runner_env, monkeypatch, tmp_path):
    """A follow-up turn's input preparation arriving while the clip's shared STT task is still
    IN FLIGHT (no completed cache yet) must join that task via the canonical entry point,
    publish the cache, and return the exact transcript STRING — never the raw
    (text, transcripts) tuple the wrapper returns — and the transcript must echo exactly once
    across the prep claim and the background claim. The background work is started through the
    REAL receipt-path wrapper (``_start_pending_voice_stt``) against an event-controlled provider,
    so the single provider invocation with two concurrent consumers is proven, not assumed.
    RED on base: the prep had no STT join at all (empty text); RED mid-history: tuple assignment
    broke the string contract.
    """
    runner, adapter = runner_env
    entered, release, calls = _blocking_stt(monkeypatch, "early join words")

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm")
    voice = _voice_event(source, _clip_file(tmp_path))
    # The REAL receipt path starts the shared per-clip work: the provider is genuinely invoked
    # (and blocks on the event) before the follow-up turn reaches input preparation.
    runner._start_pending_voice_stt(voice, adapter, source, log_context="Voice-busy-interrupt")
    await asyncio.wait_for(asyncio.to_thread(entered.wait), 5)

    # The follow-up turn's preparation arrives while that STT is still unresolved: it must join
    # the SAME shared task (one provider call), never restart the clip.
    prep = asyncio.ensure_future(
        runner._prepare_profile_scoped_inbound_message_text(
            event=voice, source=source, history=[], session_key="conversation"))
    await asyncio.sleep(0)  # let prep reach the shared-task await
    assert not prep.done()
    release.set()
    message_text = await asyncio.wait_for(prep, 5)
    await asyncio.gather(*adapter._background_tasks)
    # The regression: assert the STRING contract, not just "contains".
    assert isinstance(message_text, str)
    assert message_text.startswith('"early join words"')
    # ONE actual provider call with two concurrent consumers (background claim + prep join).
    assert Counter(calls) == Counter([voice.media_urls[0]])
    # Cache published by the shared work: the two claims cannot double-echo.
    assert getattr(voice, "_gateway_pending_stt_text", None) == message_text
    delivered = [call.args[1] for call in adapter.send.await_args_list]
    assert delivered == ['🎙️ "early join words"']
    # Cleanup ownership: the background wrapper finished and left no tracked task behind.
    assert not adapter._background_tasks


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
