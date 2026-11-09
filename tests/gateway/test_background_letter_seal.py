"""/bg seals credential letters before vision (B3): the secret never reaches the model prompt."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent import vault_letter as vl
from gateway.config import Platform
from gateway.session import SessionSource
from tests.agent.test_vault_letter import FIRST, IDENT, SECRET, VERIFY
from tests.gateway.test_background_command import _make_runner


@pytest.fixture(autouse=True)
def _intake(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(vl, "_intake_cfg", lambda: {"enabled": True})
    vl.clear_letters()
    from agent.redact import clear_vault_redaction_values
    clear_vault_redaction_values()
    yield
    vl.clear_letters()


@pytest.mark.asyncio
async def test_bg_letter_photo_is_sealed_before_vision(tmp_path):
    letter, plain = tmp_path / "letter.jpg", tmp_path / "plain.jpg"
    letter.write_bytes(b"jpeg")
    plain.write_bytes(b"jpeg")
    replies = {str(letter): iter([FIRST, VERIFY]), str(plain): iter([{"has_login_secret": False}])}

    async def fake_ask(path, prompt):
        return next(replies[path])

    runner = _make_runner()
    adapter = AsyncMock()
    adapter.extract_media = MagicMock(return_value=([], "done"))
    adapter.extract_images = MagicMock(return_value=([], "done"))
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._enrich_message_with_vision = AsyncMock(side_effect=lambda text, paths: f"{text}\n[described {len(paths)}]")
    source = SessionSource(platform=Platform.TELEGRAM, user_id="12345", chat_id="67890", user_name="testuser")
    agent = MagicMock()
    agent.run_conversation.return_value = {"final_response": "done", "messages": []}

    with patch.object(vl, "_ask_vision", side_effect=fake_ask), \
         patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
         patch("gateway.run._load_gateway_config", return_value={}), \
         patch("run_agent.AIAgent", return_value=agent):
        await runner._run_background_task(
            "fill the form", source, "bg_letter", media_urls=[str(letter), str(plain)],
            media_types=["image/jpeg", "image/jpeg"])

    # Only the ordinary photo reaches vision; the letter's pixels are gone from the cache.
    runner._enrich_message_with_vision.assert_awaited_once()
    assert runner._enrich_message_with_vision.await_args.args[1] == [str(plain)]
    assert not letter.exists() and plain.exists()
    prompt = agent.run_conversation.call_args.kwargs["user_message"]
    assert "letter handle: " in prompt and IDENT in prompt and "[described 1]" in prompt
    assert SECRET not in prompt and SECRET not in json.dumps(adapter.send.call_args_list, default=str)
