"""reset_password's page decision (J-0643, D-1086 a; logins approach review 2, KEEP): one generated whole-input test.

The generator builds pages of a known kind: a real choose-a-new-password page (token path, query token, SPA root,
Devise, Django, hash route, Laravel with an email field) or a sign-up form that only looks like one (forgot words in a
modal query, a hash route, a return_to, "preset", a stale /forgot-password path, a hostile /reset posting to
/api/reset, the root), crossed with sign-up texts in six languages (incl. "Get started"/"Continue"), unreadable and
oversized forms, field labels (Password, Create/Choose a password, New password) and navigation chrome ("Don't have an
account? Sign up" as a link, a button[type=button] or a role=link). The oracle is the kind the generator built: a
sign-up page never fills, a reset page always fills.

Three layers see the same pages: the classifier on the form text the inspection should report; the real inspection
script on a synthetic DOM (node); and the real inspection in real Chromium (HERMES_E2E_BROWSER=1, integration:
JS stripping, rendering and heading capture on a real engine)."""

from __future__ import annotations

import glob
import html
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional

import pytest

from agent.vault_login_classifier import (
    LoginControl,
    build_inspection_js,
    reset_page_refusal,
    select_signup_targets_detailed,
)
from tests.tools.test_browser_vault_plain_code import NONCE, run_node

O = "https://site.example"
FILLER = "x" * 1600  # above the inspection's 1500-character cap


@dataclass(frozen=True)
class Page:
    kind: str  # "reset" or "signup": the oracle
    name: str
    url: str
    action: Optional[str]  # None: no <form> (an SPA)
    heading: str
    para: str
    label: str
    submit: str  # "input:<value>" renders an <input type=submit>
    nav: str  # "", "link", "button", "role-link"
    heading_outside: bool  # the heading sits in the form's card, outside the form
    email: bool
    confirm: bool
    state: str = "readable"  # or "oversized", "unreadable" (a throwing innerText: synthetic DOM only)


_NAV = {"": (), "link": ("Don't have an account?", ("A", "Sign up", {})),
        "button": ("Don't have an account?", ("BUTTON", "Sign up", {"htmlType": "button"})),
        "role-link": ("New here?", ("SPAN", "Create account", {"role": "link"}))}

_RESET_KINDS = {  # name: (url, form action or None, labels a real page of that kind uses, email, confirm)
    "token path": (O + "/password/reset/8f2k3j4h5g", O + "/password/reset", ("Password", "New password",
                                                                              "Create a new password"), False, True),
    "query token": (O + "/reset-password?token=abc", O + "/reset-password", ("Password", "New password"), False, False),
    "spa root": (O + "/", None, ("New password", "Choose a new password"), False, False),
    "devise": (O + "/users/password/edit?reset_password_token=x", O + "/users/password", ("New password",), False, True),
    "django": (O + "/accounts/reset/MQ/set-password/", O + "/accounts/reset/MQ/set-password/", ("New password",
                                                                                                "Password"), False, True),
    "hash route": (O + "/#/reset-password?t=1", None, ("Password", "New password"), False, False),
    "laravel": (O + "/reset-password/tok123?email=a%40b.example", O + "/reset-password", ("Password",), True, True),
}
_RESET_TEXTS = [("Reset your password", "", "Save"), ("Choose a new password", "Enter it twice", "Update password"),
                ("Passwort zurücksetzen", "", "Speichern"), ("Réinitialiser votre mot de passe", "", "Valider"),
                ("Change your password", "", "Change my password"), ("", "", "Save")]
_SIGNUP_KINDS = {  # name: (url, form action or None)
    "modal forgot": (O + "/?modal=forgot-password", None),
    "hash forgot": (O + "/#/forgot", None),
    "return_to": (O + "/users/new?return_to=/password/reset", O + "/users"),
    "preset": (O + "/welcome?plan=preset", O + "/welcome"),
    "stale forgot path": (O + "/forgot-password", O + "/forgot-password"),
    "hostile reset path": (O + "/reset", O + "/api/reset"),
    "root": (O + "/", None),
    "root posting to reset": (O + "/", O + "/api/reset"),
}
_SIGNUP_TEXTS = [("Create your account", "", "Sign up"), ("Get started", "Work email", "Continue"),
                 ("Start your free trial", "", "Continue"), ("Konto erstellen", "", "Weiter"),
                 ("Créer un compte", "", "Continuer"), ("Account aanmaken", "", "Doorgaan"),
                 ("Crear una cuenta", "", "Continuar"), ("Crea un account", "", "Continua"),
                 ("Join us today", "", "Join"), ("Welcome", "", "Create account"), ("Welcome", "", "input:Sign up")]
_SIGNUP_LABELS = ("Password", "Create a password", "Choose a password", "New password")
# A sign-up page that names itself nowhere is told apart only by the missing reset evidence (no reset path segment,
# no "new password"): the verdict's accepted residual is the same page with such evidence, so it is not generated.
_QUIET_KINDS = ("modal forgot", "return_to", "preset", "root", "root posting to reset")
_QUIET_LABELS = ("Password", "Create a password", "Choose a password", "Set your password")


