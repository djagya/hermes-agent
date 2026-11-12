"""openai-codex bare ``{"detail": "Bad Request"}`` replay rejection -> invalid_encrypted_content."""

import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from tests.agent.test_error_classifier import MockAPIError


@pytest.mark.parametrize(("provider", "body", "status", "msg", "expected"), [
    ("openai-codex", {"detail": "Bad Request"}, 400, "Error code: 400 - {'detail': 'Bad Request'}",
     FailoverReason.invalid_encrypted_content),
    ("openai-codex", {"detail": " bad request "}, 400, "Error code: 400 - {'detail': ' bad request '}",
     FailoverReason.invalid_encrypted_content),  # case-insensitive, stripped
    ("openai-codex", None, 400, "Error code: 400 - {'detail': 'Bad Request'}",
     FailoverReason.invalid_encrypted_content),  # body-less string form
    ("openai-codex", None, None, 'HTTP error: {"detail": "Bad Request"}',
     FailoverReason.invalid_encrypted_content),  # status-less stream form
    ("openai", {"detail": "Bad Request"}, 400, "Error code: 400 - {'detail': 'Bad Request'}",
     FailoverReason.format_error),  # elsewhere unchanged
    ("openai-codex", {"detail": "Bad Request: missing field"}, 400, "Error code: 400 - {'detail': 'Bad Request: missing field'}",
     FailoverReason.format_error),  # exact detail only
    ("openai-codex", None, 400, "Error code: 400 - bad request detail unknown", FailoverReason.format_error),
], ids=["codex-dict-body", "codex-case-strip", "codex-message-only", "codex-statusless", "other-provider",
        "other-detail", "loose-message"])
def test_codex_bad_request_detail_reaches_replay_strip(self, provider, body, status, msg, expected):
    """The ChatGPT Codex backend now rejects a replayed sol-minted reasoning item as a bare
    ``{"detail": "Bad Request"}`` 400. Same hints as the other masked replay rejections."""
    e = MockAPIError(msg, status_code=status, body=body)
    result = classify_api_error(e, provider=provider, model="gpt-5.5")
    assert result.reason == expected
    assert result.retryable is False and result.should_fallback is True

def test_codex_bad_request_detail_other_status_stays_format(self):
    e = MockAPIError("Error code: 422 - {'detail': 'Bad Request'}", status_code=422, body={"detail": "Bad Request"})
    assert classify_api_error(e, provider="openai-codex", model="gpt-5.5").reason != FailoverReason.invalid_encrypted_content
