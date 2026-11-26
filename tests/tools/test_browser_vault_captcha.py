"""Captcha signal (J-0643, D-1084 a): the login and code writer's own answer names a captcha the page shows, so the
agent stops re-clicking a submit a bot check swallows. Puzzle vs text vs invisible is decided in the page; Python
accepts only the fixed codes. Synthetic DOM in node."""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from agent.vault_captcha import CAPTCHA_JS, CHALLENGES, challenge_fields
from agent.vault_login_classifier import build_fill_js
from tools.browser_vault_outcome import login_write_result

ORIGIN = "https://www.qatarairways.com"

# A page of elements {tag, attrs, box: [w, h], hidden, shadow: [...], doc: [...]}; enough DOM for the probe and for
# the fill script's walk. `shadow` is an open shadow root's children, `doc` a same-origin frame document's; each
# window answers getComputedStyle for its own elements (`hideIn` hides an element only for the named window).
_DOM = r'''
const spec = @@SPEC@@;
const styleOf = (win) => (e) => ({ display: "block", visibility: (e.hidden || e.hideIn === win.name) ? "hidden" : "visible", opacity: "1" });
const rootOf = (list) => ({ querySelectorAll: (sel) => (sel === "*" ? list : []) });
const build = (specs, win) => specs.map((s) => {
  const e = {
    tagName: s.tag.toUpperCase(), attrs: s.attrs, box: s.box, hidden: !!s.hidden, hideIn: s.hideIn, children: [],
    ownerDocument: { defaultView: win, baseURI: @@ORIGIN@@ + "/login" },
    getAttribute(a) { return a in this.attrs ? this.attrs[a] : null; },
    removeAttribute(a) { delete this.attrs[a]; },
    getBoundingClientRect() { const [w, h] = this.box; return { width: w, height: h, left: 10, top: 10, right: 10 + w, bottom: 10 + h }; },
  };
  if (s.shadow) e.shadowRoot = rootOf(build(s.shadow, win));
  if (s.doc) {
    const inner = { name: "frame", innerWidth: 1280, innerHeight: 800 };
    inner.getComputedStyle = styleOf(inner);
    e.contentDocument = Object.assign(rootOf(build(s.doc, inner)), { location: { origin: @@ORIGIN@@ } });
  }
  return e;
});
global.window = { name: "top", location: { origin: @@ORIGIN@@ }, innerWidth: 1280, innerHeight: 800 };
window.getComputedStyle = styleOf(window);
const els = build(spec, window);
const match = (sel) => {
  if (sel === "*") return els;
  const tags = sel.split(",").map((t) => t.trim().toUpperCase());
  return els.filter((e) => tags.includes(e.tagName));
};
global.document = { baseURI: @@ORIGIN@@ + "/login", querySelectorAll: match };
global.getComputedStyle = window.getComputedStyle;
Promise.resolve(@@SCRIPT@@).then((raw) => process.stdout.write(JSON.stringify(raw)));
'''


def _run(script: str, page: list[dict]) -> object:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the synthetic page")
    src = _DOM.replace("@@SPEC@@", json.dumps(page)).replace("@@ORIGIN@@", json.dumps(ORIGIN)).replace(
        "@@SCRIPT@@", script)
    run = subprocess.run([node], input=src, text=True, capture_output=True, timeout=20, check=False)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def frame(src, box=(304, 78), hidden=False):
    return {"tag": "iframe", "attrs": {"src": src}, "box": list(box), "hidden": hidden}


RECAPTCHA = "https://www.google.com/recaptcha/api2/anchor?ar=1&k=key&size="
BADGE = frame(RECAPTCHA + "invisible", (256, 60))  # the Qatar 11-24 page
CHALLENGE = "https://www.google.com/recaptcha/api2/bframe?hl=en&k=key"


@pytest.mark.parametrize("page, expected", [
    ([BADGE], "captcha_invisible"),
    ([BADGE, frame(CHALLENGE, (400, 580), hidden=True)], "captcha_invisible"),
    ([BADGE, frame(CHALLENGE, (400, 580))], "captcha_puzzle"),
    ([frame(RECAPTCHA + "normal")], "captcha_puzzle"),
    ([frame("https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox")], "captcha_puzzle"),
    ([frame("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/")], "captcha_puzzle"),
    ([{"tag": "img", "attrs": {"src": "/captcha/image.php?t=1"}, "box": [180, 50]},
      {"tag": "input", "attrs": {"name": "code"}, "box": [180, 30]}], "captcha_text"),
    ([{"tag": "img", "attrs": {"src": "/captcha.php"}, "box": [180, 50]}, BADGE], "captcha_text"),
    ([{"tag": "input", "attrs": {"name": "captcha_token", "type": "hidden"}, "box": [0, 0]}], ""),
    ([frame(RECAPTCHA + "normal", (0, 0))], ""),
    ([frame("https://www.google.com/maps/embed?pb=1")], ""),
    ([], ""),
])
def test_the_page_names_the_captcha_it_shows(page, expected):
    assert _run("(" + CAPTCHA_JS + ")()", page) == expected


