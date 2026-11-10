"""Document-pinned payment operations on the existing supervisor socket.

No whole-checkout transaction is claimed. Card controls sit in one document, or are split
across documents on the merchant and at most ONE reviewed PSP origin with every card field
exactly once (Datatrans Secure Fields, Stripe split elements); secrets never go through CLI
eval or a model tool argument.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from agent.vault_login_classifier import LoginControl, classify_checkout_control
from agent.vault_payment_policy import (
    PAYMENT_PSP_ORIGINS,
    PaymentPolicyError,
    coverage_hint,
    https_origin,
    provider_for_origin,
)


_MAX_CARD_DOCUMENTS = 4
# Refusals that mean "no complete reviewed card document": these get coverage_hint's reason.
_COVERAGE_REFUSALS = frozenset({
    "Missing, ambiguous or split payment documents",
    "Incomplete or ambiguous card fields",
    "Missing card expiry fields",
})
# Bounded wait between the last document's write and the all-documents retention check: long enough
# for a provider's deferred re-render, validation or brand detection to land, short enough to stay
# well inside commit_payment's 20 s budget. Never a retry window. dlz: 2 s, not 1 s: a reset that
# lands between 1 and 2 s after the last write was still reported as success.
_SETTLE_SECONDS = 2.0
# dlz: commit_payment's whole budget; no write lands in a renderer after it (see _renderer_deadline_ms).
_COMMIT_BUDGET_SECONDS = 20.0
_RETENTION_STATES = frozenset({"held", "empty", "changed", "replaced"})
# Card fields a saved card may lack: left empty for the user instead of refusing the whole fill.
OPTIONAL_CARD_TOKENS = frozenset({"cc-name", "postal-code"})


@dataclass
class PaymentDocument:
    """One document holding card controls, pinned by frame, session and isolated-world object."""

    frame_id: str
    frame_session: str
    object_id: str
    origin: str
    controls: tuple
    # dlz: host monotonic time just after this document's closure was created (its renderer clock anchor).
    clock_anchor: float | None = None


@dataclass
class PaymentInspection:
    operation_id: str
    page_session: str
    page_id: str
    merchant_origin: str
    documents: tuple
    tree: tuple
    expires_at: float
    consumed: bool = False
    owner: Any = field(default=None, repr=False)
    child_sessions: dict = field(default_factory=dict, repr=False)
    # dlz: parent frame id -> (session, [(frame id, pinned owner <iframe> handle)]) for every frame from a card
    # document up to the page, all handles of one parent in one isolated world (one call checks them all).
    frame_owners: dict = field(default_factory=dict, repr=False)
    # dlz: the one release pass (asyncio task) shared by commit and discard.
    release_task: Any = field(default=None, repr=False, compare=False)

    @property
    def controls(self) -> tuple:
        return tuple(c for d in self.documents for c in d.controls)

    @property
    def target_origins(self) -> tuple:
        """Every origin that receives card fields, once each, in document order."""
        return tuple(dict.fromkeys(d.origin for d in self.documents))

    @property
    def frame_origin(self) -> str:
        """The hosted PSP origin when one receives fields, else the merchant."""
        hosted = [o for o in self.target_origins if o != self.merchant_origin]
        return hosted[0] if hosted else self.merchant_origin

    @property
    def frame_session(self) -> str:
        """The card document's session (the first one of a split checkout)."""
        return self.documents[0].frame_session

    def prompt(self) -> dict:
        n = len(self.documents)
        return {
            "operation_id": self.operation_id,
            "merchant_origin": self.merchant_origin,
            "target_origin": ", ".join(self.target_origins),
            "target_origins": list(self.target_origins),
            "fields": sorted(c.token for c in self.controls),
            "checks": [
                "HTTPS origins",
                "exact document and field references",
                f"card fields split across {n} documents, each field once" if n > 1 else "single card document",
            ],
            "merchant_reputation": "not established; verify the seller independently",
        }


def _card_documents(candidates: list, merchant: str) -> tuple:
    """The documents to fill: one, or a split checkout where every card field appears exactly once
    across documents and the fields reach at most the merchant plus ONE hosted PSP origin. Anything
    else is ambiguous and refused. ``candidates`` holds ``(frame_id, session, object_id, origin,
    controls)`` for each allowed document that has card controls."""
    hosted = {c[3] for c in candidates} - {merchant}
    if len(hosted) > 1:
        raise PaymentPolicyError("Card fields split across payment providers")
    provider = provider_for_origin(next(iter(hosted))) if hosted else None
    if not candidates or len(candidates) > (provider.max_documents if provider else _MAX_CARD_DOCUMENTS):
        raise PaymentPolicyError("Missing, ambiguous or split payment documents")
    tokens = [control.token for c in candidates for control in c[4]]
    if len(tokens) != len(set(tokens)) or not {"cc-number", "cc-csc"} <= set(tokens):
        raise PaymentPolicyError("Incomplete or ambiguous card fields")
    if not ("cc-exp" in tokens or {"cc-exp-month", "cc-exp-year"} <= set(tokens)):
        raise PaymentPolicyError("Missing card expiry fields")
    if "cc-exp" in tokens and ("cc-exp-month" in tokens or "cc-exp-year" in tokens):
        raise PaymentPolicyError("Ambiguous card expiry fields")
    return tuple(PaymentDocument(*c) for c in candidates)


def _explained_card_documents(candidates: list, merchant: str, tree: tuple, allowed: tuple) -> tuple:
    """``_card_documents``, with a coverage refusal naming the provider-level cause it can see in the
    frame tree (an unsupported PSP, a reviewed one not enabled, a non-card-field page)."""
    try:
        return _card_documents(candidates, merchant)
    except PaymentPolicyError as exc:
        hint = coverage_hint(tree, merchant, allowed) if str(exc) in _COVERAGE_REFUSALS else ""
        if hint:
            raise PaymentPolicyError(f"{exc} ({hint})") from None
        raise


def _frame_admitted(record: tuple, by_id: dict, merchant: str, allowed: tuple) -> bool:
    """Whether a frame ``(id, loader, url, origin, parent)`` may hold card fields: the merchant or a
    reviewed provider origin, on that provider's card-field path, under an ancestry of the same. False
    = not a candidate (ads, a provider's wallet/login page); raises on a reviewed frame placed under an
    unapproved ancestor."""
    def on_card_path(r: tuple) -> bool:
        provider = provider_for_origin(r[3]) if r[3] != merchant else None
        return provider is None or provider.admits(r[2])

    def may_contain(r: tuple) -> bool:
        provider = provider_for_origin(r[3]) if r[3] != merchant else None
        return provider is None or provider.admits_ancestor(r[2])

    # Ignore unrelated ads, but never inspect/inject into an arbitrary PSP.
    if record[3] not in (merchant, *allowed) or not on_card_path(record):
        return False
    https_origin(record[3])
    ancestor = record[4]
    while ancestor:
        above = by_id.get(ancestor)
        if above is None or above[3] not in (merchant, *allowed) or not may_contain(above):
            raise PaymentPolicyError("Unapproved payment frame ancestry")  # e.g. inside a wallet page
        ancestor = above[4]
    return True


