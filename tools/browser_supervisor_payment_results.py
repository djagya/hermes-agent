"""What a card fill leaves for the user and what an inspection saw (dlz, D-0358 a, D-0359 a, 11-18): the per-document
field classification, the card controls with their one billing postcode, the fields left for the user, the counts-only
inspection summary, the closure's blank-field answer and the ambiguous page check's reason codes. Split out of
``browser_supervisor_payment`` (build B r3: file size); callers and tests import the names from here.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from agent.vault_login_classifier import LoginControl, classify_checkout_control


def _classified(raw: list) -> list:
    """``(row, classified control or None)`` for each descriptor row, in row order."""
    return [(r, classify_checkout_control(LoginControl.from_dict(r))) for r in raw]


# A field that names the shipping or delivery address (autocomplete section, name, label or placeholder, in the
# languages of the shops this reaches) is never the card's billing postcode.
_SHIPPING = re.compile(r"\b(?:ship\w*|deliver\w*|liefer\w*|versand\w*|livraison|envio|spedizione|bezorg\w*)")


def shipping_field(control: Any) -> bool:
    """Whether the control is addressed to the shipping/delivery address: the ``shipping`` autocomplete section, or
    shipping/delivery words in its name, label or placeholder (accents and camelCase folded)."""
    if "shipping" in str(control.autocomplete).lower().split():
        return True
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", f"{control.name} {control.label}")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return bool(_SHIPPING.search(re.sub(r"[^a-z0-9]+", " ", text)))


def _as_form(form: Any) -> int | None:
    """A descriptor's ``formIndex`` as the index of its ``<form>``; None for a field outside every form."""
    return form if type(form) is int and form >= 0 else None


def _form(control: Any) -> int | None:
    return _as_form(control.form_index)


def _card_scope(card: tuple) -> tuple:
    """``(form indexes, formless)`` of a document's card controls: the forms they sit in, and whether any of them sits
    outside every form (a div-based checkout): the document's formless fields are then the card's form too."""
    return ({f for c in card if (f := _form(c.control)) is not None}, any(_form(c.control) is None for c in card))


def _card_of(classified: list, merchant_doc: bool = False) -> tuple:
    """A document's card controls: its cc-* fields and (dlz, D-0359 a) its billing postcode, which joins them only
    beside card fields, when the document has exactly one postcode field that is not a shipping one, and on the
    merchant's own page only inside the card's form (a shipping address form beside the card form is not the
    card's; with no ``<form>`` around the card fields, the page's formless fields are its form). A postcode that
    ``_one_postcode`` later drops (one in each of two documents) is dropped from the controls, not from the
    document: the inspection counts it among the fields left for the user."""
    controls = [c for _r, c in classified if c is not None]
    card = tuple(c for c in controls if c.token.startswith("cc-"))
    postal = [c for c in controls if c.token == "postal-code" and not shipping_field(c.control)]
    if not card or len(postal) != 1:
        return card
    if merchant_doc:
        forms, formless = _card_scope(card)
        form = _form(postal[0].control)
        if not (form in forms if form is not None else formless):
            return card
    return tuple(sorted((*card, postal[0]), key=lambda c: c.control.index))


def _left_for_user(classified: list, card: tuple, merchant_doc: bool) -> tuple:
    """dlz (D-0358 a): the inspection indexes of a card document's exposed fields the fill does not write: every
    other field of a hosted card document; on the merchant's own page, those in a form that holds a card field (its
    header search or newsletter box is not part of the card form), or, when a card field sits outside every form,
    those outside every form too. Indexes only; the result reports counts."""
    mine = {c.control.index for c in card}
    forms, formless = _card_scope(card)
    out = []
    for row, c in classified:
        index = row.get("index") if isinstance(row, dict) else None
        if type(index) is not int or index in mine:
            continue
        if merchant_doc:
            form = _as_form(row.get("formIndex"))
            if not (form in forms if form is not None else formless):
                continue
            if form is None and _page_chrome(row):
                continue
        out.append(index)
    return tuple(out)


