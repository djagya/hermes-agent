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
import json
import threading
import time
from typing import Any
from urllib.parse import urlsplit

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
_REINSPECT_FINAL = frozenset({"navigated", "origin_changed", "role_changed", "role_unproven", "too_late", "budget",
                              "fills"})
# cart_changed: a card form caught mid-render can lack its amount text; read again, adopted only on an equal cart.
# documents_changed: more card documents, or another document path, than approved (a re-render shows old and new).
_REINSPECT_RETRIED = frozenset({"incomplete", "ambiguous", "fields_changed", "unsettled", "unverifiable",
                                "unavailable", "cart_changed", "documents_changed"})
# The commit stops a re-inspection may continue from: a changed field set or frame. Never an expiry, a changed cart,
# a switched-off policy, an invalid plan or a write whose outcome is unknown.
_REINSPECTABLE = frozenset({"payment_fields_changed", "payment_page_changed"})
# Repair order (a field that lost its value): card number, expiry, holder, postcode, CVC last.
_REPAIR_RANK = {"cc-number": 0, "cc-exp": 1, "cc-exp-month": 1, "cc-exp-year": 2, "cc-name": 3, "postal-code": 4,
                "cc-csc": 5}
# r3 G6: the caller's thread clears an arming it stopped waiting for while the loop may be finishing it.
_ARM_LOCK = threading.Lock()


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
            armed = _schedule(self._arm_payment_auto(inspected), self._loop, timeout=10) is True
        except Exception:  # health: allow BLE001 -- not armed: the fill runs as it did before re-inspection existed
            armed = False
        if not armed:
            # The caller's view is authoritative: an arming that timed out may still have got as far as setting the
            # flag, and an inspection that is armed for the supervisor (relabel acceptance, re-inspection) but whose
            # caller runs the base fill would be a fill that is neither. r3 G6: the arming may also still be running
            # on the loop and finish later; ``arm_abandoned`` (under the lock it checks under) keeps it from arming.
            with _ARM_LOCK:
                inspected.arm_abandoned = True
                inspected.auto, inspected.approval = False, None
        return armed

    async def _arm_payment_auto(self, i: Any) -> bool:
        if i.consumed or i.owner is not self or not i.approval_id:
            return False
        if await self._remember_roles(i, frozenset()) is not None:
            return False
        approval = _approval_facts(i)
        with _ARM_LOCK:  # no await inside: the caller's give-up and this either come first or after, never between
            if i.arm_abandoned:
                return False
            i.approval, i.auto = approval, True
        return True

    async def _remember_roles(self, i: Any, earlier: frozenset) -> str | None:
        """dlz (fill-auto): record, in each card document's isolated world, the role this inspection gave every
        exposed field (its closure's ``remember``; card-free absence witnesses: every role empty); None when every
        document did. ``role_changed``: an element an
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
        # r3 G1: a card-free document the discovery pass keeps (an absence witness) records its exposed controls'
        # empty roles too, so an element that held a card role in an earlier reading and is still exposed here under
        # another one is a changed role. Best effort: the approval's ``free`` set already refuses any later card
        # field in a document read card-free, whether or not its record could be written.
        for sid, obj in i.absence:
            try:
                answer = _sp()._value(await self._cdp("Runtime.callFunctionOn", {
                    "objectId": obj, "functionDeclaration": "function(a,r){return this.remember(a,r)}",
                    "arguments": [{"value": i.approval_id}, {"value": {}}], "returnByValue": True,
                }, session_id=sid))
            except Exception:  # health: allow BLE001 -- see above: the free set holds without the record
                continue
            if isinstance(answer, dict) and answer.get("moved") is True:
                return "role_changed"
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
        # dlz: what the last refused reading held, for the stop record: card field tokens per APPROVED origin (any
        # other origin is the fixed word `other` and a count: a page chooses its origins, and values are in the page
        # by now), or the discovery's own count of frames it looked into.
        prior.approval.pop("seen", None)
        while time.monotonic() < deadline:
            fresh = None
            adopted = False
            try:
                try:
                    fresh = await self._inspect_payment("", (), prior.allowed_frames, bool(prior.unreviewed_origins),
                                                        prior.cart, prior=prior)
                except PaymentDestinationError as exc:
                    refused = {"payment_fields_unsupported": "incomplete",
                               "payment_fields_ambiguous": "ambiguous"}.get(exc.error_type, "unverifiable")
                    prior.approval["seen"] = _seen_detail(exc.detail, prior.approval)
                except PaymentPolicyError as exc:
                    refused = _reinspect_refusal(str(exc))
                except Exception:  # health: allow BLE001 -- a page that cannot be read now may be read a moment later
                    refused = "unavailable"
                if fresh is not None:
                    refused = _approval_mismatch(prior.approval, fresh) or ""
                    prior.approval["seen"] = _seen_fields(prior.approval, fresh)
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
                        # r3 G1: every document an adopted reading found card-free stays in ``free`` for the rest of
                        # the approval, a card document of an earlier reading too (A -> B -> A: A's promo or address
                        # controls of the middle reading must not come back as its card fields).
                        prior.approval["free"] = prior.approval["free"] | _free_documents(fresh)
                        prior.approval.pop("seen", None)
                        # The hand-over has no await in it: the fresh inspection owns the shared frame sessions from
                        # here and a cancellation finds one owner. Only the earlier one's objects are left to go.
                        prior.child_sessions = {}
                        prior.consumed = True
                        fresh.adopted = True  # r3 G2: its fields are checked as re-inspected ones by every generation
                        await self._release_once(prior)  # cancelled here: ``finally`` still releases ``fresh``
                        adopted = True  # no await between this and the return: the caller has it from here
                        return fresh, ""
            finally:
                if fresh is not None and not adopted:
                    # Refused (or cancelled mid-way): release the fresh reading with only the sessions it attached
                    # itself (by session, not by frame: a new child frame can share its parent's session).
                    # After a hand-over that was cancelled in the release, ``prior.child_sessions`` is empty: all go.
                    owned = set(prior.child_sessions.values())
                    fresh.consumed = True
                    fresh.child_sessions = {k: v for k, v in fresh.child_sessions.items() if v not in owned}
                    await self._release_once(fresh)
            if refused in _REINSPECT_FINAL:
                return None, refused
            await asyncio.sleep(_REINSPECT_POLL_SECONDS)
        return None, refused

    async def _ownership_stop(self, i: Any, ownership) -> str | None:
        """dlz (fill-auto, r1 F11): the error type when the browser is no longer this task's to act on (the lease ended,
        the supervisor was replaced, a human took control), asked of the vault's live check on a worker thread (it may
        wait on control); None when it holds. A check that cannot answer is a stop."""
        try:
            return await asyncio.get_running_loop().run_in_executor(None, ownership, i)
        except Exception:  # health: allow BLE001 -- an ownership check that cannot answer is not a yes
            return "payment_browser_changed"

    async def _commit_payment_auto(self, i: Any, fills: list[dict], until: float | None, strict: bool,
                                   refill, adopted, ownership=None) -> dict:
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
                # The vault checked ownership at the commit fence for the first generation; a later generation starts
                # after re-inspections and settling (seconds): the browser must still be this task's to act on.
                if current is not i and ownership is not None:
                    stopped = await self._ownership_stop(current, ownership)
                    if stopped:
                        current.consumed = True
                        return _auto_result({"success": False, "error_type": stopped}, st)
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
        wrote; a field that already holds its value is not written again. ``i.adopted`` (r3 G2): ``i`` is a
        re-inspection (before the card read or after an earlier generation): every field gets the exposure/hit test. ``mode`` "repair" writes the card number
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
        auto = {"mode": mode, "kept": kept, "adopted": i.adopted is True}
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
    the tab and its top document (frame, loader, URL, origin), the card field tokens each origin receives, how many
    card documents each origin has and at which paths, the unreviewed origins, the cart readings, the card documents
    whose field roles are recorded so far (``documents``) and the admissible documents that held no card field
    (``free``: a document that held none, in this or any adopted reading of the approval, may not become card-bearing
    later; a card document that an adopted reading found card-free stays in it)."""
    fields: dict = {}
    counts: dict = {}
    paths: dict = {}
    by_id = {f[0]: f for f in i.tree}
    for d in i.documents:
        fields.setdefault(d.origin, set()).update(c.token for c in d.controls)
        counts[d.origin] = counts.get(d.origin, 0) + 1
        paths.setdefault(d.origin, set()).add(_path(by_id.get(d.frame_id)))
    loaders = {f[0]: f[1] for f in i.tree}
    return {"page": i.page_id, "top": i.tree[0] if i.tree else None,
            "fields": {origin: frozenset(tokens) for origin, tokens in fields.items()},
            "counts": counts, "paths": {origin: frozenset(p) for origin, p in paths.items()},
            "unreviewed": frozenset(i.unreviewed_origins), "cart": _cart_reading(i),
            "documents": {(d.frame_id, loaders.get(d.frame_id)) for d in i.documents},
            "free": _free_documents(i) - {(d.frame_id, loaders.get(d.frame_id)) for d in i.documents}}


