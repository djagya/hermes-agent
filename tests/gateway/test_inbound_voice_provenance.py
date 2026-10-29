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
