"""Real isolated Chromium: the Payrails hosted card form as its public bundle builds it (dlz, 11-14).

HERMES_E2E_BROWSER=1 scripts/run_tests.sh -m integration this file.

The shape is read off the public Payrails Web SDK (npm ``@payrails/web-sdk`` 5.50.0) and the secure-frame bundle it
loads (``assets.payrails.io/apps/secure-frame/v1``), not off a captured checkout:

- the merchant page holds TWO frames of the provider's origin: a 1px ``sdk-observer`` telemetry frame appended to the
  body when the SDK starts, and the card form frame, created with ``width=100% height=0`` and no border;
- the card frame's document is empty until the merchant page answers its ``READY`` with one ``SHOW`` (the SDK listens
  for the first ``READY`` only); each field's container is ``display:none`` until its own ``SHOW``; the frame reports
  its size (``RESIZE``) and the SDK then sets the iframe's height;
- all four inputs sit in the one document, with ``autocomplete`` tokens, a placeholder and an ``aria-label``; a field
  that completes moves the focus to the next one; focus, change and blur are reported to the merchant page;
- the checkout is a long page: the card form is below the fold until something scrolls to it.
"""

from __future__ import annotations

import json
import os

import pytest

from tests.tools import test_browser_vault_payment_live as live
from tests.tools.test_browser_vault_payment_live import CARD, _fa_fill, _fa_setup, navigate
from tests.waiting import wait_until

payment_browser = live.payment_browser  # the fixture: one isolated Chromium and two synthetic HTTPS origins

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("HERMES_E2E_BROWSER") != "1", reason="explicit isolated browser opt-in required"),
]

_FRAME_CSS = (
    "html{font-size:16px;font-family:Inter,sans-serif}*{margin:0;padding:0;box-sizing:border-box;"
    "background-color:transparent}.card-form{display:flex;flex-direction:column;width:100%}"
    ".card-form-row{display:flex;width:100%}.card-form-field{flex:1;min-width:0}.hidden{visibility:hidden}"
    ".container{box-sizing:border-box;padding:0;margin:0;text-wrap:nowrap;display:none;position:relative}"
    ".container.show{display:block;width:100%}.input-wrapper{position:relative}"
    ".card-icons{position:absolute;right:8px;bottom:calc(50% - 12px);display:flex;align-items:center;gap:4px;"
    "pointer-events:none}.card-icon{height:24px;max-height:100%;width:auto}"
    "input{display:block;width:100%;padding:10px 16px;color:#1d1d1d;background-color:#fff;outline:none;"
    "border:1px solid #eae8ee}")
