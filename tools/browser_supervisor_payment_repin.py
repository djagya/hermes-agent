"""Re-pinning a card document's fields during one approved commit, and the stop record of a commit.

A provider may re-render its card form while Hermes writes it: brand detection on one field re-describes or
replaces another (a new ``<input>`` for the CVC, a changed label or length), or keeps a field out of view until
the card number is in. The commit closure (``browser_supervisor_payment_closure``) then pauses with a re-pin
request instead of writing; ``browser_supervisor_payment`` re-proves the frame and asks the closure for the
document's exposed fields (``rebind()``); :func:`repin_pins` decides, from that reading alone, the new pin of
every field token, or refuses. The rule, all of it required:

- the same closure answers, so the same document object at the same URL with the same same-origin ancestry (the
  closure checks ``document``, ``location.href``, its owner chain and the top URL; the supervisor checks the
  frame's loader, URL, origin and every owner ``<iframe>`` first);
- the exposed fields, classified with the inspection's own classifier, hold each card token at most once, and no
  card token the inspection did not see in this document;
- a field element pinned so far that is still exposed keeps its token (no role swap; and an old field still
  shown beside a new one with its token is refused, not chosen between);
- every control the inspection judged exposed that is still exposed keeps the role it had at inspection: a card
  token, or none (Codex r1 P1-2: an unfilled holder field relabelled as the card number, a promo field turned
  into a CVC, are refused);
- each re-pinned field has the shape the planned value was computed for (Codex r1 P2): a select stays a select
  with the identical options (a re-listed month select would show another month), an input stays an input, a
  separate expiry year keeps its two-character limit or its absence; a CVC's length may change;
- a token may be missing only when it is not written yet, is not the card number, and the card number is not
  written yet anywhere: it is written after the number, re-pinned again then, and must be there;
- while nothing is written in the document yet, a token whose inspected element left the document must be
  shown again (Opus r1 P3-3): a document replaced as a whole by a form with fewer card fields (a gift card's
  number and PIN) is refused, not filled with the card number; a field only hidden may still wait;
- the frame's loader, URL, origin and owner <iframe>s are re-proved after this rule accepted, right before the
  closure resumes (Opus r1 P3-1);
- the closure adopts the answer only if its field set is element-for-element and description-for-description
  what ``rebind()`` read, then re-runs every pre-write check on the new pins (live, focus, shown, deadline,
  select option match) before any setter; every re-pinned field must also be fully exposed (its centre in the
  viewport and hit by a click there) and, from the parents, its owner chain hit at that point, whatever the
  fill's strictness (Codex r1 P1-1).

Navigation, a changed frame or owner chain and a failed hit test are never re-pinned. Unreviewed destinations
are never re-pinned (their fills keep the strict, stop-on-any-change rule). No value is read or returned.
"""

from __future__ import annotations

import json
from typing import Any

from agent.vault_login_classifier import LoginControl, classify_checkout_control

# The closure's own bound (MAX_REPINS in browser_supervisor_payment_closure); also bounds the answers per document.
MAX_REPINS = 3
CHECK_REASONS = frozenset({"navigated", "frame_changed", "fields_replaced", "fields_relabeled", "field_hidden",
                           "detached", "hidden", "hit_test", "deferred", "options_changed"})
_DESCRIPTOR_KEYS = frozenset({"index", "autocomplete", "name", "label", "type", "formIndex", "maxLength"})
_CHECK_KEYS = frozenset({"token", "reason", "step", "fields", "keys", "repin_refused"})
# Why the supervisor declined a re-pin (fill_check.repin_refused): fixed codes.
REFUSALS = frozenset({"unreadable", "frame_changed", "too_late", "token_twice", "new_token", "role_changed",
                      "shape_changed", "set_changed", "written_gone", "number_gone", "still_missing"})
_MAX_REBIND_FIELDS = 2000
_MAX_REBIND_BYTES = 1024 * 1024


def valid_check(check: Any, tokens: frozenset, fills: int) -> bool:
    """One closure stop record: ``{token, reason, step[, fields][, keys]}``, fixed vocabularies only."""
    if not isinstance(check, dict) or not {"token", "reason", "step"} <= set(check) <= _CHECK_KEYS - {"repin_refused"}:
        return False
    names = tokens | {"other"}
    fields, keys = check.get("fields", ["other"]), check.get("keys", [])
    return (check["token"] in names and check["reason"] in CHECK_REASONS
            and type(check["step"]) is int and 1 <= check["step"] <= max(fills, 1)
            and isinstance(fields, list) and 0 < len(fields) <= len(names) and set(fields) <= names
            and isinstance(keys, list) and set(keys) <= _DESCRIPTOR_KEYS | {"options"})


def valid_pause(result: dict, kind: str, tokens: frozenset, fills: int, acknowledged: int) -> bool:
    """A re-pin request ``{repin, filled_fields, check, written}`` or a deferral ``{deferred, filled_fields}``: exact
    keys, a count that never goes backwards and leaves at least one field to write."""
    count = result.get("filled_fields")
    if type(count) is not int or not acknowledged <= count < fills:
        return False
    if kind == "deferred":
        return set(result) == {"deferred", "filled_fields"}
    written = result.get("written")
    return (set(result) == {"repin", "filled_fields", "check", "written"} and valid_check(result["check"], tokens, fills)
            and isinstance(written, list) and len(written) == count == len(set(written))
            and all(isinstance(t, str) for t in written) and set(written) <= tokens)


