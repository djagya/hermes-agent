"""enter_code finds ONE plain "code" input on a verification page (Google/Microsoft account signup) and
refuses every other shape: promo/gift/postal/search/chat "code" fields, a verification word that is not in the
field's own form/block, hidden text, several candidates, and inputs that are not rendered and visible.
Synthetic pages only (a tiny Node DOM runs the real inspection and fill scripts)."""

from __future__ import annotations

import json
import shutil
import subprocess
from unittest.mock import patch

import pytest

from agent.vault_login_classifier import (
    LoginControl, build_fill_js, build_inspection_js, build_otp_fills, classify_otp_controls,
    classify_otp_controls_detailed,
)

VERIFY = "Verify your email address Enter the code we sent to someone@example.test"


def ctl(**kw) -> dict:
    base = {"index": 0, "type": "text", "name": "", "label": "", "autocomplete": "", "formIndex": 0,
            "context": VERIFY, "rendered": True, "visible": True}
    base.update(kw)
    return base


def picked(*raw: dict) -> list[int]:
    return [c.control.index for c in classify_otp_controls([LoginControl.from_dict(r) for r in raw])]


# ── classifier: accepted ──────────────────────────────────────────────────────────────────────────────

def test_google_shaped_code_input_in_a_verification_form():
    assert picked(ctl(type="tel", name="code code", label="Enter code")) == [0]


def test_microsoft_enter_code_on_confirm_your_email_page():
    assert picked(ctl(type="text", label="Enter code", context="Confirm your email We sent a code to a@b.test")) == [0]


@pytest.mark.parametrize("name,label", [
    ("code", ""), ("", "Code"), ("", "Enter code"), ("", "Enter the code"), ("pin", ""),
    ("", "Enter the 6-digit code"), ("code", "Please enter your code"),
])
def test_bare_field_shapes_with_verification_text_in_the_form(name, label):
    assert picked(ctl(type="tel", name=name, label=label)) == [0]


@pytest.mark.parametrize("text", [
    "Verify your account", "Verification needed", "Confirm your phone", "We sent a text", "Google sent a code",
    "Enter the code", "Enter the 6-digit code", "One-time passcode", "Two-step sign in", "Enter your security code",
])
def test_each_positive_phrase_in_the_fields_own_form_qualifies(text):
    assert picked(ctl(name="code", context=text)) == [0]


def test_split_digit_boxes_are_never_a_plain_fallback_widget():
    for form_index in (0, None):  # in one form, and form-less
        boxes = [ctl(index=i, type="tel", name=f"code-{i}", maxLength=1, formIndex=form_index) for i in range(6)]
        assert picked(*boxes) == []
        assert classify_otp_controls_detailed([LoginControl.from_dict(r) for r in boxes])[1] == "several_code_fields"