# The secure frame: `late` ms before the form is built after SHOW (a slow frame), nothing built without SHOW.
_FRAME = "<style>" + _FRAME_CSS + """</style><div id="app"></div><script>
const q = new URLSearchParams(location.search), id = q.get('id') || 'CARD_FORM-x', late = Number(q.get('late') || 0);
const post = (name, data) => parent.postMessage({bus: 'payrails', name: id + '-' + name, data}, '*');
const root = document.createElement('div');
root.className = 'card-form'; root.id = id; document.getElementById('app').appendChild(root);
new ResizeObserver(() => { const r = root.getBoundingClientRect();
  post('RESIZE', {width: r.width + 'px', height: r.height + 'px'}); }).observe(root);
const TYPES = {CARDHOLDER_NAME: ['cc-name', 'text', 50, 'Card holder'], CARD_NUMBER: ['cc-number', 'numeric', 23,
  '1234 1234 1234 1234'], EXPIRATION_DATE: ['cc-exp', 'numeric', 5, 'MM/YY'], CVV: ['cc-csc', 'numeric', 3, 'CVC']};
const ORDER = ['CARDHOLDER_NAME', 'CARD_NUMBER', 'EXPIRATION_DATE', 'CVV'], inputs = {}, done = {};
window.log = [];
const mask = (type, v) => type === 'CARD_NUMBER' ? v.replace(/\\D/g, '').replace(/(\\d{4})(?=\\d)/g, '$1 ')
  : type === 'EXPIRATION_DATE' ? v.replace(/\\D/g, '').replace(/^(\\d{2})(\\d)/, '$1/$2') : type === 'CVV' ? v.replace(/\\D/g, '') : v;
const complete = (type, v) => type === 'CARD_NUMBER' ? v.replace(/\\D/g, '').length === 16
  : type === 'EXPIRATION_DATE' ? v.length === 5 : type === 'CVV' ? v.length === 3 : false;
const field = type => {
  const [autocomplete, inputmode, maxlength, placeholder] = TYPES[type], fid = id + '-' + type;
  const cell = document.createElement('div'), box = document.createElement('div'), wrap = document.createElement('div');
  const el = document.createElement('input');
  cell.className = 'card-form-field'; box.className = 'container'; box.id = fid; wrap.className = 'input-wrapper';
  for (const [k, v] of Object.entries({id: fid + '-' + type + '-input', name: type, placeholder, autocomplete, inputmode,
      maxlength, autocapitalize: 'off', type: 'text', autocorrect: 'off', spellcheck: 'false', 'aria-required': 'true',
      required: '', 'aria-label': placeholder, 'data-payrails-field': type})) el.setAttribute(k, v);
  el.addEventListener('focus', () => { Promise.resolve().then(() => el.setSelectionRange(el.value.length, el.value.length));
    post('FOCUS', {isFocused: true}); });
  el.addEventListener('input', () => {
    const before = el.value; el.value = mask(type, before);
    window.log.push(type);
    post('CHANGE', {frameType: 'CARD_FORM', isEmpty: !el.value});
    const now = complete(type, el.value), k = ORDER.indexOf(type);
    if (now && !done[type] && k + 1 < ORDER.length && ORDER.slice(1, k + 1).every(t => complete(t, inputs[t].value)))
      inputs[ORDER[k + 1]].focus();  // the next field takes the focus when this one completes
    done[type] = now;
  });
  el.addEventListener('blur', () => { if (!el.value) el.setAttribute('aria-invalid', 'true'); else el.removeAttribute('aria-invalid');
    post('BLUR', {isFocused: false}); });
  wrap.appendChild(el);
  if (type !== 'CARDHOLDER_NAME') { const icons = document.createElement('span'); icons.className = 'card-icons';
    const img = document.createElement('img'); img.className = 'card-icon'; img.alt = ''; img.setAttribute('aria-hidden', 'true');
    img.src = 'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2236%22 height=%2224%22/%3E';
    icons.appendChild(img); wrap.appendChild(icons); }
  box.appendChild(wrap); cell.appendChild(box); inputs[type] = el;
  setTimeout(() => box.classList.add('show'), 0);  // the field's own SHOW
  return cell;
};
let built = false;
addEventListener('message', e => {
  const m = e.data;
  if (!m || m.bus !== 'payrails' || m.name !== id + '-SHOW' || built) return;
  built = true;
  setTimeout(() => {
    for (const row of [['CARDHOLDER_NAME'], ['CARD_NUMBER'], ['EXPIRATION_DATE', 'CVV']]) {
      const r = document.createElement('div'); r.className = 'card-form-row';
      for (const type of row) r.appendChild(field(type));
      root.appendChild(r);
    }
    setTimeout(() => post('READY', {frameType: 'CARD_FORM'}), 0);
  }, late);
});
post('READY', {});
</script>"""
# The merchant page: a long checkout with a sticky header; the SDK part mounts the observer at once and the card form
# when the card method is chosen. `remount(late)` is the SDK's unmount + mount (a new element, a new frame);
# `moved()` moves the mounted frame's element in the DOM (its document loads again, and the SDK, which heard its one
# READY already, never answers the new one: the frame reports an empty form and collapses); `own()` replaces the hosted
# form by card inputs of the merchant page itself; `retotal()` changes the total the page shows. `stickyBar()` (11-15)
# adds a fixed bottom bar and, at the first focus in the card frame, the scroll a focus makes to a field below the
# fold: the frame's bottom edge onto the viewport's, under the bar; `covered()`: the bar is at the frame's centre.
_MERCHANT = """<meta charset="utf-8"><style>body{margin:0;font-family:sans-serif}
.hdr{position:sticky;top:0;height:64px;background:#fff;border-bottom:1px solid #ccc;z-index:10}
.sec{padding:24px;max-width:720px;margin:0 auto}</style>
<div class="hdr">Reservierung 09:59</div>
<div class="sec"><h2>Fahrgast</h2><label for="fn">Vorname</label><input id="fn" autocomplete="given-name" value="A">
<label for="em">E-Mail</label><input id="em" autocomplete="email" value="a@b.example"></div>
<div style="height:1500px"></div>
<div class="sec" id="pay"><h2>Zahlung</h2>
<label><input type="radio" name="pm" id="payrails_payrailsCC"> Kartenzahlung</label><div id="mount"></div><div id="other"></div></div>
<div class="sec"><p>1 x Erwachsener</p><p id="total">Gesamtbetrag 57,47 €</p><button type="button" id="paynow">Jetzt bezahlen</button></div>
<div style="height:700px"></div>
<script>
const PSP = %(psp)s;
window.card = {focus: 0, resize: 0, ready: 0};
const observer = document.createElement('iframe');
observer.id = 'sdk-observer'; observer.src = PSP + '/pr/342723c/observability/index.html?version=5.50.0&packageName=web-sdk';
observer.width = '1px'; observer.height = '1px'; document.body.appendChild(observer);
let n = 0, current = null;
const mount = late => {
  const el = document.createElement('div'), f = document.createElement('iframe');
  el.className = 'payrails-secure-field'; el.id = 'CARD_FORM-' + (++n) + '-' + Math.random().toString(16).slice(2, 10);
  f.setAttribute('title', 'Payrails Card Form'); f.setAttribute('aria-label', 'Payrails Card Form');
  f.setAttribute('data-frame-id', el.id); f.style.borderStyle = 'none'; f.width = '100%%'; f.height = '0';
  f.setAttribute('src', PSP + '/pr/apps/secure-frame/v1/index.html?mode=collect&type=CARD_FORM&id=' + el.id + '&late=' + (late || 0));
  let ready = false;
  const on = e => {
    const m = e.data;
    if (!m || m.bus !== 'payrails' || e.source !== f.contentWindow) return;
    if (m.name === el.id + '-READY' && !m.data.frameType && !ready) { ready = true;
      f.contentWindow.postMessage({bus: 'payrails', name: el.id + '-SHOW', data: {}}, PSP); }
    if (m.name === el.id + '-READY' && m.data.frameType) window.card.ready++;
    if (m.name === el.id + '-RESIZE') { f.width = '100%%'; f.height = m.data.height; window.card.resize++; }
    if (m.name === el.id + '-FOCUS') { window.card.focus++;
      if (window.onFocusOnce) { const act = window.onFocusOnce; window.onFocusOnce = null; act(); } }
  };
  addEventListener('message', on);
  document.getElementById('mount').appendChild(el); el.appendChild(f);
  current = {el, f, off: () => removeEventListener('message', on)};
};
document.getElementById('payrails_payrailsCC').addEventListener('change', () => { if (!current) mount(0); });
window.remount = late => { current.off(); current.el.remove(); window.card.ready = 0; mount(late); };
window.moved = () => { document.getElementById('other').appendChild(current.el); };
window.own = () => { current.off(); current.el.remove();
  document.getElementById('mount').innerHTML = '<input id="n" autocomplete="cc-number"><input id="e" autocomplete="cc-exp">' +
    '<input id="c" autocomplete="cc-csc"><input id="h" autocomplete="cc-name">'; };
window.stickyBar = () => { const bar = document.createElement('div'); bar.id = 'bar';
  bar.style.cssText = 'position:fixed;left:0;right:0;bottom:0;height:90px;background:#fff;border-top:1px solid #ccc;z-index:20';
  bar.textContent = 'Weiter'; document.body.appendChild(bar);
  window.onFocusOnce = () => { const r = current.f.getBoundingClientRect(); scrollBy(0, r.bottom - innerHeight); }; };
window.covered = () => { const r = current.f.getBoundingClientRect();
  return document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2) !== current.f; };
window.retotal = () => { document.getElementById('total').textContent = 'Gesamtbetrag 59,47 €'; };
</script>"""


