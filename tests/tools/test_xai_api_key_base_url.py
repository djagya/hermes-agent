"""An xAI API key goes only to a validated xAI API origin, never to the subscription proxy or a foreign host.

Every API-key surface (cloud STT, TTS, streaming STT, the Desktop's direct STT config) resolves its base URL
through ``tools.xai_http.xai_api_base_url``: the explicit override (``stt.xai.base_url`` /
``XAI_STT_BASE_URL`` / ``tts.xai.base_url`` / ``XAI_BASE_URL``) wins only when it is an HTTPS ``*.x.ai``
origin. Checked at the transport: the URL the request is actually sent to.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

API = "https://api.x.ai/v1"
CUSTOM = "https://us-east-1.api.x.ai/v1"
OVERRIDES = [
    ("https://cli-chat-proxy.grok.com/v1", API),  # the subscription proxy: an API key never goes there
    ("https://llm.example.net/v1", API),
    ("http://api.x.ai/v1", API),
    ("https://api.x.ai.example.net/v1", API),
    (CUSTOM, CUSTOM),
    ("", API),
]


@pytest.fixture
def api_key_only(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("HERMES_XAI_BASE_URL", "XAI_BASE_URL", "XAI_STT_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XAI_API_KEY", "synthetic-api-key")
    import agent.credential_pool as cp
    monkeypatch.setattr(cp, "load_pool", lambda provider: (_ for _ in ()).throw(RuntimeError("no OAuth")))
    return home


@pytest.mark.parametrize(("override", "expected"), OVERRIDES)
def test_helper(override, expected):
    from tools.xai_http import xai_api_base_url
    assert xai_api_base_url(override) == expected
    assert xai_api_base_url("", override) == expected  # first non-empty candidate decides


def _stt(tmp_path, stt_config):
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"OggS" + b"\0" * 64)
    response = MagicMock(status_code=200)
    response.json.return_value = {"text": "hi", "language": "en", "duration": 1.0}
    with patch("tools.transcription_tools._load_stt_config", return_value=stt_config), \
            patch("requests.post", return_value=response) as post:
        from tools.transcription_tools import _transcribe_xai
        assert _transcribe_xai(str(audio), "grok-stt")["success"] is True
    url = post.call_args[0][0] if post.call_args[0] else post.call_args.kwargs["url"]
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer synthetic-api-key"
    return url


@pytest.mark.parametrize("source", ["config", "XAI_STT_BASE_URL", "XAI_BASE_URL"])
@pytest.mark.parametrize(("override", "expected"), OVERRIDES)
def test_cloud_stt_api_key_route(api_key_only, tmp_path, monkeypatch, source, override, expected):
    config = {"xai": {"base_url": override}} if source == "config" else {}
    if source != "config" and override:
        monkeypatch.setenv(source, override)
    assert _stt(tmp_path, config) == f"{expected}/stt"


@pytest.mark.parametrize("source", ["config", "XAI_BASE_URL"])
@pytest.mark.parametrize(("override", "expected"), OVERRIDES)
def test_tts_api_key_route(api_key_only, tmp_path, monkeypatch, source, override, expected):
    from tools import tts_tool
    seen = {}

    def post(url, **kwargs):
        seen["url"], seen["auth"] = url, kwargs.get("headers", {}).get("Authorization")
        response = MagicMock(content=b"audio")
        response.raise_for_status = MagicMock()
        return response
    if source != "config" and override:
        monkeypatch.setenv(source, override)
    with patch("requests.post", side_effect=post):
        tts_tool._generate_xai_tts("hi", str(tmp_path / "out.mp3"),
                                   {"xai": {"base_url": override}} if source == "config" else {})
    assert seen == {"url": f"{expected}/tts", "auth": "Bearer synthetic-api-key"}


@pytest.mark.parametrize(("override", "expected"), OVERRIDES)
def test_streaming_stt_api_key_route(api_key_only, monkeypatch, override, expected):
    from tools import transcription_streaming as ts
    seen = {}

    class Session:
        def __init__(self, api_key, base_url, *a, **k):
            seen["base"], seen["key"] = base_url, api_key
    monkeypatch.setattr(ts, "XAIStreamingSession", Session)
    ts._builtin_session("xai", {"xai": {"base_url": override}}, None, None, None)
    assert seen == {"base": expected, "key": "synthetic-api-key"}


@pytest.mark.parametrize(("override", "expected"), OVERRIDES)
def test_desktop_direct_stt_config_route(api_key_only, monkeypatch, override, expected):
    from tools import transcription_tools as tt
    from tools import voice_client_config as vc
    monkeypatch.setattr(tt, "_load_stt_config", lambda: {"provider": "xai", "xai": {"base_url": override}})
    monkeypatch.setattr(tt, "is_stt_enabled", lambda cfg: True)
    monkeypatch.setattr(tt, "_get_provider", lambda cfg: "xai")
    monkeypatch.setattr(tt, "_is_local_stt_provider", lambda provider, cfg: False)
    out = vc._resolve_stt_client_config()
    assert out["mode"] == "direct" and out["base_url"] == expected
