"""Login / checkout form control classifier for vault autofill.

Python port (~170 LOC) of Merit-Systems/OpenInstinct's
``lib/manager/server/kernel-login-autofill.ts`` (MIT). Classifies visible
input controls on a page into login-autofill tokens. The vault fill path
uses the classification to select the single best current-password control
(the identifier is agent-visible metadata and is typed by the agent
itself via normal input tools).

Scoring:
- exact autocomplete-token match ................ 100
- type=password (not new/confirm/create/repeat) .. 90
- type=email / type=tel .......................... 85
- label/name regex heuristics .................. 70-75
Hard exclusions: autocomplete ``new-password`` / ``one-time-code``, and
label/name text matching ``(new|confirm|create|repeat)\\s*password``.
Explicit signup selection is separate and never weakens these login exclusions.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import itertools

LOGIN_AUTOFILL_TOKENS = ("username", "email", "tel", "current-password")

# Payment / address autocomplete tokens (WHATWG) the checkout fill targets. ``cc-exp`` (combined
# MM/YY) is derived at fill time from exp_month + exp_year. Field-name/label heuristics below back
# up sites that omit autocomplete attributes.
PAYMENT_AUTOFILL_TOKENS = ("cc-number", "cc-name", "cc-exp", "cc-exp-month", "cc-exp-year", "cc-csc")
ADDRESS_AUTOFILL_TOKENS = ("address-line1", "address-line2", "address-level2", "address-level1",
                           "postal-code", "country-name", "country")
# Labels are matched after _normalize_text (lower-case ASCII, umlauts/accents folded: "Prüfnummer" ->
# "prufnummer"). Besides English: German, French, Dutch, Spanish and Italian checkout labels; the CVC row
# comes before the number row because "Kartenprüfnummer" also names a number. ``cardpan``, ``cardcvc2``,
# ``cardexpiremonth`` and ``cardexpireyear`` are the PAYONE Client API hosted-iFrame field names.
_CHECKOUT_HEURISTICS = (
    (re.compile(r"\b(?:cvc|cvv|csc|cvn|ccv|security\s*code|card\s*code|(?:karten)?prufnummer|prufziffer|"
                r"sicherheitscode|cryptogramme|code\s*de\s*securite|beveiligingscode|codigo\s*de\s*seguridad|"
                r"codice\s*di\s*sicurezza|cardcvc2?)\b"), "cc-csc"),
    (re.compile(r"\b(?:card\s*number|cardnumber|ccnumber|cc\s*num|pan|kartennummer|numero\s*de\s*(?:la\s*)?carte|"
                r"kaartnummer|numero\s*de\s*(?:la\s*)?tarjeta|numero\s*(?:della\s*)?carta|cardpan)\b"), "cc-number"),
    (re.compile(r"\b(?:name\s*on\s*card|cardholder|cc\s*name|ccname|karteninhaber(?:in|s)?|name\s*auf\s*der\s*karte|"
                r"titulaire(?:\s*de\s*la\s*carte)?|kaarthouder|titular(?:\s*de\s*la\s*tarjeta)?|"
                r"intestatario(?:\s*della\s*carta)?)\b"), "cc-name"),
    (re.compile(r"\b(?:exp(?:iry|iration)?\s*month|exp\s*mm|ccmonth|ablaufmonat|cardexpiremonth)\b"), "cc-exp-month"),
    (re.compile(r"\b(?:exp(?:iry|iration)?\s*year|exp\s*yy(?:yy)?|ccyear|ablaufjahr|cardexpireyear)\b"), "cc-exp-year"),
    (re.compile(r"\b(?:exp(?:iry|iration)?(?:\s*date)?|mm\s*yy|valid\s*thru|ablaufdatum|gultig\s*bis|"
                r"verfallsdatum|date\s*d\s*expiration|vervaldatum|fecha\s*de\s*(?:caducidad|vencimiento)|"
                r"data\s*di\s*scadenza|scadenza)\b"), "cc-exp"),
    (re.compile(r"\b(?:address\s*(?:line\s*)?2|apt|suite|unit)\b"), "address-line2"),
    (re.compile(r"\b(?:address(?:\s*line\s*1)?|street)\b"), "address-line1"),
    (re.compile(r"\b(?:city|town|locality)\b"), "address-level2"),
    (re.compile(r"\b(?:state|province|region|county)\b"), "address-level1"),
    (re.compile(r"\b(?:zip|postal|postcode)\b"), "postal-code"),
    (re.compile(r"\b(?:country)\b"), "country-name"),
)

# "Issue date (MM / YY)" / "Start date" / "Valid from" fields (Maestro and some debit cards) share
# the expiry's MM/YY shape but are not the expiry.
_RE_NOT_EXPIRY = re.compile(r"\b(?:issue(?:d)?|start(?:s)?|valid\s*(?:from|since)|gultig\s*ab|ausstellungsdatum)\b")
_RE_EXPLICIT_EXPIRY = re.compile(r"\b(?:exp(?:iry|iration)?|valid\s*thru|ablaufdatum|gultig\s*bis|verfallsdatum)\b")

_EXCLUDED_AUTOCOMPLETE = {"new-password", "one-time-code"}

_RE_EXCLUDED_PASSWORD = re.compile(r"\b(?:new|confirm|create|repeat)\s*password\b")
_RE_EMAIL = re.compile(r"\b(?:e[\s-]?mail|email address)\b")
_RE_TEL = re.compile(r"\b(?:phone|telephone|mobile)\b")
_RE_USERNAME = re.compile(
    r"\b(?:user\s*name|username|login|account|member|membership|mileageplus)\b"
)


def _normalize_text(value: str) -> str:
    # Drop the combining marks NFKD splits off ("Prüfnummer" -> "prufnummer"); replacing them with a
    # space split words ("pru fnummer") and no accented label could ever match.
    value = "".join(ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


@dataclass(frozen=True)
class LoginControl:
    """Descriptor of a visible input control, as inspected in the page."""

    autocomplete: str
    form_index: Optional[int]
    index: int
    label: str
    name: str
    type: str
    max_length: Optional[int] = None
    # ``<select>`` only: ``(value, visible text, disabled)`` per option, in DOM order. Months and years
    # are not secrets; the native card fill matches the expiry against them (match_expiry_option).
    options: tuple = ()
    page_url: str = ""
    form_action: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LoginControl:
        form_index = raw.get("formIndex", raw.get("form_index"))
        max_length = raw.get("maxLength", raw.get("max_length"))
        return cls(
            autocomplete=str(raw.get("autocomplete") or ""),
            form_index=int(form_index) if form_index is not None else None,
            index=int(raw.get("index") or 0),
            label=str(raw.get("label") or ""),
            name=str(raw.get("name") or ""),
            type=str(raw.get("type") or ""),
            max_length=int(max_length) if max_length is not None else None,
            options=_options(raw.get("options")),
            page_url=str(raw.get("pageUrl") or ""),
            form_action=str(raw.get("formAction") or ""),
        )


def _options(raw: Any) -> tuple:
    if not isinstance(raw, list):
        return ()
    return tuple((str(o[0]), str(o[1]), o[2] is True) for o in raw[:400]
                 if isinstance(o, (list, tuple)) and len(o) == 3)


@dataclass(frozen=True)
class ClassifiedLoginControl:
    control: LoginControl
    score: int
    token: str


def classify_login_control(control: LoginControl) -> Optional[ClassifiedLoginControl]:
    """Classify one control, or return None if it is not a login fill target."""
    autocomplete_tokens = [
        t for t in control.autocomplete.lower().split() if t
    ]
    if any(t in _EXCLUDED_AUTOCOMPLETE for t in autocomplete_tokens):
        return None

    for token in LOGIN_AUTOFILL_TOKENS:
        if token in autocomplete_tokens:
            return ClassifiedLoginControl(control, 100, token)

    searchable = _normalize_text(
        " ".join(part for part in (control.name, control.label) if part)
    )
    if _RE_EXCLUDED_PASSWORD.search(searchable):
        return None
    if control.type == "password":
        return ClassifiedLoginControl(control, 90, "current-password")
    if control.type == "email":
        return ClassifiedLoginControl(control, 85, "email")
    if control.type == "tel":
        return ClassifiedLoginControl(control, 85, "tel")
    if _RE_EMAIL.search(searchable):
        return ClassifiedLoginControl(control, 75, "email")
    if _RE_TEL.search(searchable):
        return ClassifiedLoginControl(control, 75, "tel")
    if _RE_USERNAME.search(searchable):
        return ClassifiedLoginControl(control, 70, "username")
    return None


_RE_OTP = re.compile(
    r"\b(?:one[\s-]?time|verification|security|auth(?:entication|enticator)?|2fa|two[\s-]?factor|mfa|totp|otp|"
    r"passcode|sms)\b.*\b(?:code|pin|token)\b|\b(?:otp|totp|2fa|mfa|verification\s*code|passcode)\b"
)


def classify_otp_controls(controls: list[LoginControl]) -> list[ClassifiedLoginControl]:
    """The controls that take a second-factor code. ``autocomplete=one-time-code`` is authoritative;
    otherwise a text/tel/number input whose name/label says code/OTP/2FA/verification. Some sites split
    the code into one input per digit (``maxlength=1`` boxes): they are returned in DOM order and the
    fill spreads the code across them."""
    out: list[ClassifiedLoginControl] = []
    for c in controls:
        tokens = c.autocomplete.lower().split()
        if "one-time-code" in tokens:
            out.append(ClassifiedLoginControl(c, 100, "one-time-code"))
            continue
        if c.type not in ("text", "tel", "number", "password", ""):
            continue
        if _RE_OTP.search(_normalize_text(" ".join(p for p in (c.name, c.label) if p))):
            out.append(ClassifiedLoginControl(c, 70, "one-time-code"))
    return out


def select_signup_control(controls: list[LoginControl]) -> Optional[ClassifiedLoginControl]:
    """Conservative, separate signup intent: one explicit new password, no confirmation.

    Require a registration route and a real form; refuse recovery/change routes,
    OTP, mixed login forms and unrelated credential forms anywhere on the page.
    This never relaxes the ordinary login classifier's exclusions.
    """
    from urllib.parse import unquote, urlsplit

    passwords = [c for c in controls if c.type == "password" or
                 {"current-password", "new-password"}.intersection(c.autocomplete.lower().split())]
    if len(passwords) != 1 or classify_otp_controls(controls):
        return None
    target = passwords[0]
    if (target.type != "password" or target.form_index is None or
            target.autocomplete.lower().split() != ["new-password"]):
        return None
    text = _normalize_text(target.name + " " + target.label)
    if re.search(r"\b(?:confirm\w*|repeat\w*|retype\w*|re ?enter\w*|again|reset|recover\w*|change|current|old)\b", text):
        return None
    page, action = urlsplit(target.page_url), urlsplit(target.form_action)
    if (page.scheme != "https" or not page.netloc or
            (page.scheme, page.netloc) != (action.scheme, action.netloc)):
        return None
    routes = " ".join(unquote(urlsplit(u).path + "?" + urlsplit(u).query + "#" + urlsplit(u).fragment)
                      for u in (target.page_url, target.form_action))
    route_words = _normalize_text(routes)
    if (re.search(r"reset|recover|forgot|changepassword|passwordchange", route_words.replace(" ", "")) or
            re.search(r"\b(?:change|update|security|settings|login|log in|signin|sign in)\b", route_words)):
        return None
    page_path = _normalize_text(unquote(page.path))
    if not re.search(r"\b(?:register|registration|signup|sign up|create account)\b", page_path):
        return None
    # Other visible login/identifier controls must belong to the target's form.
    for control in controls:
        if classify_login_control(control) is not None and control.form_index != target.form_index:
            return None
    return ClassifiedLoginControl(target, 100, "new-password")


def select_password_fill(
    classified: list[ClassifiedLoginControl],
    password: str,
) -> list[dict[str, Any]]:
    """Select the single best current-password control to fill.

    The vault fill path is password-only: the identifier is agent-visible
    metadata and is typed by the agent via normal input tools. This picks
    the highest-scoring ``current-password`` control (ties broken by DOM
    order) and returns ``[{"index": int, "token": "current-password",
    "value": password}]`` or ``[]`` when no password field exists.
    """
    passwords = [c for c in classified if c.token == "current-password"]
    if not passwords or not password:
        return []
    best_password = min(passwords, key=lambda c: (-c.score, c.control.index))
    return [
        {
            "index": best_password.control.index,
            "token": "current-password",
            "value": password,
        }
    ]


_SELECT_CARD_TOKENS = frozenset({"cc-exp-month", "cc-exp-year"})


def classify_checkout_control(control: LoginControl) -> Optional[ClassifiedLoginControl]:
    """Classify one control as a payment/address fill target (autocomplete token exact match 100,
    label/name heuristic 70), or None. Password/email inputs are never checkout targets. A ``<select>``
    is only ever an expiry month or year among the card tokens (PAYONE, Worldline HTP, Computop), never
    the number, CVC, holder or a combined expiry."""
    result = _classify_checkout_control(control)
    if (result is not None and control.type == "select" and result.token.startswith("cc-")
            and result.token not in _SELECT_CARD_TOKENS):
        return None
    return result


def _classify_checkout_control(control: LoginControl) -> Optional[ClassifiedLoginControl]:
    tokens = [t for t in control.autocomplete.lower().split() if t]
    for token in PAYMENT_AUTOFILL_TOKENS + ADDRESS_AUTOFILL_TOKENS:
        if token in tokens:
            return ClassifiedLoginControl(control, 100, "country-name" if token == "country" else token)
    if control.type == "email":
        return None
    raw_text = " ".join(part for part in (control.name, control.label) if part)
    searchable = _normalize_text(raw_text)
    if _RE_EMAIL.search(searchable):
        return None  # "CARDHOLDER_EMAIL" (Nexi Build) is not the holder name
    date_words = _normalize_text(re.sub(r"([a-z])([A-Z])", r"\1 \2", raw_text))  # issueDate -> issue date
    label_words = _normalize_text(control.label)
    expiry = _RE_EXPLICIT_EXPIRY.search(label_words)
    other_date = _RE_NOT_EXPIRY.search(label_words)
    explicit_expiry = expiry is not None and (other_date is None or expiry.start() < other_date.start())
    for pattern, token in _CHECKOUT_HEURISTICS:
        if pattern.search(searchable):
            if control.type == "password" and token != "cc-csc":
                return None  # a masked input is only ever the CVC (Unzer UI v2), never a login password
            if token.startswith("cc-exp") and _RE_NOT_EXPIRY.search(date_words) and not explicit_expiry:
                return None  # an issue/start date (Maestro) is not the expiry
            return ClassifiedLoginControl(control, 70, token)
    return None


def _fit_expiry(token: str, value: str, max_length: Optional[int]) -> str:
    """Separate expiry inputs in the shape the field takes: a two-digit month, and a two-digit year
    where the input holds only two characters (Worldpay HPP). A script-set value ignores maxlength,
    so a four-digit year would sit in the field and fail the provider's own check on submit."""
    if token == "cc-exp-month" and value.isdigit():
        return value.zfill(2)
    if token == "cc-exp-year" and value.isdigit() and max_length == 2:
        return value[-2:]
    return value