def _pages(path, origins):
    path = path.split("?")[0]
    if path == "/pr-checkout":
        return _MERCHANT % {"psp": json.dumps(origins.get("psp", ""))}
    if path == "/pr/apps/secure-frame/v1/index.html":
        return _FRAME
    if path == "/pr/342723c/observability/index.html":
        return '<meta charset="utf-8"><title>Payrails SDK observer</title><div id="app"></div>'
    return None


_CART = {"items": [{"description": "Adult ticket", "quantity": 1}], "total": "57.47", "currency": "EUR"}


def _frame_values(sup):
    """Per card field: held / empty / other, and the input events each got, read in the one card frame."""
    from tools.browser_supervisor import _schedule

    targets = _schedule(sup._cdp("Target.getTargets"), sup._loop, timeout=10)["result"]["targetInfos"]
    frames = [t for t in targets if t["type"] == "iframe" and "/secure-frame/" in t["url"]]
    if len(frames) != 1:
        return None
    fsid = _schedule(sup._cdp("Target.attachToTarget", {"targetId": frames[0]["targetId"], "flatten": True}),
                     sup._loop, timeout=10)["result"]["sessionId"]
    try:
        reply = _schedule(sup._cdp("Runtime.evaluate", {
            "expression": "JSON.stringify([[...document.querySelectorAll('input')].map(e=>[e.name,e.value]),window.log||[]])",
            "returnByValue": True}, session_id=fsid), sup._loop, timeout=10)
    finally:
        _schedule(sup._cdp("Target.detachFromTarget", {"sessionId": fsid}), sup._loop, timeout=10)
    pairs, log = json.loads(reply["result"]["result"]["value"])
    full = {"CARD_NUMBER": CARD["card_number"], "EXPIRATION_DATE": "0735", "CVV": CARD["cvc"]}
    plain = {k: "".join(c for c in v if c.isalnum()) for k, v in pairs}
    return ({k: "held" if v and v == full.get(k) else "empty" if not v else "other" for k, v in plain.items()},
            {k: log.count(k) for k in plain})


