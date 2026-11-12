"""enter_code finds a plain "code" input on a verification page (Google/Microsoft account signup),
and still ignores promo/gift/postal/discount/country "code" fields. Synthetic pages only."""

from __future__ import annotations

import json
import shutil
import subprocess
from unittest.mock import patch

import pytest

from agent.vault_login_classifier import LoginControl, build_inspection_js, classify_otp_controls

VERIFY = "Verify your email address Google Enter the code we sent to someone@example.test"


def ctl(**kw) -> dict:
    base = {"index": 0, "type": "text", "name": "", "label": "", "autocomplete": "", "formIndex": 0, "context": ""}
    base.update(kw)
    return base


def picked(*raw: dict) -> list[int]:
    controls = [LoginControl.from_dict(r) for r in raw]
    return [c.control.index for c in classify_otp_controls(controls)]


# ── accepted ──────────────────────────────────────────────────────────────────────────────────────────

def test_google_shaped_code_input_on_verify_page():
    # <input type=tel name=code id=code autocomplete=off aria-label="Enter code"> beside a "G-" prefix,
    # with an email-less form: the heading says verify.
    assert picked(ctl(type="tel", name="code code", label="Enter code", context=VERIFY)) == [0]


def test_google_shaped_input_with_a_second_text_input_needs_the_context():
    pair = [ctl(type="tel", name="code code", label="Enter code"), ctl(index=1, name="recovery", label="Recovery")]
    assert picked(*pair) == []  # no verification context, not alone
    pair[0]["context"] = VERIFY
    assert picked(*pair) == [0]


def test_microsoft_enter_code_on_confirm_your_email_page():
    assert picked(ctl(type="text", name="", label="Enter code", context="Confirm your email We sent a code")) == [0]


@pytest.mark.parametrize("name,label", [
    ("code", ""), ("", "Code"), ("", "Enter code"), ("", "Enter the code"), ("pin", ""),
    ("", "Enter the 6-digit code"), ("code", "Please enter your code"),
])
def test_plain_single_input_form_without_context(name, label):
    assert picked(ctl(type="tel", name=name, label=label)) == [0]


def test_number_type_and_placeholder_text_are_enough():
    assert picked(ctl(type="number", name="code", label="Enter code", context="Check your email")) == [0]


def test_split_digit_boxes_with_verify_context_still_get_through_the_regular_path():
    boxes = [ctl(index=i, type="tel", name=f"digit{i}", autocomplete="one-time-code", maxLength=1) for i in range(6)]
    assert picked(*boxes) == list(range(6))


# ── authoritative / unchanged paths ───────────────────────────────────────────────────────────────────

def test_one_time_code_autocomplete_stays_authoritative_even_with_a_promo_label():
    assert picked(ctl(name="promo code", autocomplete="one-time-code")) == [0]


def test_existing_regex_path_unchanged_and_wins_over_the_plain_path():
    both = [ctl(name="otp", label="Authentication code"), ctl(index=1, name="code", label="Code", context=VERIFY)]
    assert picked(*both) == [0]


# ── refused ───────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,label", [
    ("promo_code", "Promo code"), ("code", "Promo code"), ("", "Gift card code"), ("giftCardCode", ""),
    ("postal_code", "Postal code"), ("zip", "ZIP code"), ("code", "Discount code"), ("coupon", ""),
    ("voucherCode", "Enter code"), ("code", "Referral code"), ("invite_code", ""), ("code", "Area code"),
    ("country_code", "Country code"), ("code", "Enter your coupon code"),
])
def test_other_kinds_of_code_are_never_otp_targets_even_with_a_verify_heading(name, label):
    assert picked(ctl(type="tel", name=name, label=label, context=VERIFY)) == []
    assert picked(ctl(type="tel", name=name, label=label)) == []


def test_discount_code_beside_an_email_field_is_not_a_target():
    form = [ctl(type="email", name="email", label="Email"), ctl(index=1, name="code", label="Code")]
    assert picked(*form) == []


def test_coupon_on_a_checkout_with_several_inputs():
    checkout = [ctl(name="name", label="Full name"), ctl(index=1, name="street", label="Street"),
                ctl(index=2, name="code", label="Enter code", context="Checkout Order summary Have a coupon?")]
    assert picked(*checkout) == []


def test_lone_code_input_on_a_promo_page_is_refused_by_its_context():
    assert picked(ctl(name="code", label="Code", context="Redeem your gift card Enter the code on the back")) == []


def test_country_code_select_and_non_text_types_are_ignored():
    assert picked(ctl(type="select", name="code", label="Code", context=VERIFY)) == []
    assert picked(ctl(type="email", name="code", label="Code", context=VERIFY)) == []
    assert picked(ctl(type="password", name="code", label="Code", context=VERIFY)) == []