def match_expiry_option(token: str, value: str, options: tuple) -> Optional[str]:
    """The value of the ONE enabled option of an expiry ``<select>`` whose value or visible text is the
    month ("07" or "7") or the year (four-digit "2035" or two-digit "35"), or None when no option or
    several options match: the caller then refuses the fill. Only numbers match, never a month name, so
    a zero-based month list ("6" shown as "07") reads as ambiguous instead of picking June."""
    if not value.isdigit():
        return None
    if token == "cc-exp-month":
        wanted = {str(int(value)), str(int(value)).zfill(2)}
    elif token == "cc-exp-year" and len(value) == 4:
        wanted = {value, value[-2:]}
    else:
        return None
    matches = [o for o in options if not o[2] and o[0] and (o[0].strip() in wanted or o[1].strip() in wanted)]
    if len(matches) != 1 or sum(o[0] == matches[0][0] for o in options) != 1:
        return None
    return matches[0][0]


def select_checkout_fills(classified: list[ClassifiedLoginControl], secret: dict[str, str],
                          field_tokens: dict[str, str]) -> list[dict[str, Any]]:
    """Map a payment/address secret payload onto the best control per autocomplete token.

    ``field_tokens`` is ``PAYMENT_FIELDS`` / ``ADDRESS_FIELDS`` (agent/vault_store.py). A combined
    ``cc-exp`` control gets ``MM/YY`` from exp_month + exp_year and then suppresses the separate
    month/year fills. Returns ``[{"index", "token", "value"}]``: one control per token, highest
    score then DOM order.
    """
    values: dict[str, str] = {tok: secret[f] for f, tok in field_tokens.items() if secret.get(f)}
    if "cc-exp-month" in values and "cc-exp-year" in values:
        values["cc-exp"] = f"{values['cc-exp-month'].zfill(2)}/{values['cc-exp-year'][-2:]}"
    fills: list[dict[str, Any]] = []
    for token, value in values.items():
        candidates = sorted((c for c in classified if c.token == token), key=lambda c: (-c.score, c.control.index))
        if candidates:
            fills.append({"index": candidates[0].control.index, "token": token,
                          "value": _fit_expiry(token, value, candidates[0].control.max_length)})
    if any(f["token"] == "cc-exp" for f in fills):
        fills = [f for f in fills if f["token"] not in ("cc-exp-month", "cc-exp-year")]
    return fills


