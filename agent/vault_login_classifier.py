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

# Matched on camelCase-split text ("passwordConfirm" -> "password confirm") and, squashed, on run-together
# names ("retypepassword"): a new or confirmation password is never a login target, autocomplete or not.
_RE_EXCLUDED_PASSWORD = re.compile(
    r"\b(?:new|confirm\w*|create|repeat|retype|re ?type|re ?enter|verify)\s*password\b"
    r"|\bpassword\s*(?:confirm\w*|repeat|again|retype|verify\w*)\b")
_RE_EXCLUDED_PASSWORD_SQUASHED = re.compile(
    r"(?:new|confirm|create|repeat|retype|reenter|verify)pass|pass(?:word)?(?:confirm|repeat|again|retype|verify)")
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
    # Inspection's strict geometry: at least 2x2 px, horizontally on screen, inside the document, not
    # transparent. Only a signup target needs it; None (an older inspection) never qualifies.
    rendered: Optional[bool] = None

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
            rendered=raw.get("rendered") if isinstance(raw.get("rendered"), bool) else None,
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


def _camel_text(control: LoginControl) -> str:
    # camelCase split first: "passwordConfirm" must read as "password confirm", not one opaque word.
    return _normalize_text(re.sub(r"([a-z])([A-Z])", r"\1 \2", control.name + " " + control.label))


def _excluded_password(control: LoginControl) -> bool:
    text = _camel_text(control)
    return bool(_RE_EXCLUDED_PASSWORD.search(text) or _RE_EXCLUDED_PASSWORD_SQUASHED.search(text.replace(" ", "")))


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
    if _excluded_password(control):
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


@dataclass(frozen=True)
class SignupTargets:
    """The approved signup write set: the new-password field and, optionally, its ONE explicitly
    labelled confirmation field in the same form. Both receive the same saved password."""

    primary: LoginControl
    confirm: Optional[LoginControl] = None

    @property
    def controls(self) -> tuple[LoginControl, ...]:
        return (self.primary,) if self.confirm is None else (self.primary, self.confirm)


# Shared with the page-JS gate in build_signup_fill_js (same patterns, same normalisation), so the role
# decided here is re-decided in the page right before the write. Confirmation intent must be explicit.
_SIGNUP_CONFIRM_RE = r"\b(?:confirm\w*|repeat\w*|retype\w*|re ?type\w*|re ?enter\w*|verify|again)\b"
# Field intent other than a new password, read in context (shared with the page gate, same normalisation).
# name/id (camelCase split): whole tokens, so "updatePassword", "editPassword", "oldpassword", "passwordreset"
# refuse while "householdPassword" (squashed "...oldpass...") does not.
_SIGNUP_NAME_FORBIDDEN_RE = (r"\b(?:(?:reset|recover|change|update|current|edit)[a-z0-9]*|old|existing"
                             r"|(?:old|current|change|update|reset|existing|edit)pass[a-z0-9]*"
                             r"|[a-z0-9]*pass(?:word)?(?:change|update|reset|edit)[a-z0-9]*)\b")
# Labels, aria text, placeholder, title: a phrase naming another password role ("Current password", "Change
# your password", "Password reset"), unless advice comes first ("New password (do not reuse an existing
# password)", "must differ from your old password").
_SIGNUP_LABEL_FORBIDDEN_RE = (r"^(?:(?!\b(?:not|cannot|can t|don t|dont|never|reuse|re use|differ[a-z]*|different|"
                              r"avoid|unlike|than)\b).)*?"
                              r"\b(?:(?:old|current|existing|previous)\s+password"
                              r"|(?:reset|recover|change|update|edit)[a-z]*\s+(?:(?:your|the|my|account)\s+)?password"
                              r"|password\s+(?:reset|recovery|change|update))\b")


def _camel_words(text: str) -> str:
    return _normalize_text(re.sub(r"([a-z])([A-Z])", r"\1 \2", text))


def _signup_forbidden(control: LoginControl) -> bool:
    return bool(re.search(_SIGNUP_NAME_FORBIDDEN_RE, _camel_words(control.name))
                or re.search(_SIGNUP_LABEL_FORBIDDEN_RE, _camel_words(control.label)))