def _fills_by_document(i: PaymentInspection, fills: list) -> list:
    """Group fills by their ``document`` index (default 0); every card document needs its fields."""
    groups: list = [[] for _ in i.documents]
    for f in fills:
        k = f.get("document", 0) if isinstance(f, dict) else None
        if type(k) is not int or not 0 <= k < len(groups):
            raise PaymentPolicyError("Fill targets an unknown document")
        groups[k].append({key: v for key, v in f.items() if key != "document"})
    # A document may go unwritten only when everything in it is optional (a holder-only frame and a
    # saved card without a holder name); a document with a required card field always needs its fill.
    if not any(groups) or any(not g and any(c.token not in OPTIONAL_CARD_TOKENS for c in d.controls)
                              for d, g in zip(i.documents, groups)):
        raise PaymentPolicyError("Every card document needs its fields")
    return groups


def _value(reply: dict) -> Any:
    payload = reply.get("result", {})
    if payload.get("exceptionDetails"):
        raise PaymentPolicyError("Payment document unavailable")
    return payload.get("result", {}).get("value")


class PaymentSupervisionMixin:
    def inspect_payment(
        self,
        merchant_origin: str = "",
        *,
        bound_origins: tuple = (),
        frame_origins: tuple = PAYMENT_PSP_ORIGINS,
    ) -> PaymentInspection:
        from tools.browser_supervisor import _schedule

        return _schedule(
            self._inspect_payment(merchant_origin, bound_origins, frame_origins),
            self._loop,
            timeout=25,
        )

    async def _payment_tree(
        self, sid: str, child_sessions: dict | None = None
    ) -> tuple:
        child_sessions = child_sessions if child_sessions is not None else {}
        response = await self._cdp("Page.getFrameTree", session_id=sid)
        frames = []

        def walk(node, parent=""):
            frame = node["frame"]
            frames.append((
                frame["id"],
                frame.get("loaderId", ""),
                frame.get("url", ""),
                frame.get("securityOrigin", ""),
                parent,
            ))
            for child in node.get("childFrames", []):
                walk(child, frame["id"])

        walk(response["result"]["frameTree"])
        # Page.getFrameTree is renderer-scoped: OOPIF documents must be
        # read from their own sessions, then anchored with DOM.getFrameOwner.
        targets = (await self._cdp("Target.getTargets"))["result"]["targetInfos"]
        pending = [t for t in targets if t.get("type") == "iframe"]
        while pending:
            progress = False
            for target in list(pending):
                fid = target["targetId"]
                parent = None
                # Discover ownership from the current DOM, including frames that
                # existed before this supervisor attached. Event history is not proof.
                for record in frames:
                    parent_sid = child_sessions.get(record[0], sid)
                    obj = None
                    try:
                        owner = (
                            await self._cdp(
                                "DOM.getFrameOwner",
                                {"frameId": fid},
                                session_id=parent_sid,
                            )
                        )["result"]
                        context = (
                            await self._cdp(
                                "Page.createIsolatedWorld",
                                {
                                    "frameId": record[0],
                                    "worldName": "hermes-payment-ancestry",
                                },
                                session_id=parent_sid,
                            )
                        )["result"]["executionContextId"]
                        obj = (
                            await self._cdp(
                                "DOM.resolveNode",
                                {
                                    "backendNodeId": owner["backendNodeId"],
                                    "executionContextId": context,
                                },
                                session_id=parent_sid,
                            )
                        )["result"]["object"]["objectId"]
                        belongs = _value(
                            await self._cdp(
                                "Runtime.callFunctionOn",
                                {
                                    "objectId": obj,
                                    "functionDeclaration": "function(){return this.ownerDocument===document && this.isConnected}",
                                    "returnByValue": True,
                                },
                                session_id=parent_sid,
                            )
                        )
                        if belongs is True:
                            parent = record[0]
                            break
                    except Exception:
                        continue
                    finally:
                        if obj:
                            await self._release_payment_object(parent_sid, obj)
                if not parent:
                    continue
                if len(frames) > 64:
                    raise PaymentPolicyError("Payment frame tree too large")
                parent_sid = child_sessions.get(parent, sid)
                fsid = child_sessions.get(fid)
                if not fsid:
                    fsid = (
                        await self._cdp(
                            "Target.attachToTarget", {"targetId": fid, "flatten": True}
                        )
                    )["result"]["sessionId"]
                    child_sessions[fid] = fsid
                    await self._enable_page_domains(fsid, timeout=5)
                child = (await self._cdp("Page.getFrameTree", session_id=fsid))[
                    "result"
                ]["frameTree"]
                # Replace the parent's placeholder with the child's authoritative loader/origin.
                frames[:] = [f for f in frames if f[0] != fid]
                known = {f[0] for f in frames}
                walk(child, parent)
                # Same-origin frames inside this OOPIF (PayPal's guest card form inside /smart/buttons)
                # are in its process: they are reached through its session, never the page's.
                for f in frames:
                    if f[0] not in known:
                        child_sessions.setdefault(f[0], fsid)
                pending.remove(target)
                progress = True
            if not progress:
                break
        return (frames[0], *sorted(frames[1:]))

    async def _inspect_payment(
        self, origin: str, bound: tuple, allowed_frames: tuple
    ) -> PaymentInspection:
        if origin:
            https_origin(origin)
        with self._state_lock:
            sid = self._page_session_id if self._active else None
        if not sid:
            raise PaymentPolicyError("Supervised browser required")
        targets = (await self._cdp("Target.getTargets"))["result"]["targetInfos"]
        if origin:
            from agent.vault_store import normalize_origin

            pages = [
                t
                for t in targets
                if t["type"] == "page"
                and t.get("url", "").startswith("https://")
                and normalize_origin(t["url"]) == origin
            ]
            if len(pages) != 1:
                raise PaymentPolicyError("Merchant tab missing or ambiguous")
            current = (await self._cdp("Target.getTargetInfo", session_id=sid))[
                "result"
            ]["targetInfo"]
            if current["targetId"] != pages[0]["targetId"]:
                old_sid = sid
                sid = (
                    await self._cdp(
                        "Target.attachToTarget",
                        {"targetId": pages[0]["targetId"], "flatten": True},
                    )
                )["result"]["sessionId"]
                try:
                    await self._enable_page_domains(sid, timeout=5)
                    await self._install_dialog_bridge(sid)
                except Exception:
                    await self._release_payment_sessions({"new": sid})
                    raise
                with self._state_lock:
                    self._page_session_id = sid
                await self._release_payment_sessions({"old": old_sid})
        page = (await self._cdp("Target.getTargetInfo", session_id=sid))["result"][
            "targetInfo"
        ]
        child_sessions = {}
        operation = secrets.token_hex(16)
        candidates = []
        anchors: dict = {}
        try:
            tree = await self._payment_tree(sid, child_sessions)
            merchant = https_origin(tree[0][3])
            if (origin and merchant != origin) or (bound and merchant not in bound):
                raise PaymentPolicyError("Merchant origin mismatch")
            on_provider = provider_for_origin(merchant)
            if on_provider is not None and not on_provider.admits(tree[0][2]):
                # The tab itself is a provider's non-card page (PayPal wallet or login), not a checkout.
                raise PaymentPolicyError("Merchant origin mismatch")
            by_id = {f[0]: f for f in tree}
            for fid, _loader, _url, frame_origin, parent in tree:
                if not _frame_admitted(by_id[fid], by_id, merchant, allowed_frames):
                    continue
                # A PSP field frame the page keeps hidden (Shopify's Maestro-only issue date and
                # number) looks visible from inside its own document; judge its iframe element.
                if parent and not await self._frame_rendered(fid, parent, child_sessions.get(parent, sid)):
                    continue
                fsid = child_sessions.get(fid, sid)
                context = (
                    await self._cdp(
                        "Page.createIsolatedWorld",
                        {"frameId": fid, "worldName": "hermes-payment-" + operation},
                        session_id=fsid,
                    )
                )["result"]["executionContextId"]
                reply = await self._cdp(
                    "Runtime.evaluate",
                    {
                        "expression": _INSPECT,
                        "contextId": context,
                        "returnByValue": False,
                    },
                    session_id=fsid,
                )
                anchor = time.monotonic()  # after the closure read its clock: see _renderer_deadline_ms
                obj = reply.get("result", {}).get("result", {}).get("objectId")
                if not obj:
                    raise PaymentPolicyError("Payment document unavailable")
                anchors[obj] = anchor
                raw = _value(
                    await self._cdp(
                        "Runtime.callFunctionOn",
                        {
                            "objectId": obj,
                            "functionDeclaration": "function(){return this.describe()}",
                            "returnByValue": True,
                        },
                        session_id=fsid,
                    )
                )
                controls = tuple(
                    c
                    for r in raw
                    if (c := classify_checkout_control(LoginControl.from_dict(r)))
                    and c.token.startswith("cc-")
                )
                if controls:
                    candidates.append((fid, fsid, obj, frame_origin, controls))
                else:
                    await self._cdp(
                        "Runtime.releaseObject", {"objectId": obj}, session_id=fsid
                    )
            documents = _explained_card_documents(candidates, merchant, tree, allowed_frames)
            for d in documents:
                d.clock_anchor = anchors.get(d.object_id)
            if await self._payment_tree(sid, child_sessions) != tree:
                raise PaymentPolicyError("Payment page changed during inspection")
            owners = await self._pin_frame_owners(documents, tree, sid, child_sessions)
            return PaymentInspection(
                operation,
                sid,
                page["targetId"],
                merchant,
                documents,
                tree,
                time.monotonic() + 120,
                owner=self,
                child_sessions=child_sessions,
                frame_owners=owners,
            )
        except Exception:
            for _, fsid, obj, _, _ in candidates:
                await self._release_payment_object(fsid, obj)
            await self._release_payment_sessions(child_sessions)
            raise

    async def _pin_frame_owners(self, documents: tuple, tree: tuple, page_sid: str, child_sessions: dict) -> dict:
        """dlz: pin the owner <iframe> of every frame between a card document and the page, grouped by parent
        document (one isolated world each). Unresolvable: the inspection is refused."""
        by_id = {f[0]: f for f in tree}
        frames: list = []
        for d in documents:
            fid = d.frame_id
            while fid in by_id and by_id[fid][4] and fid not in frames:
                frames.append(fid)
                fid = by_id[fid][4]
        groups: dict = {}
        contexts: dict = {}
        try:
            for fid in frames:
                parent = by_id[fid][4]
                psid = child_sessions.get(parent, page_sid)
                if parent not in contexts:
                    contexts[parent] = (await self._cdp("Page.createIsolatedWorld", {
                        "frameId": parent, "worldName": "hermes-payment-owners"}, session_id=psid))["result"]["executionContextId"]
                owner = (await self._cdp("DOM.getFrameOwner", {"frameId": fid}, session_id=psid))["result"]
                obj = (await self._cdp("DOM.resolveNode", {"backendNodeId": owner["backendNodeId"],
                                                           "executionContextId": contexts[parent]},
                                       session_id=psid))["result"]["object"]["objectId"]
                groups.setdefault(parent, (psid, []))[1].append((fid, obj))
        except Exception:  # health: allow BLE001 -- no pinned owner, no verifiable card frame; fail closed
            await self._release_frame_owners(groups)
            raise PaymentPolicyError("Payment document unavailable") from None
        return groups

    async def _release_frame_owners(self, groups: dict) -> None:
        for psid, items in groups.values():
            for _fid, obj in items:
                await self._release_payment_object(psid, obj)

    async def _shown_documents(self, i: PaymentInspection) -> set:
        """dlz: frame ids of the card documents the user still sees: every pinned owner <iframe> from the
        document up to the page is connected, laid out, visible and not transparent (ancestors included).
        One call per parent document; any failure counts as not shown."""
        shown: dict = {}
        for psid, items in i.frame_owners.values():
            try:
                answer = _value(await self._cdp("Runtime.callFunctionOn", {
                    "objectId": items[0][1], "functionDeclaration": _OWNERS_SHOWN,
                    "arguments": [{"objectId": obj} for _fid, obj in items], "returnByValue": True,
                }, session_id=psid))
            except Exception:  # health: allow BLE001 -- an unreadable owner is not a frame the user sees
                answer = None
            ok = isinstance(answer, list) and len(answer) == len(items)
            for k, (fid, _obj) in enumerate(items):
                shown[fid] = ok and answer[k] is True
        by_id = {f[0]: f for f in i.tree}
        out = set()
        for d in i.documents:
            fid, chain_ok = d.frame_id, d.frame_id in by_id
            while chain_ok and by_id[fid][4]:
                chain_ok = shown.get(fid) is True and by_id[fid][4] in by_id
                fid = by_id[fid][4]
            if chain_ok:
                out.add(d.frame_id)
        return out

    async def _frame_rendered(self, fid: str, parent: str, parent_sid: str) -> bool:
        """Whether the iframe element hosting ``fid`` is laid out and visible in its parent document."""
        obj = None
        try:
            owner = (await self._cdp("DOM.getFrameOwner", {"frameId": fid}, session_id=parent_sid))["result"]
            context = (await self._cdp(
                "Page.createIsolatedWorld", {"frameId": parent, "worldName": "hermes-payment-ancestry"},
                session_id=parent_sid))["result"]["executionContextId"]
            obj = (await self._cdp(
                "DOM.resolveNode", {"backendNodeId": owner["backendNodeId"], "executionContextId": context},
                session_id=parent_sid))["result"]["object"]["objectId"]
            return _value(await self._cdp("Runtime.callFunctionOn", {
                "objectId": obj,
                "functionDeclaration": "function(){const r=this.getBoundingClientRect();return this.isConnected&&"
                                       "this.getClientRects().length>0&&r.width>2&&r.height>2&&"
                                       "getComputedStyle(this).visibility!=='hidden'}",
                "returnByValue": True,
            }, session_id=parent_sid)) is True
        except (KeyError, TypeError, PaymentPolicyError):
            # No resolvable owner element: not a frame the user can see or type into.
            return False
        finally:
            if obj:
                await self._release_payment_object(parent_sid, obj)

    async def _release_payment_sessions(self, sessions: dict) -> None:
        # In-process child frames share their OOPIF's session: detach each session once.
        for sid in dict.fromkeys(sessions.values()):
            try:
                await self._cdp("Target.detachFromTarget", {"sessionId": sid})
            except Exception:
                # A frame gone from the browser has no live session to detach.
                continue

    async def _release_payment_object(self, sid: str, obj: str) -> None:
        try:
            await self._cdp("Runtime.releaseObject", {"objectId": obj}, session_id=sid)
        except Exception:
            # Navigation/disconnect already destroys the isolated world's object.
            return

    def payment_is_current(self, inspected: PaymentInspection) -> bool:
        from tools.browser_supervisor import _schedule

        return _schedule(self._payment_is_current(inspected), self._loop, timeout=15)

    async def _payment_is_current(self, i: PaymentInspection) -> bool:
        with self._state_lock:
            active = self._active and self._page_session_id == i.page_session
        if (
            not active
            or i.owner is not self
            or i.consumed
            or time.monotonic() >= i.expires_at
        ):
            return False
        try:
            if await self._payment_tree(i.page_session, i.child_sessions) != i.tree:
                return False
            for d in i.documents:
                valid = _value(
                    await self._cdp(
                        "Runtime.callFunctionOn",
                        {
                            "objectId": d.object_id,
                            "functionDeclaration": "function(){return this.valid()}",
                            "returnByValue": True,
                        },
                        session_id=d.frame_session,
                    )
                )
                if valid is not True:
                    return False
            return True
        except Exception:  # health: allow BLE001 -- any CDP failure means the approved page is not current; fail closed
            return False

    def commit_payment(self, inspected: PaymentInspection, fills: list[dict], strict: bool = False) -> dict:
        """Write the fills. ``strict`` (unreviewed destinations) adds a hit test at each field's centre
        before its setter; reviewed provider shapes keep ``False`` so they cannot regress."""
        import concurrent.futures

        from agent.async_utils import safe_schedule_threadsafe
        from tools.browser_supervisor import _LoopUnavailable

        until = time.monotonic() + _COMMIT_BUDGET_SECONDS
        future = safe_schedule_threadsafe(self._commit_payment(inspected, fills, until, strict), self._loop)
        if future is None:
            raise _LoopUnavailable("Browser supervisor loop unavailable")
        try:
            return future.result(timeout=_COMMIT_BUDGET_SECONDS)
        except concurrent.futures.TimeoutError:
            # dlz: stop the commit where it is; a write already in flight is refused by the renderer past
            # `until`, and what landed is unknown. Consumed (never a retry), so the caller skips its discard:
            # the release pass is awaited here instead (bounded), also when the commit never started.
            future.cancel()
            inspected.consumed = True
            try:
                self.discard_payment(inspected)
            except Exception:  # health: allow BLE001 -- a stalled loop or gone browser; the release task keeps going
                pass
            return {"success": False, "error_type": "payment_transport_uncertain", "partially_filled": True}

    async def _commit_payment(self, i: PaymentInspection, fills: list[dict], until: float | None = None,
                              strict: bool = False) -> dict:
        if i.consumed:
            return {"success": False, "error_type": "payment_consent_consumed"}
        try:
            return await self._commit_payment_once(i, fills, until, strict)
        finally:
            # dlz: one cleanup for the whole lifecycle (freshness included) that a cancellation cannot cut short.
            await self._release_once(i)

    async def _commit_payment_once(self, i: PaymentInspection, fills: list[dict], until: float | None,
                                   strict: bool = False) -> dict:
        if time.monotonic() >= i.expires_at:
            # dlz: the inspection window closed (late approval, slow card lookup): nothing is written, and
            # the result says so instead of reading as a changed page.
            i.consumed = True
            return {"success": False, "error_type": "payment_consent_expired"}
        if not await self._payment_is_current(i):
            i.consumed = True
            return {"success": False, "error_type": "payment_page_changed"}
        if time.monotonic() >= i.expires_at:  # dlz: the freshness pass itself can outlast the window
            i.consumed = True
            return {"success": False, "error_type": "payment_consent_expired"}
        # Consume before ingress; transport uncertainty is never a retry authorization.
        i.consumed = True
        # dlz: the renderer's write deadline: the consent window or the commit budget, whichever ends first.
        stop = i.expires_at if until is None else min(i.expires_at, until)
        filled = 0
        try:
            # Every document was re-validated just above. A split checkout is written one
            # document at a time: no cross-document transaction exists, so a later failure
            # reports what an earlier document already received instead of retrying.
            written = []
            order = _commit_order(i, _fills_by_document(i, fills))
            for document, document_fills in order:
                if time.monotonic() >= i.expires_at:
                    # dlz: no write after the window closes; earlier documents' values are reported, not
                    # completed.
                    return _partial({"success": False, "error_type": "payment_consent_expired"}, filled)
                # dlz: an earlier document's page handlers ran after its writes: before the next document, every
                # approved chain is checked again against the pinned state (bounded: no OOPIF rediscovery), and
                # each document written so far must still be the one that received its fields.
                if written and not await self._payment_scope_current(i, [d for d, _f in written] + [document]):
                    return _partial({"success": False, "error_type": "payment_page_changed"}, filled)
                # dlz: strict fills in a frame pause after each focus for the owner-chain check from the parents
                # (a cross-origin parent's blur handler is out of the frame's reach).
                paced = (lambda point, d=document: self._owner_chain_current(i, d, point)) \
                    if strict and _has_parent(i, document) else None
                result = await self._commit_payment_document(
                    document, document_fills, deadline_ms=_renderer_deadline_ms(document, stop), strict=strict,
                    paced=paced)
                if not result.get("success"):
                    return _partial(result, filled)
                filled += result.get("filled_fields", 0)
                written.append((document, document_fills))
            # A document's own check runs right after its writes. Providers reset controlled inputs
            # later, and a later document (brand detection on the number, a blur) can clear an
            # earlier one, so success is only reported once EVERY document still holds its values.
            await asyncio.sleep(_SETTLE_SECONDS)
            # dlz: every frame up to the page must be shown before AND after the reads: a frame hidden while
            # the answers were collected is not the one the user sees, so its fields count as replaced.
            before = await self._shown_documents(i)
            answers = [(d, f, await self._payment_retention(i, d)) for d, f in written]
            after = await self._shown_documents(i)
            result = _retention_result([(f, a if d.frame_id in before and d.frame_id in after else None)
                                        for d, f, a in answers])
            if await self._top_navigated(i):
                result["navigated_after_fill"] = True
            return result
        except PaymentPolicyError:
            return _partial({"success": False, "error_type": "payment_fields_invalid"}, filled)
        except Exception:  # health: allow BLE001 -- CDP/transport text can carry values; reported as a fixed type, never retried
            return _partial({"success": False, "error_type": "payment_transport_uncertain"}, filled)

    async def _payment_scope_current(self, i: PaymentInspection, receiving: list) -> bool:
        """dlz: between the documents of a split fill, bounded and against the pinned state (no OOPIF
        rediscovery), over EVERY approved chain:
        - each ``receiving`` document (those written so far and the next) still has its inspected id, loader,
          URL and origin in its own session (one ``Page.getFrameTree`` per session);
        - every parent document on any card chain is still at its inspected URL and origin, and all its
          pinned owner <iframe>s are connected and shown (one call per parent, in the parent's pinned world:
          a new parent document destroys that world and the call fails).
        Frames off the card chains (ads) are ignored. Any failure to answer counts as changed."""
        by_id = {f[0]: f for f in i.tree}
        framed: set = set()
        for d in i.documents:
            fid, hops = d.frame_id, 0
            while fid in by_id and by_id[fid][4] and hops < 16:
                framed.add(fid)
                fid, hops = by_id[fid][4], hops + 1
            if fid not in by_id or by_id[fid][4]:
                return False
        pinned = {fid for _sid, items in i.frame_owners.values() for fid, _obj in items}
        if not framed <= pinned or any(p not in by_id for p in i.frame_owners):
            return False
        leaves = list(dict.fromkeys(d.frame_id for d in receiving))
        try:
            seen: dict = {}
            for sid in dict.fromkeys(i.child_sessions.get(fid, i.page_session) for fid in leaves):
                frames: dict = {}

                def walk(node):
                    frame = node["frame"]
                    frames[frame["id"]] = (frame.get("loaderId", ""), frame.get("url", ""),
                                           frame.get("securityOrigin", ""))
                    for child in node.get("childFrames", []):
                        walk(child)

                walk((await self._cdp("Page.getFrameTree", session_id=sid))["result"]["frameTree"])
                seen[sid] = frames
            for fid in leaves:
                sid = i.child_sessions.get(fid, i.page_session)
                if seen[sid].get(fid) != tuple(by_id[fid][1:4]):
                    return False
            for parent, (psid, items) in i.frame_owners.items():
                answer = _value(await self._cdp("Runtime.callFunctionOn", {
                    "objectId": items[0][1], "functionDeclaration": _OWNERS_SCOPE,
                    "arguments": [{"value": by_id[parent][2]}, {"value": by_id[parent][3]}]
                                 + [{"objectId": obj} for _fid, obj in items],
                    "returnByValue": True,
                }, session_id=psid))
                if not (isinstance(answer, list) and len(answer) == len(items) and all(a is True for a in answer)):
                    return False
            return True
        except Exception:  # health: allow BLE001 -- an unreadable page is not the approved page; fail closed
            return False

    async def _owner_chain_current(self, i: PaymentInspection, d: PaymentDocument, point) -> bool:
        """dlz: strict fills, after the focus and before each setter (Codex A2 r3 #1): from the card document up
        to the page, each parent document is still at its inspected URL and origin, the pinned owner <iframe>
        is connected, shown and not transparent, and the focused field's centre, mapped into the parent, hits
        that <iframe> (nothing of the merchant's covers it). One call per parent; unprovable = False."""
        by_id = {f[0]: f for f in i.tree}
        if not (isinstance(point, (list, tuple)) and len(point) == 2
                and all(type(v) in (int, float) and math.isfinite(v) for v in point)):
            return False
        x, y = point
        fid, hops = d.frame_id, 0
        try:
            while fid in by_id and by_id[fid][4]:
                parent = by_id[fid][4]
                psid, items = i.frame_owners.get(parent, (None, ()))
                owner = next((obj for f, obj in items if f == fid), None)
                hops += 1
                if parent not in by_id or owner is None or hops > 16:
                    return False
                answer = _value(await self._cdp("Runtime.callFunctionOn", {
                    "objectId": owner, "functionDeclaration": _OWNER_HIT,
                    "arguments": [{"value": by_id[parent][2]}, {"value": by_id[parent][3]},
                                  {"value": x}, {"value": y}],
                    "returnByValue": True,
                }, session_id=psid))
                if not (isinstance(answer, list) and len(answer) == 2
                        and all(type(v) in (int, float) and math.isfinite(v) for v in answer)):
                    return False
                x, y = answer
                fid = parent
        except Exception:  # health: allow BLE001 -- an owner chain that cannot answer is not provably the approved one
            return False
        return fid in by_id and not by_id[fid][4]

    async def _top_navigated(self, i: PaymentInspection) -> bool:
        """dlz: whether the top document's URL differs from the inspected one after the fill (True only when
        read and different; an unreadable page is reported through retention instead)."""
        try:
            tree = await self._cdp("Page.getFrameTree", session_id=i.page_session)
            url = tree["result"]["frameTree"]["frame"]["url"]
        except Exception:  # health: allow BLE001 -- retention already reports a page that cannot answer
            return False
        return bool(i.tree) and isinstance(url, str) and url != i.tree[0][2]

    async def _commit_payment_document(self, d: PaymentDocument, fills: list[dict], deadline_ms: float | None = None,
                                       strict: bool = False, paced=None) -> dict:
        """One document's commit. ``paced`` (strict fills in a frame): an async ``check(point)`` run after each
        focus, before that field's setter; False stops the document with nothing more written."""
        result = _value(
            await self._cdp(
                "Runtime.callFunctionOn",
                {
                    "objectId": d.object_id,
                    "functionDeclaration": "function(fills,deadline,strict,paced){"
                                           "return this.commit(fills,deadline,strict,paced)}",
                    "arguments": [{"value": fills}, {"value": deadline_ms}, {"value": strict is True},
                                  {"value": paced is not None}],
                    "returnByValue": True,
                },
                session_id=d.frame_session,
            )
        )
        for _ in range(len(fills) + 1):
            if paced is None or not isinstance(result, dict) or result.get("pending") is not True:
                break
            attempted = result.get("filled_fields")
            if set(result) != {"pending", "filled_fields", "x", "y"} or type(attempted) is not int \
                    or not 0 <= attempted < len(fills):
                return {"success": False, "error_type": "payment_result_invalid"}
            if not await paced((result["x"], result["y"])):
                # dlz: the focused field is never written; earlier fields of this document are reported.
                return {"success": False, "error_type": "payment_page_changed", "filled_fields": attempted,
                        "partially_filled": attempted > 0}
            result = _value(await self._cdp("Runtime.callFunctionOn", {
                "objectId": d.object_id, "functionDeclaration": "function(){return this.resume()}",
                "returnByValue": True,
            }, session_id=d.frame_session))
        if not isinstance(result, dict) or set(result) - {
            "success",
            "filled_fields",
            "error_type",
            "partially_filled",
        }:
            return {"success": False, "error_type": "payment_result_invalid"}
        return result

    async def _payment_retention(self, i: PaymentInspection, d: PaymentDocument) -> list | None:
        """Per written field ``{token, state, length}`` from the document's commit closure, or None when
        the document cannot answer (navigated, replaced, transport failure): its fields are then unknown
        and reported as replaced. Never retried, never a value. Frame visibility: ``_shown_documents``."""
        try:
            states = _value(await self._cdp("Runtime.callFunctionOn", {
                "objectId": d.object_id,
                "functionDeclaration": "function(){return this.retained()}",
                "returnByValue": True,
            }, session_id=d.frame_session))
        except Exception:  # health: allow BLE001 -- a gone document is a failed retention check, never a value
            return None
        if not isinstance(states, list):
            return None
        return states

    async def _release_payment_documents(self, i: PaymentInspection) -> None:
        await self._release_frame_owners(i.frame_owners)
        i.frame_owners = {}
        for d in i.documents:
            await self._release_payment_object(d.frame_session, d.object_id)
        await self._release_payment_sessions(i.child_sessions)

    async def _release_once(self, i: PaymentInspection) -> None:
        """dlz: release every pinned object and child session once per inspection. The pass runs as its own
        task (each CDP call bounded by ``_cdp``'s timeout), so cancelling a commit or a discard that waits on it
        never cuts it short; a later discard awaits the same pass instead of starting another."""
        if i.release_task is None:
            i.release_task = asyncio.ensure_future(self._release_payment_documents(i))
        await asyncio.shield(i.release_task)

    def discard_payment(self, i: PaymentInspection) -> None:
        from tools.browser_supervisor import _schedule

        i.consumed = True
        _schedule(self._release_once(i), self._loop, timeout=12)


