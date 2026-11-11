"""Vault-value registry redaction: digit values leave no oracle; short-lived codes expire (agent.redact_vault)."""


class TestVaultDigitValuesLeaveNoOracle:
    """r8 (owner policy: card values never reach the model): a pure-digit vault value is masked by length,
    never where it matches, so printing candidates tells nothing; a payment window closes on release."""

    CANDIDATES = " ".join(str(n) for n in range(730, 745)) + "; pin 4711, code 042172"

    def test_same_output_whatever_the_registered_digits_are(self):
        from agent import redact

        outs = []
        try:
            for cvc, pin in (("737", "4711"), ("738", "4712")):
                redact.clear_vault_redaction_values()
                redact.register_card_window("shopping", digits=("4000056655665556", cvc), words=("Li",))
                redact.register_vault_redaction_value(pin)
                redact.register_vault_redaction_value("042172", known_to_model=True)  # the model typed it
                outs.append((redact.redact_sensitive_text(self.CANDIDATES, force=True),
                             redact.redact_terminal_output("x 737 y 4000 0566 5566 5556 z Li", force=True)))
            assert outs[0] == outs[1], outs
            assert not any(ch.isdigit() for ch in outs[0][0].split(";")[0]), outs[0]
            assert "4711" not in outs[0][0] and "code 042172" in outs[0][0]  # r9: a model-typed code: not registered
            assert outs[0][1] == "x ••• y •••• •••• •••• •••• z «redacted-card»"
            assert redact.release_card_window("shopping")
            assert redact.redact_sensitive_text("printed 737, Li", force=True) == "printed 737, Li"
        finally:
            redact.clear_vault_redaction_values()

    def test_a_one_time_code_stops_masking_once_its_ttl_passes(self, monkeypatch):
        from agent import redact, redact_vault

        clock = [1000.0]
        monkeypatch.setattr(redact_vault.time, "monotonic", lambda: clock[0])
        try:
            redact.clear_vault_redaction_values()
            redact.register_vault_redaction_value("042172", ttl_s=300)  # a code the vault generated
            assert redact.redact_sensitive_text("order 123456", force=True) == "order ••••••"
            clock[0] += 301
            assert redact.redact_sensitive_text("order 123456", force=True) == "order 123456"
        finally:
            redact.clear_vault_redaction_values()