def _setup(payment_browser, tmp_path, monkeypatch, wait=10.0):
    sup, origins, card = _fa_setup(payment_browser, tmp_path, monkeypatch, wait=wait)
    original = live._provider_page
    monkeypatch.setattr(live, "_provider_page", lambda path, o: _pages(path, o) or original(path, o))
    monkeypatch.setattr("tools.browser_vault_tool._is_managed", lambda: True)
    monkeypatch.setattr("tools.browser_vault_tool._managed_supervisor", lambda task_id, admit=False: sup)
    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr("tools.browser_use_cli.vault_route", lambda task_id: {"lease": "live", "token": "t"})
    monkeypatch.setattr("tools.browser_control_route.set_capture_guard", lambda lease, token: (True, ""))
    monkeypatch.setattr("tools.browser_payment_surface.claim_payment_surface", lambda s, i: None)
    monkeypatch.setattr("tools.browser_vault_payment.destination_policy", lambda: (True, "live", False))
    navigate(sup, origins["merchant"] + "/pr-checkout")
    return sup, origins, card


def _choose_card(sup, scroll):
    """Choose the card method; ``scroll``: the payment section is scrolled into view first (a click does that)."""
    if scroll:
        sup.evaluate_runtime("document.getElementById('pay').scrollIntoView({block:'center'})")
    sup.evaluate_runtime("document.getElementById('payrails_payrailsCC').click()")
    wait_until(lambda: sup.evaluate_runtime("window.card.ready")["result"] == 1, what="the card form is shown")


_RECEIPT = ("success", "error_type", "filled_fields", "fill_reinspect", "fill_check", "destination_reviewed", "fields",
            "page_match")


@pytest.mark.parametrize("scroll", [True, False])
def test_the_payrails_card_form_fills_once_after_owner_approval(payment_browser, tmp_path, monkeypatch, caplog, scroll):
    """The form as the SDK builds it, in view or below the fold when the fill starts: discovery names the four fields
    in one document on the provider's origin, the approved fill writes number, expiry and CVC once each (the saved card
    has no holder name) and submits nothing."""
    sup, origins, card = _setup(payment_browser, tmp_path, monkeypatch)
    _choose_card(sup, scroll)
    first = _fa_fill(sup, origins, card, caplog)
    assert first["error_type"] == "payment_destination_approval_required", first
    assert first["receiving"] == [{"origin": origins["psp"], "reviewed": False,
                                   "fields": ["cc-csc", "cc-exp", "cc-name", "cc-number"]}], first
    assert first["documents"] == 1, first
    out = _fa_fill(sup, origins, card, caplog, cart=_CART, approve_origins=[origins["psp"]])
    states, writes = _frame_values(sup)
    print("RECEIPT payrails-real", "scroll" if scroll else "below-fold",
          json.dumps({k: out.get(k) for k in _RECEIPT}, sort_keys=True), json.dumps(states), json.dumps(writes))
    assert out["success"] and out["filled_fields"] == 3, out
    assert states == {"CARDHOLDER_NAME": "empty", "CARD_NUMBER": "held", "EXPIRATION_DATE": "held", "CVV": "held"}, states
    assert writes == {"CARDHOLDER_NAME": 0, "CARD_NUMBER": 1, "EXPIRATION_DATE": 1, "CVV": 1}, writes


