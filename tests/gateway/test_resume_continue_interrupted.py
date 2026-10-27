"""Startup auto-resume of a Telegram turn cut off by a restart drain timeout.

Regression (fork, 2026-10-21/22 deploys): in a shared Telegram topic the empty internal resume event got
the sender prefix ("[Danil] "), so the resume path saw a NEW non-empty message, told the model to address
it, and the model answered "the message arrived empty". The resumed turn must instead carry the
interrupted request and continue it (``gateway.resume_interrupted: continue``, the fork default).
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionEntry, SessionSource, build_session_key
from gateway.turn_context import TurnContext

REQUEST = "[Danil] Draft the admin review pricing decision note"
HISTORY = [
    {"role": "user", "content": REQUEST},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
     "function": {"name": "write_file", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": '{"bytes_written": 8265}'},
    {"role": "assistant", "content": "Operation interrupted: waiting for model response (34.6s elapsed)."},
]


def _runner(policy=None) -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    kwargs = {} if policy is None else {"resume_interrupted": policy}
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")}, **kwargs)
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    runner._decide_image_input_mode = lambda **_: "native"
    # Telegram adapter: an interactive platform.
    runner._delivery_adapter_for = lambda _source: SimpleNamespace(interactive_resume=True)
    return runner


def _topic_source() -> SessionSource:
    # Forum topic shared across participants (thread_sessions_per_user=False) -> sender attribution on.
    return SessionSource(platform=Platform.TELEGRAM, chat_id="-100123", chat_type="group",
                         thread_id="17", user_id="42", user_name="Danil")


async def _resume_turn(runner, source):
    """Run the startup auto-resume event through inbound prep + turn-message prep."""
    key = build_session_key(source)
    now = datetime.now()
    entry = SessionEntry(session_key=key, session_id="sid", created_at=now, updated_at=now,
                         resume_pending=True, resume_reason="shutdown_timeout", last_resume_marked_at=now)
    runner.session_store = SimpleNamespace(_entries={key: entry})
    event = MessageEvent(text="", message_type=MessageType.TEXT, source=source, internal=True)
    text = await runner._prepare_inbound_message_text(event=event, source=source, history=HISTORY, session_key=key)
    ctx = TurnContext(source=source, message=text, history=HISTORY, session_key=key)
    persisted, _ts = TurnRunner(runner, ctx)._prepare_turn_message([])
    return text, ctx.message, persisted


@pytest.mark.asyncio
async def test_interrupted_telegram_turn_resumes_original_request():
    text, message, persisted = await _resume_turn(_runner(), _topic_source())

    assert text == ""  # no "[Danil] " attribution on a sender-less internal event
    assert REQUEST in message
    assert "CONTINUE the interrupted task" in message
    assert "NEW message" not in message
    assert "ask what they would like to do next" not in message
    assert persisted.strip() and persisted.strip() != "[Danil]"
    assert not persisted.startswith("[Danil]")


@pytest.mark.asyncio
async def test_ask_policy_keeps_upstream_restore_and_ask():
    _text, message, _persisted = await _resume_turn(_runner("ask"), _topic_source())

    assert "ask what they would like to do next" in message
    assert REQUEST not in message


@pytest.mark.asyncio
async def test_real_user_text_still_attributed_and_addressed_first():
    runner = _runner()
    source = _topic_source()
    event = MessageEvent(text="status?", message_type=MessageType.TEXT, source=source)
    text = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    assert text == "[Danil] status?"


def test_config_parses_resume_interrupted():
    assert GatewayConfig.from_dict({}).resume_interrupted == "continue"
    assert GatewayConfig.from_dict({"gateway": {"resume_interrupted": "ASK"}}).resume_interrupted == "ask"
    assert GatewayConfig.from_dict({"resume_interrupted": "bogus"}).resume_interrupted == "continue"