def test_two_six_box_widgets_and_a_box_next_to_a_bare_field_are_refused():
    two = [ctl(index=i, type="tel", name=f"code-{i}", maxLength=1, formIndex=i // 6) for i in range(12)]
    assert picked(*two) == []
    mixed = [ctl(index=0, type="tel", name="code", maxLength=1), ctl(index=1, name="code", label="Enter code")]
    assert picked(*mixed) == []
    one_short = ctl(name="code", maxLength=1)  # a lone one-digit box cannot take a whole code either
    assert picked(one_short) == []
    assert picked(ctl(name="code", maxLength=2)) == [] and picked(ctl(name="code", maxLength=3)) == []


def test_whole_code_fields_with_no_maxlength_or_a_roomy_one_are_accepted():
    assert picked(ctl(name="code")) == [0]
    assert picked(ctl(name="code", maxLength=6)) == [0]
    assert picked(ctl(name="code", maxLength=4)) == [0]


def test_split_boxes_stay_supported_through_the_one_time_code_and_regex_paths():
    auto = [ctl(index=i, name=f"d{i}", maxLength=1, autocomplete="one-time-code", context="") for i in range(6)]
    assert picked(*auto) == list(range(6))
    regex = [ctl(index=i, type="tel", name=f"otp-{i}", maxLength=1, context="") for i in range(6)]
    assert picked(*regex) == list(range(6))
    fills = build_otp_fills(classify_otp_controls([LoginControl.from_dict(r) for r in auto]), "042172")
    assert [f["value"] for f in fills] == list("042172")


def test_build_otp_fills_never_writes_a_whole_code_into_a_short_maxlength_control():
    one = classify_otp_controls([LoginControl.from_dict(ctl(name="otp", label="Verification code", maxLength=4, context=""))])
    assert build_otp_fills(one, "042172") == []
    scattered = classify_otp_controls([LoginControl.from_dict(ctl(index=i, name=f"otp-{i}", maxLength=1, context=""))
                                       for i in range(3)])
    assert build_otp_fills(scattered, "042172") == []  # not len(code) boxes: refuse, no fallback to the first box
    roomy = classify_otp_controls([LoginControl.from_dict(ctl(name="otp", maxLength=6, context=""))])
    assert build_otp_fills(roomy, "042172") == [{"index": 0, "token": "one-time-code", "value": "042172"}]


def test_one_time_code_autocomplete_stays_authoritative_even_with_a_promo_label_and_no_context():
    assert picked(ctl(name="promo code", autocomplete="one-time-code", context="", visible=None)) == [0]


def test_existing_regex_path_unchanged_and_wins_over_the_plain_path():
    both = [ctl(name="otp", label="Authentication code", context=""), ctl(index=1, name="code", label="Code")]
    assert picked(*both) == [0]


# ── classifier: refused ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,label", [
    ("promo_code", "Promo code"), ("code", "Promo code"), ("", "Gift card code"), ("giftCardCode", ""),
    ("postal_code", "Postal code"), ("zip", "ZIP code"), ("code", "Discount code"), ("coupon", ""),
    ("voucherCode", "Enter code"), ("code", "Referral code"), ("invite_code", ""), ("code", "Area code"),
    ("country_code", "Country code"), ("code", "Enter your coupon code"), ("search_code", ""),
    ("code", "Enter the code from your bank statement"),
])
def test_other_kinds_of_code_are_never_otp_targets_even_with_verification_text(name, label):
    assert picked(ctl(type="tel", name=name, label=label)) == []


@pytest.mark.parametrize("text", [
    "Verify shipping details Have a coupon?", "Verify your email Redeem a gift card", "Verify Referral program",
    "Enter the code Promo", "Verification Discount", "Verify Postal code", "Verify Search", "Verify Chat with us",
    "Verify Leave a comment", "Verify Send us a message", "Verify Tracking number", "Verify Order number",
    "Verify Booking reference number", "Enter the code Voucher", "Verify Country",
])
def test_a_non_otp_purpose_in_the_form_text_refuses_before_any_acceptance_route(text):
    assert picked(ctl(name="code", context=text)) == []


@pytest.mark.parametrize("text", ["", "Search", "Enter code", "Welcome", "Create your account Choose a name",
                                  "Account setup Step 2 of 3"])
def test_no_verification_evidence_in_the_form_is_a_refusal(text):
    # headings elsewhere on the page are not in the field's context, and nothing is "alone in its form" anymore
    assert picked(ctl(name="code", context=text)) == []


def test_missing_or_truncated_context_never_qualifies():
    assert picked(ctl(name="code", context="")) == []
    assert picked(ctl(name="code", context="Verify " * 400)) == []  # past the inspection's cap
    assert LoginControl.from_dict({"name": "code", "context": 5}).context == ""


def test_not_visible_or_not_rendered_or_unknown_never_qualifies():
    assert picked(ctl(name="code", visible=False)) == []
    assert picked(ctl(name="code", rendered=False)) == []
    assert picked(ctl(name="code", visible=None)) == []  # an older inspection


def test_several_qualifying_candidates_are_refused_never_the_first():
    two = [ctl(name="code", label="Code"), ctl(index=1, name="code", label="Enter code", formIndex=1)]
    controls = [LoginControl.from_dict(r) for r in two]
    out, why = classify_otp_controls_detailed(controls)
    assert out == [] and why == "several_code_fields"
    assert picked(*two) == []
    one_bad = [two[0], dict(two[1], visible=False)]
    assert picked(*one_bad) == [0]  # an invisible lookalike does not count


def test_country_code_select_and_non_text_types_are_ignored():
    assert picked(ctl(type="select", name="code", label="Code")) == []
    assert picked(ctl(type="email", name="code", label="Code")) == []
    assert picked(ctl(type="password", name="code", label="Code")) == []