def test_a_lone_code_field_with_a_password_sibling_is_not_alone():
    login = [ctl(name="code", label="Code"), ctl(index=1, type="password", name="pw", label="Password")]
    assert picked(*login) == []


def test_separate_forms_count_separately():
    forms = [ctl(name="code", label="Code", formIndex=0), ctl(index=1, name="q", label="Search", formIndex=1)]
    assert picked(*forms) == [0]


def test_unrelated_label_words_keep_a_field_out_of_the_bare_path():
    assert picked(ctl(name="code", label="Enter the code from your bank statement")) == []


# ── enter_code end to end: origin gate, redaction, never echoed ───────────────────────────────────────

ORIGIN = "https://accounts.example.test"


def run_enter_code(controls, **kw):
    from tools import browser_vault_tool

    seen: dict = {}
    with patch("agent.vault_backends.unlock.can_prompt_here", return_value=False), \
         patch("agent.vault_backends.unlock.get_code_prompt_callback", return_value=None), \
         patch.object(browser_vault_tool, "_focus_bound_origin", lambda *a, **k: None), \
         patch.object(browser_vault_tool, "_eval_js", side_effect=lambda t, e: {
             "success": True, "result": json.dumps(controls) if "querySelectorAll" in e else ORIGIN + "/signup/verify"}), \
         patch.object(browser_vault_tool, "_eval_js_secret",
                      side_effect=lambda t, e: seen.update(expr=e) or {"success": True, "result": json.dumps({"filled": 1})}):
        raw = browser_vault_tool.browser_vault_enter_code(task_id="t", **kw)
    return json.loads(raw), raw, seen


def test_enter_code_fills_the_google_shaped_field_and_never_echoes_the_code():
    out, raw, seen = run_enter_code([ctl(type="tel", name="code code", label="Enter code", context=VERIFY)],
                                    code="042172", expected_origin=ORIGIN)
    assert out["success"] and out["filled_fields"] == 1 and out["source"] == "supplied"
    assert "042172" not in raw and '"value": "042172"' in seen["expr"]


def test_enter_code_still_reports_no_code_field_for_a_promo_input():
    out, raw, seen = run_enter_code([ctl(name="promo", label="Promo code", context=VERIFY)],
                                    code="042172", expected_origin=ORIGIN)
    assert out["error_type"] == "no_code_field" and "expr" not in seen and "042172" not in raw


def test_origin_gate_unchanged_for_a_plain_code_field():
    out, raw, seen = run_enter_code([ctl(type="tel", name="code", label="Enter code", context=VERIFY)],
                                    code="042172", expected_origin="https://evil.example.test")
    assert out["success"] is False and "expr" not in seen and "042172" not in raw


# ── the real inspection script reports the context ────────────────────────────────────────────────────

_NODE = r'''
class El {
  constructor(tag, o = {}) { Object.assign(this, { tagName: tag, children: [], parentElement: null, text: "", attrs: {}, type: "", name: "",
    id: "", autocomplete: "", disabled: false, readOnly: false, labels: [], form: null, maxLength: -1 }, o); }
  add(c) { c.parentElement = this; this.children.push(c); return c; }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(" "); }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  getClientRects() { return [1]; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 200, height: 30, right: 200 }; }
  getRootNode() { return document; }
}
const body = new El("BODY");
const h1 = body.add(new El("H1", { text: "Verify your email address" }));
const box = body.add(new El("DIV"));
box.add(new El("P", { text: "We sent a code to someone@example.test" }));
const input = box.add(new El("INPUT", { type: "tel", name: "code", id: "code" }));
input.attrs["aria-label"] = "Enter code";
const all = [h1, input];
const walkAll = { querySelectorAll: (s) => (s === "*" ? [h1, input] : s.startsWith("h1") ? [h1] : []) };
const window = { innerWidth: 1280, innerHeight: 800, scrollY: 0 };
const getComputedStyle = () => ({ display: "block", visibility: "visible" });
const document = { URL: "https://x.test/", title: "Create your account", documentElement: { scrollHeight: 2000 },
  querySelectorAll: walkAll.querySelectorAll, getElementById: () => null };
process.stdout.write(@@SCRIPT@@);
'''


def test_inspection_script_exposes_headings_and_nearby_text_for_the_classifier():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for synthetic JS probe")
    run = subprocess.run([node], input=_NODE.replace("@@SCRIPT@@", build_inspection_js("n1")),
                         text=True, capture_output=True, timeout=20, check=False)
    assert run.returncode == 0, run.stderr
    rows = json.loads(run.stdout)
    assert len(rows) == 1 and rows[0]["type"] == "tel" and rows[0]["label"].strip() == "Enter code"
    assert "Verify your email address" in rows[0]["context"] and "We sent a code" in rows[0]["context"]
    assert len(rows[0]["context"]) <= 900
    assert [c.control.index for c in classify_otp_controls([LoginControl.from_dict(r) for r in rows])] == [0]