# JS expression evaluated in the page to inspect candidate input controls.
# Ported from OpenInstinct's nativeLoginControlInspectionExpression.
# Inspection stamps every input with ``<nonce>:<index>`` under a per-inspection attribute; the fill
# script resolves targets by the stamp of ITS OWN inspection instead of re-querying by position, so
# neither a DOM reflow nor a second inspection in between can redirect the password into another field.
INSPECTION_STAMP_ATTR = "data-hermes-vault-slot"


def build_otp_fills(otp_controls: list[ClassifiedLoginControl], code: str) -> list[dict[str, Any]]:
    """One fill per box. Default: the single best-scoring code field takes the whole code.

    Per-digit entry only when the page unmistakably uses it: exactly len(code) OTP controls that are all
    ``maxlength=1``, all in the same form, and adjacent in DOM order (the classic N-box widget). Anything
    looser (several code-like inputs scattered over a page) gets ONE field, never a digit sprayed across
    unrelated inputs."""
    best = max(otp_controls, key=lambda c: c.score)
    boxes = sorted((c for c in otp_controls if c.control.max_length == 1), key=lambda c: c.control.index)
    if (len(boxes) == len(code)
            and len({b.control.form_index for b in boxes}) == 1
            and all(b.control.index - a.control.index == 1 for a, b in itertools.pairwise(boxes))):
        return [{"index": b.control.index, "token": "one-time-code", "value": ch} for b, ch in zip(boxes, code)]
    return [{"index": best.control.index, "token": "one-time-code", "value": code}]