def generate() -> list[Page]:
    pages = []
    for (name, (url, action, labels, email, confirm)), (heading, para, submit), nav in itertools.product(
            _RESET_KINDS.items(), _RESET_TEXTS, _NAV):
        for label in labels:
            pages.append(Page("reset", name, url, action, heading, para, label, submit, nav, False, email, confirm))
    for (name, (url, action)), (heading, para, submit), label, nav in itertools.product(
            _SIGNUP_KINDS.items(), _SIGNUP_TEXTS, _SIGNUP_LABELS, _NAV):
        for outside in ((False, True) if action else (False,)):
            pages.append(Page("signup", name, url, action, heading, para, label, submit, nav, outside, True, False))
    for name, label, nav in itertools.product(_QUIET_KINDS, _QUIET_LABELS, _NAV):
        url, action = _SIGNUP_KINDS[name]
        pages.append(Page("signup", name, url, action, "Welcome", "", label, "Continue", nav, False, True, False))
    for (name, (url, action)), state, label in itertools.product(_SIGNUP_KINDS.items(), ("oversized", "unreadable"),
                                                                 _SIGNUP_LABELS):
        pages.append(Page("signup", name, url, action, "Create your account", "", label, "Continue", "link", False,
                          True, False, state))
    return pages


PAGES = generate()


# ── one page as a tree: (tag, text, props, children) ───────────────────────────────────────────────────────────

def _tree(page: Page) -> tuple:
    def el(tag, text="", kids=(), **props):
        return (tag, text, props, list(kids))

    def field(kind, name, aria):
        return el("INPUT", type=kind, name=name, id=name, aria=aria,
                  autocomplete={"password": "new-password", "email": "email"}[kind])

    content = [el("H1", page.heading)] if page.heading and not page.heading_outside else []
    content += [el("P", page.para)] if page.para else []
    content += [el("P", FILLER)] if page.state == "oversized" else []
    content += [el("LABEL", "Email"), field("email", "email", "Email")] if page.email else []
    content += [el("LABEL", page.label), field("password", "password", page.label)]
    content += [el("LABEL", "Confirm password"), field("password", "password_confirmation", "Confirm password")] \
        if page.confirm else []
    content.append(el("INPUT", type="submit", value=page.submit[6:]) if page.submit.startswith("input:")
                   else el("BUTTON", page.submit))
    if page.nav:
        words, (tag, text, props) = _NAV[page.nav]
        content += [el("P", words), el(tag, text, **props)]
    unreadable = {"unreadable": True} if page.state == "unreadable" else {}
    if page.action is None:
        card = el("DIV", "", content, **unreadable)
    else:
        outside = [el("H1", page.heading)] if page.heading and page.heading_outside else []
        card = el("DIV", "", outside + [el("FORM", "", content, action=page.action, **unreadable)])
    return el("DIV", "", [card])


def expected_form_text(page: Page) -> Optional[str]:
    """What the inspection must report: the page's rendered text without links and plain buttons."""
    if page.state != "readable":
        return None
    words = [page.heading, page.para, "Email" if page.email else "", page.label,
             "Confirm password" if page.confirm else "", page.submit.removeprefix("input:"), _NAV[page.nav][0] if page.nav else ""]
    return " ".join(w for w in words if w)


def _to_h(node: tuple) -> str:
    tag, text, props, kids = node
    js = dict(props, **({"text": text} if text else {}))
    if tag == "INPUT":
        js["type"] = props["type"]
    return f"h({json.dumps(tag)}, {json.dumps(js)}" + "".join(", " + _to_h(k) for k in kids) + ")"


def _to_html(node: tuple) -> str:
    tag, text, props, kids = node
    names = {"aria": "aria-label", "htmlType": "type"}
    attrs = "".join(f' {names.get(k, k)}="{html.escape(str(v))}"' for k, v in props.items() if k != "unreadable")
    if tag == "INPUT":
        return f"<input{attrs}>"
    return f"<{tag.lower()}{attrs}>{html.escape(text)}" + "".join(map(_to_html, kids)) + f"</{tag.lower()}>"


def _rows_per_page(page: Page) -> int:
    return 1 + page.email + page.confirm


def decide(page: Page, rows: list[dict]) -> str:
    """reset_password's page decision on these inspection rows ("fill" or the refusal). The page's address and form
    action are the generator's (the synthetic DOM and the file:// page have their own)."""
    controls = [LoginControl.from_dict(dict(r, pageUrl=page.url, formAction=page.action or "")) for r in rows]
    targets, why = select_signup_targets_detailed(controls, "reset")
    return why if targets is None else (reset_page_refusal(targets) or "fill")