_signup_intent_text = _camel_text


def select_signup_targets(controls: list[LoginControl]) -> Optional[SignupTargets]:
    """Conservative, separate signup intent: one explicit new password, optionally paired with ONE
    explicitly labelled confirmation field (confirm/repeat/retype/re-enter/verify/again in its name,
    id, label, aria text or placeholder). Two indistinguishable password inputs are never a pair.

    Require a registration route and a real form; refuse recovery/change routes, a third password,
    OTP, mixed login forms and unrelated credential forms anywhere on the page.
    This never relaxes the ordinary login classifier's exclusions.
    """
    from urllib.parse import unquote, urlsplit

    passwords = [c for c in controls if c.type == "password" or
                 {"current-password", "new-password"}.intersection(c.autocomplete.lower().split())]
    if len(passwords) not in (1, 2) or classify_otp_controls(controls):
        return None
    for c in passwords:
        if (c.type != "password" or c.form_index is None or c.autocomplete.lower().split() != ["new-password"]
                or c.rendered is not True or _signup_forbidden(c)):
            return None
    confirms = [c for c in passwords if re.search(_SIGNUP_CONFIRM_RE, _signup_intent_text(c))]
    primaries = [c for c in passwords if c not in confirms]
    if len(primaries) != 1 or len(confirms) != len(passwords) - 1:
        return None
    target, confirm = primaries[0], (confirms[0] if confirms else None)
    # Either DOM order: the roles are explicit (CSS may place the confirmation first in the markup).
    if confirm is not None and (confirm.form_index != target.form_index or
                                (confirm.page_url, confirm.form_action) != (target.page_url, target.form_action)):
        return None
    page, action = urlsplit(target.page_url), urlsplit(target.form_action)
    if (page.scheme != "https" or not page.netloc or
            (page.scheme, page.netloc) != (action.scheme, action.netloc)):
        return None
    routes = " ".join(unquote(urlsplit(u).path + "?" + urlsplit(u).query + "#" + urlsplit(u).fragment)
                      for u in (target.page_url, target.form_action))
    route_words = _normalize_text(routes)
    if (re.search(r"reset|recover|forgot|(?:change|edit|update)password|password(?:change|edit|update)",
                  route_words.replace(" ", "")) or
            re.search(r"\b(?:change|update|edit|security|settings|login|log in|signin|sign in)\b", route_words)):
        return None
    page_path = _normalize_text(unquote(page.path))
    if not re.search(r"\b(?:register|registration|signup|sign up|create account)\b", page_path):
        return None
    # Other visible login/identifier controls must belong to the target's form.
    for control in controls:
        if classify_login_control(control) is not None and control.form_index != target.form_index:
            return None
    return SignupTargets(target, confirm)


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


def registry_name(nonce: str) -> str:
    """The window property under which an inspection (with ``bind``) leaves its identity registry: a function
    holding the inspected input and form REFERENCES in its closure. Nothing is marked on the elements (a JS
    property on a node is copyable onto a clone via getOwnPropertyNames/descriptors); the signup writer gets
    the references back only by calling it with ``bind``, a per-fill secret never written to the DOM, and
    compares by identity. One-shot: the first authorised call releases the references."""
    return "hermesVault_" + nonce


# ``(name, secret, elements, forms)``: copies the references into null-prototype records (no page-reachable
# Array/Object prototype is touched by the copy or by later reads) and defines a non-writable,
# non-configurable getter-less function, so the page can neither replace it nor read what it holds.
_REGISTER_JS = """((name, secret, elements, forms) => {
    const keep = { __proto__: null }, keepForms = { __proto__: null };
    for (let i = 0; i < elements.length; i++) keep[i] = elements[i];
    for (let i = 0; i < forms.length; i++) keepForms[i] = forms[i];
    let held = { __proto__: null, elements: keep, forms: keepForms };
    const gate = (s) => { if (s !== secret || held === null) return null; const h = held; held = null; return h; };
    Object.defineProperty(window, name, { value: gate, writable: false, enumerable: false, configurable: false });
  })"""