class TestVaultRedactionByAudience:
    """r9: length masking only where the model reads text it did not write, for a bounded time; exact scrub in
    logs; persisted model-authored arguments keep their digits; TTLs never shorten permanent values."""

    def test_a_pin_and_a_model_typed_guess_print_the_same(self):
        # Codex P1 / peer P2-1: a known (model-typed) guess equal to the PIN used to print dots, any other
        # guess the placeholder
        from agent import redact

        try:
            outs = []
            for guess in ("4711", "4712"):
                redact.clear_vault_redaction_values()
                redact.register_vault_redaction_value("4711")  # a login PIN the vault filled
                redact.register_vault_redaction_value(guess, known_to_model=True)
                outs.append(redact.redact_sensitive_text(f"echo {guess}", force=True))
            assert outs == ["echo ••••", "echo ••••"], outs
        finally:
            redact.clear_vault_redaction_values()

    def test_an_expiring_registration_never_shortens_a_permanent_one(self, monkeypatch):
        # Codex P2 / peer probe3: the same bytes as a one-time code (ttl 300) dropped the permanent password
        from agent import redact, redact_vault

        clock = [1000.0]
        monkeypatch.setattr(redact_vault.time, "monotonic", lambda: clock[0])
        try:
            redact.clear_vault_redaction_values()
            for value in ("hunter-42", "4711"):
                redact.register_vault_redaction_value(value)
                redact.register_vault_redaction_value(value, ttl_s=300)
            clock[0] += 301
            assert redact.redact_sensitive_text("pw hunter-42 pin 4711", force=True) == \
                "pw «redacted-vault-secret» pin ••••"
        finally:
            redact.clear_vault_redaction_values()

    def test_a_login_pin_masks_for_fifteen_minutes_and_logs_never_scrub_it_exactly(self, monkeypatch):
        # r9 (peer P2-2): length masking only for 15 minutes; logs mask by length in that window too. r10 (peer
        # P2-1): afterwards logs do not scrub a pure-digit value at all (an exact scrub is a PIN oracle)
        import logging

        from agent import redact, redact_vault

        clock = [1000.0]
        monkeypatch.setattr(redact_vault.time, "monotonic", lambda: clock[0])
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "pin 4711 HTTP 404 after 1234 ms", None, None)
        log = redact.RedactingFormatter("%(asctime)s %(message)s")
        try:
            redact.clear_vault_redaction_values()
            redact.register_vault_redaction_value("4711")
            assert redact.redact_sensitive_text("port 8080", force=True) == "port ••••"
            assert log.format(record).endswith(" pin •••• HTTP 404 after •••• ms")
            assert any(ch.isdigit() for ch in log.format(record).split(" pin ")[0])  # the timestamp is kept
            clock[0] += redact_vault.LOGIN_MASK_S + 1
            assert redact.redact_sensitive_text("port 8080 pin 4711", force=True) == "port 8080 pin 4711"
            assert log.format(record).endswith(" pin 4711 HTTP 404 after 1234 ms")
            redact.register_vault_redaction_value("1234567890", length_mask=False)  # a numeric username
            assert redact.redact_sensitive_text("ts 1762862047", force=True) == "ts 1762862047"
        finally:
            redact.clear_vault_redaction_values()

    def test_persisted_tool_calls_keep_the_digits_the_model_wrote(self):
        # peer P2-2: length masking turned JSON numbers into dots; the reload then dropped the arguments
        import json

        from agent import redact
        from agent.session_persistence import _durable_tool_calls

        args = json.dumps({"timeout": 1200, "x": 737, "ref": "e123", "command": "echo 4711"})
        try:
            redact.clear_vault_redaction_values()
            redact.register_vault_redaction_value("4711")
            redact.register_card_window("shopping", digits=("4000056655665556", "737"), words=("Li",))
            out = _durable_tool_calls([{"id": "c", "type": "function",
                                        "function": {"name": "terminal", "arguments": args}}])
            assert json.loads(out[0]["function"]["arguments"]) == json.loads(args)
        finally:
            redact.clear_vault_redaction_values()

    def test_persisted_tool_call_arguments_stay_valid_json_whatever_a_secret_is(self):
        # r10 Codex P2: a registered password "true" turned {"background": true} into invalid JSON
        import json

        from agent import redact
        from agent.session_persistence import _durable_tool_calls

        def persisted(args):
            out = _durable_tool_calls([{"id": "c", "type": "function",
                                        "function": {"name": "terminal", "arguments": args}}])
            return out[0]["function"]["arguments"]

        try:
            redact.clear_vault_redaction_values()
            redact.register_vault_redaction_value("true")
            redact.register_vault_redaction_value("pw\\")  # ends in a backslash
            out = json.loads(persisted(json.dumps({"background": True, "command": "echo true", "k": "pw\\"})))
            assert out == {"background": True, "command": "echo «redacted-vault-secret»",
                           "k": "«redacted-vault-secret»"}
            assert persisted("not json true") == "not json «redacted-vault-secret»"  # old behaviour
        finally:
            redact.clear_vault_redaction_values()

    def test_serialized_json_callers_get_valid_json_during_a_mask_window(self):
        # r10 peer P3-2: kanban metadata (and trace upload, request dumps) re-parsed redacted JSON
        import json

        from agent import redact
        from tools.kanban_tools import _redact_metadata

        try:
            redact.clear_vault_redaction_values()
            redact.register_vault_redaction_value("true")
            redact.register_vault_redaction_value("4711")
            text = redact.redact_json_text(json.dumps({"ok": True, "n": 4711, "m": 12, "s": "x true"}), "model")
            assert json.loads(text) == {"ok": True, "n": "••••", "m": 12, "s": "x «redacted-vault-secret»"}
            assert _redact_metadata({"ok": True, "n": 4711, "s": "true"}) == \
                {"ok": True, "n": "••••", "s": "«redacted-vault-secret»"}
        finally:
            redact.clear_vault_redaction_values()

    def test_a_pin_with_a_trailing_newline_is_the_digit_value(self):
        # r10 peer P3: "4711\n" was also kept as an exact non-digit value: an oracle for "is it 4711?"
        from agent import redact

        outs = []
        try:
            for shown in ("4711", "4712"):
                redact.clear_vault_redaction_values()
                redact.register_vault_redaction_value("4711\n")
                outs.append(redact.redact_sensitive_text(f"{shown}\n", force=True))  # what print(...) writes
            assert outs == ["••••\n", "••••\n"], outs
        finally:
            redact.clear_vault_redaction_values()

    def test_a_numeric_leaf_of_the_vault_preview_is_masked(self):
        # r10 peer P3: preview reads masked strings only; a number of a masked length leaked a PIN
        import json

        from agent import redact
        from tools.browser_vault_preview import redact_preview_result

        try:
            redact.clear_vault_redaction_values()
            redact.register_vault_redaction_value("4711")
            assert json.loads(redact_preview_result(json.dumps({"count": 4711, "rows": 3}))) == \
                {"count": "••••", "rows": 3}
        finally:
            redact.clear_vault_redaction_values()

    def test_duplicate_keys_and_padded_pins_leave_nothing_unscrubbed(self):
        # r10 Codex/peer P3: a duplicate key hid the first value from the parsed comparison; "4711 " was an
        # exact non-digit value (an oracle for "is it 4711?")
        import json

        from agent import redact

        try:
            redact.clear_vault_redaction_values()
            redact.register_vault_redaction_value("hunter-42-pw")
            out = redact.redact_json_text('{"command":"hunter-42-pw","command":"safe"}', "authored")
            assert "hunter-42-pw" not in out
            redact.clear_vault_redaction_values()
            redact.register_vault_redaction_value("4711 ")
            assert redact.redact_sensitive_text("4711 4712", force=True) == "•••• ••••"
            assert json.loads(redact.redact_json_text('{"a": 1}', "authored")) == {"a": 1}
        finally:
            redact.clear_vault_redaction_values()