def build_inspection_js(nonce: str) -> str:
    return _LOGIN_CONTROL_INSPECTION_JS_TEMPLATE.replace("__NONCE__", json.dumps(nonce))


_LOGIN_CONTROL_INSPECTION_JS_TEMPLATE = """(() => {
  const nonce = __NONCE__;
  // Shadow-piercing collect: many logins (Home Assistant, custom-element SPAs) render their
  // form inside a shadow root, where document.querySelectorAll never sees it. Walk every
  // element (shadow hosts can be anything, not just inputs) and recurse into open shadow roots.
  // Forms are collected in the same walk: document.forms misses a <form> inside a shadow root, which
  // would leave its controls ungrouped (formIndex null) and merge split OTP boxes across forms.
  const elements = [];
  const forms = [];
  (function walk(root) {
    for (const e of root.querySelectorAll("*")) {
      if (e.tagName === "INPUT" || e.tagName === "SELECT") elements.push(e);
      else if (e.tagName === "FORM") forms.push(e);
      if (e.shadowRoot) walk(e.shadowRoot);
    }
  })(document);
  elements.forEach((element, index) => element.setAttribute("data-hermes-vault-slot", nonce + ":" + index));
  const out = elements.flatMap((element, index) => {
    if (element.disabled || element.readOnly) return [];
    if (["hidden", "submit", "button", "reset", "file", "image", "checkbox", "radio"].includes(element.type)) return [];
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || element.getClientRects().length === 0) return [];
    const labels = element.labels ? Array.from(element.labels, (l) => l.textContent || "") : [];
    const ariaText = (element.getAttribute("aria-labelledby") || "")
      .split(/\\s+/).filter(Boolean)
      .map((id) => { const r = element.getRootNode(); const n = (r.getElementById ? r.getElementById(id) : null) || document.getElementById(id); return n ? (n.textContent || "") : ""; })
      .join(" ");
    const resolvedFormIndex = element.form ? forms.indexOf(element.form) : -1;
    return [{
      autocomplete: element.autocomplete || "",
      formIndex: resolvedFormIndex >= 0 ? resolvedFormIndex : null,
      pageUrl: document.URL,
      formAction: element.form ? element.form.action : "",
      index,
      maxLength: element.maxLength > 0 ? element.maxLength : null,
      label: [
        ...labels,
        element.getAttribute("aria-label") || "",
        ariaText,
        element.getAttribute("placeholder") || "",
        element.getAttribute("title") || "",
      ].join(" "),
      name: [element.name, element.id].join(" "),
      type: element.tagName === "SELECT" ? "select" : (element.type || ""),
    }];
  });
  return JSON.stringify(out);
})()"""