def _renderer_deadline_ms(d: PaymentDocument, stop: float) -> float:
    """dlz: the write deadline in the document's own monotonic clock: ms after its closure was created
    (``born = performance.now()``). The anchor is taken after that creation returned, so the deadline lands
    at or before ``stop`` on the host however late the commit call runs, and a wall-clock step cannot move
    it (no margin needed). No anchor: 0, nothing is written."""
    if d.clock_anchor is None:
        return 0.0
    return max(0.0, (stop - d.clock_anchor) * 1000)


# dlz: each pinned owner <iframe> is still shown to the user (strict: transparent or hidden ancestors too).
_OWNER_VISIBLE = ("e=>{if(!e||!e.isConnected)return false;"
                  "const r=e.getBoundingClientRect(),s=getComputedStyle(e);return e.getClientRects().length>0&&"
                  "r.width>2&&r.height>2&&s.visibility!=='hidden'&&Number(s.opacity)>0&&"
                  "(typeof e.checkVisibility!=='function'||e.checkVisibility({opacityProperty:true,"
                  "visibilityProperty:true}))}")
_OWNERS_SHOWN = "function(...els){return els.map(" + _OWNER_VISIBLE + ")}"
# dlz: in the parent's pinned world: the parent document is still at its inspected URL (CDP frame URLs carry
# no fragment) and origin, and each owner <iframe> belongs to it and is shown. null = the parent changed.
_PARENT_AT = ("if(String(location.href).split('#')[0]!==href||location.origin!==origin)return null;")
_OWNERS_SCOPE = ("function(href,origin,...els){" + _PARENT_AT + "return els.map(e=>!!e&&e.ownerDocument===document&&("
                 + _OWNER_VISIBLE + ")(e))}")
