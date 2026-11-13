"""The armed card fill: one approval, several inspections (dlz, 11-13).

A hosted card form re-renders: between the inspection and the owner's approval (FlixBus / Payrails), on every write
(SBB / Datatrans re-labels the other fields with validation text), or after the number went in (Eurowings clears the
CVC). Before 11-13 each of these stopped the fill, because the fill pinned the DOM it had inspected before the
approval. The approval does not bind DOM nodes. It binds the tab and its top document, the card field tokens each
origin receives, the unreviewed origins and the cart readings (``_approval_facts``).

So an armed fill (``arm_payment_auto``; config ``vault.payment_fill_auto``, on by default) may be continued on a
re-inspection: the full inspection again, by the same code, with one judge only (``_reinspect``). There is no second,
cheaper rule that decides which element is a card field or whether the owner can see it. On top of the inspection:

- the reading must match the approval (``_approval_mismatch``) and be still current a moment later;
- every element an earlier inspection of the approval classified, and that is still exposed, must have kept its role
  (``_remember_roles``: a record per element in the document's isolated world);
- a label-only change of the same elements is classified again in place and accepted only when every role is
  unchanged (``_accept_relabel``); it uses no re-inspection;
- a field that an earlier generation may have written is not written again while it holds its value;
- a field that is empty or was replaced after the page settled gets one repair generation.

Navigation, another tab, a card field on an origin the approval did not name, a changed cart reading, a changed role,
an expired window, a switched-off policy and a write whose outcome is unknown always end the fill. This module is the
supervisor mixin for it; ``tools.browser_supervisor_payment`` holds the inspection and the per-document commit, and is
imported lazily (it imports this module).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from agent.vault_payment_policy import PaymentDestinationError, PaymentPolicyError


def _sp():
    from tools import browser_supervisor_payment

    return browser_supervisor_payment


# Bounds of an armed fill: a re-rendered card form is inspected again in full inside the same approval window:
# at most _MAX_REINSPECTIONS per fill, at most _MAX_REPAIRS of them for a field that lost its value after settling,
# each waiting at most _REINSPECT_WAIT_SECONDS for a complete form that matches the approval. The whole commit has
# AUTO_BUDGET_SECONDS (and never outlives the approval window).
_MAX_REINSPECTIONS = 3
_MAX_REPAIRS = 1
_REINSPECT_WAIT_SECONDS = 10.0
_REINSPECT_POLL_SECONDS = 0.4
_REINSPECT_SETTLE_SECONDS = 0.25
AUTO_BUDGET_SECONDS = 50.0
# Why a re-inspection did not continue the fill (fill_reinspect.refused): fixed codes. The first group ends the
# attempt at once; the second is retried until the wait is over (a form still rendering).
_REINSPECT_FINAL = frozenset({"navigated", "origin_changed", "role_changed", "role_unproven", "cart_changed",
                              "too_late", "budget", "fills"})
_REINSPECT_RETRIED = frozenset({"incomplete", "ambiguous", "fields_changed", "unsettled", "unverifiable",
                                "unavailable"})
# The commit stops a re-inspection may continue from: a changed field set or frame. Never an expiry, a changed cart,
# a switched-off policy, an invalid plan or a write whose outcome is unknown.
_REINSPECTABLE = frozenset({"payment_fields_changed", "payment_page_changed"})
# Repair order (a field that lost its value): card number, expiry, holder, postcode, CVC last.
_REPAIR_RANK = {"cc-number": 0, "cc-exp": 1, "cc-exp-month": 1, "cc-exp-year": 2, "cc-name": 3, "postal-code": 4,
                "cc-csc": 5}


class PaymentAutoMixin:
    async def _accept_relabel(self, i: Any, d: Any) -> bool:
        """dlz (fill-auto): the document's fields are the pinned elements and only label text changed (the closure's
        ``relabeled()``: validation text, a floating label, a placeholder): its exposed fields, read now, are
        classified by the inspection's classifier, and only when every card control keeps its index and token and no
        other control became a card field does the closure take the new description as pinned (``accept()``, which
        requires the description to be still the one read). Anything else: False, and the change stands as one."""
        try:
            raw = _sp()._value(await self._cdp("Runtime.callFunctionOn", {
                "objectId": d.object_id, "functionDeclaration": "function(){return this.relabeled()}",
                "returnByValue": True}, session_id=d.frame_session))
            if raw is None:
                return False
            rows = _sp()._bounded_description(raw) if i.unreviewed_origins else raw
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                return False
            if _roles(_sp()._card_controls(rows)) != _roles(d.controls):
                return False
            return _sp()._value(await self._cdp("Runtime.callFunctionOn", {
                "objectId": d.object_id, "functionDeclaration": "function(){return this.accept()}",
                "returnByValue": True}, session_id=d.frame_session)) is True
        except Exception:  # health: allow BLE001 -- an unreadable or malformed reading is never accepted
            return False

    def arm_payment_auto(self, inspected: Any) -> bool:
        """dlz (fill-auto): make this inspection the first of an approval that may be continued on re-inspections:
        record what the approval binds and, in each card document, the role of every exposed field. False (and the
        fill stays the stop-on-any-change fill it was) when a document cannot record its roles."""
        from tools.browser_supervisor import _schedule

        try:
            return _schedule(self._arm_payment_auto(inspected), self._loop, timeout=10) is True
        except Exception:  # health: allow BLE001 -- not armed: the fill runs as it did before re-inspection existed
            return False

    async def _arm_payment_auto(self, i: Any) -> bool:
        if i.consumed or i.owner is not self or not i.approval_id:
            return False
        if await self._remember_roles(i, frozenset()) is not None:
            return False
        i.approval = _approval_facts(i)
        i.auto = True
        return True

    async def _remember_roles(self, i: Any, earlier: frozenset) -> str | None:
        """dlz (fill-auto): record, in each card document's isolated world, the role this inspection gave every
        exposed field (its closure's ``remember``); None when every document did. ``role_changed``: an element an
        earlier inspection of this approval classified, still exposed, has another role now (a CVC field turned
        into a promo code, a holder field into the card number). ``role_unproven``: a document in ``earlier`` (the
        ``(frame, loader)`` of the documents already recorded) does not hold the approval's record, or did not
        answer: its fields' earlier roles cannot be compared, so the fill does not go on."""
        loaders = {f[0]: f[1] for f in i.tree}
        for d in i.documents:
            roles = {str(c.control.index): c.token for c in d.controls}
            try:
                answer = _sp()._value(await self._cdp("Runtime.callFunctionOn", {
                    "objectId": d.object_id, "functionDeclaration": "function(a,r){return this.remember(a,r)}",
                    "arguments": [{"value": i.approval_id}, {"value": roles}], "returnByValue": True,
                }, session_id=d.frame_session))
            except Exception:  # health: allow BLE001 -- a document that cannot answer proves nothing
                answer = None
            if not (isinstance(answer, dict) and set(answer) == {"known", "moved"}
                    and type(answer["known"]) is bool and type(answer["moved"]) is bool):
                return "role_unproven"
            if answer["moved"]:
                return "role_changed"
            if (d.frame_id, loaders.get(d.frame_id)) in earlier and not answer["known"]:
                return "role_unproven"
        return None

    def reinspect_payment(self, prior: Any, wait: float | None = None) -> tuple:
        """dlz (fill-auto): ``(fresh inspection, "")`` that continues ``prior``'s approval on the page as it is now
        (``prior`` is released), or ``(None, code)`` when no complete card form matching the approval was found
        within ``wait`` seconds (``prior`` is then left as it was, for the caller to discard)."""
        from tools.browser_supervisor import _schedule

        wait = _REINSPECT_WAIT_SECONDS if wait is None else wait
        stop = min(prior.expires_at, time.monotonic() + wait)
        return _schedule(self._reinspect(prior, stop, wait), self._loop, timeout=wait + 30)

    async def _reinspect(self, prior: Any, stop: float, wait: float) -> tuple:
        """One re-inspection inside an approval: the full inspection again (``_inspect_payment``: the same frame
        admission, exposure rules, classifier and exact-once rules, the reviewed or the discovery pass as the first
        one was), repeated until ``stop`` or ``wait`` seconds while the form is incomplete or still moving. The
        reading continues the fill only when it matches the approval (``_approval_mismatch``: same tab and top
        document, the same card field tokens on the same origins, no other origin, the same cart readings), every
        element an earlier inspection classified kept its role (``_remember_roles``), and it is still current a
        moment later. Its window is the approval's; nothing extends it."""
        if prior.auto is not True or not isinstance(prior.approval, dict):
            return None, "unavailable"
        deadline = min(stop, prior.expires_at, time.monotonic() + wait)
        refused = "too_late"
        # dlz: what the last refused reading held, for the stop record: card field tokens per origin, or the
        # discovery's own count of frames it looked into. Origins, fixed tokens and counts only.
        prior.approval.pop("seen", None)
        while time.monotonic() < deadline:
            fresh = None
            try:
                fresh = await self._inspect_payment("", (), prior.allowed_frames, bool(prior.unreviewed_origins),
                                                    prior.cart, prior=prior)
            except PaymentDestinationError as exc:
                refused = {"payment_fields_unsupported": "incomplete",
                           "payment_fields_ambiguous": "ambiguous"}.get(exc.error_type, "unverifiable")
                prior.approval["seen"] = _seen_detail(exc.detail)
            except PaymentPolicyError as exc:
                refused = _reinspect_refusal(str(exc))
            except Exception:  # health: allow BLE001 -- a page that cannot be read now may be read a moment later
                refused = "unavailable"
            if fresh is not None:
                refused = _approval_mismatch(prior.approval, fresh) or ""
                prior.approval["seen"] = {origin: sorted(tokens)
                                          for origin, tokens in _approval_facts(fresh)["fields"].items()}
                if not refused:
                    # Still the same reading a moment later (a form caught mid-render is read again instead).
                    await asyncio.sleep(_REINSPECT_SETTLE_SECONDS)
                    if time.monotonic() >= deadline or not await self._payment_is_current(fresh):
                        refused = "unsettled"  # changed again, or no time left to see that it did not
                if not refused:
                    refused = await self._remember_roles(fresh, frozenset(prior.approval["documents"])) or ""
                if not refused:
                    loaders = {f[0]: f[1] for f in fresh.tree}
                    prior.approval["documents"] |= {(d.frame_id, loaders.get(d.frame_id)) for d in fresh.documents}
                    prior.approval.pop("seen", None)
                    # The fresh inspection owns the shared frame sessions from here; the earlier one's objects go.
                    prior.child_sessions = {}
                    prior.consumed = True
                    await self._release_once(prior)
                    return fresh, ""
                fresh.consumed = True
                fresh.child_sessions = {k: v for k, v in fresh.child_sessions.items()
                                        if prior.child_sessions.get(k) != v}
                await self._release_once(fresh)
            if refused in _REINSPECT_FINAL:
                return None, refused
            await asyncio.sleep(_REINSPECT_POLL_SECONDS)
        return None, refused

    async def _commit_payment_auto(self, i: Any, fills: list[dict], until: float | None, strict: bool,
                                   refill, adopted) -> dict:
        """dlz (fill-auto): the commit of an armed fill, as generations: each one is a whole commit on one
        inspection (``_commit_generation``: the same freshness, cart, policy and window checks, the same
        per-document and per-setter checks, the settled retention check). When a generation stops on a changed
        field set or frame, or a written field lost its value after settling, the page is inspected again in full
        (``_reinspect``); if that reading matches the approval, the next generation writes, into the fields it
        classified, what they do not hold yet. Bounded: ``_MAX_REINSPECTIONS`` re-inspections, ``_MAX_REPAIRS`` of
        them for lost values, the commit budget and the approval window. Every other stop ends the fill as before,
        and the stop it returns is the generation's own, with ``fill_reinspect`` saying what was tried."""
        stop = i.expires_at if until is None else min(i.expires_at, until)
        st = {"reinspections": 0, "repairs": 0, "refused": "", "wrote": 0}
        current, current_fills, mode = i, fills, "fill"
        try:
            while True:
                again, result = await self._commit_generation(current, current_fills, stop, strict, mode,
                                                              st["reinspections"] > 0, st)
                if not again:
                    return _auto_result(result, st)
                if st["reinspections"] >= _MAX_REINSPECTIONS or (again == "repair" and st["repairs"] >= _MAX_REPAIRS):
                    st["refused"] = "budget"
                    return _auto_result(result, st)
                fresh, st["refused"] = await self._reinspect(current, stop, _REINSPECT_WAIT_SECONDS)
                if fresh is None:
                    st["seen"] = current.approval.get("seen")
                    return _auto_result(result, st)
                current = fresh
                st["reinspections"] += 1
                st["repairs"] += again == "repair"
                mode = "repair" if again == "repair" else "fill"
                try:
                    if adopted is not None:
                        adopted(fresh)
                    current_fills = refill(fresh)
                except Exception:  # health: allow BLE001 -- no plan for this reading (an expiry list without the card's month)
                    current_fills = None
                if not current_fills:
                    st["refused"] = "fills"
                    return _auto_result(result, st)
        finally:
            if current is not i:
                current.consumed = True
                await self._release_once(current)

    async def _commit_generation(self, i: Any, fills: list[dict], stop: float, strict: bool,
                                 mode: str, kept: bool, st: dict) -> tuple:
        """One generation of an armed commit on inspection ``i``: ``(again, result)``. ``again``: "" (the result is
        final), "changed" (stopped on a changed field set or frame; a re-inspection may continue) or "repair" (every
        field was written, and after settling some are empty or were replaced). ``kept``: an earlier generation
        wrote; a field that already holds its value is not written again. ``mode`` "repair" writes the card number
        first and the CVC last."""
        def later(result: dict) -> dict:  # a stop after an earlier generation wrote is a partial fill
            return {**result, "partially_filled": True} if st["wrote"] and not result.get("success") else result

        if time.monotonic() >= i.expires_at:
            i.consumed = True
            return "", later({"success": False, "error_type": "payment_consent_expired"})
        if not await self._payment_is_current(i):
            i.consumed = True
            return "changed", later({"success": False, "error_type": "payment_page_changed"})
        if i.witnesses and not await self._cart_current(i):
            i.consumed = True
            return "", later({"success": False, "error_type": "payment_cart_changed"})
        if not _sp()._policy_holds(i):
            i.consumed = True
            return "", later({"success": False, "error_type": "payment_destination_disabled"})
        if time.monotonic() >= i.expires_at:
            i.consumed = True
            return "", later({"success": False, "error_type": "payment_consent_expired"})
        i.consumed = True  # consume before ingress, as _commit_payment_once does
        filled = 0
        state = {"number": False, "repins": 0, "repin": False}
        auto = {"mode": mode, "kept": kept}
        try:
            written: list = []
            order = _sp()._commit_order(i, _sp()._fills_by_document(i, fills))
            if mode == "repair":
                order = sorted(order, key=lambda p: min(_REPAIR_RANK.get(str(f.get("token")), 4) for f in p[1]))
            for position, (document, document_fills) in enumerate(order, 1):
                result = await self._commit_step(i, position, len(order), document, document_fills, None, written,
                                                 stop, strict, state, auto=auto)
                count = result.get("filled_fields")
                st["wrote"] = max(st["wrote"], filled + (count if type(count) is int else 0),
                                  1 if result.get("write_uncertain") else 0)
                if not result.get("success"):
                    result = later(_sp()._partial(result, filled))
                    retry = result.get("error_type") in _REINSPECTABLE and not result.get("write_uncertain")
                    return ("changed" if retry else ""), result
                filled += result.get("filled_fields", 0)
                written.append((document, document_fills))
            await asyncio.sleep(_sp()._SETTLE_SECONDS)
            before = await self._shown_documents(i)
            answers = [(d, f, await self._payment_retention(i, d)) for d, f in written]
            after = await self._shown_documents(i)
            result = _sp()._retention_result([(f, a if d.frame_id in before and d.frame_id in after else None)
                                        for d, f, a in answers])
            if await self._top_navigated(i):
                result["navigated_after_fill"] = True
                return "", result
            if result.get("success"):
                return "", result
            lost = set(result.get("field_retention", {}).values()) - {"held"}
            return ("repair" if lost <= {"empty", "replaced"} else ""), result
        except PaymentPolicyError:
            return "", later(_sp()._partial({"success": False, "error_type": "payment_fields_invalid"}, filled))
        except Exception:  # health: allow BLE001 -- CDP/transport text can carry values; reported as a fixed type, never retried
            return "", later(_sp()._partial({"success": False, "error_type": "payment_transport_uncertain"}, filled))


def _roles(controls: Any) -> list:
    """The card roles of a document's classified controls: ``(inspection index, token)`` in document order."""
    return [(c.control.index, c.token) for c in controls]


def _approval_facts(i: Any) -> dict:
    """dlz (fill-auto): what the owner's approval binds, read off the inspection the approval card was built from:
    the tab and its top document (frame, loader, URL, origin), the card field tokens each origin receives, the
    unreviewed origins, the cart readings, and the card documents whose field roles are recorded so far."""
    fields: dict = {}
    for d in i.documents:
        fields.setdefault(d.origin, set()).update(c.token for c in d.controls)
    loaders = {f[0]: f[1] for f in i.tree}
    return {"page": i.page_id, "top": i.tree[0] if i.tree else None,
            "fields": {origin: frozenset(tokens) for origin, tokens in fields.items()},
            "unreviewed": frozenset(i.unreviewed_origins), "cart": (i.cart, i.cart_pins, tuple(i.page_match)),
            "documents": {(d.frame_id, loaders.get(d.frame_id)) for d in i.documents}}


def _approval_mismatch(approval: dict, fresh: Any) -> str | None:
    """dlz (fill-auto): why a re-inspection is not the checkout the owner approved, or None. ``navigated``: another
    tab or top document (a reload or a changed URL counts); ``origin_changed``: a card field on an origin the approval
    did not name, or another unreviewed set; ``fields_changed``: an approved origin receives other card fields than
    approved (fewer too: a form still rendering reads like that and is read again); ``cart_changed``: a page-check
    reading differs."""
    now = _approval_facts(fresh)
    if now["page"] != approval["page"] or now["top"] is None or now["top"] != approval["top"]:
        return "navigated"
    if not set(now["fields"]) <= set(approval["fields"]) or now["unreviewed"] - approval["unreviewed"]:
        return "origin_changed"
    if now["fields"] != approval["fields"] or now["unreviewed"] != approval["unreviewed"]:
        return "fields_changed"
    return None if now["cart"] == approval["cart"] else "cart_changed"


def _reinspect_refusal(text: str) -> str:
    """The code for a re-inspection the reviewed pass refused with ``text``."""
    head = text.split(" (", 1)[0]
    if head in _sp()._COVERAGE_REFUSALS:
        return "incomplete"
    if head in ("Ambiguous card expiry fields", "Card fields split across payment providers"):
        return "ambiguous"
    return "navigated" if head in ("Supervised browser required", "Merchant origin mismatch") else "unavailable"


def _auto_result(result: dict, st: dict) -> dict:
    """dlz (fill-auto): the armed commit's result: a stop after any write is a partial fill that counts the writes,
    and ``fill_reinspect`` says how many re-inspections continued the fill (``repairs`` of them for lost values)
    and, when one did not, why (a fixed code)."""
    out = dict(result)
    if not out.get("success") and st["wrote"]:
        out["partially_filled"] = True
        if out.get("error_type") != "payment_fields_not_retained":
            count = out.get("filled_fields")
            out["filled_fields"] = max(count if type(count) is int else 0, st["wrote"])
    if st["reinspections"] or st["refused"]:
        refused = st["refused"] if st["refused"] in _REINSPECT_FINAL | _REINSPECT_RETRIED else ""
        out["fill_reinspect"] = {"count": st["reinspections"], "repairs": st["repairs"],
                                 **({"refused": refused} if refused else {}),
                                 **({"seen": st["seen"]} if refused and st.get("seen") else {})}
    return out


def _seen_detail(detail: Any) -> dict | None:
    """What a refused discovery reading reported (``PaymentDestinationError.detail``), for ``fill_reinspect.seen``:
    its count of unreviewed frames looked into, or the card field tokens per origin. Nothing else passes."""
    if not isinstance(detail, dict):
        return None
    count = detail.get("unreviewed_frames_inspected")
    if type(count) is int:
        return {"unreviewed_frames_inspected": count}
    rows = detail.get("receiving_origins")
    if isinstance(rows, list) and all(isinstance(r, dict) and isinstance(r.get("origin"), str)
                                      and isinstance(r.get("fields"), list) for r in rows):
        return {r["origin"]: sorted(str(t) for t in r["fields"]) for r in rows[:8]}
    return None
