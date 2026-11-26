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
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional
import itertools

from agent.vault_captcha import CAPTCHA_JS

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
# ``card\s*holder`` covers the separated forms too: Unzer UI v1's hosted holder input is named
# "card-holder" (normalised "card holder") with only a placeholder name as its label. The card rows also match
# the camelCase-split text ("encryptedCardNumber" -> "encrypted card number"); the address rows do not.
# The month row skips "Exp MM/YY" (a combined expiry): "exp mm" followed by a year shape is the cc-exp row's.
_CHECKOUT_HEURISTICS = (
    (re.compile(r"\b(?:cv[cv]2?|csc|cvn|ccv|security\s*code|card\s*code|card\s*verification\s*(?:code|number|value)|"
                r"(?:karten)?prufnummer|(?:karten)?prufziffer|prufnr|(?:karten)?sicherheitscode|cryptogramme|"
                r"code\s*de\s*securite|beveiligingscode|codigo\s*de\s*seguridad|codice\s*di\s*sicurezza|cardcvc2?)\b"),
     "cc-csc"),
    (re.compile(r"\b(?:card\s*number|cardnumber|ccnumber|cc\s*num(?:ber)?|pan|(?:kredit)?kartennummer|"
                r"numero\s*de\s*(?:la\s*)?carte|kaartnummer|numero\s*de\s*(?:la\s*)?tarjeta|numero\s*(?:della\s*)?carta|"
                r"cardpan)\b"), "cc-number"),
    (re.compile(r"\b(?:name\s*on\s*(?:the\s*)?card|name\s*as\s*(?:it\s*)?appears\s*on\s*(?:the\s*|your\s*)?card|"
                r"card\s*holder|cc\s*name|ccname|karteninhaber(?:in|s)?|name\s*auf\s*der\s*karte|"
                r"titulaire(?:\s*de\s*la\s*carte)?|nom\s*sur\s*la\s*carte|kaarthouder|naam\s*op\s*(?:de\s*)?kaart|"
                r"titular(?:\s*de\s*la\s*tarjeta)?|nombre\s*en\s*la\s*tarjeta|"
                r"intestatario(?:\s*della\s*carta)?|titolare\s*della\s*carta)\b"), "cc-name"),
    (re.compile(r"\b(?:exp(?:iry|iration|ires?)?\s*month|exp\s*mm(?!\s*(?:yy|jj|aa))|ccmonth|ablaufmonat|"
                r"cardexpiremonth)\b"), "cc-exp-month"),
    (re.compile(r"\b(?:exp(?:iry|iration|ires?)?\s*year|exp\s*yy(?:yy)?|ccyear|ablaufjahr|cardexpireyear)\b"),
     "cc-exp-year"),
    (re.compile(r"\b(?:exp(?:iry|iration|ires?)?(?:\s*date)?|mm\s*(?:yy|jj|aa){1,2}|valid\s*(?:thru|until)|ablaufdatum|"
                r"gultig\s*bis|gultigkeitsdatum|verfallsdatum|date\s*d\s*expiration|vervaldatum|"
                r"fecha\s*de\s*(?:caducidad|vencimiento)|data\s*di\s*scadenza|scadenza)\b"), "cc-exp"),
    (re.compile(r"\b(?:address\s*(?:line\s*)?2|apt|suite|unit)\b"), "address-line2"),
    (re.compile(r"\b(?:address(?:\s*line\s*1)?|street)\b"), "address-line1"),
    (re.compile(r"\b(?:city|town|locality)\b"), "address-level2"),
    (re.compile(r"\b(?:state|province|region|county)\b"), "address-level1"),
    (re.compile(r"\b(?:zip|postal|postcode)\b"), "postal-code"),
    (re.compile(r"\b(?:country)\b"), "country-name"),
)
_HOLDER_ROW = next(n for n, (_p, token) in enumerate(_CHECKOUT_HEURISTICS) if token == "cc-name")

# A holder cue next to another purpose ("Card holder ZIP code", "card_holder_first_name", "Card holder phone",
# name "card-holder" with label "Merchant name") names that purpose, not the holder's full name: the holder row
# is skipped and the later rows (expiry, address, postal code) still classify the field. A name component (first
# name only, last name only) conflicts; a full-name instruction ("first and last name", "Vorname Nachname", "Name
# Surname", ``_RE_FULL_NAME``) and an instruction or alternative ("as on ID", "or company name",
# ``_RE_HOLDER_INSTRUCTION``) do not. An identification cue conflicts when it names one ("ID number", "Ausweis",
# ``_RE_ID_SEMANTIC``); a bare "id" only in the visible label, never as a DOM id suffix in the name/id attributes
# (``id="cardholder-id"``, ``holderId``).
_RE_HOLDER_CONFLICT = re.compile(
    r"\b(?:\w*phone\w*|tel|telefon\w*|telefono|handy\w*|mobil\w*|cell|fax\w*|whats\s*app|e[\s-]?mail|contact\w*|"
    r"kontakt\w*|birth|dob|age|geburt\w*|identification|document|passport|ssn|social\s*security|nationality|"
    r"nationalitat|staatsangehorigkeit|adres(?:se)?|anschrift|strasse|plz|postleitzahl|stadt|wohnort|ort|land|telefoon\w*|"
    r"woonplaats|indirizzo|direccion|"
    r"(?:house|building)\s*(?:number|no|nr)|hausnummer|benutzer\w*|firma|vat|tax\w*|ust|steuer\w*|iban|account\w*|"
    r"konto|title|salutation|anrede|signature|unterschrift|reference|referenz|initials|occupation|employer|"
    r"relationship|"
    r"gift|loyalty|member\w*|bonus|payback|(?:first|last|given|family|sur|middle)\s*name|vorname|nachname|prenom|"
    r"login|user\s*name|username|password|merchant|company|business)\b")
_RE_FULL_NAME = re.compile(
    r"\b(?:(?:first|given)\s*(?:name\s*)?(?:and\s*)?(?:last|family|sur)\s*name|"
    r"(?:last|family|sur)\s*name\s*(?:and\s*)?(?:first|given)\s*name|name\s*(?:and\s*)?surname|surname\s*(?:and\s*)?name|"
    r"vor(?:name)?\s*(?:und\s*)?nachname|nachname\s*(?:und\s*)?vorname|prenom\s*(?:et\s*)?nom|nom\s*(?:et\s*)?prenom)\b")
_RE_HOLDER_INSTRUCTION = re.compile(
    r"\b(?:as\s*(?:it\s*)?(?:(?:appears|shown|written|printed|stated)\s*)?(?:on|in)\s*(?:your\s*|the\s*)?"
    r"(?:id(?:\s*card)?|passport|card|ausweis)|or\s*(?:a\s*|the\s*)?(?:company|business|firm|organi[sz]ation)"
    r"(?:\s*name)?)\b")
_RE_ID_SEMANTIC = re.compile(
    r"\b(?:id\s*(?:number|no|nr|nummer|card)|(?:national|personal|tax)\s*id|identity|\w*ausweis\w*|cpf|cnpj|dni)\b")
_RE_ID_WORD = re.compile(r"\bid\b")
# The holder named explicitly, so that a tel input type alone does not make it another purpose: the holder words in
# the visible label ("Karteninhaber", "Name on card"), or with a name word anywhere ("cardholderName"). The holder
# words alone in the name/id attributes (``name="card-holder"``) are not explicit.
_RE_NAME_WORD = re.compile(r"\b(?:name|nom|naam|nombre|nome)\b")
_HOLDER_FOREIGN_TYPES = frozenset({"tel", "number", "date", "month", "week", "time", "datetime-local", "url"})
# The WHATWG autocomplete tokens that name a purpose other than the holder's full name. An unknown token ("nope",
# "chrome-off", "false", anti-autofill noise) and new-password name nothing and leave the holder.
_FOREIGN_AUTOCOMPLETE = frozenset({
    "email", "username", "street-address", "postal-code", "given-name", "family-name", "additional-name",
    "one-time-code", "current-password", "cc-given-name", "cc-family-name", "cc-additional-name", "cc-number",
    "cc-csc", "cc-type", "nickname", "sex", "url", "photo", "impp", "language", "webauthn"})
_FOREIGN_AUTOCOMPLETE_PREFIXES = ("tel", "address-", "country", "bday", "honorific-", "organization", "cc-exp",
                                  "transaction-")

# The holder row's joined spellings (the base row's "cardholder", "name on card", "Karteninhaber" and the other
# languages). The separated "card holder" (from "card-holder", "card_holder", "cardHolderName") is new reach and
# counts only on positive evidence (``_separated_holder_named``): one attribute or the visible label is the holder
# phrase plus only ``_RE_HOLDER_NEUTRAL`` words. Unzer UI v1's hosted input is id "card-holder".
_RE_HOLDER_JOINED = re.compile(_CHECKOUT_HEURISTICS[_HOLDER_ROW][0].pattern.replace(r"card\s*holder", "cardholder"))
# The person-name placeholders sites put in a holder input ("Max Mustermann", "John Doe", "Vorname Nachname"): no
# purpose of their own. A holder named only by its name/id attribute counts only beside an empty, neutral or
# placeholder label ("Security question", "PIN", "Recipient" name another purpose).
_RE_HOLDER_PLACEHOLDER = re.compile(
    r"(?:max|erika)\s+mustermann|(?:john|jane)\s+doe|jean\s+dupont|mario\s+rossi|juan\s+perez|jan\s+jansen|"
    r"vorname\s+nachname|first\s+last")
_RE_HOLDER_NEUTRAL = re.compile(
    r"(?:\s*\b(?:name|full|first\s+and\s+last|vorname\s+(?:und\s+)?nachname|name\s+and\s+surname|"
    r"surname\s+and\s+name|(?:of|on)(?:\s+the)?|as|it\s+appears|card|\d{1,2})\b)*\s*"
    r"(?:\b" + _RE_HOLDER_PLACEHOLDER.pattern + r"\b)?\s*")

# Gift, voucher, loyalty, ticket and customer cards share the bank card's words ("Gift card number", "PAYBACK
# Kartennummer", "Ticket expiry date"): such a field is never a card field (the card rows are skipped). "card" is
# a word here, never the start of "cardholder" ("customerCardholder" is the customer's card holder); the German
# compounds (Kundenkarte, Geschenkkarte, Gutscheincode) still match.
_RE_CARD_DECOY = re.compile(
    r"\b(?:gift|geschenk|gutschein|voucher|coupon|promo|loyalty|payback|miles(?:\s*(?:and\s*)?more)?|bonus|"
    r"member(?:ship)?|kunden|ticket|customer)\s*(?:card(?!holder)|karte|kart|code|number|nummer|nr|pin|exp|valid|gultig)")