CAPTCHA_IMG = {"tag": "img", "attrs": {"src": "/captcha.php"}, "box": [180, 50]}
FRAME_URL = "/embedded/login"


@pytest.mark.parametrize("page, expected", [
    # Codex A4: the fill writes into same-origin frames and open shadow roots, so a captcha there is reported too.
    ([{"tag": "iframe", "attrs": {"src": FRAME_URL}, "box": [400, 400], "doc": [CAPTCHA_IMG]}], "captcha_text"),
    ([{"tag": "div", "attrs": {}, "box": [400, 400], "shadow": [CAPTCHA_IMG]}], "captcha_text"),
    ([{"tag": "div", "attrs": {}, "box": [400, 400], "shadow": [BADGE]}], "captcha_invisible"),
    ([{"tag": "iframe", "attrs": {"src": FRAME_URL}, "box": [400, 400], "doc": [frame(CHALLENGE, (400, 580))]}],
     "captcha_puzzle"),
    # measured in its own window: hidden there, so not shown (the top window's style says visible)
    ([{"tag": "iframe", "attrs": {"src": FRAME_URL}, "box": [400, 400],
       "doc": [dict(CAPTCHA_IMG, hideIn="frame")]}], ""),
    # a frame that is itself hidden shows nothing
    ([{"tag": "iframe", "attrs": {"src": FRAME_URL}, "box": [400, 400], "hidden": True, "doc": [CAPTCHA_IMG]}], ""),
])
def test_a_captcha_in_a_same_origin_frame_or_shadow_root_is_reported(page, expected):
    assert _run("(" + CAPTCHA_JS + ")()", page) == expected


def test_the_login_writer_answer_carries_the_captcha_and_nothing_else_new():
    # No fills: the writer's own answer, read in the same evaluate as the write (no extra round trip).
    answer = json.loads(_run(build_fill_js([], ORIGIN), [BADGE]))
    assert answer == {"filled": 0, "reasons": [], "challenge": "captcha_invisible"}
    assert json.loads(_run(build_fill_js([], ORIGIN), [])) == {"filled": 0, "reasons": []}


@pytest.mark.parametrize("challenge", list(CHALLENGES))
def test_login_result_reports_the_captcha_with_its_next_step(challenge):
    meta = SimpleNamespace(kind="login", has_otp=False)
    out = json.loads(login_write_result({"filled": 1, "reasons": ["written"], "challenge": challenge},
                                        [{"token": "current-password"}], 0, SimpleNamespace(name="op"), meta, ORIGIN))
    assert out["success"] and out["challenge"] == challenge and out["challenge_next"] == CHALLENGES[challenge]


@pytest.mark.parametrize("forged", ["captcha_text ", "<b>x</b>", 1, None, ["captcha_puzzle"]])
def test_a_page_forged_challenge_is_never_echoed(forged):
    assert challenge_fields({"challenge": forged}) == {}


def test_the_invisible_check_advice_keeps_a_bank_on_its_own_slot():
    """Opus A1: "sign in from the shopping slot" is for non-bank accounts; a sensitive* tab gets a handoff."""
    plain = challenge_fields({"challenge": "captcha_invisible"})
    bank = challenge_fields({"challenge": "captcha_invisible"}, bank=True)
    assert "shopping" in plain["challenge_next"] and "shopping" not in bank["challenge_next"]
    assert "browser_control handoff" in bank["challenge_next"] and "another slot" in bank["challenge_next"]
    assert challenge_fields({"challenge": "captcha_puzzle"}, bank=True) == challenge_fields({"challenge": "captcha_puzzle"})


@pytest.mark.parametrize("slot, bank", [("sensitive-2", True), ("", True), ("shopping", False)])
def test_a_login_fill_result_picks_the_advice_by_the_tab_slot(monkeypatch, slot, bank):
    from tools.browser_vault_outcome import on_bank_slot

    monkeypatch.setattr("tools.browser_use_cli.vault_route", lambda task: {"slot_id": slot})
    assert on_bank_slot("t1") is bank
    meta = SimpleNamespace(kind="login", has_otp=False)
    out = json.loads(login_write_result({"filled": 1, "reasons": ["written"], "challenge": "captcha_invisible"},
                                        [{"token": "current-password"}], 0, SimpleNamespace(name="op"), meta, ORIGIN,
                                        on_bank_slot("t1")))
    assert ("shopping" not in out["challenge_next"]) is bank