def test_plain_fallback_fills_are_marked_strict_and_regular_ones_are_not():
    plain = classify_otp_controls([LoginControl.from_dict(ctl(name="code"))])
    assert build_otp_fills(plain, "042172") == [{"index": 0, "token": "one-time-code", "value": "042172", "strict": True}]
    regular = classify_otp_controls([LoginControl.from_dict(ctl(name="otp", label="Verification code", context=""))])
    assert build_otp_fills(regular, "042172") == [{"index": 0, "token": "one-time-code", "value": "042172"}]


# ── enter_code end to end (handler with mocked page transport) ────────────────────────────────────────

ORIGIN = "https://accounts.example.test"


def run_enter_code(controls, fill_answer=None, **kw):
    from tools import browser_vault_tool

    seen: dict = {}
    answer = fill_answer if fill_answer is not None else {"filled": 1}
    with patch("agent.vault_backends.unlock.can_prompt_here", return_value=False), \
         patch("agent.vault_backends.unlock.get_code_prompt_callback", return_value=None), \
         patch.object(browser_vault_tool, "_focus_bound_origin", lambda *a, **k: None), \
         patch.object(browser_vault_tool, "_eval_js", side_effect=lambda t, e: {
             "success": True, "result": json.dumps(controls) if "querySelectorAll" in e else ORIGIN + "/signup/verify"}), \
         patch.object(browser_vault_tool, "_eval_js_secret",
                      side_effect=lambda t, e: seen.update(expr=e) or {"success": True, "result": json.dumps(answer)}):
        raw = browser_vault_tool.browser_vault_enter_code(task_id="t", **kw)
    return json.loads(raw), raw, seen


def test_enter_code_fills_the_google_shaped_field_and_never_echoes_the_code():
    out, raw, seen = run_enter_code([ctl(type="tel", name="code code", label="Enter code")],
                                    code="042172", expected_origin=ORIGIN)
    assert out["success"] and out["filled_fields"] == 1 and out["source"] == "supplied"
    assert "042172" not in raw and '"value": "042172"' in seen["expr"] and '"strict": true' in seen["expr"]


def test_enter_code_still_reports_no_code_field_for_a_promo_input():
    out, raw, seen = run_enter_code([ctl(name="promo", label="Promo code")], code="042172", expected_origin=ORIGIN)
    assert out["error_type"] == "no_code_field" and "expr" not in seen and "042172" not in raw


def test_enter_code_names_the_ambiguity_and_writes_nothing():
    two = [ctl(name="code"), ctl(index=1, name="code", formIndex=1)]
    out, raw, seen = run_enter_code(two, code="042172", expected_origin=ORIGIN)
    assert out["error_type"] == "no_code_field" and "Several possible code fields" in out["error"]
    assert "expr" not in seen and "042172" not in raw


def test_enter_code_refuses_a_code_longer_than_the_fields_maxlength_and_writes_nothing():
    out, raw, seen = run_enter_code([ctl(name="otp", label="Verification code", maxLength=4, context="")],
                                    code="042172", expected_origin=ORIGIN)
    assert out["success"] is False and out["error_type"] == "no_code_field" and "expr" not in seen and "042172" not in raw


def test_enter_code_reports_a_field_that_stopped_being_visible_before_the_write():
    out, raw, _ = run_enter_code([ctl(name="code")], fill_answer={"refused": "field_not_visible"},
                                    code="042172", expected_origin=ORIGIN)
    assert out["success"] is False and out["error_type"] == "code_field_not_visible" and "042172" not in raw


def test_origin_gate_unchanged_for_a_plain_code_field():
    out, raw, seen = run_enter_code([ctl(type="tel", name="code", label="Enter code")],
                                    code="042172", expected_origin="https://evil.example.test")
    assert out["success"] is False and "expr" not in seen and "042172" not in raw


# ── the real inspection, classifier and fill scripts on synthetic DOMs ───────────────────────────────

