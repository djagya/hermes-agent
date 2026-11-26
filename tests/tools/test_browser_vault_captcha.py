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

# A page of elements {tag, attrs, box: [w, h], hidden}; enough DOM for the probe and for the fill script's walk.
_DOM = r'''
const spec = @@SPEC@@;
const els = spec.map((s) => ({
  tagName: s.tag.toUpperCase(), attrs: s.attrs, box: s.box, hidden: !!s.hidden, children: [],
  getAttribute(a) { return a in this.attrs ? this.attrs[a] : null; },
  removeAttribute(a) { delete this.attrs[a]; },
  getBoundingClientRect() { const [w, h] = this.box; return { width: w, height: h, left: 10, top: 10, right: 10 + w, bottom: 10 + h }; },
}));
const match = (sel) => {
  if (sel === "*") return els;
  const tags = sel.split(",").map((t) => t.trim().toUpperCase());
  return els.filter((e) => tags.includes(e.tagName));
};
global.window = { location: { origin: @@ORIGIN@@ }, innerWidth: 1280, innerHeight: 800 };
global.document = { baseURI: @@ORIGIN@@ + "/login", querySelectorAll: match };
global.getComputedStyle = (e) => ({ display: "block", visibility: e.hidden ? "hidden" : "visible", opacity: "1" });
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
