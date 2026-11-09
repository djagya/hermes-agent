"""Durable native-voice provenance, independent of transcript/model prose."""
from types import SimpleNamespace

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_turn import GatewayTurnMixin
from gateway.voice_source_metadata import inbound_voice_metadata
from tests.gateway.test_gateway_silence_tokens import _runner, _source


@pytest.mark.parametrize("case", ["single", "duplicate", "multiple", "text", "audio", "missing_id", "empty", "relative", "remote", "mixed"])
def test_voice_metadata_contract(tmp_path, case):
    first, second = [str(tmp_path / name) for name in ("first.ogg", "second.ogg")]
    paths = {
        "duplicate": [first, first], "multiple": [first, second],
        "empty": [], "relative": ["clip.ogg"],
        "remote": ["https://example.invalid/clip.ogg"],
        "mixed": [first, "https://example.invalid/clip.ogg"],
    }.get(case, [first])
    kind = {"text": MessageType.TEXT, "audio": MessageType.AUDIO}.get(case, MessageType.VOICE)
    event = MessageEvent(text="arbitrary transcript", message_type=kind,
                         message_id=None if case == "missing_id" else "fixture-message", media_urls=paths)
    result = inbound_voice_metadata(event)
    if case in {"single", "duplicate", "multiple"}:
        assert result == {"inbound_voice": {
            "schema": 1, "platform_message_id": "fixture-message",
            "audio_paths": [first, second] if case == "multiple" else [first],
        }}
        assert event.media_urls == paths
    else:
        assert result == {}


@pytest.mark.parametrize("owner", [None, "gateway"])
def test_failed_turn_row_retains_voice_and_existing_metadata(tmp_path, owner):
    path = str(tmp_path / "voice.ogg")
    event = MessageEvent(text="model-visible text", message_type=MessageType.VOICE,
                         message_id="fixture-message", media_urls=[path])
    prepared = SimpleNamespace(persist_user_message="clean transcript", message_text="fallback",
                               persist_user_timestamp=1.0, persist_user_display_kind="voice",
                               persistence_owner=owner)
    row = GatewayTurnMixin._hmwa_user_transcript_entry(event, prepared, 2.0)
    assert row["content"] == "clean transcript"
    assert row["timestamp"] == 1.0
    assert row["display_kind"] == "voice"
    assert row["message_id"] == "fixture-message"
    assert row["display_metadata"]["inbound_voice"] == {
        "schema": 1, "platform_message_id": "fixture-message", "audio_paths": [path],
    }
    assert row["display_metadata"].get("gateway_input_owner") == owner


@pytest.mark.asyncio
@pytest.mark.parametrize("followup", [False, True])
async def test_native_voice_reaches_agent_persistence_metadata(followup, monkeypatch, tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr("gateway.run.GatewayRunner._VOICE_MODE_PATH", tmp_path / "gateway_voice_mode.json")
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    runner = _runner(monkeypatch, tmp_path)
    path = str(tmp_path / "voice.ogg")
    event = MessageEvent(text="synthetic transcript", message_type=MessageType.VOICE,
                         source=_source(), message_id="fixture-message", media_urls=[path])
    runner._run_agent = AsyncMock(return_value={
        "final_response": "synthetic response", "messages": [], "tools": [],
        "history_offset": 0, "last_prompt_tokens": 0, "api_calls": 1, "failed": False,
    })
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="synthetic transcript")
    if followup:
        runner._MAX_INTERRUPT_DEPTH = 8
        runner._is_goal_continuation_event = MagicMock(return_value=False)
        runner._session_key_for_source = MagicMock(return_value="fixture-session-key")
        runner._delivery_adapter_for = MagicMock(return_value=None)
        runner._refresh_agent_cache_message_count = AsyncMock()
        turn_ctx = SimpleNamespace(
            source=_source(), session_id="fixture-session", session_key="fixture-session-key",
            run_generation=1, _interrupt_depth=0, history=[], _status_thread_metadata=None,
            context_prompt=None, result_holder=[None])
        await runner._run_agent_queued_followup(
            turn_ctx, adapter=None, pending="synthetic transcript", pending_event=event,
            response="previous response", result={"interrupted": True, "messages": []}, stream_task=None)
    else:
        await runner._handle_message_with_agent(event, _source(), "fixture-session-key", 1)
    metadata = runner._run_agent.await_args.kwargs["persist_user_display_metadata"]
    assert metadata["inbound_voice"] == {
        "schema": 1, "platform_message_id": "fixture-message", "audio_paths": [path],
    }