# dlz: strict, per setter: as _OWNERS_SCOPE for this one owner, plus a hit test of the focused field's centre
# (x, y in the child's viewport) mapped through the <iframe>'s border and padding; the mapped point, or null.
_OWNER_HIT = ("function(href,origin,x,y){" + _PARENT_AT + "const e=this;"
              "if(e.ownerDocument!==document||!(" + _OWNER_VISIBLE + ")(e))return null;"
              "const r=e.getBoundingClientRect(),s=getComputedStyle(e);if(s.pointerEvents==='none')return null;"
              "const px=r.left+e.clientLeft+(parseFloat(s.paddingLeft)||0)+x,"
              "py=r.top+e.clientTop+(parseFloat(s.paddingTop)||0)+y;"
              "const n=e.getRootNode&&e.getRootNode(),root=n&&typeof n.elementFromPoint==='function'?n:document;"
              "return root.elementFromPoint(px,py)===e?[px,py]:null}")


def _has_parent(i: PaymentInspection, d: PaymentDocument) -> bool:
    record = next((f for f in i.tree if f[0] == d.frame_id), None)
    return record is None or bool(record[4])  # an unknown frame is paced too (and then refused)


def _commit_order(i: PaymentInspection, groups: list) -> list:
    """Documents with something to write, the one holding the card number LAST: providers re-shape
    the other fields once they detect the brand (Mollie drops the CVC maxLength from 4 to 3), which a
    later write would correctly see as a changed document."""
    pairs = [(d, g) for d, g in zip(i.documents, groups) if g]
    return sorted(pairs, key=lambda p: any(c.token == "cc-number" for c in p[0].controls))