# An identity document's fields share the card's words ("Passport valid until", "ID card number", "Ausweis gültig
# bis"): the card rows are skipped. Only explicit document purposes count: "Visa" alone is the card brand ("Visa
# CVC", "Expiry date (Visa)") and a visa document only beside a travel word; "document" counts in the visible label,
# and in a name/id attribute only without a card word in it ("documentExpiryDate" is the document's,
# "document-card-number" the card's). Strong card evidence (``_STRONG_CARD_NAMES``) is never vetoed.
_RE_DOCUMENT_PURPOSE = re.compile(
    r"\b(?:passport\w*|reisepass\w*|pass\s*(?:nummer|nr|gultig\w*|ablauf\w*)|passnummer|\w*ausweis\w*|"
    r"(?:id|identity|identification)\s*card|national\s*id|driv(?:er|ing)\w*(?:\s*s)?\s*licen[cs]e|"
    r"fuhrerschein\w*|residence\s*permit|aufenthalts\w*)\b")
_RE_DOCUMENT_WORD = re.compile(r"\b(?:document\w*|dokument\w*|docs?|id\s*(?:exp\w*|valid\w*|gultig\w*|ablauf\w*))\b")
_RE_ATTRIBUTE_CARD_WORD = re.compile(r"\b(?:card\w*|\w*karte\w*|carte|tarjeta|carta|kaart|cc|credit|debit|kredit\w*)\b")
_RE_VISA = re.compile(r"\bvisa\b")
_RE_VISA_TRAVEL = re.compile(r"\b(?:passport\w*|reisepass\w*|travel\w*|entry|einreise\w*|immigration)\b")
# A date of birth shares the expiry's MM/YY shape ("Date of birth (MM/YY)", "Geburtsdatum MM/JJ"): the expiry rows
# are skipped.
_RE_BIRTH = re.compile(r"\b(?:birth\w*|geburt\w*|dob)\b")
# A name/id attribute that is a card field's own name: its field is the card's whatever the label's words. A bare
# "expiry"/"exp" is no such name (a passport form's id="expiry" is the passport's): only card-qualified expiry names.
_STRONG_CARD_NAMES = frozenset({
    "cvc", "cvv", "cvc2", "cvv2", "csc", "cvn", "ccv", "cccsc", "cardcvc", "cardcvv", "cardcvc2", "securitycode",
    "cardnumber", "ccnumber", "ccnum", "cardno", "pan", "cardpan", "ccexp", "cardexpiry", "cardexpirydate", "ccmonth",
    "ccyear", "cardexpiremonth", "cardexpireyear", "ccname", "cardholder", "cardholdername", "nameoncard",
    "encryptedexpirydate", "encryptedexpirymonth", "encryptedexpiryyear"})

# The expiry rows' reach on the base wording ("Expiry date", "Exp MM", "MM/YY", "valid thru", "Ablaufdatum"): any
# label or name with it keeps matching. The wider reach (camelCase-split names, "expires", "valid until",
# "Gültigkeitsdatum", "MM/JJ") counts only with card evidence in the field's own text (``_expiry_reach``): "ESTA valid
# until", "Visa valid until", ``visaValidUntil``, ``visaExpMonth`` are a document's.
_BASE_EXPIRY = {
    "cc-exp-month": re.compile(r"\b(?:exp(?:iry|iration)?\s*month|exp\s*mm(?!\s*(?:yy|jj|aa))|ccmonth|ablaufmonat|"
                               r"cardexpiremonth)\b"),
    "cc-exp-year": re.compile(r"\b(?:exp(?:iry|iration)?\s*year|exp\s*yy(?:yy)?|ccyear|ablaufjahr|cardexpireyear)\b"),
    "cc-exp": re.compile(r"\b(?:exp(?:iry|iration)?(?:\s*date)?|mm\s*yy|valid\s*thru|ablaufdatum|gultig\s*bis|"
                         r"verfallsdatum|date\s*d\s*expiration|vervaldatum|fecha\s*de\s*(?:caducidad|vencimiento)|"
                         r"data\s*di\s*scadenza|scadenza)\b")}

# "Issue date (MM / YY)" / "Start date" / "Valid from" fields (Maestro and some debit cards) share
# the expiry's MM/YY shape but are not the expiry.
_RE_NOT_EXPIRY = re.compile(r"\b(?:issue(?:d)?|start(?:s)?|valid\s*(?:from|since)|gultig\s*ab|ausstellungsdatum)\b")
_RE_EXPLICIT_EXPIRY = re.compile(r"\b(?:exp(?:iry|iration|ires?)?|valid\s*(?:thru|until)|ablaufdatum|gultig\s*bis|"
                                 r"gultigkeitsdatum|verfallsdatum)\b")

_EXCLUDED_AUTOCOMPLETE = {"new-password", "one-time-code"}

# Matched on camelCase-split text ("passwordConfirm" -> "password confirm") and, squashed, on run-together
# names ("retypepassword"): a new or confirmation password is never a login target, autocomplete or not.
_RE_EXCLUDED_PASSWORD = re.compile(
    r"\b(?:new|confirm\w*|create|repeat|retype|re ?type|re ?enter|verify)\s*password\b"
    r"|\bpassword\s*(?:confirm\w*|repeat|again|retype|verify\w*)\b")
_RE_EXCLUDED_PASSWORD_SQUASHED = re.compile(
    r"(?:new|confirm|create|repeat|retype|reenter|verify)pass|pass(?:word)?(?:confirm|repeat|again|retype|verify)")
_RE_EMAIL = re.compile(r"\b(?:e[\s-]?mail|email address)\b")
# "mobile" alone is a layout word (mobile-search, mobile menu): a phone field only next to number/phone.
_RE_TEL = re.compile(r"\b(?:phone|telephone)\b|\bmobile\s*(?:number|no|phone|tel)\b|\b(?:number|phone)\s*mobile\b")
_RE_USERNAME = re.compile(
    r"\b(?:user\s*name|username|login|account|member|membership|mileageplus)\b"
)