_NODE = r'''
class HTMLInputElement {}
class Event { constructor(t) { this.type = t; } }
class InputEvent extends Event {}
const HIDDEN = (n) => n.style.display === "none" || n.style.visibility === "hidden";
class El {
  constructor(tag, p = {}, kids = []) {
    this.tagName = tag; this.parentElement = null; this.children = []; this.text = p.text || "";
    this.attrs = {}; this.type = p.type || ""; this.name = p.name || ""; this.id = p.id || "";
    this.autocomplete = p.autocomplete || ""; this.unreadable = !!p.unreadable; this.disabled = false; this.readOnly = false; this.labels = [];
    this.maxLength = p.maxLength || -1; this.value = ""; this.isConnected = true;
    this.style = Object.assign({ display: "block", visibility: "visible", opacity: "1" }, p.style || {});
    this.rect = p.rect || { left: 10, top: 10, width: 200, height: 30, right: 210 };
    if (p.aria) this.attrs["aria-label"] = p.aria;
    if (p.placeholder) this.attrs.placeholder = p.placeholder;
    if (p.ref) refs[p.ref] = this;
    for (const k of kids.flat()) { k.parentElement = this; this.children.push(k); }
  }
  get form() { for (let n = this.parentElement; n; n = n.parentElement) if (n.tagName === "FORM") return n; return null; }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(" "); }
  get innerText() {
    if (this.unreadable) throw new Error("not readable");
    const walk = (n, top) => (!top && HIDDEN(n) ? "" : [n.text].concat(n.children.map((c) => walk(c, false))).join(" "));
    return walk(this, true);
  }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  removeAttribute(n) { delete this.attrs[n]; }
  getClientRects() { for (let n = this; n; n = n.parentElement) if (n.style.display === "none") return []; return [1]; }
  getBoundingClientRect() { return this.rect; }
  getRootNode() { return document; }
  focus() {}
  dispatchEvent() { return true; }
  querySelectorAll(sel) {
    const all = []; const go = (n) => { for (const c of n.children) { all.push(c); go(c); } }; go(this);
    if (sel === "*") return all;
    const m = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(sel);
    return m ? all.filter((e) => m[1] in e.attrs && (m[2] === undefined || e.attrs[m[1]] === m[2])) : [];
  }
}
const refs = {};
const h = (tag, p, ...kids) => new El(tag, p || {}, kids);
const filler = (n) => "x".repeat(n);
const body = new El("BODY");
for (const n of (@@PAGE@@)) { n.parentElement = body; body.children.push(n); }
const window = { innerWidth: 1280, innerHeight: 800, scrollY: 0, location: { origin: "https://accounts.example.test" } };
const getComputedStyle = (e) => e.style;
const document = { URL: "https://accounts.example.test/verify", title: "Verify your email", documentElement: { scrollHeight: 2000 },
  querySelectorAll: (s) => body.querySelectorAll(s), getElementById: () => null };
const inspected = @@INSPECT@@;
@@BETWEEN@@
const fillAnswer = @@FILL@@;
console.log(JSON.stringify({ rows: JSON.parse(inspected), fill: fillAnswer === null ? null : JSON.parse(fillAnswer),
  values: Object.fromEntries(Object.entries(refs).map(([k, v]) => [k, v.value])) }));
'''

NONCE = "n1"
CODE = "042172"


def _need_node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for synthetic JS probe")
    return node


def run_node(page: str, fill_js: str = "null", between: str = "") -> dict:
    src = (_NODE.replace("@@PAGE@@", page).replace("@@INSPECT@@", build_inspection_js(NONCE))
           .replace("@@BETWEEN@@", between).replace("@@FILL@@", fill_js))
    run = subprocess.run([_need_node()], input=src, text=True, capture_output=True, timeout=30, check=False)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def probe(page: str, between: str = "") -> dict:
    """Inspect, classify, build the fill and run it on the same synthetic page. ``written`` maps ref -> value."""
    first = run_node(page)
    controls = [LoginControl.from_dict(r) for r in first["rows"]]
    otp, why = classify_otp_controls_detailed(controls)
    if not otp:
        return {"otp": [], "why": why, "fill": None, "written": {}}
    fills = build_otp_fills(otp, CODE)
    second = run_node(page, build_fill_js(fills, "https://accounts.example.test", nonce=NONCE), between)
    return {"otp": otp, "why": why, "fill": second["fill"], "written": {k: v for k, v in second["values"].items() if v}}


def field(**kw) -> str:
    props = {"type": "tel", "name": "code", "id": "code", "aria": "Enter code", "ref": "real"}
    props.update(kw)
    return "h('INPUT', " + json.dumps(props) + ")"


def attacker(**kw) -> str:
    return field(ref="attacker", **kw)