def _approved_fill(sup, origins, card, caplog, monkeypatch, change):
    """The approved fill, with ``change`` (page script) run where the real stop of 11-14 fell: after the approval and
    its page checks, while the payment lock is claimed and the card is read, before the commit's first check."""
    first = _fa_fill(sup, origins, card, caplog)
    assert first["error_type"] == "payment_destination_approval_required", first

    def claim(_supervisor, _inspection):
        sup.evaluate_runtime(change)

    monkeypatch.setattr("tools.browser_payment_surface.claim_payment_surface", claim)
    out = _fa_fill(sup, origins, card, caplog, cart=_CART, approve_origins=[origins["psp"]])
    print("RECEIPT payrails-real", change, json.dumps({k: out.get(k) for k in _RECEIPT}, sort_keys=True),
          json.dumps([r.getMessage() for r in caplog.records if "card fill" in r.getMessage()]))
    return out


@pytest.mark.parametrize("late", [0, 1500])
def test_a_payrails_form_mounted_again_after_the_approval_is_filled_in_its_new_frame(payment_browser, tmp_path,
                                                                                    monkeypatch, caplog, late):
    """The SDK unmounts the card form and mounts it again (a new element, a new frame, shown at once or 1.5 s later):
    the armed fill finds the complete form in the new frame inside its window and writes each field once."""
    sup, origins, card = _setup(payment_browser, tmp_path, monkeypatch)
    _choose_card(sup, True)
    out = _approved_fill(sup, origins, card, caplog, monkeypatch, f"window.remount({late})")
    states, writes = _frame_values(sup)
    assert out["success"] and out["filled_fields"] == 3 and out["fill_reinspect"] == {"count": 1, "repairs": 0}, out
    assert states == {"CARDHOLDER_NAME": "empty", "CARD_NUMBER": "held", "EXPIRATION_DATE": "held", "CVV": "held"}, states
    assert writes == {"CARDHOLDER_NAME": 0, "CARD_NUMBER": 1, "EXPIRATION_DATE": 1, "CVV": 1}, writes


@pytest.mark.parametrize("change", ["window.moved()", "window.remount(60000)"])
def test_a_payrails_form_that_does_not_come_back_stops_with_its_stage_and_window(payment_browser, tmp_path, monkeypatch,
                                                                                   caplog, change):
    """The stop of 11-14 (payment_page_changed, nothing written, re-inspection refused ``incomplete`` after seeing one
    unreviewed frame), reproduced two ways: the frame's element moved in the page (its document loads again and is never
    shown) and a re-mount whose frame does not show in time. The one frame the re-inspection saw is the SDK's 1px
    observer frame; the card frame is collapsed. The result says where the stop fell (``stopped``) and how
    the re-inspection window was spent (``window``), in counts and fixed codes, from what the fill path holds anyway.
    The text names the lock and payment_close instead of "check the page"."""
    sup, origins, card = _setup(payment_browser, tmp_path, monkeypatch, wait=3.0)
    _choose_card(sup, True)
    out = _approved_fill(sup, origins, card, caplog, monkeypatch, change)
    again = out["fill_reinspect"]
    assert out["error_type"] == "payment_page_changed" and not out.get("filled_fields"), out
    assert (again["count"], again["refused"], again["seen"]) == (0, "incomplete", {"unreviewed_frames_inspected": 1}), again
    assert again["stopped"] == {"stage": "before_write", "reason": "frames_card", "exposure": "shown",
                                "admission": "strict"}, again  # r8: the strict pass chose the (then visible) form
    window = again["window"]
    assert window["codes"] == {"incomplete": window["polls"]} and window["polls"] >= 2 and window["ms"] >= 2500, window
    assert set(again) == {"count", "repairs", "refused", "seen", "stopped", "window"}, out
    assert 0 < window["inspected_ms_ago"] < 60000, window  # the window ended seconds after the inspection
    raw = json.dumps(again)
    assert not any(part in raw for part in ("127.0.0.1", "localhost", "http", "/pr/", "CARD_FORM", "Card holder")), raw
    assert "payment_close" in out["error"] and "Check the page" not in out["error"], out["error"]
    # nothing was written into the card frame (its form was never shown or is gone)
    assert _frame_values(sup) is None or set(_frame_values(sup)[0].values()) <= {"empty"}