def wrong(pages: list[Page], rows_of) -> list[str]:
    bad = []
    for page in pages:
        got = decide(page, rows_of(page))
        if (got == "fill") != (page.kind == "reset"):
            bad.append(f"{page.kind} {page.name!r} {page.heading!r} {page.label!r} nav={page.nav} "
                       f"outside={page.heading_outside} {page.state}: {got}")
    return bad


def _split(pages: list[Page], rows: list[dict]) -> dict[Page, list[dict]]:
    out, at = {}, 0
    for page in pages:
        out[page], at = rows[at:at + _rows_per_page(page)], at + _rows_per_page(page)
    assert at == len(rows), (at, len(rows))
    return out


# ── layer 1: the classifier on the form text the inspection should report ──────────────────────────────────────

def _python_rows(page: Page) -> list[dict]:
    def row(name, label, index):
        return {"type": "password", "name": f"{name} {name}", "label": label, "autocomplete": "new-password",
                "formIndex": 0 if page.action else None, "index": index, "rendered": True, "visible": True,
                "formText": expected_form_text(page)}
    rows = [row("password", page.label, 1)]
    if page.confirm:
        rows.append(row("password_confirmation", "Confirm password", 2))
    if page.email:
        rows.append({"type": "email", "name": "email email", "label": "Email", "autocomplete": "email",
                     "formIndex": 0 if page.action else None, "index": 0, "rendered": True, "visible": True})
    return rows


def test_generated_pages_cover_both_kinds_every_kind_and_every_chrome():
    kinds = {p.kind for p in PAGES}
    assert kinds == {"reset", "signup"} and len(PAGES) > 1000
    assert {p.name for p in PAGES} == set(_RESET_KINDS) | set(_SIGNUP_KINDS)
    assert {p.nav for p in PAGES if p.kind == "reset"} == set(_NAV) == {p.nav for p in PAGES if p.kind == "signup"}


def test_classifier_never_fills_a_generated_signup_page_and_always_fills_a_generated_reset_page():
    assert wrong(PAGES, _python_rows) == []


# ── layer 2: the real inspection script on a synthetic DOM ──────────────────────────────────────────────────────

def _node_rows() -> dict[Page, list[dict]]:
    out = run_node("[" + ", ".join(_to_h(_tree(p)) for p in PAGES) + "]")
    return _split(PAGES, out["rows"])


def test_inspection_reports_the_form_text_without_navigation_and_with_the_cards_heading():
    rows = _node_rows()
    for page in PAGES:
        texts = [r.get("formText") for r in rows[page] if r["type"] == "password"]
        assert texts == [expected_form_text(page)] * len(texts), page
        assert all("formText" not in r for r in rows[page] if r["type"] != "password")


def test_inspected_pages_never_fill_a_signup_page_and_always_fill_a_reset_page():
    rows = _node_rows()
    assert wrong(PAGES, rows.__getitem__) == []


# ── layer 3: the same pages in real Chromium (opt-in) ───────────────────────────────────────────────────────────

def _chromium() -> Optional[str]:
    found = os.environ.get("HERMES_CHROMIUM") or shutil.which("chromium") or shutil.which("google-chrome")
    if found:
        return found
    shells = sorted(glob.glob(os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell")))
    return shells[-1] if shells else None


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("HERMES_E2E_BROWSER", "").strip() != "1",
                    reason="real-browser E2E: set HERMES_E2E_BROWSER=1 to opt in")
def test_real_chromium_inspection_gives_the_same_form_text_and_decision():
    chrome = _chromium()
    if not chrome:
        pytest.skip("no Chromium (set HERMES_CHROMIUM)")
    pages = [p for p in PAGES if p.state != "unreadable"]  # a throwing innerText exists only in the synthetic DOM
    script = ("document.getElementById('out').textContent = " + build_inspection_js(NONCE) + ";")
    doc = ('<!doctype html><html><head><meta charset="utf-8"></head><body>' + "".join(_to_html(_tree(p)) for p in pages)
           + f'<pre id="out"></pre><script>{script}</script></body></html>')
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pages.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(doc)
        run = subprocess.run([chrome, "--headless", "--disable-gpu", "--no-sandbox", f"--user-data-dir={tmp}/profile",
                              "--window-size=1280,800", "--dump-dom", "file://" + path],
                             capture_output=True, text=True, timeout=180, check=False)
    assert run.returncode == 0, run.stderr[-2000:]
    out = re.search(r'<pre id="out">(.*?)</pre>', run.stdout, re.S)
    assert out, run.stdout[-2000:]
    rows = _split(pages, json.loads(html.unescape(out.group(1))))
    mismatched = [(p, [r.get("formText") for r in rows[p] if r["type"] == "password"], expected_form_text(p))
                  for p in pages if any(r.get("formText") != expected_form_text(p)
                                        for r in rows[p] if r["type"] == "password")]
    assert mismatched == []
    assert wrong(pages, rows.__getitem__) == []