def _normalize_text(value: str) -> str:
    # Drop the combining marks NFKD splits off ("Prüfnummer" -> "prufnummer"); replacing them with a
    # space split words ("pru fnummer") and no accented label could ever match.
    value = "".join(ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


_CONTEXT_MAX = 1500


def _bounded_context(raw: Any) -> str:
    """The inspection's context, or "" when it is not a string or exceeds the cap (a truncated context is no
    evidence either way, so it never qualifies a field)."""
    text = raw if isinstance(raw, str) else ""
    return text if len(text) <= _CONTEXT_MAX else ""


def _form_text(raw: Any) -> Optional[str]:
    """The inspection's form text, or None when missing, not a string or oversized (no evidence either way).
    The cap is the form's (checked in the page) plus its card's heading."""
    return raw if isinstance(raw, str) and len(raw) <= 2 * _CONTEXT_MAX else None


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
    # Plain-"code" OTP path only. ``context`` is the RENDERED text (innerText) of the control's own form, or of its
    # nearest container block when it has no form; "" when unavailable or longer than the cap (truncated context
    # never qualifies). Matched against fixed patterns, never echoed. ``visible`` is the inspection's strict
    # visibility (rendered, not transparent, on screen).
    context: str = ""
    # Password fields only, for reset_page_refusal: the rendered text of the field's form (or block without a form)
    # WITHOUT links, [role=link] and button[type=button] (navigation such as "Don't have an account? Sign up"),
    # plus the nearest heading of the card around a form. None when oversized, unreadable or not inspected.
    form_text: Optional[str] = None
    visible: Optional[bool] = None
    # Checkout path only: supplemental text (the payment closure's aria-labelledby targets and title), kept apart
    # from the field's own name/label/placeholder. It may classify a field whose own text is silent and names no
    # other purpose, never override or promote one whose own evidence says otherwise (classify_checkout_control).
    hint: str = ""
    # Field choice (select_password_fill_detailed): the element at the field's centre is the field (or its label);
    # None = not measurable (off screen, older inspection). ``modal``: inside an open <dialog>, an aria-modal
    # container or the URL's :target. ``readonly``: a readonly-until-focus password (listed since 11-18).
    # ``search``: a search box (type=search, inside role=search, or named q/search/query): never a login field.
    topmost: Optional[bool] = None
    modal: Optional[bool] = None
    readonly: bool = False
    search: bool = False

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
            context=_bounded_context(raw.get("context")),
            form_text=_form_text(raw.get("formText")),
            visible=raw.get("visible") if isinstance(raw.get("visible"), bool) else None,
            hint=str(raw.get("hint") or ""),
            topmost=raw.get("topmost") if isinstance(raw.get("topmost"), bool) else None,
            modal=raw.get("modal") if isinstance(raw.get("modal"), bool) else None,
            readonly=raw.get("readonly") is True,
            search=raw.get("search") is True,
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
    # Plain-"code" fallback only: the fill script re-checks visibility right before writing.
    strict: bool = False


def _camel_text(control: LoginControl) -> str:
    # camelCase split first: "passwordConfirm" must read as "password confirm", not one opaque word.
    return _normalize_text(re.sub(r"([a-z])([A-Z])", r"\1 \2", control.name + " " + control.label))


def _excluded_password(control: LoginControl) -> bool:
    text = _camel_text(control)
    return bool(_RE_EXCLUDED_PASSWORD.search(text) or _RE_EXCLUDED_PASSWORD_SQUASHED.search(text.replace(" ", "")))


def classify_login_control(control: LoginControl) -> Optional[ClassifiedLoginControl]:
    """Classify one control, or return None if it is not a login fill target."""
    if control.search:
        return None  # a search box is never an identifier, whatever its autocomplete or name says
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


# D-0361 (owner, 2026-11-18): a re-auth step that names its only password "confirm password" (PyPI's step-up
# dialog, ``confirm_password``) asks for the CURRENT password. Only confirm / verify / re-enter wording; a
# new/create/repeat field, or any field with a partner password in its form, stays excluded.
_RE_LONE_CONFIRM = re.compile(r"\b(?:confirm\w*|verify|re ?enter)\s*(?:your\s+)?password\b"
                              r"|\bpassword\s*(?:confirm\w*|verify\w*)\b")
_RE_NOT_LONE_CONFIRM = re.compile(r"\b(?:new|create|repeat|again|choose|set)\b")
LONE_CONFIRM_SCORE = 80  # below an explicit current-password (100) and a plain password (90)


def _lone_confirm(control: LoginControl, controls: list[LoginControl]) -> bool:
    if control.type != "password" or control.search:
        return False
    if {"new-password", "one-time-code"} & set(control.autocomplete.lower().split()):
        return False
    text = _camel_text(control)
    if not _RE_LONE_CONFIRM.search(text) or _RE_NOT_LONE_CONFIRM.search(text):
        return False
    return not any(o is not control and o.type == "password" and o.form_index == control.form_index
                   for o in controls)


def classify_login_controls(controls: list[LoginControl]) -> list[ClassifiedLoginControl]:
    """:func:`classify_login_control` per control, plus D-0361: a lone confirm/verify/re-enter password (the
    only password in its form, no new-password partner) is a current-password target at ``LONE_CONFIRM_SCORE``."""
    out: list[ClassifiedLoginControl] = []
    for c in controls:
        result = classify_login_control(c)
        if result is None and _lone_confirm(c, controls):
            result = ClassifiedLoginControl(c, LONE_CONFIRM_SCORE, "current-password")
        if result is not None:
            out.append(result)
    return out


_RE_OTP = re.compile(
    r"\b(?:one[\s-]?time|verification|security|auth(?:entication|enticator)?|2fa|two[\s-]?factor|mfa|totp|otp|"
    r"passcode|sms)\b.*\b(?:code|pin|token)\b|\b(?:otp|totp|2fa|mfa|verification\s*code|passcode)\b"
    # German, French, Dutch, Spanish and Italian code words, after _normalize_text (accents folded, "ä" -> "a";
    # field names also spell it "ae").
    r"|\b(?:best(?:a|ae)tigungs?code|verifizierungs?code|sicherheitscode|einmal(?:kennwort|passwort|code)|tan|"
    r"code\s*de\s*(?:verification|confirmation)|verificatiecode|bevestigingscode|"
    r"codigo\s*de\s*verificacion|codice\s*di\s*verifica)\b"
)
# Recovery / backup codes (D-0365): typed only by enter_code with code_source=recovery.
_RE_RECOVERY_CODE = re.compile(r"\b(?:recovery|backup)\s*(?:code|key)s?\b|\brecovery\s*code\s*value\b")
# A field that looks like it takes a code (counted, never classified): code words in its own text, or a short
# text/tel/number box.
_RE_CODEISH = re.compile(r"\b(?:code|codes|otp|pin|token|digit|tan|codigo|codice)\b|code\b")


# A bare "code" field (Google/Microsoft email-verification pages: ``<input type=tel name=code
# aria-label="Enter code">``). The field's own text (name, id, label, placeholder) may hold only these filler
# words besides code/pin; any other word (promo, gift, postal, ...) keeps it out of the bare path.
_BARE_CODE_FILLER = frozenset({"code", "pin", "enter", "the", "your", "a", "please", "type", "input", "digit",
                               "digits", "numeric", "g", "sent", "we", "you", "to", "received", "below", "here"})
# Non-OTP purposes. Checked FIRST, on the field's own text and on its own form/block text, before any
# acceptance route: a verification word elsewhere never outweighs one of these.
_RE_NOT_OTP_CODE = re.compile(
    r"\b(?:promo\w*|gift\w*|coupon\w*|voucher\w*|discount\w*|referr?al\w*|refer|invit\w*|postal|postcode|zip\w*|"
    r"area|country|dial\w*|calling|currency|swift|bic|cvv|cvc|csc|billing|shipping|tracking|sku|redeem\w*|"
    r"redemption|reward\w*|loyalty|affiliate|campaign|search|chat|comments?|"
    r"(?:order|booking|reservation|reference|ref|invoice|ticket|parcel|shipment)\s*(?:number|no|id|code)|"
    r"confirmation\s+(?:number|no|id)|send\s+(?:us\s+a\s+)?message|leave\s+a\s+message|write\s+a\s+message|"
    r"contact\s+us)\b")
# The field's own form (or nearest block) says it is verifying something it just sent.
_RE_VERIFY_CONTEXT = re.compile(
    r"\bverify\b|\bverification\b|\bconfirm\s+your\s+(?:e[\s-]?mail|email address|phone|number|identity|account)\b"
    r"|\bwe\s*(?:ve\s+|have\s+)?sent\b|\bsent\s+(?:you\s+)?(?:a|the|an)\s+(?:\w+\s+)?code\b"
    r"|\benter\s+the\s+(?:\d+\s*digit\s+)?code\b|\bone[\s-]?time\b|\b(?:two|2)[\s-]?step\b"
    r"|\bsecurity\s+code\b")


def _field_text(c: LoginControl) -> str:
    return _normalize_text(re.sub(r"([a-z])([A-Z])", r"\1 \2", " ".join(p for p in (c.name, c.label) if p)))


def _is_bare_code_field(c: LoginControl) -> bool:
    if c.type not in ("text", "tel", "number"):
        return False
    tokens = _field_text(c).split()
    if not any(t in ("code", "pin") for t in tokens):
        return False
    return all(t in _BARE_CODE_FILLER or t.isdigit() for t in tokens)


def _bare_candidate_qualifies(c: LoginControl) -> bool:
    if not _is_bare_code_field(c):
        return False
    # Rendered and visible (strict inspection geometry); an older inspection without it never qualifies.
    if c.visible is not True or c.rendered is not True:
        return False
    block = _normalize_text(c.context)
    if not block:  # missing or truncated context never establishes suitability
        return False
    # 1. a non-OTP purpose in the field's own text or its own form/block refuses, before any acceptance route
    if _RE_NOT_OTP_CODE.search(_field_text(c)) or _RE_NOT_OTP_CODE.search(block):
        return False
    # 2. positive evidence scoped to the field's own form/block (rendered text; page title/headings never count)
    return bool(_RE_VERIFY_CONTEXT.search(block))


SEVERAL_CODE_FIELDS = "several_code_fields"
_MIN_WHOLE_CODE_LEN = 4  # a plain-"code" fallback field must be able to hold a whole code


def classify_otp_controls_detailed(controls: list[LoginControl]) -> tuple[list[ClassifiedLoginControl], str]:
    """``(otp controls, refusal reason)``. The reason is ``""`` or ``SEVERAL_CODE_FIELDS`` (more than one plain
    "code" widget qualified on the page: nothing is chosen)."""
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
    if out:
        return out, ""
    # Only when nothing above matched: a plain "code"/"pin" input, accepted only when it is the page's ONE
    # qualifying field and takes a whole code (no maxlength, or at least 4). Every qualifying field counts, split
    # one-digit boxes included: a fallback never groups boxes into a widget (those go through the
    # one-time-code / regex paths above), so several boxes are "several fields" and refuse.
    plain = [c for c in controls if _bare_candidate_qualifies(c)]
    if len(plain) > 1:
        return [], SEVERAL_CODE_FIELDS
    if plain and (plain[0].max_length is None or plain[0].max_length >= _MIN_WHOLE_CODE_LEN):
        return [ClassifiedLoginControl(plain[0], 60, "one-time-code", strict=True)], ""
    return [], ""


def count_codeish_controls(controls: list[LoginControl]) -> int:
    """How many listed inputs look like a code box (no classification, no values): code words in the field's
    own text, or a text/tel/number input with a maxlength of 1-8. Feeds ``code_field_unrecognised``."""
    n = 0
    for c in controls:
        if c.type not in ("text", "tel", "number", "password", "") or c.search:
            continue
        if _RE_NOT_OTP_CODE.search(_field_text(c)) or _RE_NOT_OTP_CODE.search(_normalize_text(c.context)):
            continue  # a promo / gift / postal / order code is not an unrecognised one-time-code box
        if _RE_CODEISH.search(_field_text(c)) or (c.type != "password" and c.max_length is not None
                                                  and 1 <= c.max_length <= 8):
            n += 1
    return n


def classify_recovery_code_controls(controls: list[LoginControl]) -> list[ClassifiedLoginControl]:
    """D-0365: the field that takes a recovery/backup code ("Enter recovery code"), else the page's one-time-code
    field (some sites take either in one box)."""
    named = [ClassifiedLoginControl(c, 90, "recovery-code") for c in controls
             if c.type in ("text", "tel", "number", "password", "") and not c.search
             and _RE_RECOVERY_CODE.search(_field_text(c))]
    return named or classify_otp_controls(controls)


def classify_otp_controls(controls: list[LoginControl]) -> list[ClassifiedLoginControl]:
    """The controls that take a second-factor code. ``autocomplete=one-time-code`` is authoritative;
    otherwise a text/tel/number input whose name/label says code/OTP/2FA/verification. Some sites split
    the code into one input per digit (``maxlength=1`` boxes): they are returned in DOM order and the
    fill spreads the code across them. Only when none of that matches, a plain "code"/"pin" input is
    accepted if it is the page's single visible candidate whose own form/block text says verification and
    names no other purpose (see ``classify_otp_controls_detailed``)."""
    return classify_otp_controls_detailed(controls)[0]


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


# Registration words (D-0362), matched on normalised route text (path, query, fragment of the page and the form
# action), on the primary field's name/id, and on its form's rendered text: English, German, French, Dutch,
# Spanish and Italian.
_RE_REGISTER_WORDS = re.compile(
    r"\b(?:register|registration|signup|sign up|join|create (?:an )?account|new account|create your account|"
    r"registrieren|registrierung|konto erstellen|konto anlegen|neues konto|"
    r"inscription|s inscrire|inscrire|creer (?:un )?compte|"
    r"registreren|registratie|account aanmaken|"
    r"registro|registrarse|registrate|crear (?:una )?cuenta|"
    r"registrazione|registrati|crea (?:un )?account)\b")
_RE_REGISTER_SQUASHED = re.compile(r"signup|register|registration|newaccount|createaccount|registrier|inscription")
_RE_RECOVERY_ROUTE = re.compile(r"reset|recover|forgot|(?:change|edit|update)password|password(?:change|edit|update)")
_RE_LOGIN_ROUTE = re.compile(r"\b(?:change|update|edit|security|settings|login|log in|signin|sign in)\b")
# D-0364: a choose / set / reset-a-new-password page (emailed links, activations, invitations).
_RE_RESET_ROUTE = re.compile(r"reset|recover|forgot|setpassword|newpassword|choosepassword|activat|invit|"
                             r"passwor(?:t|d)(?:reset|neu)|wachtwoord|motdepasse|contrasena")
_RE_RESET_LABEL = re.compile(
    r"\b(?:new|choose|set|create)\s+(?:a\s+|your\s+)?password\b|\bneues\s+passwort\b|\bnouveau\s+mot\s+de\s+passe\b"
    r"|\bnieuw\s+wachtwoord\b|\bnueva\s+contrasena\b|\bnuova\s+password\b|\bpassword\s+reset\b")
# reset_page_refusal only (no approval, D-1086 a). Sign-up invitations beyond _RE_REGISTER_WORDS:
_RE_SIGNUP_INVITE = re.compile(r"\b(?:get started|start (?:your )?free trial|try (?:it )?(?:for )?free|"
                               r"create (?:your |a )?profile)\b")
# A reset/forgot/recover path segment, on the raw lower-cased path ("/preset" and "/users/new?x=/reset" are not one).
_RE_RESET_SEGMENT = re.compile(r"(?:^|[^a-z])(?:reset|forgot(?:ten)?|recover(?:y)?)(?:[^a-z]|password|passwort|$)"
                               r"|passwordreset")
# A sign-in route segment (reset_page_refusal): "/login?reset=1" says "reset" only in its query.
_RE_LOGIN_SEGMENT = re.compile(r"(?:^|[^a-z])(?:log[-_]?in|sign[-_]?in|anmelden|einloggen|connexion|se-?connecter|"
                               r"inloggen|iniciar-?sesion|accedi)(?:[^a-z]|$)")
# The page says it sets a NEW password (label or form text); "create / choose / set a password" is sign-up wording.
_RE_RESET_PHRASE = re.compile(
    r"\b(?:new password|password reset|reset (?:your |my |the )?password|"
    r"neues passwort|passwort zurucksetzen|nouveau mot de passe|reinitialiser (?:votre |le |mon )?mot de passe|"
    r"nieuw wachtwoord|wachtwoord (?:opnieuw instellen|herstellen|resetten)|"
    r"nueva contrasena|restablecer (?:tu |su |la )?contrasena|nuova password|reimposta(?:re)? (?:la )?password)\b")
# Old/current password wording: a change-password form, never a reset target (the old password is not ours to set).
_RESET_OLD_RE = r"\b(?:old|current|existing|previous)\s*password\b|(?:old|current|existing)pass"


def _route_text(*urls: str) -> str:
    from urllib.parse import unquote, urlsplit

    return " ".join(unquote(urlsplit(u).path + "?" + urlsplit(u).query + "#" + urlsplit(u).fragment)
                    for u in urls if u)


def select_signup_targets_detailed(controls: list[LoginControl],
                                   purpose: str = "signup") -> tuple[Optional[SignupTargets], str]:
    """``(targets, "")`` or ``(None, reason)``, a fixed reason code. ``purpose`` is ``signup`` or ``reset``.

    One new password, optionally paired with ONE explicitly labelled confirmation field (confirm/repeat/
    retype/re-enter/verify/again in its name, id, label, aria text or placeholder) in the same form (or both
    outside any form, an SPA). Two indistinguishable password inputs are never a pair; a third password on the
    page, a code field, a current-password autocomplete or a page posting to another origin refuse.

    Signup (D-0362, widened 11-18): any autocomplete but current-password; registration evidence in the route
    (several languages), the field's name or its form's text; recovery routes refuse (use purpose=reset), and
    a login/settings route only without registration words in the route. Reset (D-0364): evidence of a
    choose-a-new-password page (route, label or autocomplete=new-password); old/current-password fields refuse.
    """
    from urllib.parse import urlsplit

    passwords = [c for c in controls if c.type == "password" or
                 {"current-password", "new-password"}.intersection(c.autocomplete.lower().split())]
    if not passwords:
        return None, "no_password_field"
    if len(passwords) > 2:
        return None, "too_many_password_fields"
    if classify_otp_controls(controls):
        return None, "code_field_present"
    for c in passwords:
        if c.type != "password":
            return None, "field_not_password_type"
        if c.rendered is not True:
            return None, "field_not_rendered"
        if "current-password" in c.autocomplete.lower().split():
            return None, "field_is_current_password"
        if purpose == "signup" and _signup_forbidden(c):
            return None, "field_names_other_password_role"
        if purpose == "reset" and (re.search(_RESET_OLD_RE, _camel_words(c.name + " " + c.label))
                                   or re.search(_RESET_OLD_RE, _camel_words(c.name).replace(" ", ""))):
            return None, "field_is_old_password"
    confirms = [c for c in passwords if re.search(_SIGNUP_CONFIRM_RE, _signup_intent_text(c))]
    primaries = [c for c in passwords if c not in confirms]
    if len(primaries) != 1 or len(confirms) != len(passwords) - 1:
        return None, "password_pair_unclear"
    target, confirm = primaries[0], (confirms[0] if confirms else None)
    # Either DOM order: the roles are explicit (CSS may place the confirmation first in the markup).
    if confirm is not None and (confirm.form_index != target.form_index or
                                (confirm.page_url, confirm.form_action) != (target.page_url, target.form_action)):
        return None, "confirm_in_other_form"
    page = urlsplit(target.page_url)
    action = urlsplit(target.form_action) if target.form_action else page
    if page.scheme != "https" or not page.netloc:
        return None, "page_not_https"
    if (page.scheme, page.netloc) != (action.scheme, action.netloc):
        return None, "form_posts_elsewhere"
    route_words = _normalize_text(_route_text(target.page_url, target.form_action))
    squashed = route_words.replace(" ", "")
    if purpose == "reset":
        if not (_RE_RESET_ROUTE.search(squashed) or _RE_RESET_LABEL.search(_camel_words(target.label + " " + target.name))
                or "new-password" in target.autocomplete.lower().split()):
            return None, "no_reset_evidence"
        return SignupTargets(target, confirm), ""
    if _RE_RECOVERY_ROUTE.search(squashed):
        return None, "recovery_route"
    # A login/settings PATH refuses unless the path itself also names registration ("/login?next=/signup" is a
    # login page); registration words in the query or fragment count only on a path that names no login.
    path_words = _normalize_text(" ".join(urlsplit(u).path for u in (target.page_url, target.form_action) if u))
    path_register = bool(_RE_REGISTER_WORDS.search(path_words) or _RE_REGISTER_SQUASHED.search(path_words.replace(" ", "")))
    if _RE_LOGIN_ROUTE.search(path_words) and not path_register:
        return None, "login_route"
    if not (_RE_REGISTER_WORDS.search(route_words) or _RE_REGISTER_SQUASHED.search(squashed)
            or _RE_REGISTER_SQUASHED.search(_camel_words(target.name).replace(" ", ""))
            or _RE_REGISTER_WORDS.search(_normalize_text(target.context))):
        return None, "no_registration_evidence"
    return SignupTargets(target, confirm), ""


def _route_paths(*urls: str) -> str:
    """Each URL's path and hash-route path (``#/...``, ``#!/...``), unquoted and lower-cased; never a query."""
    from urllib.parse import unquote, urlsplit

    parts = []
    for url in filter(None, urls):
        split = urlsplit(url)
        parts += [split.path, split.fragment.split("?")[0] if split.fragment.startswith(("/", "!/")) else ""]
    return unquote(" ".join(parts)).lower()


def _names_registration(text: str) -> bool:
    words = _normalize_text(text)
    return bool(_RE_REGISTER_WORDS.search(words) or _RE_SIGNUP_INVITE.search(words))


def reset_page_refusal(targets: SignupTargets) -> str:
    """The strict gate of ``browser_vault reset_password``, which asks nobody. One ordered check, no override:

    1. ``registration_page``: registration words in the page's path or hash-route path, the form action's path, the
       field's name or label (label, placeholder, aria text, title), or the field's ``form_text`` (links and plain
       buttons already left out by the inspection);
    2. ``login_page``: a login/sign-in path segment (any of several languages) and no reset segment in the same
       path: "/login?reset=1" is a sign-in page that says it was reset, never a choose-a-new-password page;
    3. ``form_text_unreadable``: no ``form_text`` (oversized, unreadable, an older inspection);
    4. fill (``""``) only on positive reset evidence: a reset/forgot/recover segment in the page's path or hash-route
       path, or "new password" / "reset your password" (or its translation) in the label or the form text;
       else ``no_reset_evidence``. ``autocomplete=new-password`` and "create/choose/set a password" are sign-up
       wording too, and a query or form action never counts as reset evidence.

    ``fill purpose=reset`` keeps its looser selection because it asks the user first."""
    field = targets.primary
    where = _normalize_text(_route_paths(field.page_url, field.form_action)) + " " + _camel_words(field.name)
    if (_names_registration(where) or _RE_REGISTER_SQUASHED.search(where.replace(" ", ""))
            or any(_names_registration(_camel_words(c.label + " " + c.name))
                   or _RE_REGISTER_SQUASHED.search(_camel_words(c.name).replace(" ", "")) for c in targets.controls)
            or (field.form_text is not None and _names_registration(field.form_text))):
        return "registration_page"
    path = _route_paths(field.page_url)
    if _RE_LOGIN_SEGMENT.search(path) and not _RE_RESET_SEGMENT.search(path):
        return "login_page"
    if field.form_text is None:
        return "form_text_unreadable"
    if (_RE_RESET_SEGMENT.search(path)
            or _RE_RESET_PHRASE.search(_normalize_text(field.label + " " + field.form_text))):
        return ""
    return "no_reset_evidence"


def select_signup_targets(controls: list[LoginControl]) -> Optional[SignupTargets]:
    """Signup targets or None; see :func:`select_signup_targets_detailed` (which also names the reason).
    This never relaxes the ordinary login classifier's exclusions."""
    return select_signup_targets_detailed(controls)[0]


PASSWORD_FIELD_AMBIGUOUS = "password_field_ambiguous"
PASSWORD_OUTSIDE_DIALOG = "password_outside_open_dialog"
NO_PASSWORD_FIELD = "no_password_field"


def _shown_rank(c: ClassifiedLoginControl) -> tuple:
    """Lower is better: rendered + visible first, then the field that is on top at its centre (None, not
    measurable, sits between a field on top and a covered one), then the classifier score."""
    ctl = c.control
    shown = 0 if (ctl.visible is True and ctl.rendered is True) else 1
    top = {True: 0, None: 1, False: 2}[ctl.topmost]
    return shown, top, -c.score


def choose_password_control(classified: list[ClassifiedLoginControl],
                            controls: Optional[list[LoginControl]] = None) -> tuple[Optional[ClassifiedLoginControl], str]:
    """``(the current-password control to fill, "")`` or ``(None, reason)``.

    An open dialog (``<dialog open>``, ``aria-modal``, the URL's ``:target``) that holds a password input is
    what the user sees: only password fields inside it count (PyPI's step-up dialog over the change-password
    form). A dialog without one (a newsletter box with an e-mail field) does not hide the page's login form.
    Then rendered + visible + topmost fields win, then the score; among equal best candidates the first in the
    DOM is filled (a header quick-login and the main form, as before 11-18). Equal best candidates in different
    documents (frames, origins) are ``password_field_ambiguous``: nothing is written rather than a guess."""
    passwords = [c for c in classified if c.token == "current-password"]
    if not passwords:
        return None, NO_PASSWORD_FIELD
    pool = controls if controls is not None else [c.control for c in classified]
    if any(c.modal is True and c.type == "password" for c in pool):
        passwords = [c for c in passwords if c.control.modal is True]
        if not passwords:
            return None, PASSWORD_OUTSIDE_DIALOG
    best = min(_shown_rank(c) for c in passwords)
    tied = sorted((c for c in passwords if _shown_rank(c) == best), key=lambda c: c.control.index)
    if len({c.control.page_url for c in tied}) > 1:
        return None, PASSWORD_FIELD_AMBIGUOUS
    return tied[0], ""


def select_password_fill_detailed(classified: list[ClassifiedLoginControl], password: str,
                                  controls: Optional[list[LoginControl]] = None) -> tuple[list[dict[str, Any]], str]:
    """``(fills, reason)``: one current-password fill, or ``([], reason)`` (see :func:`choose_password_control`)."""
    target, reason = choose_password_control(classified, controls)
    if target is None or not password:
        return [], reason or NO_PASSWORD_FIELD
    return [{"index": target.control.index, "token": "current-password", "value": password}], ""


def select_password_fill(
    classified: list[ClassifiedLoginControl],
    password: str,
) -> list[dict[str, Any]]:
    """Select the single best current-password control to fill.

    The vault fill path is password-only: the identifier is agent-visible
    metadata and is typed by the agent via normal input tools. Returns
    ``[{"index": int, "token": "current-password", "value": password}]`` or
    ``[]`` when no password field exists or the choice is ambiguous (see
    :func:`select_password_fill_detailed` for the reason).
    """
    return select_password_fill_detailed(classified, password)[0]


_SELECT_CARD_TOKENS = frozenset({"cc-exp-month", "cc-exp-year"})


def classify_checkout_control(control: LoginControl) -> Optional[ClassifiedLoginControl]:
    """Classify one control as a payment/address fill target (autocomplete token exact match 100,
    label/name heuristic 70), or None. Password/email inputs are never checkout targets. A ``<select>``
    is only ever an expiry month or year among the card tokens (PAYONE, Worldline HTP, Computop), never
    the number, CVC, holder or a combined expiry.

    The field's own text (name, id, label, aria-label, placeholder) decides. The supplemental ``hint`` (the
    aria-labelledby targets and title) is read only when that own text classifies nothing and is positively
    neutral (empty or generic ids only), the input's type and autocomplete leave it free, and the hint names one
    card role (``_hint_may_classify``): an input labelled "ID" or "Contact number" and titled "Card number" stays
    None, an input with no own text labelled by "Card number" is the number."""
    result = _select_guard(control, _classify_checkout_control(control))
    if result is not None or not control.hint.strip():
        return result
    if not _hint_may_classify(control):
        return None
    view = replace(control, name="", label=control.hint, hint="")
    hinted = _select_guard(view, _classify_checkout_control(view))
    if hinted is None:
        return None
    return ClassifiedLoginControl(control, hinted.score, hinted.token)


def _select_guard(control: LoginControl, result: Optional[ClassifiedLoginControl]) -> Optional[ClassifiedLoginControl]:
    if (result is not None and control.type == "select" and result.token.startswith("cc-")
            and result.token not in _SELECT_CARD_TOKENS):
        return None
    return result


# Input types whose own purpose leaves room for a card field named only by the supplemental text. tel, number,
# email, date and the rest name a purpose of their own (a phone, a quantity) the hint must not override.
_HINT_FREE_TYPES = frozenset({"", "text", "password", "select"})
# Own-text words that name nothing: generated ids ("f1", "field-3", "input2", "el7"), a single letter, digits, a
# random id (letters in runs of one between digits: "f_3a9", ":r1:", "a1b2c3"), a hex/UUID-ish id, punctuation or
# mask characters ("••••"). Every other word ("ID", "Contact", "Phone", "Name", "Max") blocks the hint.
_RE_GENERIC_ID = re.compile(r"(?:f\d+|field\d*|input\d*|el\d+|\d+|[a-z]|(?:[a-z]?\d+)+[a-z]?)")
_RE_HEX_ID = re.compile(r"(?=[0-9a-f]*\d)[0-9a-f]{8,}")


def _generic_word(word: str) -> bool:
    compact = re.sub(r"[^a-z0-9]+", "", _normalize_text(word))
    return not compact or bool(_RE_GENERIC_ID.fullmatch(compact) or _RE_HEX_ID.fullmatch(compact))


def _card_roles(text: str) -> set:
    """The card roles (number, CVC, holder, expiry) the card rows find in ``text``."""
    texts = (_normalize_text(text), _split_camel(text))
    return {("cc-exp" if token.startswith("cc-exp") else token)
            for pattern, token in _CHECKOUT_HEURISTICS if token.startswith("cc-")
            for t in texts if pattern.search(t)}


def _drops_letters(text: str) -> bool:
    """Whether ``_normalize_text`` would drop a letter of ``text`` (Cyrillic, Chinese, Arabic, "ß", "ø"): the folded
    text then reads as if the word were not there, so it is no evidence of silence ("Номер телефона")."""
    folded = (ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return any(ord(ch) > 127 and unicodedata.category(ch)[0] == "L" for ch in folded)


# The words a hint may hold besides its one card role ("Enter your card number", "Card number (16 digits)",
# "Security code (3 digits on the back)"). Anything else ("and PIN", "recovery code", "or phone") is another purpose.
_HINT_NEUTRAL_WORDS = frozenset({"enter", "your", "the", "a", "please", "type", "input", "field", "digit", "digits",
                                 "numeric", "required", "full", "name", "of", "on", "back", "front", "card", "credit",
                                 "debit"})


def _hint_is_one_role(hint: str) -> bool:
    """The whole hint is ONE card role plus ``_HINT_NEUTRAL_WORDS`` and short numbers: the card rows' matches are cut
    out and every word left must be neutral (checked on the plain and the camel-split text). A hint naming one role
    among other words ("Card number and PIN", "Card number recovery code") says nothing about THIS input."""
    if _drops_letters(hint) or len(_card_roles(hint)) != 1:
        return False
    for text in (_normalize_text(hint), _split_camel(hint)):
        for pattern, token in _CHECKOUT_HEURISTICS:
            if token.startswith("cc-"):
                text = pattern.sub(" ", text)
        if all(w in _HINT_NEUTRAL_WORDS or (w.isdigit() and len(w) <= 2) for w in text.split()):
            return True
    return False


def _hint_may_classify(control: LoginControl) -> bool:
    """Whether the supplemental text may name ``control``: its own name/id/label/placeholder is silent (every word
    generic, ``_generic_word``, and no letter the folding drops, ``_drops_letters``), its type is free
    (``_HINT_FREE_TYPES``), no autocomplete token names a purpose (only off/on/billing/shipping/section-*), and the
    whole hint is one card role plus neutral words (``_hint_is_one_role``): a group legend "card number, expiry date
    and security code" names none of the fields in particular, "Card number and PIN" may describe the PIN input."""
    if control.type not in _HINT_FREE_TYPES:
        return False
    if any(t not in ("off", "on", "billing", "shipping") and not t.startswith("section-")
           for t in control.autocomplete.lower().split()):
        return False
    own = f"{control.name} {control.label}"
    if _drops_letters(own) or not all(_generic_word(word) for word in own.split()):
        return False
    return _hint_is_one_role(control.hint)


def _split_camel(value: str) -> str:
    return _normalize_text(re.sub(r"([a-z])([A-Z])", r"\1 \2", value))


def _foreign_autocomplete(control: LoginControl) -> bool:
    return any(t in _FOREIGN_AUTOCOMPLETE or t.startswith(_FOREIGN_AUTOCOMPLETE_PREFIXES)
               for t in control.autocomplete.lower().split())


def _separated_holder_named(control: LoginControl) -> bool:
    """Positive evidence for the separated "card holder": one name/id attribute on its own, or the visible label
    (labels, aria-label, placeholder) on its own, is the holder phrase plus only neutral words ("name", "full",
    "first and last", "of the", "as it appears on", "card", short numbers) or a person-name placeholder ("Card holder
    Max Mustermann"), and no source names the holder beside other words. When only the name/id attributes name the
    holder, the visible label must be empty, neutral or a placeholder too ("Security question", "PIN", "Recipient"
    name another purpose). "card-holder", "cardHolderName", "Name of the card holder", id "card-holder" with the
    placeholder "Max Mustermann" are; "Card holder fax", "card_holder_first_name", name "card-holder" with the label
    "Card holder initials" or "Security question", name "card" + label "Holder" are not."""
    holder = _CHECKOUT_HEURISTICS[_HOLDER_ROW][0]
    label = _split_camel(control.label)
    sources = [_split_camel(part) for part in control.name.split()] + [label]
    neutral = False
    for source in sources:
        match = holder.search(source)
        if match is None:
            continue
        if not _RE_HOLDER_NEUTRAL.fullmatch(_RE_FULL_NAME.sub(" ", f"{source[:match.start()]} {source[match.end():]}")):
            return False
        neutral = True
    if neutral and not holder.search(label):
        return _RE_HOLDER_NEUTRAL.fullmatch(_RE_FULL_NAME.sub(" ", label)) is not None
    return neutral


def _holder_accepted(control: LoginControl, texts: tuple) -> bool:
    """Whether the holder row's match makes ``control`` the holder: a joined holder word (base reach) unless it
    names another purpose too (``_holder_conflict``); a separated "card holder" only on positive evidence
    (``_separated_holder_named``) and the same conflict check."""
    if not any(_RE_HOLDER_JOINED.search(t) for t in texts) and not _separated_holder_named(control):
        return False
    return not _holder_conflict(control, texts)


def _holder_conflict(control: LoginControl, texts: tuple) -> bool:
    """Whether a field the holder row matched names another purpose too (``_RE_HOLDER_CONFLICT``, an identification
    cue, a later heuristic row, a foreign autocomplete token or input type), or carries the holder words only across
    its name and label together (name "card", label "Holder"). A full-name instruction is no name component, nor is
    an instruction ("as on ID") or an alternative ("or company name"); a bare "id" counts in the label only (in the
    name/id attributes it is a DOM id suffix); a tel type counts only when the text does not name the holder
    explicitly (the label "Karteninhaber" or "Name on card", the name "cardholderName"; not the name "card-holder"
    alone). Only a recognised autocomplete token with another purpose counts (``_FOREIGN_AUTOCOMPLETE``)."""
    names = (_normalize_text(control.name), _split_camel(control.name))
    labels = (_normalize_text(control.label), _split_camel(control.label))
    holder = _CHECKOUT_HEURISTICS[_HOLDER_ROW][0]
    if not any(holder.search(t) for t in names + labels):
        return True
    named = (any(holder.search(t) for t in labels)
             or any(holder.search(t) and _RE_NAME_WORD.search(t) for t in names + labels))
    if control.type in _HOLDER_FOREIGN_TYPES and not (control.type == "tel" and named):
        return True
    if _foreign_autocomplete(control):
        return True
    if any(_names_other_purpose(t) for t in names + labels + texts):
        return True
    if any(_RE_ID_WORD.search(_plain(t)) for t in labels):
        return True
    later = [pattern for pattern, _token in _CHECKOUT_HEURISTICS[_HOLDER_ROW + 1:]]
    return any(p.search(t) for p in later for t in texts)


def _plain(text: str) -> str:
    """``text`` without full-name instructions, instructions and alternatives (``_RE_FULL_NAME``,
    ``_RE_HOLDER_INSTRUCTION``)."""
    return _RE_HOLDER_INSTRUCTION.sub(" ", _RE_FULL_NAME.sub(" ", text))


def _names_other_purpose(text: str) -> bool:
    plain = _plain(text)
    return bool(_RE_HOLDER_CONFLICT.search(plain) or _RE_ID_SEMANTIC.search(plain))


def _strong_card_name(control: LoginControl) -> bool:
    return any(re.sub(r"[^a-z0-9]+", "", part.lower()) in _STRONG_CARD_NAMES for part in control.name.split())


def _expiry_reach(control: LoginControl, token: str, searchable: str, texts: tuple) -> bool:
    """Whether an expiry row's match counts: the base wording always, the wider reach only with a card word in the
    field's own text or a card field's own name/id (``_strong_card_name``). "Visa" alone is no card evidence."""
    return bool(_BASE_EXPIRY[token].search(searchable)) or _strong_card_name(control) or any(
        _RE_ATTRIBUTE_CARD_WORD.search(t) for t in texts)


def _document_purpose(control: LoginControl, texts: tuple) -> bool:
    """An identity document, passport, residence permit or a visa document (beside a travel word): the card rows
    never apply, unless a name/id attribute is a card field's own name."""
    if _strong_card_name(control):
        return False
    if any(_RE_DOCUMENT_PURPOSE.search(t) for t in texts) or _RE_DOCUMENT_WORD.search(_normalize_text(control.label)):
        return True
    if any(_RE_DOCUMENT_WORD.search(part) and not _RE_ATTRIBUTE_CARD_WORD.search(part)
           for part in map(_split_camel, control.name.split())):
        return True
    return any(_RE_VISA.search(t) for t in texts) and any(_RE_VISA_TRAVEL.search(t) for t in texts)


def _classify_checkout_control(control: LoginControl) -> Optional[ClassifiedLoginControl]:
    tokens = [t for t in control.autocomplete.lower().split() if t]
    for token in PAYMENT_AUTOFILL_TOKENS + ADDRESS_AUTOFILL_TOKENS:
        if token in tokens:
            return ClassifiedLoginControl(control, 100, "country-name" if token == "country" else token)
    if control.type == "email":
        return None
    raw_text = " ".join(part for part in (control.name, control.label) if part)
    searchable = _normalize_text(raw_text)
    date_words = _split_camel(raw_text)  # issueDate -> issue date; encryptedCardNumber -> encrypted card number
    if _RE_EMAIL.search(searchable) or _RE_EMAIL.search(date_words):
        return None  # "CARDHOLDER_EMAIL" (Nexi Build), "cardholderEmail": not the holder name
    texts = (searchable, date_words)
    decoy = any(_RE_CARD_DECOY.search(t) for t in texts) or _document_purpose(control, texts)
    birth = not _strong_card_name(control) and any(_RE_BIRTH.search(t) for t in texts)
    label_words = _normalize_text(control.label)
    expiry = _RE_EXPLICIT_EXPIRY.search(label_words)
    other_date = _RE_NOT_EXPIRY.search(label_words)
    explicit_expiry = expiry is not None and (other_date is None or expiry.start() < other_date.start())
    for pattern, token in _CHECKOUT_HEURISTICS:
        card = token.startswith("cc-")
        if not (pattern.search(searchable) or (card and pattern.search(date_words))):
            continue
        if card and decoy:
            continue  # a gift, loyalty or ticket card's, or a passport's, number/code/expiry is no bank card field
        if token.startswith("cc-exp") and (birth or not _expiry_reach(control, token, searchable, texts)):
            continue  # a date of birth is no expiry; "valid until" with no card word is a document's
        if token == "cc-name" and not _holder_accepted(control, texts):
            continue  # "Card holder ZIP code" is the postal code; "Card holder fax" is nothing
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


def build_otp_fills(otp_controls: list[ClassifiedLoginControl], code: str,
                    listed: Optional[list[LoginControl]] = None) -> list[dict[str, Any]]:
    """One fill per box. Default: the single best-scoring code field takes the whole code.

    Per-digit entry only when the page unmistakably uses it: exactly len(code) OTP controls that are all
    ``maxlength=1``, all in the same form, and adjacent in DOM order (the classic N-box widget). Anything
    looser (several code-like inputs scattered over a page) gets ONE field, never a digit sprayed across
    unrelated inputs. A whole code is never written into a control whose ``maxlength`` is below the code
    length: that returns no fills (refusal, nothing written).

    Adjacency is counted over ``listed`` (the inspection's listed inputs, in DOM order) when given: hidden helper
    inputs between the boxes (GitHub's eight boxes) do not break the widget; a listed field in between does."""
    best = max(otp_controls, key=lambda c: c.score)
    position = ({c.index: i for i, c in enumerate(listed)} if listed is not None else None)
    where = (lambda c: position.get(c.control.index, -10**6)) if position is not None else (lambda c: c.control.index)
    boxes = sorted((c for c in otp_controls if c.control.max_length == 1), key=where)
    if (len(boxes) == len(code)
            and len({b.control.form_index for b in boxes}) == 1
            and all(where(b) - where(a) == 1 for a, b in itertools.pairwise(boxes))):
        return [{"index": b.control.index, "token": "one-time-code", "value": ch, **({"strict": True} if b.strict else {})}
                for b, ch in zip(boxes, code)]
    if best.control.max_length is not None and best.control.max_length < len(code):
        return []
    return [{"index": best.control.index, "token": "one-time-code", "value": code, **({"strict": True} if best.strict else {})}]


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
            .replace("__VISIBLE__", _VISIBLE_JS).replace("__RENDERED__", _RENDERED_JS).replace("__FRAME__", _FRAME_JS))


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


# Strict visibility for the plain-"code" fallback, shared by the inspection (``visible``) and the fill script
# (re-checked right before writing): everything ``_RENDERED_JS`` requires, plus no ancestor (shadow hosts
# included) that is display:none, visibility:hidden or (almost) transparent.
_VISIBLE_JS = """((e) => {
    if (!(__RENDERED__)(e)) return false;
    let n = e;
    for (let up = 0; n && up < 200; up++) {
      const st = getComputedStyle(n);
      if (st.display === "none" || st.visibility === "hidden" || parseFloat(st.opacity) < 0.05) return false;
      n = n.parentElement || (n.getRootNode && n.getRootNode().host) || null;
    }
    return e.isConnected !== false;
  })""".replace("__RENDERED__", _RENDERED_JS)

# A same-origin child document (iframe/frame) the inspection, the login writer and the signup writer descend
# into, or null (cross-origin, not loaded, or no access). Cross-origin frames are reported by
# ``FRAME_PROBE_JS`` instead (``form_in_frame``).
_FRAME_JS = """((e) => {
    try {
      if (e.tagName !== "IFRAME" && e.tagName !== "FRAME") return null;
      const d = e.contentDocument;
      return d && d.location && d.location.origin === window.location.origin ? d : null;
    } catch (_) { return null; }
  })"""

# Non-secret probe: the origins of the page's child frames that the inspection could NOT enter (cross-origin),
# deduplicated, at most 20. Read only when a fill found no field (``form_in_frame``).
FRAME_PROBE_JS = """(() => {
  const out = [];
  try {
    for (const f of document.querySelectorAll("iframe, frame")) {
      let entered = false;
      try { entered = !!(f.contentDocument && f.contentDocument.location.origin === window.location.origin); } catch (_) { entered = false; }
      if (entered) continue;
      try { const o = new URL(f.src || "about:blank", document.baseURI).origin; if (o && o !== "null" && !out.includes(o)) out.push(o); } catch (_) {}
      if (out.length >= 20) break;
    }
  } catch (_) {}
  return JSON.stringify(out);
})()"""

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
  const sameOriginFrame = __FRAME__;
  (function walk(root) {
    for (const e of root.querySelectorAll("*")) {
      if (e.tagName === "INPUT" || e.tagName === "SELECT") elements.push(e);
      else if (e.tagName === "FORM") forms.push(e);
      if (e.shadowRoot) walk(e.shadowRoot);
      const sub = sameOriginFrame(e);  // a login form in a same-origin iframe is inspected like the page's own
      if (sub) walk(sub);
    }
  })(document);
  elements.forEach((element, index) => element.setAttribute("data-hermes-vault-slot", nonce + ":" + index));
  // Identity registry for the signup writer (see registry_name): references in a closure, nothing on the nodes.
  const registry = __REGISTRY__;
  if (registry) { try { (__REGISTER__)(registry, __BIND__, elements, forms); } catch (_) { /* writer then refuses */ } }
  const rendered = __RENDERED__;
  // Context for the plain-"code" fallback (read by the Python classifier, never shown to the model): the RENDERED
  // text (innerText: display:none / visibility:hidden text is absent) of the control's own form, or, without a
  // form, of its nearest container block (up to 4 levels, the largest that fits). Longer than the cap or
  // unreadable -> "" (never qualifies). Page title and headings are deliberately not included.
  const BLOCK_MAX = 1500;
  const blockText = new Map();
  const textOf = (node) => {
    if (blockText.has(node)) return blockText.get(node);
    let t = null;
    try { const raw = node.innerText; t = typeof raw === "string" ? raw.replace(/\\s+/g, " ").trim() : null; } catch (_) { t = null; }
    blockText.set(node, t);
    return t;
  };
  // Without a form: walk up to 4 ancestors. If ANY ancestor in the walk is oversized or unreadable the context is
  // empty (fail closed): a smaller inner block is never kept when an enclosing one was cut off.
  const blockOf = (element) => {
    try {
      if (element.form) return element.form;
      let best = null, node = element;
      for (let up = 0; up < 4 && node; up++) {
        node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
        if (!node || node.tagName === "BODY" || node.tagName === "HTML") break;
        const t = textOf(node);
        if (t === null || t.length > BLOCK_MAX) return null;
        best = node;
      }
      return best;
    } catch (_) { return null; }
  };
  const contextOf = (element) => {
    const block = blockOf(element);
    if (!block) return { context: "" };
    const t = textOf(block);
    return { context: t !== null && t.length <= BLOCK_MAX ? t : "" };
  };
  // Form text for the reset gate (password fields only): the same block's rendered text WITHOUT links, [role=link]
  // and button[type=button] (navigation such as "Don't have an account? Sign up"), submit buttons' values kept; a
  // button[type=button] is kept when the block has no other submit control, because then it IS the submit;
  // around a form, the nearest h1-h3 / [role=heading] of its card (up to 4 ancestors, the largest within the cap):
  // the last one before the form, else the first after it. null when the block is oversized or unreadable.
  const NAV = "a,[role=link]";
  const HEADING = "h1,h2,h3,[role=heading]";
  const shown = (e) => { const s = getComputedStyle(e); return s.display !== "none" && s.visibility === "visible"; };
  const submits = (e) => {
    const t = String(e.tagName === "BUTTON" ? e.getAttribute("type") || "submit" : e.type).toLowerCase();
    return shown(e) && (e.tagName === "BUTTON" ? t !== "button" && t !== "reset" : e.tagName === "INPUT" && (t === "submit" || t === "image"));
  };
  const navFree = (root) => {
    const parts = [];
    const skip = Array.from(root.querySelectorAll("*")).some(submits) ? NAV + ",button[type=button]" : NAV;
    (function walk(n) {
      const lit = getComputedStyle(n).visibility === "visible";
      for (const c of n.childNodes) {
        if (c.nodeType === 3) { if (lit) parts.push(c.data); continue; }
        if (c.nodeType !== 1 || c.matches(skip) || getComputedStyle(c).display === "none") continue;
        if (c.tagName === "INPUT" && c.type === "submit" && shown(c)) parts.push(c.value || "");
        walk(c);
      }
    })(root);
    return parts.join(" ").replace(/\\s+/g, " ").trim();
  };
  const headingNear = (form) => {
    let card = null;
    for (let up = 0, node = form; up < 4; up++) {
      node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
      if (!node || node.tagName === "BODY" || node.tagName === "HTML") break;
      const t = textOf(node);
      if (t === null || t.length > BLOCK_MAX) break;
      card = node;
    }
    if (!card) return "";
    const all = Array.from(card.querySelectorAll("*"));
    const at = all.indexOf(form), end = at < 0 ? all.length : at + 1 + form.querySelectorAll("*").length;
    const found = all.filter((h, i) => (i < at || i >= end) && h.matches(HEADING) && shown(h) && h.getClientRects().length);
    const before = found.filter((h) => all.indexOf(h) < at);
    const pick = before.length ? before[before.length - 1] : found[0];
    return pick ? (textOf(pick) || "") : "";
  };
  const formTextOf = (element) => {
    try {
      const block = blockOf(element);
      const raw = block ? textOf(block) : null;
      if (raw === null || raw.length > BLOCK_MAX) return null;
      return ((element.form ? headingNear(element.form) : "") + " " + navFree(block)).trim();
    } catch (_) { return null; }
  };
  const visible = __VISIBLE__;
  // Field choice inputs (never values): is the field itself on top at its centre (null: not measurable), is it
  // inside an open dialog / aria-modal container / the URL's :target, is it a search box.
  const topmost = (e) => {
    try {
      const r = e.getBoundingClientRect();
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      const view = (e.ownerDocument && e.ownerDocument.defaultView) || window;
      if (!(x >= 0 && y >= 0 && x < (view.innerWidth || 0) && y < (view.innerHeight || 0))) return null;
      const root = e.getRootNode ? e.getRootNode() : null;
      const at = (root && typeof root.elementFromPoint === "function" ? root : e.ownerDocument).elementFromPoint(x, y);
      if (!at) return null;
      if (at === e || (e.contains && e.contains(at))) return true;
      return Array.from(e.labels || []).some((l) => l === at || (l.contains && l.contains(at)));
    } catch (_) { return null; }
  };
  const modal = (e) => {
    try {
      let n = e;
      for (let up = 0; n && up < 200; up++) {
        if (n.tagName === "DIALOG" && n.open) return true;
        if (n.getAttribute && n.getAttribute("aria-modal") === "true") return true;
        if (n.matches && n.matches(":target")) return true;
        n = n.parentElement || (n.getRootNode && n.getRootNode().host) || null;
      }
      return false;
    } catch (_) { return null; }
  };
  const search = (e) => {
    try {
      return e.type === "search" || /^(?:q|search|query)$/i.test(e.name || "") ||
        !!(e.closest && e.closest("[role=search]"));
    } catch (_) { return false; }
  };
  const out = elements.flatMap((element, index) => {
    // A readonly password is listed: many logins set readonly until focus to stop browser autofill.
    if (element.disabled || (element.readOnly && element.type !== "password")) return [];
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
      maxLength: element.maxLength >= 0 ? element.maxLength : null,  // dlz: 0 is a real limit (nothing fits), not "none"
      label: [
        ...labels,
        element.getAttribute("aria-label") || "",
        ariaText,
        element.getAttribute("placeholder") || "",
        element.getAttribute("title") || "",
      ].join(" "),
      name: [element.name, element.id].join(" "),
      type: element.tagName === "SELECT" ? "select" : (element.type || ""),
      ...(["text", "tel", "number", "password"].includes(element.type) ? (() => { try { return contextOf(element); } catch (_) { return {}; } })() : {}),
      ...(element.type === "password" ? { formText: formTextOf(element) } : {}),
      visible: (() => { try { return visible(element); } catch (_) { return false; } })(),
      rendered: (() => { try { return rendered(element); } catch (_) { return false; } })(),
      topmost: topmost(element),
      modal: modal(element),
      readonly: element.readOnly === true,
      search: search(element),
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
                         bind: str, tokens: dict[str, str], purpose: str = "signup") -> str:
    """The signup write: the new-password field, then its confirmation field if approved, same value.

    The approved set is the inspection's own element and form REFERENCES, fetched once from its closure
    registry (see :func:`registry_name`; a clone, a copied property or a replacement form is never the same
    object), read by fixed index, and bound by exact URL and form action. The value setter/getter, event
    dispatch, ``isConnected``, ``form``/``action`` getters and ``Reflect.apply`` are captured before the first
    write, so page code that runs on a write's events cannot redirect a write or the retention read-back
    (no array iteration on that path). Immediately before EACH setter (no ``focus()``, so no page event runs in between), after the
    writes and again after the settle, the whole set is revalidated with the inspection's own traversal (open
    shadow roots included) and predicates: identity, form, action, no ``formaction`` override, rendered size,
    roles, exactly these password inputs on the page, no OTP field (an identifier outside the form no longer
    refuses: D-0362). A form-less (SPA) pair is bound to "no form" on both fields. ``purpose=reset`` (D-0364)
    swaps the other-role name check for the old/current-password check. A changed set stops before the next
    write and is never retried. No value is ever returned: see ``SIGNUP_OUTCOMES``. Only the existing secret
    transport may evaluate this expression.
    """
    controls = target.controls
    if ([f.get("index") for f in fills] != [c.index for c in controls] or len({f.get("value") for f in fills}) != 1
            or not bind or set(tokens) != set(SIGNUP_OUTCOMES) or purpose not in ("signup", "reset")):
        raise ValueError("signup fills do not match the approved targets")
    patterns = {"confirm": _SIGNUP_CONFIRM_RE, "nameForbidden": _SIGNUP_NAME_FORBIDDEN_RE,
                "labelForbidden": _SIGNUP_LABEL_FORBIDDEN_RE, "otp": _RE_OTP.pattern, "old": _RESET_OLD_RE}
    values = {
        "__ORIGIN__": json.dumps(expected_origin),
        "__TOKENS__": json.dumps(tokens),
        "__BOUND__": json.dumps({"url": target.primary.page_url, "action": target.primary.form_action,
                                 "registry": registry_name(nonce), "secret": bind,
                                 "indices": [c.index for c in controls], "form": target.primary.form_index,
                                 "stamps": [f"{nonce}:{c.index}" for c in controls]}),
        "__COUNT__": str(len(controls)),
        "__MODE__": json.dumps(purpose),
        "__PATTERNS__": json.dumps(patterns),
        "__RENDERED__": _RENDERED_JS,
        "__FRAME__": _FRAME_JS,
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
  const mode = __MODE__;
  const reConfirm = rx("confirm"), reNameForbidden = rx("nameForbidden"), reLabelForbidden = rx("labelForbidden"),
    reOtp = rx("otp"), reOld = rx("old");
  const rendered = __RENDERED__;
  const sameOriginFrame = __FRAME__;
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
        const sub = sameOriginFrame(e);
        if (sub) walk(sub);
      }
    })(document);
    return out;
  };
  const listed = (e) => {
    if (e.disabled || (e.readOnly && e.type !== "password")) return false;  // readonly-until-focus passwords count
    if (["hidden", "submit", "button", "reset", "file", "image", "checkbox", "radio"].includes(e.type)) return false;
    const st = getComputedStyle(e);
    return !(st.display === "none" || st.visibility === "hidden" || e.getClientRects().length === 0);
  };
  const isPassword = (e) => kind(e) === "password" || ac(e).some((t) => t === "current-password" || t === "new-password");
  const isOtp = (e) => ac(e).includes("one-time-code") ||  // classify_otp_controls
    (["text", "tel", "number", "password", ""].includes(kind(e)) && reOtp.test(norm(describe(e), false)));
  const forbidden = (e) => (mode === "reset"
    ? reOld.test(norm(describeName(e) + " " + describeLabel(e), true)) || reOld.test(norm(describeName(e), true).replace(/ /g, ""))
    : reNameForbidden.test(norm(describeName(e), true)) || reLabelForbidden.test(norm(describeLabel(e), true)));
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
  let form;  // undefined: not proven; null: a form-less (SPA) pair, bound as such at inspection
  try {
    const gate = window[bound.registry];
    const refs = typeof gate === "function" ? gate(bound.secret) : null;
    let found = 0;
    for (let i = 0; refs && i < N; i++) {
      const e = refs.elements[bound.indices[i]];
      if (e && e.getAttribute("data-hermes-vault-slot") === bound.stamps[i]) { set[i] = e; found += 1; }
    }
    if (refs && found === N) form = bound.form === null ? null : (refs.forms[bound.form] || undefined);
  } catch (_) { form = undefined; }
  if (form === undefined) { unstamp(); return stop(); }
  const valid = () => {
    if (window.location.origin !== expectedOrigin || window.location.href !== bound.url) return false;
    if (read(formGet, set[0]) !== form) return false;
    if (form !== null && (read(actionGet, form) !== bound.action ||
        Array.from(form.elements || []).some((x) => x.hasAttribute && x.hasAttribute("formaction")))) return false;
    const shown = collect().filter(listed);
    const passwords = shown.filter(isPassword);
    if (passwords.length !== N || shown.some(isOtp)) return false;
    for (let i = 0; i < N; i++) {
      const e = set[i];
      if (!passwords.includes(e) || e.tagName !== "INPUT" || kind(e) !== "password" ||
          ac(e).includes("current-password") || read(formGet, e) !== form || !rendered(e) || forbidden(e) ||
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


# Per-field outcome codes the login writer reports (fixed strings; anything else is unverifiable).
FILL_REASONS = ("written", "field_missing", "not_password", "detached", "origin_changed", "not_visible",
                "value_rejected", "no_option", "write_error", "mask_failed")


def build_fill_js(fills: list[dict[str, Any]], expected_origin: str, nonce: str = "", refusal_tag: str = "",
                  settle_ms: int = 0) -> str:
    """Build a JS expression that fills the selected controls and reports only counts and fixed codes. The
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

    The answer is ``{"filled": n, "reasons": [one FILL_REASONS code per fill]}``, plus ``"challenge"`` when the page
    shows a captcha after the write (:mod:`agent.vault_captcha`). With ``settle_ms`` (the login
    password fill) the script then waits that long, like the signup writer, and adds ``"retained"``: how many
    written fields still hold their value (a framework that resets a controlled input clears it). A page that
    starts to navigate away during the settle (``beforeunload`` / ``pagehide``: the site auto-submitted) answers
    at once with ``"navigated": true`` and no ``retained``.

    A fill with ``"mask": True`` (a recovery code in a plain text box) hides the field's pixels BEFORE the value
    lands: ``-webkit-text-security: disc`` where the engine supports it, else the input becomes
    ``type=password``. If neither takes, nothing is written to that field (``mask_failed``).
    """
    payload = json.dumps(
        [{"index": f["index"], "token": f.get("token", "current-password"), "value": f["value"],
          **({"strict": True} if f.get("strict") else {}), **({"mask": True} if f.get("mask") else {})}
         for f in fills]
    )
    # One pass: a chained .replace() would rescan the already-spliced fill values, so a secret
    # containing "__NONCE__" (or any later placeholder) got JSON spliced into its string literal.
    values = {
        "__EXPECTED_ORIGIN__": json.dumps(expected_origin),
        "__FILLS__": payload,
        "__NONCE__": json.dumps(nonce),
        "__TAG__": json.dumps(refusal_tag),
        "__VISIBLE__": _VISIBLE_JS,
        "__FRAME__": _FRAME_JS,
        "__SETTLE_MS__": str(max(0, int(settle_ms))),
        "__CHALLENGE__": CAPTCHA_JS,
    }
    return _FILL_JS_PLACEHOLDER_RE.sub(lambda m: values[m.group(0)], _FILL_JS_TEMPLATE)


_FILL_JS_PLACEHOLDER_RE = re.compile(
    r"__EXPECTED_ORIGIN__|__FILLS__|__NONCE__|__TAG__|__VISIBLE__|__FRAME__|__SETTLE_MS__|__CHALLENGE__")


_FILL_JS_TEMPLATE = """(() => {
  "use strict";  // a page function we call (a hooked dispatchEvent) cannot read the fill values through .caller
  // Captured before a write's events run page code; Python still accepts only small integer counts and fixed codes.
  const stringify = JSON.stringify;
  const P0 = Promise, wait = setTimeout;
  const expectedOrigin = __EXPECTED_ORIGIN__;
  if (window.location.origin !== expectedOrigin) {
    return stringify({ refused: "origin_changed", tag: __TAG__ });
  }
  const fills = __FILLS__;
  const nonce = __NONCE__;
  const settleMs = __SETTLE_MS__;
  const captcha = __CHALLENGE__;  // agent.vault_captcha: one fixed code, added to the answer only when a captcha shows
  const answer = (o) => { try { const c = captcha(); if (c) o.challenge = c; } catch (_) {} return stringify(o); };
  // Stamped controls may live inside a shadow root or a same-origin frame (see the inspection script's walk):
  // collect the document and every open shadow root / same-origin frame document once, then look each stamp up.
  const sameOriginFrame = __FRAME__;
  const roots = [];
  (function walk(root) {
    roots.push(root);
    for (const e of root.querySelectorAll("*")) {
      if (e.shadowRoot) walk(e.shadowRoot);
      const sub = sameOriginFrame(e);
      if (sub) walk(sub);
    }
  })(document);
  const find = (selector) => roots.flatMap((r) => Array.from(r.querySelectorAll(selector)));
  let filled = 0;
  const reasons = [];
  const kept = [];  // [element, value] of each written text field, for the retention read-back
  const norm = (t) => String(t || "").trim().toLowerCase();
  const visible = __VISIBLE__;
  let valueGet = null;
  try { const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value"); valueGet = d && d.get; } catch (_) { valueGet = null; }
  // Plain-"code" fallback targets (strict): rendered and visible, checked for ALL of them before anything is
  // written; a failure writes nothing.
  for (const f of fills) {
    if (f.strict !== true) continue;
    const el = find('[data-hermes-vault-slot="' + nonce + ':' + f.index + '"]')[0];
    let ok = false;
    try { ok = !!el && el.isConnected && visible(el); } catch (_) { ok = false; }
    if (!ok) {
      clearStamps();
      return stringify({ refused: "field_not_visible" });
    }
  }
  for (const f of fills) {
    const el = find('[data-hermes-vault-slot="' + nonce + ':' + f.index + '"]')[0];
    if (!el) { reasons.push("field_missing"); continue; }
    if (f.token === "current-password" && el.type !== "password") { reasons.push("not_password"); continue; }
    try {
      if (el.tagName === "SELECT") {
        const want = norm(f.value);
        const opt = Array.from(el.options).find((o) => [o.value, o.textContent].some((t) => norm(t) === want || norm(t) === want.replace(/^0/, "")));
        if (opt) { el.value = opt.value; el.dispatchEvent(new Event("change", { bubbles: true, composed: true })); filled += 1; reasons.push("written"); }
        else reasons.push("no_option");
        continue;
      }
      el.focus();
      // focus handlers are page code: re-check the target before the value lands.
      if (!el.isConnected) { reasons.push("detached"); continue; }
      if (window.location.origin !== expectedOrigin) { reasons.push("origin_changed"); continue; }
      if (f.token === "current-password" && el.type !== "password") { reasons.push("not_password"); continue; }
      if (f.strict === true && !visible(el)) { reasons.push("not_visible"); continue; }
      if (f.mask === true && !maskField(el)) { reasons.push("mask_failed"); continue; }
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
      // one-time-code split into single-character boxes: f.value is the slice for THIS box (see build_otp_fills)
      if (setter && setter.set) { setter.set.call(el, f.value); } else { el.value = f.value; }
      // composed: a component listening on its shadow host must see the value change too.
      el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText" }));
      el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      if (el.value.length > 0) { filled += 1; reasons.push("written"); kept.push([el, f.value]); }
      else reasons.push("value_rejected");
    } catch (e) { reasons.push("write_error"); }
  }
  clearStamps();
  if (settleMs <= 0) return answer({ filled, reasons });
  // Same settle as the signup writer: frameworks reset controlled inputs on a later tick. A site that submits
  // on its own navigates now: answer before the document goes, the write itself already landed.
  return new P0((resolve) => {
    let done = false;
    const left = (e) => {  // a page-dispatched (untrusted) event is not a navigation
      if (done || (e && e.isTrusted === false)) return;
      done = true;
      resolve(stringify({ filled, reasons, navigated: true }));
    };
    try { window.addEventListener("beforeunload", left, true); window.addEventListener("pagehide", left, true); } catch (_) {}
    wait(() => {
      if (done) return;
      let retained = 0;
      try {
        for (let i = 0; i < kept.length; i++) {  // by index: no page-reachable array iterator on this path
          const el = kept[i][0];
          const now = valueGet ? valueGet.call(el) : el.value;
          if (el.isConnected && now === kept[i][1]) retained += 1;
        }
      } catch (_) { retained = -1; }
      done = true;
      resolve(window.location.origin === expectedOrigin ? answer({ filled, reasons, retained })
                                                        : stringify({ filled, reasons, navigated: true }));
    }, settleMs);
  });
  // (hoisted declarations, so the early no-write refusal above can use them too)
  function clearStamps() { find("[data-hermes-vault-slot]").forEach((n) => n.removeAttribute("data-hermes-vault-slot")); }
  // Pixels first: a screenshot taken while the field holds a recovery code shows discs, never the code.
  function maskField(el) {
    try {
      let css = false;
      try { css = typeof CSS !== "undefined" && CSS.supports("-webkit-text-security", "disc"); } catch (_) { css = false; }
      if (css) {
        el.style.setProperty("-webkit-text-security", "disc", "important");
        if (el.style.getPropertyValue("-webkit-text-security") === "disc") return true;
      }
      el.type = "password";
      return el.type === "password";
    } catch (_) { return false; }
  }
})()"""