@pytest.mark.parametrize("change, error, refused", [("window.own()", "payment_page_changed", "origin_changed"),
                                                    ("window.retotal()", "payment_cart_changed", None)])
def test_another_origin_or_another_total_after_the_approval_still_stops_the_fill(payment_browser, tmp_path, monkeypatch,
                                                                                caplog, change, error, refused):
    """The card fields on the merchant page instead of the approved provider frame, or another total on the page:
    nothing is written."""
    sup, origins, card = _setup(payment_browser, tmp_path, monkeypatch, wait=3.0)
    _choose_card(sup, True)
    out = _approved_fill(sup, origins, card, caplog, monkeypatch, change)
    assert out["error_type"] == error and out["success"] is False and not out.get("filled_fields"), out
    assert (out.get("fill_reinspect") or {}).get("refused") == refused, out
    values = json.loads(sup.evaluate_runtime("JSON.stringify([...document.querySelectorAll('#mount input')].map(e=>e.value))")["result"])
    assert not any(values), values
    frame = _frame_values(sup)
    assert frame is None or set(frame[0].values()) == {"empty"}, frame
    assert "payment_close" in out["error"], out["error"]


def test_a_first_inspection_of_a_collapsed_payrails_frame_refuses_as_before(payment_browser, tmp_path, monkeypatch,
                                                                            caplog):
    """The retry record of 11-12/13 (payment_fields_unsupported, one unreviewed frame inspected): the card frame is
    collapsed and the one frame looked into is the observer. No approval is asked, nothing is read or written, and the
    refusal is exactly what it was: no re-inspection record (the first inspection has none)."""
    sup, origins, card = _setup(payment_browser, tmp_path, monkeypatch)
    _choose_card(sup, True)
    sup.evaluate_runtime("window.moved()")
    wait_until(lambda: sup.evaluate_runtime("document.querySelector('#other iframe').height")["result"] == "0px",
               what="the moved frame collapsed")
    out = _fa_fill(sup, origins, card, caplog)
    print("RECEIPT payrails-real first-refusal", json.dumps(out, sort_keys=True))
    assert out["error_type"] == "payment_fields_unsupported" and out["unreviewed_frames_inspected"] == 1, out
    assert "fill_reinspect" not in out and "inspection" not in out, out


