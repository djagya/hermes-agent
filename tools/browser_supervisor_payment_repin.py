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
- a token may be missing only when it is not written yet, is not the card number, and the card number is not
  written yet anywhere: it is written after the number, re-pinned again then, and must be there;
- the closure adopts the answer only if its field set is element-for-element and description-for-description
  what ``rebind()`` read, then re-runs every pre-write check on the new pins (live, focus, shown, deadline,
  select option match) before any setter.

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
_CHECK_KEYS = frozenset({"token", "reason", "step", "fields", "keys"})
_MAX_REBIND_FIELDS = 2000
_MAX_REBIND_BYTES = 1024 * 1024


def valid_check(check: Any, tokens: frozenset, fills: int) -> bool:
    """One closure stop record: ``{token, reason, step[, fields][, keys]}``, fixed vocabularies only."""
    if not isinstance(check, dict) or not {"token", "reason", "step"} <= set(check) <= _CHECK_KEYS:
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


def repin_pins(controls: tuple, fill_tokens: list, answer: Any, written: list, number_in: bool) -> dict | None:
    """The new pin of every token in ``fill_tokens`` (``-1``: not shown yet, written after the card number), or
    None to stop. ``controls``: the document's card controls at inspection; ``answer``: the closure's ``rebind()``;
    ``written``: tokens this document already wrote; ``number_in``: the card number is written (any document)."""
    if not isinstance(answer, dict) or set(answer) != {"fields", "kept"}:
        return None
    fields, kept = answer["fields"], answer["kept"]
    if not isinstance(fields, list) or len(fields) > _MAX_REBIND_FIELDS or len(json.dumps(fields)) > _MAX_REBIND_BYTES:
        return None
    if not (isinstance(kept, dict) and set(kept) == set(fill_tokens)
            and all(type(v) is int and v >= -1 for v in kept.values())):
        return None
    previous = -1
    for row in fields:
        if not _rebind_row(row, previous):
            return None
        previous = row["index"]
    found = [c for r in fields if (c := classify_checkout_control(LoginControl.from_dict(r))) and c.token.startswith("cc-")]
    tokens = [c.token for c in found]
    if len(tokens) != len(set(tokens)) or not set(tokens) <= {c.token for c in controls}:
        return None
    now = {c.token: c.control.index for c in found}
    exposed = {row["index"] for row in fields}
    if any(n in exposed and now.get(token) != n for token, n in kept.items()):
        return None
    pins = {}
    for token in fill_tokens:
        if token in now:
            pins[token] = now[token]
        elif token in written or token == "cc-number" or number_in:
            return None
        else:
            pins[token] = -1
    return pins


def located(result: dict, position: int, documents: int) -> dict:
    """A document's stop with its closure record as ``fill_check``, naming the document's place in the commit
    order (1-based) and the number of documents written."""
    check = result.get("check")
    if not isinstance(check, dict):
        return result
    out = {k: v for k, v in result.items() if k != "check"}
    out["fill_check"] = {**check, "document": position, "documents": documents}
    return out
