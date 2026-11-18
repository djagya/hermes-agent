"""What a card fill leaves for the user and what an inspection saw (dlz, D-0358 a, D-0359 a, 11-18): the per-document
field classification, the card controls with their one billing postcode, the fields left for the user, the counts-only
inspection summary, the closure's blank-field answer and the ambiguous page check's reason codes. Split out of
``browser_supervisor_payment`` (build B r3: file size); that module re-exports the names its callers and tests use.
"""

from __future__ import annotations

from typing import Any

from agent.vault_login_classifier import LoginControl, classify_checkout_control


def _classified(raw: list) -> list:
    """``(row, classified control or None)`` for each descriptor row, in row order."""
    return [(r, classify_checkout_control(LoginControl.from_dict(r))) for r in raw]


def _card_of(classified: list, merchant_doc: bool = False) -> tuple:
    """A document's card controls: its cc-* fields and (dlz, D-0359 a) its billing postcode, which joins them only
    beside card fields, when the document has exactly one postcode field, and on the merchant's own page only inside
    a form that holds a card field (a shipping address form beside the card form is not the card's)."""
    controls = [c for _r, c in classified if c is not None]
    card = tuple(c for c in controls if c.token.startswith("cc-"))
    postal = [c for c in controls if c.token == "postal-code"]
    if not card or len(postal) != 1:
        return card
    form = postal[0].control.form_index
    if merchant_doc and not (type(form) is int and form >= 0 and any(c.control.form_index == form for c in card)):
        return card
    return tuple(sorted((*card, postal[0]), key=lambda c: c.control.index))


def _left_for_user(classified: list, card: tuple, merchant_doc: bool) -> tuple:
    """dlz (D-0358 a): the inspection indexes of a card document's exposed fields the fill does not write: every
    other field of a hosted card document; on the merchant's own page, those in a form that holds a card field (its
    header search or newsletter box is not part of the card form). Indexes only; the result reports counts."""
    mine = {c.control.index for c in card}
    forms = {c.control.form_index for c in card if type(c.control.form_index) is int and c.control.form_index >= 0}
    out = []
    for row, c in classified:
        index = row.get("index") if isinstance(row, dict) else None
        if type(index) is not int or index in mine:
            continue
        if merchant_doc and row.get("formIndex") not in forms:
            continue
        out.append(index)
    return tuple(out)


def _one_postcode(candidates: list) -> None:
    """dlz (D-0359 a): a postcode field in more than one card document names no billing postcode: all are dropped
    (left for the user and reported), never a reason to refuse the card form. In place."""
    if sum(any(c.token == "postal-code" for c in cand[4]) for cand in candidates) < 2:
        return
    for k, cand in enumerate(candidates):
        candidates[k] = (*cand[:4], tuple(c for c in cand[4] if c.token != "postal-code"))


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