def build_signup_fill_js(fills: list[dict[str, Any]], expected_origin: str, nonce: str,
                         control: LoginControl) -> str:
    """Revalidate approved field intent synchronously before the native secret write.

    Exact URL/action binding also rejects same-origin navigation during approval.
    Only the existing secret transport may evaluate this expression.
    """
    guard = """(() => {
      const bound = __BOUND__;
      const visible = e => !e.disabled && !e.readOnly && e.getClientRects().length > 0 &&
        getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden';
      const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
      const targets = document.querySelectorAll('[data-hermes-vault-slot="' + bound.stamp + '"]');
      const el = targets.length === 1 ? targets[0] : null;
      if (window.location.href !== bound.url || !el || !visible(el) || !el.form ||
          el.form.action !== bound.action || el.type !== 'password' ||
          Array.from(el.form.elements || []).some(e => e.hasAttribute && e.hasAttribute('formaction')) ||
          el.autocomplete.trim().toLowerCase() !== 'new-password' ||
          inputs.filter(e => e.type === 'password' || /(?:current|new)-password/.test(e.autocomplete)).length !== 1 ||
          inputs.some(e => /one-time-code/.test(e.autocomplete)))
        return JSON.stringify({refused: 'signup_target_changed', filled: 0});
      return __FILL__;
    })()"""
    bound = json.dumps({"stamp": f"{nonce}:{control.index}", "url": control.page_url,
                        "action": control.form_action})
    # Splice once: never rescan secret bytes as template placeholders.
    values = {"__BOUND__": bound, "__FILL__": build_fill_js(fills, expected_origin, nonce)}
    return re.sub(r"__BOUND__|__FILL__", lambda m: values[m.group(0)], guard)