@pytest.mark.parametrize("strict", [False, True])
def test_a_sticky_bottom_bar_over_the_card_frame_after_the_focus_scroll(payment_browser, tmp_path, monkeypatch, caplog,
                                                                       strict):
    """dlz (11-15, D-0094 "b"), the leading guess for the stops of 11-14: the first focus scrolls the card frame under
    the merchant's sticky bottom bar. The tap-approved frame is judged as a reviewed provider's is (shown: laid out,
    visible, not transparent), so the fill writes each field once. With vault.payment_consent_strict_exposure the
    strict rule calls it covered: the stop of 11-14 (payment_page_changed, nothing written, the re-inspection sees only
    the observer frame), now with ``exposure: strict`` in its record. r8: the bar is clear at discovery, so the strict
    pass chooses the frame under either setting (``admission: strict``)."""
    from tools.browser_supervisor import _schedule

    sup, origins, card = _setup(payment_browser, tmp_path, monkeypatch, wait=3.0)
    monkeypatch.setattr("tools.browser_vault_payment.destination_policy", lambda: (True, "live", strict))
    _choose_card(sup, True)
    sup.evaluate_runtime("window.stickyBar()")
    # Headless Chrome fires no focus events in an unfocused page; a real foreground tab does (the FOCUS the SDK hears).
    _schedule(sup._cdp("Page.bringToFront", {}, session_id=sup._page_session_id), sup._loop, timeout=10)
    _schedule(sup._cdp("Emulation.setFocusEmulationEnabled", {"enabled": True}, session_id=sup._page_session_id),
              sup._loop, timeout=10)
    wait_until(lambda: sup.evaluate_runtime("current.f.getBoundingClientRect().height")["result"] > 60,
               what="the card frame took its size")
    assert sup.evaluate_runtime("window.covered()")["result"] is False  # in view and clear when the fill starts
    first = _fa_fill(sup, origins, card, caplog)
    assert first["error_type"] == "payment_destination_approval_required", first
    out = _fa_fill(sup, origins, card, caplog, cart=_CART, approve_origins=[origins["psp"]])
    print("RECEIPT payrails-real sticky-bar", "strict" if strict else "shown",
          json.dumps({k: out.get(k) for k in _RECEIPT}, sort_keys=True))
    assert sup.evaluate_runtime("window.covered()")["result"] is True  # the focus scroll put the bar over it
    assert sup.evaluate_runtime("window.card.focus")["result"] >= 1
    states, writes = _frame_values(sup)
    if not strict:
        assert out["success"] and out["filled_fields"] == 3 and "fill_reinspect" not in out, out
        assert states == {"CARDHOLDER_NAME": "empty", "CARD_NUMBER": "held", "EXPIRATION_DATE": "held",
                          "CVV": "held"}, states
        assert writes == {"CARDHOLDER_NAME": 0, "CARD_NUMBER": 1, "EXPIRATION_DATE": 1, "CVV": 1}, writes
        return
    again = out["fill_reinspect"]
    assert out["error_type"] == "payment_page_changed" and not out.get("filled_fields"), out
    assert (again["count"], again["refused"], again["seen"]) == (0, "incomplete", {"unreviewed_frames_inspected": 1}), again
    assert again["stopped"] == {"stage": "after_focus", "reason": "shown", "exposure": "strict",
                                "admission": "strict"}, again
    assert set(states.values()) == {"empty"} and not any(writes.values()), (states, writes)
    window = again["window"]
    assert window["codes"] == {"incomplete": window["polls"]} and window["polls"] >= 2, window


@pytest.mark.parametrize("strict", [False, True])
def test_a_card_frame_already_under_the_sticky_bar_at_discovery(payment_browser, tmp_path, monkeypatch, caplog, strict):
    """r8 (Opus P2-1): the bar is over the card frame's centre before the first call. The strict pass finds no card form,
    so the reviewed providers' rule chooses the frame (the only one shown besides the 1px observer) and the fill writes
    each field once. With vault.payment_consent_strict_exposure there is no second pass: the form is not found."""
    sup, origins, card = _setup(payment_browser, tmp_path, monkeypatch, wait=3.0)
    monkeypatch.setattr("tools.browser_vault_payment.destination_policy", lambda: (True, "live", strict))
    _choose_card(sup, True)
    wait_until(lambda: sup.evaluate_runtime("current.f.getBoundingClientRect().height")["result"] > 60,
               what="the card frame took its size")
    sup.evaluate_runtime("window.stickyBar(); window.onFocusOnce = null;"
                         "scrollBy(0, current.f.getBoundingClientRect().bottom - innerHeight)")
    assert sup.evaluate_runtime("window.covered()")["result"] is True
    first = _fa_fill(sup, origins, card, caplog)
    if strict:
        assert first["error_type"] == "payment_fields_unsupported", first
        return
    assert first["error_type"] == "payment_destination_approval_required", first
    out = _fa_fill(sup, origins, card, caplog, cart=_CART, approve_origins=[origins["psp"]])
    print("RECEIPT payrails-real covered-at-discovery", json.dumps({k: out.get(k) for k in _RECEIPT}, sort_keys=True))
    _states, writes = _frame_values(sup)
    assert out["success"] and out["filled_fields"] == 3 and "fill_reinspect" not in out, out
    assert writes == {"CARDHOLDER_NAME": 0, "CARD_NUMBER": 1, "EXPIRATION_DATE": 1, "CVV": 1}, writes