def build_inspection_js(nonce: str, bind: str = "") -> str:
    return (_LOGIN_CONTROL_INSPECTION_JS_TEMPLATE.replace("__NONCE__", json.dumps(nonce))
            .replace("__REGISTRY__", json.dumps(registry_name(nonce) if bind else ""))
            .replace("__BIND__", json.dumps(bind)).replace("__REGISTER__", _REGISTER_JS)
            .replace("__RENDERED__", _RENDERED_JS))


# Strict geometry shared by inspection (``rendered``) and the signup writer: a real box of at least 2x2 px,
# horizontally inside the viewport, vertically inside the document (below the fold is fine, -9999px is
# not), and not hidden by opacity/visibility on any ancestor where checkVisibility exists.
_RENDERED_JS = """((e) => {
    const r = e.getBoundingClientRect();
    const de = document.documentElement;
    const height = Math.max(de ? de.scrollHeight : 0, window.innerHeight || 0);
    const top = r.top + (window.scrollY || 0);
    return r.width >= 2 && r.height >= 2 && r.right > 0 && r.left < (window.innerWidth || 0) &&
      top + r.height > 0 && top < height &&
      (typeof e.checkVisibility !== "function" || e.checkVisibility({ opacityProperty: true, visibilityProperty: true }));
  })"""


_LOGIN_CONTROL_INSPECTION_JS_TEMPLATE = """(() => {
  "use strict";  // a page function we call cannot read this script (and its bind secret) through .caller
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
  // Identity registry for the signup writer (see registry_name): references in a closure, nothing on the nodes.
  const registry = __REGISTRY__;
  if (registry) { try { (__REGISTER__)(registry, __BIND__, elements, forms); } catch (_) { /* writer then refuses */ } }
  const rendered = __RENDERED__;
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
      rendered: (() => { try { return rendered(element); } catch (_) { return false; } })(),
    }];
  });
  return JSON.stringify(out);
})()"""


# How long the signup writer waits after its writes before re-checking the form and reading back that each
# field still holds the value (frameworks reset controlled inputs on a later tick). Transports await it.
SIGNUP_SETTLE_MS = 1000

# What the signup writer can report. Every fill gets a fresh random token per outcome, known only to Python
# and to the script's closure; the script returns exactly one token, as a string primitive, and nothing else.
# A page that rewrites JSON/Promise/Array built-ins can garble that answer (it is then unverifiable and
# reported as uncertain) but cannot turn one outcome into another: "none" (nothing written) is only ever
# returned while the closure's write counter is 0, and "mask:<bits>" only after every write ran.
SIGNUP_OUTCOMES = ("none", "origin", "partial:1", "partial:2", "changed",
                   "mask:0", "mask:1", "mask:00", "mask:01", "mask:10", "mask:11")


def new_signup_outcome_tokens() -> dict[str, str]:
    import secrets

    return {outcome: "hv-" + secrets.token_hex(12) for outcome in SIGNUP_OUTCOMES}