def p(text: str) -> str:
    return "h('P', {text: " + json.dumps(text) + "})"


def form(*kids: str) -> str:
    return "h('FORM', {}, " + ", ".join(kids) + ")"


def div(*kids: str, **props) -> str:
    return "h('DIV', " + json.dumps(props) + ", " + ", ".join(kids) + ")"


def page(*nodes: str) -> str:
    return "[" + ", ".join(nodes) + "]"


def test_probe_google_shape_is_accepted_and_written():
    res = probe(page(form(p("Verify your email address"), p("Enter the code we sent to a@b.test"), field())))
    assert res["fill"] == {"filled": 1} and res["written"] == {"real": CODE}


def test_probe_microsoft_shape_without_a_form_is_accepted_and_written():
    res = probe(page(div(p("Confirm your email"), p("We sent a code to a@b.test"), field(aria="", placeholder="Enter code"))))
    assert res["written"] == {"real": CODE}


def _boxes(prefix: str = "d", n: int = 6, **extra) -> list[str]:
    return [f"h('INPUT', {json.dumps({'type': 'tel', 'name': f'code-{i}', 'maxLength': 1, 'ref': f'{prefix}{i}', **extra})})"
            for i in range(n)]


def test_probe_split_boxes_via_one_time_code_still_fill_one_digit_each():
    boxes = _boxes(autocomplete="one-time-code")
    res = probe(page(div(p("Anything"), div(*boxes))))
    assert res["written"] == {f"d{i}": CODE[i] for i in range(6)}


def test_probe_plain_code_split_boxes_are_refused_in_a_form_and_form_less():
    for wrap in (form, div):
        res = probe(page(wrap(p("Verify your email: enter the code we sent"), *_boxes())))
        assert res["otp"] == [] and res["written"] == {}


def test_probe_two_six_box_widgets_form_and_form_less_are_refused_nothing_written():
    two_forms = page(form(p("Verify your email: enter the code we sent"), *_boxes("a")),
                     form(p("Verify your email: enter the code we sent"), *_boxes("b")))
    two_blocks = page(div(p("Verify your email: enter the code we sent"), div(*_boxes("a")), div(*_boxes("b"))))
    for pg in (two_forms, two_blocks):
        res = probe(pg)
        assert res["otp"] == [] and res["why"] == "several_code_fields" and res["written"] == {}


def test_probe_a_box_next_to_a_bare_field_is_refused_nothing_written():
    res = probe(page(form(p("Verify your email address"), *_boxes(n=1), field())))
    assert res["otp"] == [] and res["written"] == {}


def test_probe_verify_heading_does_not_override_a_coupon_field_in_the_same_form():
    res = probe(page(form(p("Verify shipping details"), p("Have a coupon?"), field(aria="", ref="coupon"))))
    assert res["otp"] == [] and res["written"] == {}


def test_probe_a_hidden_verification_heading_does_not_qualify():
    hidden = "h('H1', {text: 'Verify your email', style: {display: 'none'}})"
    res = probe(page(form(hidden, p("Welcome"), field())))
    assert res["otp"] == [] and res["written"] == {}


def test_probe_page_headings_outside_the_fields_form_do_not_qualify():
    res = probe(page("h('H1', {text: 'Verify your email address We sent a code'})", form(p("Newsletter"), field())))
    assert res["otp"] == [] and res["written"] == {}


def test_probe_attacker_bare_field_before_the_real_one_is_ambiguous_and_nothing_is_written():
    evil = form(p("Verify your email address"), attacker(name="code", id="code"))
    real = form(p("Verify your email address We sent a code"), field())
    res = probe(page(evil, real))
    assert res["otp"] == [] and res["why"] == "several_code_fields" and res["written"] == {}


def test_probe_two_bare_fields_in_different_forms_are_refused():
    res = probe(page(form(p("Verify your email"), field(ref="a")), form(p("Verify your phone"), field(ref="b"))))
    assert res["why"] == "several_code_fields" and res["written"] == {}


def test_probe_two_bare_fields_in_one_form_are_refused():
    res = probe(page(form(p("Verify your email"), field(ref="a"), field(ref="b"))))
    assert res["why"] == "several_code_fields" and res["written"] == {}