def build_fill_js(fills: list[dict[str, Any]], expected_origin: str, nonce: str = "") -> str:
    """Build a JS expression that fills the selected controls and reports only a count. The
    returned expression never echoes the values back.

    ``expected_origin`` is asserted against ``window.location.origin`` synchronously inside the SAME
    evaluated script, immediately before any write. If the page navigated between inspection and fill
    (TOCTOU), the script writes nothing and returns ``{"refused": "origin_changed", "found": <actual>}``:
    proof scope equals mutation scope (#88706). Targets resolve by the ``<nonce>:<index>`` stamp of
    THIS inspection; a ``current-password`` fill additionally requires ``type=password``; ``<select>``
    controls (country, state, expiry month) match an option by value or visible text. No marker is
    left on filled controls so later model-driven DOM reads cannot address them deterministically.
    """
    payload = json.dumps(
        [{"index": f["index"], "token": f.get("token", "current-password"), "value": f["value"]} for f in fills]
    )
    # One pass: a chained .replace() would rescan the already-spliced fill values, so a secret
    # containing "__NONCE__" (or any later placeholder) got JSON spliced into its string literal.
    values = {
        "__EXPECTED_ORIGIN__": json.dumps(expected_origin),
        "__FILLS__": payload,
        "__NONCE__": json.dumps(nonce),
    }
    return _FILL_JS_PLACEHOLDER_RE.sub(lambda m: values[m.group(0)], _FILL_JS_TEMPLATE)