def _retention_result(documents: list) -> dict:
    """Success only when every written field of every document still holds its value after settling.
    ``documents`` pairs each document's fills with its ``retained()`` answer (None: unreadable). The
    result names tokens, states and lengths only."""
    states: dict = {}
    lengths: dict = {}
    held = total = 0
    for fills, answer in documents:
        answer = answer if isinstance(answer, list) and len(answer) == len(fills) else [None] * len(fills)
        for f, entry in zip(fills, answer):  # the closure answers in fill order
            token = str(f.get("token") or "")
            entry = entry if isinstance(entry, dict) and entry.get("token") == token else {}
            state = entry.get("state") if entry.get("state") in _RETENTION_STATES else "replaced"
            total += 1
            held += state == "held"
            states[token] = state
            if state != "held":
                length = entry.get("length")
                lengths[token] = length if type(length) is int and 0 <= length <= 256 else 0
    if total and held == total:
        return {"success": True, "filled_fields": held, "retention_verified": True}
    return {
        "success": False,
        "error_type": "payment_fields_not_retained",
        "filled_fields": held,
        "partially_filled": True,
        "field_retention": dict(sorted(states.items())),
        "field_lengths": dict(sorted(lengths.items())),
    }


def _partial(result: dict, filled_before: int) -> dict:
    """A failed document after earlier documents of a split checkout already received values."""
    if not filled_before:
        return result
    return {**result, "filled_fields": filled_before + result.get("filled_fields", 0), "partially_filled": True}

