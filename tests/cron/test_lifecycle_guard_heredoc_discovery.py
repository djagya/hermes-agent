"""Lifecycle guard: heredoc-masked reference discovery + typed scan verdicts.

Regression for the false-positive class where a data path appearing inside a
provably inert quoted-heredoc body was discovered as an executed script and an
oversized data file then surfaced as a gateway-restart refusal:

- a >1 MiB JSON path merely printed (or read as data) by a ``python3 - <<'EOF'``
  heredoc must NOT be blocked (discovery runs on the heredoc-masked text);
- every reference discovered OUTSIDE such bodies keeps its prior treatment:
  scanned regardless of suffix, contents, or size — ``bash catalog.json``,
  ``sh something.json``, and real referenced scripts stay covered;
- oversized/indeterminate references still fail closed, but the typed verdict
  and the cron refusal message must not claim a lifecycle command was observed.

Synthetic fixtures only. No pytest here touches the live filesystem outside
``tmp_path``.
"""

from __future__ import annotations

import json

import pytest

import cron.lifecycle_guard as lifecycle_guard

guard = lifecycle_guard.contains_gateway_lifecycle_command_or_referenced_script
classify = lifecycle_guard.classify_gateway_lifecycle_scan

LIFECYCLE = "hermes gateway restart"


@pytest.fixture
def big_json(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"items": [{"id": i, "pad": "x" * 40} for i in range(30000)]}),
                    encoding="utf-8")
    assert path.stat().st_size > lifecycle_guard._MAX_REFERENCED_SCRIPT_BYTES
    return path


def _python_heredoc(body: str) -> str:
    return f"python3 - <<'EOF'\n{body}\nEOF"


# --- the false-positive class is fixed ---------------------------------------


def test_print_of_big_json_path_in_python_heredoc_allowed(big_json):
    assert guard(_python_heredoc(f"print('{big_json}')")) is False


def test_read_of_big_json_in_python_heredoc_allowed(big_json):
    assert guard(_python_heredoc(f"import json; json.load(open('{big_json}'))")) is False


def test_small_json_path_in_python_heredoc_allowed(tmp_path):
    small = tmp_path / "small.json"
    small.write_text('{"a": [1, 2, 3]}', encoding="utf-8")
    assert guard(_python_heredoc(f"print('{small}')")) is False


def test_classify_reports_clean_for_heredoc_data_path(big_json):
    assert classify(_python_heredoc(f"print('{big_json}')")).kind == "clean"


# --- executable references keep their prior treatment ------------------------


def test_bash_on_big_json_still_blocked(big_json):
    """A referenced file is scanned/fail-closed regardless of extension."""
    assert guard(f"bash {big_json}") is True


def test_shell_script_named_json_with_lifecycle_still_blocked(tmp_path):
    tricky = tmp_path / "payload.json"
    tricky.write_text(LIFECYCLE, encoding="utf-8")
    assert guard(f"sh {tricky}") is True


def test_referenced_shell_script_with_lifecycle_still_blocked(tmp_path):
    script = tmp_path / "restart.sh"
    script.write_text(f"#!/bin/sh\n{LIFECYCLE}\n", encoding="utf-8")
    assert guard(f"bash {script}") is True


def test_unquoted_heredoc_payload_still_blocked():
    """An unquoted delimiter means the shell expands the body — never masked."""
    assert guard('python3 - <<EOF\nos.system("launchctl bootout gui/501/ai.hermes.gateway")\nEOF') is True


def test_direct_lifecycle_command_still_blocked():
    assert guard(LIFECYCLE) is True


def test_launchctl_submit_still_blocked():
    assert guard("launchctl submit -l helper -- /bin/helper.sh") is True


def test_runbook_text_in_inert_cat_heredoc_allowed():
    assert guard("cat > /tmp/notes.txt <<'EOF'\na human can run: hermes gateway restart\nEOF") is False


def test_data_sink_over_big_json_allowed(big_json):
    assert guard(f"grep foo {big_json}") is False


def test_ambiguous_unterminated_heredoc_fails_open():
    """The stripper fails open: an unterminated heredoc body stays visible to discovery."""
    command = "python3 - <<'EOF'\nprint('x')\nlaunchctl bootout gui/501/ai.hermes.gateway"
    assert guard(command) is True


def test_known_limitation_quoted_interpreter_heredoc_body_not_shell_scanned():
    """Honest record of a pre-existing limitation the discovery fix preserves: a quoted
    interpreter-heredoc body is treated as data for the SHELL scan, so shell-shaped
    lifecycle text inside it is not detected here. Real Python payloads are covered by
    execute_code's own ``contains_gateway_lifecycle_command`` gate on the code itself.
    Do not weaken the stripper to change this without re-proving the runbook cases."""
    body = "os.system('launchctl bootout gui/501/ai.hermes.gateway')"
    assert guard(_python_heredoc(body)) is False


# --- typed verdicts and honest diagnostics ------------------------------------


def test_classify_reports_inconclusive_for_oversized_reference(tmp_path):
    huge = tmp_path / "huge.bin"
    huge.write_bytes(b"echo ok\n" + b"x" * (lifecycle_guard._MAX_REFERENCED_SCRIPT_BYTES + 100))
    verdict = classify(f"bash {huge}")
    assert verdict.kind == "inconclusive_oversize"
    assert verdict.blocked is True


def test_bool_wrapper_equals_verdict_blocked():
    for command in (LIFECYCLE, "echo ok", "launchctl submit -l x -- /bin/x.sh"):
        assert guard(command) is classify(command).blocked


def test_inconclusive_refusal_message_does_not_claim_lifecycle(tmp_path):
    huge = tmp_path / "huge.bin"
    huge.write_bytes(b"echo ok\n" + b"x" * (lifecycle_guard._MAX_REFERENCED_SCRIPT_BYTES + 100))
    with pytest.raises(lifecycle_guard.GatewayLifecycleBlocked) as excinfo:
        lifecycle_guard.check_gateway_lifecycle(f"bash {huge}")
    message = str(excinfo.value)
    assert "could not be scanned" in message
    assert "no lifecycle command was found" in message
    assert "gateway lifecycle command" not in message


def test_observed_lifecycle_refusal_message_keeps_meaning():
    with pytest.raises(lifecycle_guard.GatewayLifecycleBlocked) as excinfo:
        lifecycle_guard.check_gateway_lifecycle(LIFECYCLE)
    assert "gateway lifecycle command" in str(excinfo.value)


def test_budget_exhaustion_keeps_failing_closed(monkeypatch):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_BYTES", 4)
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINE_BYTES", 4)
    verdict = classify("echo hello")
    assert verdict.blocked is True
    assert verdict.kind == "inconclusive_budget"