def _path(record: Any) -> str:
    """The pathname of a frame record's URL: a provider re-mints its session query and fragment on each render, the
    path is what a merchant could already have chosen before the approval."""
    try:
        return urlsplit(record[2]).path if record else ""
    except (ValueError, IndexError, TypeError):
        return ""


def _free_documents(i: Any) -> frozenset:
    """``(frame, loader)`` of each admissible document this inspection looked into and found without a card field."""
    loaders = {f[0]: f[1] for f in i.tree}
    return frozenset((fid, loaders.get(fid)) for fid in getattr(i, "free_frames", ()))


def _cart_reading(i: Any) -> tuple:
    """The cart readings an approval binds: the stated cart, the page-check state, and per witnessed place the union
    of its documents' labelled totals and whether any was unclear. Places are merged as ``_page_match`` merges them:
    a form re-shaped from two same-origin documents to one is the same reading."""
    places: dict = {}
    for place, raw in i.cart_pins:
        try:
            pin = json.loads(raw)
            labelled, unclear = set(pin["labelled"]), bool(pin["unclear"])
        except (ValueError, KeyError, TypeError):
            labelled, unclear = {"?"}, True
        seen = places.setdefault(place, [set(), False])
        seen[0] |= labelled
        seen[1] = seen[1] or unclear
    return (i.cart, tuple(i.page_match),
            tuple(sorted((place, tuple(sorted(map(str, v[0]))), v[1]) for place, v in places.items())))


