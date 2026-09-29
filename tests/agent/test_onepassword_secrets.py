"""Hermetic tests for the 1Password (`op` CLI) secret source.

We never invoke the real ``op`` binary: ``subprocess.run`` is mocked so the
suite stays fast and offline-safe.  A live resolve is exercised manually via
``hermes secrets onepassword sync`` outside of pytest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest


# Make the worktree importable without depending on the installed wheel.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.secret_sources import onepassword as op  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_caches():
    op._reset_cache_for_tests()
    yield
    op._reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _clean_op_env(monkeypatch):
    """Start every test from a known 1Password auth state."""
    for key in list(os.environ):
        if key.startswith("OP_SESSION_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("OP_ACCOUNT", raising=False)
    monkeypatch.delenv("OP_CONNECT_HOST", raising=False)
    monkeypatch.delenv("OP_CONNECT_TOKEN", raising=False)
    yield


def _ok(value: str):
    return mock.Mock(returncode=0, stdout=value, stderr="")


def _err(code: int, stderr: str):
    return mock.Mock(returncode=code, stdout="", stderr=stderr)


# ---------------------------------------------------------------------------
# Reference validation
# ---------------------------------------------------------------------------


def test_validate_references_filters_bad_names_and_refs():
    refs = {
        "OPENAI_API_KEY": "op://Private/OpenAI/api key",
        "1BAD_NAME": "op://Private/x/y",          # bad env name
        "HAS SPACE": "op://Private/x/y",          # bad env name
        "NOT_A_REF": "https://example.com",        # not op://
        "WHITESPACE": "  op://Private/z/field  ",  # stripped + kept
    }
    valid, warnings = op._validate_references(refs)
    assert valid == {
        "OPENAI_API_KEY": "op://Private/OpenAI/api key",
        "WHITESPACE": "op://Private/z/field",
    }
    assert len(warnings) == 3


# ---------------------------------------------------------------------------
# fetch_onepassword_secrets
# ---------------------------------------------------------------------------


def test_fetch_happy_path(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    values = {
        "op://Private/OpenAI/api key": "sk-abc\n",
        "op://Private/Anthropic/credential": "sk-ant-xyz",
    }

    def fake_run(cmd, **kwargs):
        # argv list, never shell=True; reference passed after `--`.
        assert "--" in cmd
        ref = cmd[cmd.index("--") + 1]
        return _ok(values[ref])

    monkeypatch.setattr(op.subprocess, "run", fake_run)

    secrets, warnings = op.fetch_onepassword_secrets(
        references={
            "OPENAI_API_KEY": "op://Private/OpenAI/api key",
            "ANTHROPIC_API_KEY": "op://Private/Anthropic/credential",
        },
        binary=fake_op,
        use_cache=False,
    )
    assert secrets == {"OPENAI_API_KEY": "sk-abc", "ANTHROPIC_API_KEY": "sk-ant-xyz"}
    assert warnings == []






def test_fetch_read_failure_becomes_warning(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(
        op.subprocess, "run", lambda *a, **k: _err(1, "\x1b[31m[ERROR] not signed in\x1b[0m")
    )

    secrets, warnings = op.fetch_onepassword_secrets(
        references={"K": "op://V/I/F"}, binary=fake_op, use_cache=False
    )
    assert secrets == {}
    assert len(warnings) == 1
    # ANSI control sequences are fully scrubbed from the surfaced message.
    assert "\x1b" not in warnings[0]
    assert "[31m" not in warnings[0]
    assert "not signed in" in warnings[0]










# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_inprocess_cache_hit(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _ok("v")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op._reset_cache_for_tests(tmp_path)
    for _ in range(2):
        op.fetch_onepassword_secrets(
            references={"K": "op://V/I/F"}, cache_ttl_seconds=60,
            binary=fake_op, home_path=tmp_path,
        )
    assert calls["n"] == 1  # second call served from L1 cache








def test_connect_credential_change_invalidates_cache(monkeypatch, tmp_path):
    """A different 1Password Connect identity must not reuse a cached value."""
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _ok("v")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op._reset_cache_for_tests(tmp_path)

    monkeypatch.setenv("OP_CONNECT_HOST", "https://connect.example.com")
    monkeypatch.setenv("OP_CONNECT_TOKEN", "tokenA")
    op.fetch_onepassword_secrets(
        references={"K": "op://V/I/F"}, cache_ttl_seconds=300,
        binary=fake_op, home_path=tmp_path,
    )
    # Rotate the Connect token → new identity.
    monkeypatch.setenv("OP_CONNECT_TOKEN", "tokenB")
    op._CACHE.clear()
    op.fetch_onepassword_secrets(
        references={"K": "op://V/I/F"}, cache_ttl_seconds=300,
        binary=fake_op, home_path=tmp_path,
    )
    assert calls["n"] == 2  # cache key changed → refetch






# ---------------------------------------------------------------------------
# find_op
# ---------------------------------------------------------------------------


def test_find_op_pinned_path_not_on_path(tmp_path, monkeypatch):
    pinned = tmp_path / "op"
    pinned.write_text("")
    pinned.chmod(0o755)
    # PATH lookup must NOT be consulted when a binary_path is pinned.
    monkeypatch.setattr(op.shutil, "which", lambda name: "/usr/bin/op")
    assert op.find_op(str(pinned)) == pinned




def test_op_child_env_forwards_config_directory(monkeypatch):
    """The op child must retain an explicit 1Password config location."""
    monkeypatch.setenv("OP_CONFIG_DIR", "/tmp/op-config")
    monkeypatch.setenv("UNRELATED_PROVIDER_TOKEN", "must-not-leak")

    env = op._op_child_env("")

    assert env["OP_CONFIG_DIR"] == "/tmp/op-config"
    assert "UNRELATED_PROVIDER_TOKEN" not in env


# ---------------------------------------------------------------------------
# apply_onepassword_secrets
# ---------------------------------------------------------------------------


def test_apply_disabled_returns_empty():
    result = op.apply_onepassword_secrets(enabled=False, env={"K": "op://V/I/F"})
    assert result.ok
    assert not result.applied


def test_apply_missing_binary_sets_error(monkeypatch):
    monkeypatch.setattr(op, "find_op", lambda binary_path="": None)
    result = op.apply_onepassword_secrets(
        enabled=True, env={"K": "op://V/I/F"}
    )
    assert not result.ok
    assert "op CLI" in result.error


def test_apply_sets_env(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _ok("resolved-val"))
    monkeypatch.delenv("MY_OP_KEY", raising=False)

    result = op.apply_onepassword_secrets(
        enabled=True, env={"MY_OP_KEY": "op://V/I/F"}, cache_ttl_seconds=0,
    )
    assert result.ok
    assert result.applied == ["MY_OP_KEY"]
    assert os.environ["MY_OP_KEY"] == "resolved-val"


def test_apply_skips_before_fetch_when_not_overriding(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setenv("MY_OP_KEY", "from-env")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _ok("from-1password")

    monkeypatch.setattr(op.subprocess, "run", fake_run)

    result = op.apply_onepassword_secrets(
        enabled=True, env={"MY_OP_KEY": "op://V/I/F"},
        override_existing=False, cache_ttl_seconds=0,
    )
    assert "MY_OP_KEY" in result.skipped
    assert os.environ["MY_OP_KEY"] == "from-env"
    assert calls["n"] == 0  # never even called op for a value we'd discard


def test_apply_never_overrides_token_var(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "original")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _ok("malicious")

    monkeypatch.setattr(op.subprocess, "run", fake_run)

    result = op.apply_onepassword_secrets(
        enabled=True,
        env={"OP_SERVICE_ACCOUNT_TOKEN": "op://V/I/F"},
        override_existing=True, cache_ttl_seconds=0,
    )
    assert "OP_SERVICE_ACCOUNT_TOKEN" in result.skipped
    assert os.environ["OP_SERVICE_ACCOUNT_TOKEN"] == "original"
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# Rate limiting: one item fetch per item, stop on 429, back off, serve last good
# ---------------------------------------------------------------------------

_RATE_LIMITED = ("[ERROR] 2026/09/29 10:13:18 could not read secret: Too many requests. "
                 "Your client has been rate-limited. Try again in 23 hours and 59 minutes")


def _item_json(fields):
    return json.dumps({"id": "abc", "title": "Runtime", "fields": fields})


def _age_disk_cache(home, seconds=3600):
    """Push the on-disk pull past its TTL and drop L1, as a later process would see it."""
    path = home / "cache" / "op_cache.json"
    payload = json.loads(path.read_text())
    payload["fetched_at"] -= seconds
    path.write_text(json.dumps(payload))
    op._CACHE.clear()


def _recorder(handler):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return handler(cmd)

    return calls, fake_run


def test_refs_into_one_item_cost_one_item_get(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    item = _item_json([
        {"id": "a1", "label": "API_KEY", "value": "key-a"},
        {"id": "b2", "label": "TOKEN", "value": "tok-b", "section": {"id": "s", "label": "Bot"}},
        {"id": "c3", "label": "TOKEN", "value": "tok-c", "section": {"id": "t", "label": "Other"}},
    ])

    def handler(cmd):
        if cmd[1:3] == ["item", "get"]:
            assert cmd[cmd.index("--") + 1] == "Runtime" and cmd[cmd.index("--vault") + 1] == "Vault"
            return _ok(item)
        return _ok({"op://Other/Solo/f": "solo"}[cmd[cmd.index("--") + 1]])

    calls, fake_run = _recorder(handler)
    monkeypatch.setattr(op.subprocess, "run", fake_run)

    secrets, warnings = op.fetch_onepassword_secrets(
        references={"A": "op://Vault/Runtime/API_KEY", "B": "op://Vault/Runtime/Bot/TOKEN",
                    "C": "op://Vault/Runtime/c3", "S": "op://Other/Solo/f"},
        binary=fake_op, use_cache=False)

    assert secrets == {"A": "key-a", "B": "tok-b", "C": "tok-c", "S": "solo"}
    assert warnings == []
    assert len(calls) == 2  # one item get for three refs + one op read for the lone ref


def test_ambiguous_field_falls_back_to_op_read(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    item = _item_json([{"id": "x", "label": "TOKEN", "value": "one"},
                       {"id": "y", "label": "TOKEN", "value": "two"},
                       {"id": "z", "label": "OK", "value": "fine"}])

    def handler(cmd):
        if cmd[1:3] == ["item", "get"]:
            return _ok(item)
        assert cmd[cmd.index("--") + 1] == "op://V/I/TOKEN"
        return _ok("what-op-read-says")

    calls, fake_run = _recorder(handler)
    monkeypatch.setattr(op.subprocess, "run", fake_run)

    secrets, _ = op.fetch_onepassword_secrets(
        references={"T": "op://V/I/TOKEN", "O": "op://V/I/OK"}, binary=fake_op, use_cache=False)
    assert secrets == {"T": "what-op-read-says", "O": "fine"}
    assert len(calls) == 2


def test_rate_limit_stops_pull_and_backs_off_across_processes(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    calls, fake_run = _recorder(lambda cmd: _err(1, _RATE_LIMITED))
    monkeypatch.setattr(op.subprocess, "run", fake_run)
    refs = {f"K{i}": f"op://V/Item{i}/f" for i in range(10)}

    secrets, warnings = op.fetch_onepassword_secrets(
        references=refs, binary=fake_op, cache_ttl_seconds=300, home_path=tmp_path)
    assert secrets == {}
    assert len(calls) == 1  # stopped at the first throttled call, not ten
    assert any("10 reference(s) not fetched" in w for w in warnings)

    marker = json.loads((tmp_path / "cache" / "op_rate_limit.json").read_text())
    assert 23 * 3600 < marker["until"] - time.time() <= 24 * 3600  # honours op's retry hint

    op._CACHE.clear()  # a new process: only the disk marker survives
    _, warnings = op.fetch_onepassword_secrets(
        references=refs, binary=fake_op, cache_ttl_seconds=300, home_path=tmp_path)
    assert len(calls) == 1  # backoff active → op not called at all
    assert any("not calling op until" in w for w in warnings)

    op.clear_caches(tmp_path)  # token rotation drops the marker
    assert not (tmp_path / "cache" / "op_rate_limit.json").exists()


def test_rate_limit_serves_last_complete_pull(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    refs = {"A": "op://V/One/f", "B": "op://V/Two/f"}
    monkeypatch.setattr(op.subprocess, "run", lambda cmd, **k: _ok("v-" + cmd[cmd.index("--") + 1][7:10]))
    good, _ = op.fetch_onepassword_secrets(references=refs, binary=fake_op,
                                           cache_ttl_seconds=60, home_path=tmp_path)
    assert good == {"A": "v-One", "B": "v-Two"}

    _age_disk_cache(tmp_path)  # fresh TTL expired; the disk entry is now only a last-good copy
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _err(1, _RATE_LIMITED))
    secrets, warnings = op.fetch_onepassword_secrets(references=refs, binary=fake_op,
                                                     cache_ttl_seconds=60, home_path=tmp_path)
    assert secrets == good
    assert any("last complete pull" in w for w in warnings)


def test_auth_failure_never_serves_last_good(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    refs = {"A": "op://V/One/f"}
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _ok("old"))
    op.fetch_onepassword_secrets(references=refs, binary=fake_op, cache_ttl_seconds=60, home_path=tmp_path)

    _age_disk_cache(tmp_path)
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _err(1, "[ERROR] unauthorized"))
    secrets, _ = op.fetch_onepassword_secrets(references=refs, binary=fake_op,
                                              cache_ttl_seconds=60, home_path=tmp_path)
    assert secrets == {}
    assert not (tmp_path / "cache" / "op_rate_limit.json").exists()


def test_unauthorized_item_get_skips_field_reads(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    calls, fake_run = _recorder(lambda cmd: _err(1, "[ERROR] unauthorized: invalid token"))
    monkeypatch.setattr(op.subprocess, "run", fake_run)
    secrets, warnings = op.fetch_onepassword_secrets(
        references={f"K{i}": f"op://V/Runtime/F{i}" for i in range(5)}, binary=fake_op, use_cache=False)
    assert secrets == {} and len(warnings) == 1
    assert len(calls) == 1


def test_rate_limited_item_get_skips_field_reads(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    calls, fake_run = _recorder(lambda cmd: _err(1, _RATE_LIMITED))
    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op.fetch_onepassword_secrets(
        references={f"K{i}": f"op://V/Runtime/F{i}" for i in range(22)},
        binary=fake_op, cache_ttl_seconds=300, home_path=tmp_path)
    assert len(calls) == 1 and calls[0][1:3] == ["item", "get"]


@pytest.mark.parametrize("message, seconds", [
    ("Too many requests. Try again in 23 hours and 59 minutes", 23 * 3600 + 59 * 60),
    ("rate-limited, retry in about 59 minutes", 59 * 60),
    ("Too many requests. Your client has been rate-limited.", op._DEFAULT_BACKOFF_SECONDS),
    ("Too many requests. Try again in 40 hours", op._MAX_BACKOFF_SECONDS),
])
def test_backoff_follows_op_retry_hint(message, seconds):
    assert op._backoff_seconds(message) == seconds
    assert op._classify_op_error(message) is op.ErrorKind.RATE_LIMITED