@pytest.mark.asyncio
async def test_background_voice_row_keeps_provenance_without_reply_anchor(tmp_path):
    """GW-2: /bg forwards the binding from the raw inbound id even when no reply anchor exists."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from gateway.config import Platform
    from tests.gateway.test_background_command import _make_runner

    runner = _make_runner()
    adapter = AsyncMock()
    adapter.extract_media = lambda text: ([], text)
    adapter.extract_images = lambda text: ([], text)
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._reply_anchor_for_event = MagicMock(return_value=None)
    tasks = []
    runner._track_background_task = tasks.append
    path = str(tmp_path / "voice.ogg")
    event = MessageEvent(text="/bg synthetic transcript", message_type=MessageType.VOICE,
                         source=_source(), message_id="fixture-message", media_urls=[path])
    await runner._handle_background_command(event)
    agent = MagicMock()
    agent.run_conversation.return_value = {"final_response": "done", "messages": []}
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
         patch("gateway.run._load_gateway_config", return_value={}), \
         patch("run_agent.AIAgent", return_value=agent):
        await tasks[0]
    # The real persisted row must satisfy the Morning Pages binding rule (hermes/identity
    # morning_pages_ingest/session_source.py:_voice_metadata): the binding names the row's own id.
    from agent.turn_context import build_turn_context
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from tests.gateway.test_restart_drain_recovery_dedup import _MinimalAgent, _build_turn_context_for_test

    kwargs = agent.run_conversation.call_args.kwargs
    ctx = _build_turn_context_for_test(
        build_turn_context, None, user_message=kwargs["user_message"],
        persist_user_display_metadata=kwargs.get("persist_user_display_metadata"),
        persist_user_platform_id=kwargs.get("persist_user_platform_id"))
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = db.create_session("bg-voice", "gateway")
    AIAgent._flush_messages_to_session_db_unlocked(
        _MinimalAgent(db, session_id), [m for m in ctx.messages if m.get("role") == "user"],
        conversation_history=None)
    (row,) = [m for m in db.get_messages(session_id) if m.get("role") == "user"]
    voice = row["display_metadata"]["inbound_voice"]
    assert voice == {"schema": 1, "platform_message_id": "fixture-message", "audio_paths": [path]}
    assert str(row["platform_message_id"]) == voice["platform_message_id"]


def test_voice_binding_survives_carrier_folds_and_restatement():
    """GW-1: a voice request folded into compaction carriers keeps its binding, and the plain
    restatement rebuilt from a merged carrier gets it back with the platform id it names."""
    from agent.context_compressor import (
        _INFLIGHT_TASK_REPLAY_HEADER, _SUMMARY_END_MARKER, COMPRESSED_SUMMARY_METADATA_KEY,
        SUMMARY_PREFIX, ContextCompressor)
    from tests.agent.test_cron_inflight_prompt_reappend import (
        JOB_SENTINEL, _compress_with, _make_compressor, _tool_pairs)

    voice = {"schema": 1, "platform_message_id": "777", "audio_paths": ["/abs/voice.ogg"]}
    out = [{"role": "user", "content": JOB_SENTINEL, "message_uid": "voice-uid",
            "platform_message_id": "777", "display_metadata": {"inbound_voice": voice}}, *_tool_pairs(40)]
    for cycle in (1, 2):  # voice row -> carrier, then carrier -> carrier
        out = _compress_with(2, cycle, out + (_tool_pairs(40, start=100) if cycle > 1 else []))
        (carrier,) = [m for m in out if ContextCompressor._has_merged_inflight_replay(m)]
        assert carrier["display_metadata"]["compaction_inflight_inbound_voice"] == voice, cycle
        assert "platform_message_id" not in carrier

    summary = {"role": "assistant", COMPRESSED_SUMMARY_METADATA_KEY: True,
               "content": SUMMARY_PREFIX + "\n## Summary\nran steps.\n\n" + _SUMMARY_END_MARKER}
    compressed = [{"role": "system", "content": "You are Hermes."}, summary, *_tool_pairs(1)]
    restated = _make_compressor()._reappend_inflight_user_task(compressed, carrier)[-1]
    assert _INFLIGHT_TASK_REPLAY_HEADER in restated["content"] and JOB_SENTINEL in restated["content"]
    assert restated["display_metadata"]["inbound_voice"] == voice
    assert restated["platform_message_id"] == "777"


def test_gateway_replay_keeps_voice_provenance():
    """GW-1: replay conversion keeps the binding (durable-only), merged with scaffolding flags."""
    from agent.message_metadata import INFLIGHT_TASK_RESTATEMENT
    from gateway.run import _build_gateway_agent_history

    voice = {"schema": 1, "platform_message_id": "777", "audio_paths": ["/abs/voice.ogg"]}
    stored = [
        {"role": "user", "content": "voice transcript", "platform_message_id": "777",
         "display_metadata": {"inbound_voice": voice, "unrelated": 1}},
        {"role": "assistant", "content": "carrier", "_compressed_summary": True,
         "display_metadata": {"compaction_inflight_inbound_voice": voice}},
        {"role": "user", "content": "restated", "platform_message_id": "777",
         "display_metadata": {"inbound_voice": voice, INFLIGHT_TASK_RESTATEMENT: True}},
        {"role": "assistant", "content": "plain", "platform_message_id": "778"},
    ]
    history, _ = _build_gateway_agent_history(stored)
    assert history[0]["display_metadata"] == {"inbound_voice": voice}
    assert history[0]["platform_message_id"] == "777"
    assert history[1]["display_metadata"] == {"compaction_inflight_inbound_voice": voice}
    assert "platform_message_id" not in history[1]
    assert history[2]["display_metadata"] == {INFLIGHT_TASK_RESTATEMENT: True, "inbound_voice": voice}
    assert "display_metadata" not in history[3] and "platform_message_id" not in history[3]