_INSPECT = r"""(() => {
  // dlz: the document's monotonic clock at creation; commit deadlines count from here (_renderer_deadline_ms).
  const born = performance.now();
  // dlz: and its wall clock. Commit requires BOTH elapsed times inside the deadline: the monotonic one
  // pauses while a remote browser's machine sleeps (macOS ticks exclude sleep), the wall one does not;
  // a wall-clock step alone cannot reopen the window; a constant browser/host offset cancels out.
  const bornWall = Date.now();
  const doc = document, href = location.href;
  // Inputs and selects in document order, including those inside open shadow roots (Unzer UI v2
  // renders every card input in one); closed roots stay out of reach and therefore out of scope. A
  // select is only ever an expiry month or year (PAYONE, Worldline HTP, Computop).
  const allInputs = () => {
    const out = [];
    const walk = root => {
      for (const n of root.querySelectorAll('*')) {
        if (n.tagName === 'INPUT' || n.tagName === 'SELECT') out.push(n);
        if (n.shadowRoot) walk(n.shadowRoot);
      }
    };
    walk(doc);
    return out;
  };
  const elements = allInputs();
  const isSelect = e => e.tagName === 'SELECT';
  // A select's options (months, years: never a secret) are part of the description, so a list that
  // is re-populated after consent fails valid() like any other changed field.
  const describe = () => elements.map((e,index) => ({index, autocomplete:e.autocomplete,
    name:e.name+' '+e.id, label:(e.labels?Array.from(e.labels,l=>l.textContent).join(' '):'')+' '+
      (e.getAttribute('aria-label')||'')+' '+(e.placeholder||''),
    type:isSelect(e)?(e.multiple?'select-multiple':'select'):e.type,
    formIndex:e.form?Array.from(doc.forms).indexOf(e.form):null,
    maxLength:e.maxLength>0?e.maxLength:null,
    ...(isSelect(e)?{options:Array.from(e.options,o=>[o.value,o.text.slice(0,64),o.disabled]).slice(0,400)}:{})}));
  // Honeypots and visually-hidden inputs are never fill targets: Shopify's PCI frames carry one
  // aria-hidden, tabindex=-1, 1px input per card token around the real field, and filling one
  // is a fraud signal.
  const visible = e => !(e.disabled || e.readOnly || !e.isConnected || e.ownerDocument !== doc ||
      e.getAttribute('aria-hidden') === 'true' || e.tabIndex < 0 || e.hasAttribute('data-honeypot-field')) &&
    shown(e);
  // Laid out and visible to the user; also what the settled retention check requires of a written field.
  const shown = e => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    // Mollie keeps a transparent, click-through focus-trap input beside each real field.
    return e.getClientRects().length>0 && r.width > 2 && r.height > 2 && s.visibility !== 'hidden' &&
      Number(s.opacity) > 0 && s.pointerEvents !== 'none' &&
      (typeof e.checkVisibility !== 'function' ||
       e.checkVisibility({opacityProperty: true, visibilityProperty: true}));  // a transparent ancestor too
  };
  const entries = elements.map((e,index)=>({e,index})).filter(x=>visible(x.e) &&
    !['hidden','submit','button','reset','file','image','checkbox','radio','select-multiple'].includes(x.e.type));
  const selected = new Set(entries.map(x=>x.index));
  const signature = JSON.stringify(describe());
  // dlz: the owner chain this document can reach synchronously (same-origin ancestors, pinned now) and the
  // top URL when readable: page code run by focus can hide a parent iframe or rewrite the top URL
  // (history.replaceState) without touching this document. Cross-origin ancestors are out of reach of
  // this document's scripts, and out of this check (between documents, control re-checks the owners).
  const scopeChain = [];
  for (let w = globalThis, k = 0; k < 8; k++) {
    let fe = null, pw = null, ph = null;
    try { fe = w.frameElement; pw = w.parent; ph = fe && pw ? pw.location.href : null; } catch (_) { fe = null; }
    if (!fe || !pw || pw === w || typeof ph !== 'string') break;
    scopeChain.push({fe, pw, ph});
    w = pw;
  }
  const readTop = () => { try { const t = globalThis.top; return t && t !== globalThis ? String(t.location.href) : null; }
                          catch (_) { return null; } };
  const topHref = readTop();
  const ownerShown = e => {
    const v = e.ownerDocument && e.ownerDocument.defaultView;
    if (!e.isConnected || !v) return false;
    const r = e.getBoundingClientRect(), s = v.getComputedStyle(e);
    return e.getClientRects().length > 0 && r.width > 2 && r.height > 2 && s.visibility !== 'hidden' &&
      Number(s.opacity) > 0 && (typeof e.checkVisibility !== 'function' ||
        e.checkVisibility({opacityProperty: true, visibilityProperty: true}));
  };
  const scope = () => scopeChain.every(c => { try { return c.pw.location.href === c.ph && ownerShown(c.fe); }
                                             catch (_) { return false; } }) &&
    (topHref === null || readTop() === topHref);
  const valid = () => document === doc && location.href === href && scope() &&
    JSON.stringify(describe()) === signature &&
    (now => now.length === elements.length && now.every((e,n)=>e === elements[n]))(allInputs()) &&
    entries.every(x=>visible(x.e));
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
  const selectSetter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set;
  // A select fill names the value of exactly one enabled option, chosen on the inspected options.
  const oneOption = (el,v) => Array.from(el.options).filter(o=>o.value === v && !o.disabled).length === 1 &&
    Array.from(el.options).filter(o=>o.value === v).length === 1;
  // One value in two spellings: providers re-format as they mask ("07/35" -> "07 / 35" or "0735",
  // grouped card numbers, upper-cased names). Anything else is a different value.
  const same = (a,b) => a.replace(/[\s\/.-]/g,'').toUpperCase() === b.replace(/[\s\/.-]/g,'').toUpperCase();
  const live = el => el.isConnected && el.ownerDocument === doc && allInputs().includes(el);
  // dlz: strict mode (unreviewed destinations): the focused field is what the user would hit at its centre.
  const hits = el => {
    const r = el.getBoundingClientRect(), root = el.getRootNode ? el.getRootNode() : doc;
    const at = (root && typeof root.elementFromPoint === 'function' ? root : doc)
      .elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return at === el;
  };
  let used = false, written = null, paused = null;
  // One step of the commit; a finished (or never started) commit answers changed, never a second run.
  const advance = () => {
    const step = paused ? paused.next() : {done: true, value: {success:false,error_type:'payment_fields_changed'}};
    if (step.done) paused = null;
    return step.value;
  };
  return {
    describe:()=>describe().filter(x=>selected.has(x.index)), valid,
    // After every document was written and the page had time to settle: does each written field still
    // hold its value? States and lengths only; a value never leaves this world. Answers once.
    retained: () => {
      const w = written;
      written = null;
      if (!w) return null;
      const page = document === doc && location.href === href;
      return w.map(f => {
        const el = elements[f.index];
        // dlz: a field the provider hid in favour of a fresh input is not the one the user sees.
        if (!page || !live(el) || !shown(el)) return {token:f.token,state:'replaced',length:0};
        return {token:f.token,state:same(el.value,f.value)?'held':el.value?'changed':'empty',length:el.value.length};
      });
    },
    commit: (fills, deadline, strict, paced) => {
      if (used || !valid()) return {success:false,error_type:'payment_fields_changed'};
      used = true;
      // dlz: `deadline` is ms after `born` on this document's monotonic clock (never Date.now(): a wall-clock
      // step must not reopen the window); checked here and again before every setter. Not a number: late.
      const late = () => !(typeof deadline === 'number' && performance.now() - born < deadline &&
                           Date.now() - bornWall < deadline);
      if (late()) return {success:false,error_type:'payment_consent_expired'};
      if (!Array.isArray(fills) || !fills.length ||
          fills.some(f=>!selected.has(f.index) || typeof f.value !== 'string' || !f.value) ||
          new Set(fills.map(f=>f.index)).size !== fills.length)
        return {success:false,error_type:'payment_fields_invalid'};
      // Every select target is checked before ANY write in this document: zero or several matching
      // options leave the whole document untouched.
      const selects = fills.filter(f=>isSelect(elements[f.index]));
      if (selects.some(f=>!['cc-exp-month','cc-exp-year'].includes(f.token)))
        return {success:false,error_type:'payment_fields_invalid'};
      if (selects.some(f=>!oneOption(elements[f.index],f.value)))
        return {success:false,error_type:'payment_expiry_option_unmatched'};
      // Native setters in an isolated world, one field at a time with its own events, the card number
      // last: a controlled form (Stripe Card/Payment Element) re-renders on each input event and would
      // reset sibling fields written before the event they never saw; brand detection on the number
      // re-shapes the CVC (Braintree, Mollie). Writes only reach the pinned element references.
      for (const f of fills) elements[f.index].style.setProperty('-webkit-text-security','disc','important');
      const ordered = fills.slice().sort((a,b)=>(a.token==='cc-number')-(b.token==='cc-number'));
      // Page handlers run between these writes, so each target is re-checked first, and success is
      // judged on the inputs still in the document: a re-render that replaced a node must not let
      // its detached copy report a value the live form does not hold.
      written = fills.map(f=>({index:f.index,token:String(f.token||''),value:f.value}));
      let attempted = 0;
      const changed = () => ({success:false,error_type:'payment_fields_changed',filled_fields:attempted,
                              partially_filled:attempted>0});
      const expired = () => ({success:false,error_type:'payment_consent_expired',filled_fields:attempted,
                              partially_filled:attempted>0});
      // dlz: `paced` (strict fills in a frame): after each focus the closure pauses and hands back the field's
      // centre; the supervisor checks the cross-origin owner chain from the parents (unreachable from here)
      // and only then calls resume(), which re-runs every local check before the setter. Unpaced, the same
      // steps run in one go.
      const steps = function* () {
        for (const f of ordered) {
          const el = elements[f.index];
          if (!valid() || !live(el))
            return changed();
          el.focus();
          if (late())  // focus handlers run page code; never write past the window
            return expired();
          if (paced === true) {
            const r = el.getBoundingClientRect();
            yield {pending:true,filled_fields:attempted,x:r.left + r.width / 2,y:r.top + r.height / 2};
          }
          // dlz: focus handlers can hide, swap, relabel or navigate: the whole pinned set is re-checked
          // right before this setter, not only before the loop.
          if (!valid() || !live(el) || !shown(el) || (strict === true && !hits(el)))
            return changed();
          if (late())  // the checks above run page-visible code too (layout, style): the window again, last
            return expired();
          attempted += 1;
          if (isSelect(el)) {
            selectSetter.call(el,f.value);
            el.dispatchEvent(new Event('input',{bubbles:true}));
          } else {
            setter.call(el,f.value);
            el.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText'}));
          }
          el.dispatchEvent(new Event('change',{bubbles:true}));
          el.dispatchEvent(new FocusEvent('blur'));
          el.blur();
        }
        const held = fills.filter(f=>live(elements[f.index]) && same(elements[f.index].value,f.value)).length;
        return held === fills.length && location.href === href ? {success:true,filled_fields:held} :
          {success:false,error_type:'payment_validation_failed',filled_fields:held,partially_filled:true};
      };
      paused = steps();
      return advance();
    },
    resume: () => advance()
  };
})()"""