_FILL_JS_PLACEHOLDER_RE = re.compile(r"__EXPECTED_ORIGIN__|__FILLS__|__NONCE__")


_FILL_JS_TEMPLATE = """(() => {
  const expectedOrigin = __EXPECTED_ORIGIN__;
  if (window.location.origin !== expectedOrigin) {
    return JSON.stringify({ refused: "origin_changed", found: window.location.origin });
  }
  const fills = __FILLS__;
  const nonce = __NONCE__;
  // Stamped controls may live inside a shadow root (see the inspection script's walk): collect the
  // document and every open shadow root once, then look each stamp up in those roots.
  const roots = [];
  (function walk(root) {
    roots.push(root);
    for (const e of root.querySelectorAll("*")) if (e.shadowRoot) walk(e.shadowRoot);
  })(document);
  const find = (selector) => roots.flatMap((r) => Array.from(r.querySelectorAll(selector)));
  let filled = 0;
  const norm = (t) => String(t || "").trim().toLowerCase();
  for (const f of fills) {
    const el = find('[data-hermes-vault-slot="' + nonce + ':' + f.index + '"]')[0];
    if (!el || (f.token === "current-password" && el.type !== "password")) continue;
    try {
      if (el.tagName === "SELECT") {
        const want = norm(f.value);
        const opt = Array.from(el.options).find((o) => [o.value, o.textContent].some((t) => norm(t) === want || norm(t) === want.replace(/^0/, "")));
        if (opt) { el.value = opt.value; el.dispatchEvent(new Event("change", { bubbles: true, composed: true })); filled += 1; }
        continue;
      }
      el.focus();
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
      // one-time-code split into single-character boxes: f.value is the slice for THIS box (see build_otp_fills)
      if (setter && setter.set) { setter.set.call(el, f.value); } else { el.value = f.value; }
      // composed: a component listening on its shadow host must see the value change too.
      el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText" }));
      el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      if (el.value.length > 0) filled += 1;
    } catch (e) { /* skip */ }
  }
  find("[data-hermes-vault-slot]").forEach((n) => n.removeAttribute("data-hermes-vault-slot"));
  return JSON.stringify({ filled });
})()"""