def build_signup_fill_js(fills: list[dict[str, Any]], expected_origin: str, nonce: str, target: SignupTargets, *,
                         bind: str, tokens: dict[str, str]) -> str:
    """The signup write: the new-password field, then its confirmation field if approved, same value.

    The approved set is the inspection's own element and form REFERENCES, fetched once from its closure
    registry (see :func:`registry_name`; a clone, a copied property or a replacement form is never the same
    object), read by fixed index, and bound by exact URL and form action. The value setter/getter, event
    dispatch, ``isConnected``, ``form``/``action`` getters and ``Reflect.apply`` are captured before the first
    write, so page code that runs on a write's events cannot redirect a write or the retention read-back
    (no array iteration on that path). Immediately before EACH setter (no ``focus()``, so no page event runs in between), after the
    writes and again after the settle, the whole set is revalidated with the inspection's own traversal (open
    shadow roots included) and predicates: identity, form, action, no ``formaction`` override, rendered size,
    roles, exactly these password inputs on the page, no OTP field, no login control outside the form. A
    changed set stops before the next write and is never retried. No value is ever returned: see
    ``SIGNUP_OUTCOMES``. Only the existing secret transport may evaluate this expression.
    """
    controls = target.controls
    if ([f.get("index") for f in fills] != [c.index for c in controls] or len({f.get("value") for f in fills}) != 1
            or not bind or set(tokens) != set(SIGNUP_OUTCOMES)):
        raise ValueError("signup fills do not match the approved targets")
    patterns = {"confirm": _SIGNUP_CONFIRM_RE, "nameForbidden": _SIGNUP_NAME_FORBIDDEN_RE,
                "labelForbidden": _SIGNUP_LABEL_FORBIDDEN_RE, "otp": _RE_OTP.pattern,
                "email": _RE_EMAIL.pattern, "tel": _RE_TEL.pattern, "username": _RE_USERNAME.pattern,
                "excluded": _RE_EXCLUDED_PASSWORD.pattern, "excludedSquashed": _RE_EXCLUDED_PASSWORD_SQUASHED.pattern}
    values = {
        "__ORIGIN__": json.dumps(expected_origin),
        "__TOKENS__": json.dumps(tokens),
        "__BOUND__": json.dumps({"url": target.primary.page_url, "action": target.primary.form_action,
                                 "registry": registry_name(nonce), "secret": bind,
                                 "indices": [c.index for c in controls], "form": target.primary.form_index,
                                 "stamps": [f"{nonce}:{c.index}" for c in controls]}),
        "__COUNT__": str(len(controls)),
        "__PATTERNS__": json.dumps(patterns),
        "__RENDERED__": _RENDERED_JS,
        "__SETTLE_MS__": str(int(SIGNUP_SETTLE_MS)),
        "__VALUE__": json.dumps(fills[0]["value"]),
    }
    # One pass: the spliced secret is never rescanned for placeholders.
    return re.sub("|".join(values), lambda m: values[m.group(0)], _SIGNUP_FILL_JS_TEMPLATE)