# A formless field that is page chrome, not checkout: a search box, a coupon/voucher/gift-card or newsletter box.
# Without a <form> around the card these sit in the same scope; counting them would hold Pay on every such page.
_CHROME = re.compile(r"\b(?:search\w*|query|q|suche\w*|recherche\w*|buscar|cerca|zoeken|coupon\w*|promo\w*|voucher\w*|"
                     r"gutschein\w*|rabatt\w*|discount\w*|gift\w*|geschenk\w*|newsletter\w*)\b")


def _page_chrome(row: dict) -> bool:
    if str(row.get("type") or "").lower() == "search":
        return True
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", f"{row.get('name') or ''} {row.get('label') or ''}")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return bool(_CHROME.search(re.sub(r"[^a-z0-9]+", " ", text)))


def _one_postcode(candidates: list) -> list:
    """dlz (D-0359 a): a postcode field in more than one card document names no billing postcode: all are dropped
    from the card controls (left for the user, never a reason to refuse the card form). In place; returns
    ``(frame session, object id, index)`` of each dropped postcode so the caller counts it among the fields left."""
    if sum(any(c.token == "postal-code" for c in cand[4]) for cand in candidates) < 2:
        return []
    dropped = []
    for k, cand in enumerate(candidates):
        dropped += [(cand[1], cand[2], c.control.index) for c in cand[4] if c.token == "postal-code"]
        candidates[k] = (*cand[:4], tuple(c for c in cand[4] if c.token != "postal-code"))
    return dropped


# dlz (11-18): the counts of an inspection summary (``_summary``), besides frames, documents and tokens.
_SUMMARY_COUNTS = ("frames_inspected", "frames_cardless", "inputs_unrecognised")


def _summary(tree: tuple, candidates: list, found: dict) -> dict:
    """dlz (11-18): what an inspection saw, counts and card field tokens only (no origin, label, name or value):
    frames in the tree, frames inspected, card documents, the distinct card field tokens found, how many tokens
    appeared more than once, frames on a hosted origin with fields but no card field, and fields that classified as
    nothing."""
    tokens = [c.token for cand in candidates for c in cand[4]]
    stats = found.get("stats") or dict.fromkeys(_SUMMARY_COUNTS, 0)
    return {"frames": len(tree), "documents": len(candidates), "tokens": sorted(set(tokens)),
            "tokens_duplicated": len(tokens) - len(set(tokens)), **{k: int(stats.get(k, 0)) for k in _SUMMARY_COUNTS}}


def _blank_indexes(answer: Any, limit: int) -> frozenset | None:
    """The closure's ``blanks()`` answer (exposed fields holding no value), or None when it is not a list of at most
    ``limit`` indexes."""
    if not isinstance(answer, list) or len(answer) > limit or \
            not all(type(n) is int and n >= 0 for n in answer):
        return None
    return frozenset(answer)


def _page_match_reasons(pins: list, cents: int) -> dict:
    """dlz (11-18): why a page check is ``ambiguous``, fixed reason codes and counts, never an amount: ``unclear``
    (a total-labelled text this check does not read clearly), ``unlabelled`` (the stated total appears, but no
    total-labelled amount does), ``other_total`` (a total-labelled amount besides the stated one); with the number of
    distinct labelled amounts, of other ones, of the stated total's occurrences and of documents checked."""
    labelled = {v for _place, pin in pins for v in pin["labelled"]}
    reasons = [code for code, hit in (("unclear", any(pin["unclear"] for _place, pin in pins)),
                                      ("unlabelled", not labelled), ("other_total", bool(labelled - {cents}))) if hit]
    return {"reasons": reasons, "labelled": len(labelled), "other": len(labelled - {cents}),
            "stated": sum(pin["stated"] for _place, pin in pins), "documents": len(pins)}