def _approval_mismatch(approval: dict, fresh: Any) -> str | None:
    """dlz (fill-auto): why a re-inspection is not the checkout the owner approved, or None. ``navigated``: another
    tab or top document (a reload or a changed URL counts); ``origin_changed``: a card field on an origin the approval
    did not name, or another unreviewed set; ``fields_changed``: an approved origin receives other card fields than
    approved (fewer too: a form still rendering reads like that and is read again); ``role_changed``: a document that
    held no card field when the approval was built holds one now (its controls had another role); ``documents_changed``:
    an origin has more card documents than approved, or one at a path the approval did not show for that origin;
    ``cart_changed``: a page-check reading differs."""
    now = _approval_facts(fresh)
    if now["page"] != approval["page"] or now["top"] is None or now["top"] != approval["top"]:
        return "navigated"
    if not set(now["fields"]) <= set(approval["fields"]) or now["unreviewed"] - approval["unreviewed"]:
        return "origin_changed"
    if now["fields"] != approval["fields"] or now["unreviewed"] != approval["unreviewed"]:
        return "fields_changed"
    if now["documents"] & approval["free"]:
        return "role_changed"
    if any(n > approval["counts"].get(origin, 0) or not now["paths"][origin] <= approval["paths"].get(origin, frozenset())
           for origin, n in now["counts"].items()):
        return "documents_changed"
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


def _seen_fields(approval: dict, fresh: Any) -> dict:
    """What a refused reading held, for ``fill_reinspect.seen``: the card field tokens of each origin the approval
    names; every other origin is only counted, as the fixed word ``other``."""
    fields = _approval_facts(fresh)["fields"]
    named = set(approval["fields"]) | set(approval["unreviewed"])
    out: dict = {origin: sorted(tokens) for origin, tokens in fields.items() if origin in named}
    if len(fields) > len(out):
        out["other"] = len(fields) - len(out)
    return out


def _seen_detail(detail: Any, approval: dict) -> dict | None:
    """What a refused discovery reading reported (``PaymentDestinationError.detail``), for ``fill_reinspect.seen``:
    its count of unreviewed frames looked into, or the card field tokens per approved origin (other origins: only
    ``other`` and their count). Nothing else passes."""
    if not isinstance(detail, dict):
        return None
    count = detail.get("unreviewed_frames_inspected")
    if type(count) is int:
        return {"unreviewed_frames_inspected": count}
    rows = detail.get("receiving_origins")
    if isinstance(rows, list) and all(isinstance(r, dict) and isinstance(r.get("origin"), str)
                                      and isinstance(r.get("fields"), list) for r in rows):
        named = set(approval["fields"]) | set(approval["unreviewed"])
        out = {r["origin"]: sorted(str(t) for t in r["fields"]) for r in rows[:8] if r["origin"] in named}
        if len(rows) > len(out):
            out["other"] = len(rows) - len(out)
        return out
    return None
