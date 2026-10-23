"""1Password failures reach the model typed, with advice; item metadata is cached briefly, never secrets."""

import json
import subprocess
from unittest.mock import patch

import pytest

from agent.vault_backends import BackendAuthExpired, BackendNetwork, BackendRateLimited, ItemNotFound, UnlockRequired
from agent.vault_backends import onepassword
from agent.vault_backends.onepassword import OnePasswordLoginBackend


@pytest.fixture(autouse=True)
def _fresh_cache():
    onepassword.clear_list_cache()
    yield
    onepassword.clear_list_cache()


def _backend(service_token=""):
    backend = OnePasswordLoginBackend({"enabled": True})
    backend._service_token = service_token
    return backend


def _fail(stderr):
    return subprocess.CompletedProcess(["op"], 1, "", stderr)


@pytest.mark.parametrize("stderr,service,expected", [
    ("[ERROR] Too many requests. Try again later", "svc", BackendRateLimited),
    ("[ERROR] \"abc\" isn't an item in the \"Hermes\" vault", "svc", ItemNotFound),
    ("[ERROR] invalid token: service account token expired", "svc", BackendAuthExpired),
    ("[ERROR] dial tcp: lookup my.1password.com: no such host", "svc", BackendNetwork),
    ("[ERROR] session expired, sign in to create a new session", "", UnlockRequired),
])
def test_op_failures_are_typed(stderr, service, expected):
    backend = _backend(service)
    with patch.object(onepassword, "find_op", return_value="/bin/op"), \
         patch.object(onepassword._unlock, "get_session_token", return_value="sess"), \
         patch.object(onepassword, "run_cli", return_value=_fail(stderr)):
        with pytest.raises(expected):
            backend._run("item", "get", "x")


def test_typed_error_result_carries_advice_not_cli_text():
    exc = BackendRateLimited("1Password", "[ERROR] Too many requests for account XYZ")
    out = exc.result()
    assert out["error_type"] == "vault_rate_limited" and "hour" in out["error"] and "XYZ" not in out["error"]


def test_timeout_is_a_network_error():
    backend = _backend("svc")
    with patch.object(onepassword, "find_op", return_value="/bin/op"), \
         patch.object(onepassword, "run_cli", side_effect=RuntimeError("op timed out")):
        with pytest.raises(BackendNetwork):
            backend._run("item", "list")


def test_otp_outage_is_raised_but_a_missing_otp_field_is_not():
    backend = _backend("svc")
    with patch.object(backend, "_resolve_handle", return_value=("v", "i")), \
         patch.object(backend, "_run", side_effect=BackendRateLimited("1Password", "too many requests")):
        with pytest.raises(BackendRateLimited):
            backend.resolve_otp("op:v:i")
    with patch.object(onepassword, "find_op", return_value="/bin/op"), \
         patch.object(backend, "_resolve_handle", return_value=("v", "i")), \
         patch.object(onepassword, "run_cli", return_value=_fail("[ERROR] item has no one-time password field")):
        assert backend.resolve_otp("op:v:i") is None


def test_item_list_is_cached_until_lock():
    items = json.dumps([{"id": "a", "title": "Shop", "urls": [{"href": "https://shop.example"}]}])
    backend = _backend("svc")
    with patch.object(OnePasswordLoginBackend, "_run", return_value=items) as run:
        backend.list_items()
        _backend("svc").list_items()  # a fresh backend object (one per tool call) hits the cache
        assert run.call_count == 1
        onepassword._unlock.lock(backend.name)
        _backend("svc").list_items()
        assert run.call_count == 2