def test_probe_an_invisible_attacker_field_does_not_count_and_the_real_one_is_filled():
    off = {"left": -9999, "top": 10, "width": 200, "height": 30, "right": -9799}
    res = probe(page(form(p("Verify your email address"), attacker(rect=off), field())))
    assert res["written"] == {"real": CODE}


@pytest.mark.parametrize("purpose", ["Search the site", "Chat with support", "Leave a comment", "Send us a message"])
def test_probe_single_code_field_in_a_search_chat_or_comment_form(purpose):
    res = probe(page(form(p(purpose), field(aria="Code"))))
    assert res["otp"] == [] and res["written"] == {}


def test_probe_coupon_text_far_above_the_field_in_a_big_form_is_truncated_context():
    big = form(p("Have a coupon? Enter it below."), "h('P', {text: filler(2500)})", p("Verify your email"), field())
    res = probe(page(big))
    assert res["otp"] == [] and res["written"] == {}


def test_probe_coupon_text_in_the_fields_block_without_a_form_refuses():
    res = probe(page(div(p("Have a coupon?"), div(p("Verify your email address"), field()))))
    assert res["otp"] == [] and res["written"] == {}


@pytest.mark.parametrize("kw", [
    {"rect": {"left": -9999, "top": 10, "width": 200, "height": 30, "right": -9799}},
    {"rect": {"left": 5000, "top": 10, "width": 200, "height": 30, "right": 5200}},
    {"rect": {"left": 10, "top": 10, "width": 0, "height": 0, "right": 10}},
    {"style": {"opacity": "0"}},
    {"style": {"opacity": "0.01"}},
    {"style": {"visibility": "hidden"}},
    {"style": {"display": "none"}},
])
def test_probe_off_screen_transparent_or_hidden_inputs_are_refused(kw):
    res = probe(page(form(p("Verify your email address"), field(**kw))))
    assert res["otp"] == [] and res["written"] == {}


def test_probe_a_transparent_ancestor_is_refused():
    res = probe(page(div(p("Verify your email address"), div(field(), style={"opacity": "0"}))))
    assert res["otp"] == [] and res["written"] == {}


def test_probe_fill_script_rechecks_visibility_and_writes_nothing_when_it_fails():
    res = probe(page(form(p("Verify your email address"), field())), between="refs.real.style.opacity = '0';")
    assert res["fill"] == {"refused": "field_not_visible"} and res["written"] == {}
    res = probe(page(form(p("Verify your email address"), field())),
                between="refs.real.rect = {left: -9999, top: 0, width: 200, height: 30, right: -9799};")
    assert res["fill"] == {"refused": "field_not_visible"} and res["written"] == {}


def test_probe_regular_path_fill_is_not_subject_to_the_new_visibility_recheck():
    res = probe(page(form(field(name="otp", id="otp", aria="Verification code", ref="real"))),
                between="refs.real.style.opacity = '0';")
    assert res["fill"] == {"filled": 1}


def test_inspection_context_is_rendered_form_text_and_never_includes_the_title_or_headings_elsewhere():
    rows = run_node(page("h('H1', {text: 'Sign up now'})", form(p("Verify your email"), field())))["rows"]
    assert len(rows) == 1 and rows[0]["visible"] is True and rows[0]["rendered"] is True
    assert rows[0]["context"] == "Verify your email" and "Sign up" not in rows[0]["context"]


def test_probe_oversized_outer_block_with_a_coupon_purpose_fails_closed():
    inner = div(p("Enter the code"), field(aria="", ref="coupon"))
    res = probe(page(div(p("Have a coupon?"), "h('P', {text: filler(1800)})", inner)))
    assert res["otp"] == [] and res["written"] == {}
    rows = run_node(page(div(p("Have a coupon?"), "h('P', {text: filler(1800)})", inner)))["rows"]
    assert rows[0]["context"] == ""


def test_probe_an_unreadable_outer_block_fails_closed():
    inner = div(p("Verify your email address"), field())
    res = probe(page(div(inner, unreadable=True)))
    assert res["otp"] == [] and res["written"] == {}
    res = probe(page(div(div(inner, p("Have a coupon?")), unreadable=True)))
    assert res["otp"] == [] and res["written"] == {}


def test_probe_a_readable_chain_within_the_cap_still_qualifies():
    res = probe(page(div(div(p("Verify your email address"), div(field())))))
    assert res["written"] == {"real": CODE}