_SIGNUP_FILL_JS_TEMPLATE = """(async () => {
  "use strict";  // a page function we call cannot read this source (value, tokens) through .caller
  const T = __TOKENS__;
  // Intrinsics captured before any page code can run during this script (a write's events are the first).
  const apply = Reflect.apply, P0 = Promise, wait = setTimeout, IE = InputEvent, EV = Event;
  let written = 0;
  // Before any write this is the proven no-write answer; after one it can only be a partial answer.
  const stop = () => (written === 0 ? T["none"] : T["partial:" + written]);
  const expectedOrigin = __ORIGIN__;
  try { if (window.location.origin !== expectedOrigin) return T["origin"]; } catch (_) { return stop(); }
  const bound = __BOUND__;
  const N = __COUNT__;
  const P = __PATTERNS__;
  const rx = (k) => new RegExp(P[k]);
  const reConfirm = rx("confirm"), reNameForbidden = rx("nameForbidden"), reLabelForbidden = rx("labelForbidden"),
    reOtp = rx("otp"), reEmail = rx("email"), reTel = rx("tel"), reUser = rx("username"), reExcluded = rx("excluded"),
    reSquashed = rx("excludedSquashed");
  const rendered = __RENDERED__;
  // Same text and normalisation as the Python classifier (name/id; labels, aria text, placeholder, title).
  const norm = (raw, camel) => {
    let t = String(raw || "");
    if (camel) t = t.replace(/([a-z])([A-Z])/g, "$1 $2");
    return t.normalize("NFKD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  };
  const describeName = (e) => [e.name, e.id].join(" ");
  const describeLabel = (e) => {
    const labels = e.labels ? Array.from(e.labels, (l) => l.textContent || "") : [];
    const root = e.getRootNode ? e.getRootNode() : document;
    const aria = (e.getAttribute("aria-labelledby") || "").split(/\\s+/).filter(Boolean)
      .map((id) => { const n = (root.getElementById ? root.getElementById(id) : null) || document.getElementById(id); return n ? (n.textContent || "") : ""; })
      .join(" ");
    return [...labels, e.getAttribute("aria-label") || "", aria, e.getAttribute("placeholder") || "",
      e.getAttribute("title") || ""].join(" ");
  };
  const describe = (e) => describeName(e) + " " + describeLabel(e);
  const kind = (e) => (e.tagName === "SELECT" ? "select" : (e.type || ""));
  const ac = (e) => String(e.autocomplete || "").toLowerCase().split(/\\s+/).filter(Boolean);
  // The inspection's walk and filter: what the Python classifier saw as the page's controls.
  const collect = () => {
    const out = [];
    (function walk(root) {
      for (const e of root.querySelectorAll("*")) {
        if (e.tagName === "INPUT" || e.tagName === "SELECT") out.push(e);
        if (e.shadowRoot) walk(e.shadowRoot);
      }
    })(document);
    return out;
  };
  const listed = (e) => {
    if (e.disabled || e.readOnly) return false;
    if (["hidden", "submit", "button", "reset", "file", "image", "checkbox", "radio"].includes(e.type)) return false;
    const st = getComputedStyle(e);
    return !(st.display === "none" || st.visibility === "hidden" || e.getClientRects().length === 0);
  };
  const isPassword = (e) => kind(e) === "password" || ac(e).some((t) => t === "current-password" || t === "new-password");
  const isOtp = (e) => ac(e).includes("one-time-code") ||  // classify_otp_controls
    (["text", "tel", "number", "password", ""].includes(kind(e)) && reOtp.test(norm(describe(e), false)));
  const excluded = (e) => { const t = norm(describe(e), true); return reExcluded.test(t) || reSquashed.test(t.replace(/ /g, "")); };
  const isLogin = (e) => {  // classify_login_control
    const t = ac(e);
    if (t.includes("new-password") || t.includes("one-time-code")) return false;
    if (["username", "email", "tel", "current-password"].some((x) => t.includes(x))) return true;
    if (excluded(e)) return false;
    if (["password", "email", "tel"].includes(kind(e))) return true;
    const s = norm(describe(e), false);
    return reEmail.test(s) || reTel.test(s) || reUser.test(s);
  };
  const forbidden = (e) => reNameForbidden.test(norm(describeName(e), true)) || reLabelForbidden.test(norm(describeLabel(e), true));
  const unstamp = () => { try { collect().forEach((e) => e.removeAttribute("data-hermes-vault-slot")); } catch (_) { /* best effort */ } };
  const own = (proto, key, part) => { const d = Object.getOwnPropertyDescriptor(proto, key); return d && typeof d[part] === "function" ? d[part] : null; };
  let valueGet = null, valueSet = null, connected = null, formGet = null, actionGet = null, dispatch = null;
  try {
    valueGet = own(HTMLInputElement.prototype, "value", "get"); valueSet = own(HTMLInputElement.prototype, "value", "set");
    connected = own(Node.prototype, "isConnected", "get"); formGet = own(HTMLInputElement.prototype, "form", "get");
    actionGet = own(HTMLFormElement.prototype, "action", "get"); dispatch = own(EventTarget.prototype, "dispatchEvent", "value");
  } catch (_) { valueGet = null; }
  if (!valueGet || !valueSet || !connected || !formGet || !actionGet || !dispatch) { unstamp(); return stop(); }
  const read = (f, self) => apply(f, self, []);
  // The approved set: the inspection's own references by fixed index, in records no page prototype reaches.
  const set = { __proto__: null };
  let form = null;
  try {
    const gate = window[bound.registry];
    const refs = typeof gate === "function" ? gate(bound.secret) : null;
    let found = 0;
    for (let i = 0; refs && i < N; i++) {
      const e = refs.elements[bound.indices[i]];
      if (e && e.getAttribute("data-hermes-vault-slot") === bound.stamps[i]) { set[i] = e; found += 1; }
    }
    if (refs && found === N) form = refs.forms[bound.form] || null;
  } catch (_) { form = null; }
  if (!form) { unstamp(); return stop(); }
  const valid = () => {
    if (window.location.origin !== expectedOrigin || window.location.href !== bound.url) return false;
    if (read(formGet, set[0]) !== form || read(actionGet, form) !== bound.action ||
        Array.from(form.elements || []).some((x) => x.hasAttribute && x.hasAttribute("formaction"))) return false;
    const shown = collect().filter(listed);
    const passwords = shown.filter(isPassword);
    if (passwords.length !== N || shown.some(isOtp) || shown.some((x) => isLogin(x) && x.form !== form)) return false;
    for (let i = 0; i < N; i++) {
      const e = set[i];
      if (!passwords.includes(e) || e.tagName !== "INPUT" || kind(e) !== "password" ||
          ac(e).join(" ") !== "new-password" || read(formGet, e) !== form || !rendered(e) || forbidden(e) ||
          reConfirm.test(norm(describe(e), true)) !== (i > 0)) return false;
    }
    return true;
  };
  const ok = () => { try { return valid(); } catch (_) { return false; } };
  const value = __VALUE__;
  try {
    for (let i = 0; i < N; i++) {
      // Immediately before each setter; no focus(), so no page handler runs between this check and the write.
      if (!ok()) { unstamp(); return stop(); }
      const e = set[i];
      written += 1;
      apply(valueSet, e, [value]);
      apply(dispatch, e, [new IE("input", { __proto__: null, bubbles: true, composed: true, inputType: "insertText" })]);
      apply(dispatch, e, [new EV("change", { __proto__: null, bubbles: true, composed: true })]);
    }
  } catch (_) { unstamp(); return stop(); }
  const afterWrites = ok();
  unstamp();
  // A page that broke Promise makes this reject: the secret wrapper answers that as unverifiable.
  await new P0((resolve) => wait(resolve, __SETTLE_MS__));
  try {
    if (!afterWrites || !ok()) return T["changed"];
    // Retention: each approved field by its fixed index, through the captured getters only.
    let bits = "";
    for (let i = 0; i < N; i++) bits += read(connected, set[i]) === true && read(valueGet, set[i]) === value ? "1" : "0";
    return T["mask:" + bits];
  } catch (_) { return T["changed"]; }
})()"""


