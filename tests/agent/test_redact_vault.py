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
            assert "4711" not in outs[0][0] and "«redacted-vault-secret»" in outs[0][0]  # 042172: exact
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