def _rebind_row(row: Any, previous: int) -> bool:
    """One ``describeOne`` row of the reviewed closure (no discovery text limits): exact keys and types."""
    if not isinstance(row, dict) or not isinstance(row.get("type"), str):
        return False
    select = row["type"] in ("select", "select-multiple")
    if set(row) != (_DESCRIPTOR_KEYS | {"options"} if select else _DESCRIPTOR_KEYS):
        return False
    index, form, length = row["index"], row["formIndex"], row["maxLength"]
    return (type(index) is int and previous < index < _MAX_REBIND_FIELDS
            and all(type(row[k]) is str for k in ("autocomplete", "name", "label"))
            and (form is None or (type(form) is int and form >= -1))
            and (length is None or (type(length) is int and length > 0))
            and (not select or isinstance(row["options"], list)))


def _inspected_pairs(raw: Any, roles: dict) -> list | None:
    """``rebind()``'s ``inspected``: ``[inspection index, new index]`` per control the inspection judged exposed,
    inspection indexes strictly increasing, every inspected card control among them; None when malformed."""
    if not isinstance(raw, list) or len(raw) > _MAX_REBIND_FIELDS:
        return None
    previous = -1
    for pair in raw:
        if not (isinstance(pair, list) and len(pair) == 2 and all(type(v) is int for v in pair)
                and previous < pair[0] < _MAX_REBIND_FIELDS and -1 <= pair[1] < _MAX_REBIND_FIELDS):
            return None
        previous = pair[0]
    return raw if set(roles) <= {old for old, _new in raw} else None


def _shape(token: str, control: LoginControl) -> tuple:
    """What the planned value of ``token`` was computed from (``_document_fills``: ``match_expiry_option``,
    ``_fit_expiry``): select or not, a select's options, a separate year's two-character limit."""
    select = control.type in ("select", "select-multiple")
    return (select, control.options if select else (),
            control.max_length == 2 if token == "cc-exp-year" and not select else None)


def repin_pins(controls: tuple, fill_tokens: list, answer: Any, written: list, number_in: bool,
               planned: dict | None = None) -> tuple:
    """``(pins, None)``: the new pin of every token in ``fill_tokens`` (``-1``: not shown yet, written after the
    card number); or ``(None, refusal)`` to stop, ``refusal`` one of ``REFUSALS``. ``controls``: the document's
    card controls at inspection; ``answer``: the closure's ``rebind()``; ``written``: tokens this document already
    wrote; ``number_in``: the card number is written (any document); ``planned``: per fill token, the inspection
    index of the control its value was computed for (default: the one inspected control with that token)."""
    if not isinstance(answer, dict) or set(answer) != {"fields", "kept", "inspected"}:
        return None, "unreadable"
    fields, kept = answer["fields"], answer["kept"]
    if not isinstance(fields, list) or len(fields) > _MAX_REBIND_FIELDS or len(json.dumps(fields)) > _MAX_REBIND_BYTES:
        return None, "unreadable"
    if not (isinstance(kept, dict) and set(kept) == set(fill_tokens)
            and all(type(v) is int and v >= -1 for v in kept.values())):
        return None, "unreadable"
    roles = {c.control.index: c.token for c in controls}
    by_index = {c.control.index: c.control for c in controls}
    planned = planned if planned is not None else {t: next((c.control.index for c in controls if c.token == t), None)
                                                   for t in fill_tokens}
    if set(planned) != set(fill_tokens) or any(planned[t] not in by_index or roles[planned[t]] != t for t in planned):
        return None, "unreadable"
    inspected = _inspected_pairs(answer["inspected"], roles)
    if inspected is None:
        return None, "unreadable"
    previous = -1
    for row in fields:
        if not _rebind_row(row, previous):
            return None, "unreadable"
        previous = row["index"]
    rows = {row["index"]: LoginControl.from_dict(row) for row in fields}
    found = [c for r in rows.values() if (c := classify_checkout_control(r)) and c.token.startswith("cc-")]
    tokens = [c.token for c in found]
    if len(tokens) != len(set(tokens)):
        return None, "token_twice"
    if not set(tokens) <= {c.token for c in controls}:
        return None, "new_token"
    now = {c.token: c.control.index for c in found}
    role_now = {n: t for t, n in now.items()}
    if any(n in rows and now.get(token) != n for token, n in kept.items()):
        return None, "role_changed"
    if any(new in rows and role_now.get(new) != roles.get(old) for old, new in inspected):
        return None, "role_changed"
    if not written and any(new == -1 and roles.get(old) in fill_tokens and roles[old] not in now
                           for old, new in inspected):
        return None, "set_changed"
    pins = {}
    for token in fill_tokens:
        if token in now:
            if _shape(token, rows[now[token]]) != _shape(token, by_index[planned[token]]):
                return None, "shape_changed"
            pins[token] = now[token]
        elif token in written:
            return None, "written_gone"
        elif token == "cc-number":
            return None, "number_gone"
        elif number_in:
            return None, "still_missing"
        else:
            pins[token] = -1
    return pins, None


def located(result: dict, position: int, documents: int, refused: str | None = None) -> dict:
    """A document's stop with its closure record as ``fill_check``, naming the document's place in the commit
    order (1-based), the number of documents written and, when the supervisor declined a re-pin, why."""
    check = result.get("check")
    if not isinstance(check, dict):
        return result
    out = {k: v for k, v in result.items() if k != "check"}
    out["fill_check"] = {**check, "document": position, "documents": documents,
                         **({"repin_refused": refused} if refused in REFUSALS else {})}
    return out