def build_fill_js(fills: list[dict[str, Any]], expected_origin: str, nonce: str = "", refusal_tag: str = "") -> str:
    """Build a JS expression that fills the selected controls and reports only a count. The
    returned expression never echoes the values back.

    ``expected_origin`` is asserted against ``window.location.origin`` synchronously inside the SAME
    evaluated script, immediately before any write. If the page navigated between inspection and fill
    (TOCTOU), the script writes nothing and returns ``{"refused": "origin_changed", "found": <actual>}``:
    proof scope equals mutation scope (#88706). Targets resolve by the ``<nonce>:<index>`` stamp of
    THIS inspection; a ``current-password`` fill additionally requires ``type=password``; ``<select>``
    controls (country, state, expiry month) match an option by value or visible text. No marker is
    left on filled controls so later model-driven DOM reads cannot address them deterministically.
    ``refusal_tag`` (a per-fill random string) rides on the ``origin_changed`` answer, which runs before any
    write, so the caller can tell a real no-write refusal from one a page forged after a write. The page's
    current origin is not reported (a forged ``found`` could carry an encoded value). ``focus()`` runs page
    handlers, so the target is re-checked after it, immediately before the value is set.
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
        "__TAG__": json.dumps(refusal_tag),
    }
    return _FILL_JS_PLACEHOLDER_RE.sub(lambda m: values[m.group(0)], _FILL_JS_TEMPLATE)


_FILL_JS_PLACEHOLDER_RE = re.compile(r"__EXPECTED_ORIGIN__|__FILLS__|__NONCE__|__TAG__")


_FILL_JS_TEMPLATE = """(() => {
  "use strict";  // a page function we call (a hooked dispatchEvent) cannot read the fill values through .caller
  // Captured before a write's events run page code; Python still accepts only a small integer count.
  const stringify = JSON.stringify;
  const expectedOrigin = __EXPECTED_ORIGIN__;
  if (window.location.origin !== expectedOrigin) {
    return stringify({ refused: "origin_changed", tag: __TAG__ });
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
      // focus handlers are page code: re-check the target before the value lands.
      if (!el.isConnected || window.location.origin !== expectedOrigin ||
          (f.token === "current-password" && el.type !== "password")) continue;
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
  return stringify({ filled });
})()"""
