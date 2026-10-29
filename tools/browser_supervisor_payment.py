"""Document-pinned payment operations on the existing supervisor socket.

No whole-checkout transaction is claimed. Card controls sit in one document, or are split
across documents on the merchant and at most ONE reviewed PSP origin with every card field
exactly once (Datatrans Secure Fields, Stripe split elements); secrets never go through CLI
eval or a model tool argument.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from agent.vault_login_classifier import LoginControl, classify_checkout_control
from agent.vault_payment_policy import (
    PAYMENT_PSP_ORIGINS,
    PaymentPolicyError,
    https_origin,
    provider_for_origin,
)


_MAX_CARD_DOCUMENTS = 4
# Bounded wait between the last document's write and the all-documents retention check: long enough
# for a provider's deferred re-render, validation or brand detection to land, short enough to stay
# well inside commit_payment's 20 s budget. Never a retry window.
_SETTLE_SECONDS = 1.0
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


def _frame_admitted(record: tuple, by_id: dict, merchant: str, allowed: tuple) -> bool:
    """Whether a frame ``(id, loader, url, origin, parent)`` may hold card fields: the merchant or a
    reviewed provider origin, on that provider's card-field path, under an ancestry of the same. False
    = not a candidate (ads, a provider's wallet/login page); raises on a reviewed frame placed under an
    unapproved ancestor."""
    def on_card_path(r: tuple) -> bool:
        provider = provider_for_origin(r[3]) if r[3] != merchant else None
        return provider is None or provider.admits(r[2])

    # Ignore unrelated ads, but never inspect/inject into an arbitrary PSP.
    if record[3] not in (merchant, *allowed) or not on_card_path(record):
        return False
    https_origin(record[3])
    ancestor = record[4]
    while ancestor:
        above = by_id.get(ancestor)
        if above is None or above[3] not in (merchant, *allowed) or not on_card_path(above):
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
                walk(child, parent)
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
                obj = reply.get("result", {}).get("result", {}).get("objectId")
                if not obj:
                    raise PaymentPolicyError("Payment document unavailable")
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
            documents = _card_documents(candidates, merchant)
            if await self._payment_tree(sid, child_sessions) != tree:
                raise PaymentPolicyError("Payment page changed during inspection")
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
            )
        except Exception:
            for _, fsid, obj, _, _ in candidates:
                await self._release_payment_object(fsid, obj)
            await self._release_payment_sessions(child_sessions)
            raise

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
        for sid in sessions.values():
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

    def commit_payment(self, inspected: PaymentInspection, fills: list[dict]) -> dict:
        from tools.browser_supervisor import _schedule

        return _schedule(self._commit_payment(inspected, fills), self._loop, timeout=20)

    async def _commit_payment(self, i: PaymentInspection, fills: list[dict]) -> dict:
        if i.consumed:
            return {"success": False, "error_type": "payment_consent_consumed"}
        if not await self._payment_is_current(i):
            i.consumed = True
            await self._release_payment_documents(i)
            return {"success": False, "error_type": "payment_page_changed"}
        # Consume before ingress; transport uncertainty is never a retry authorization.
        i.consumed = True
        filled = 0
        try:
            # Every document was re-validated just above. A split checkout is written one
            # document at a time: no cross-document transaction exists, so a later failure
            # reports what an earlier document already received instead of retrying.
            written = []
            for document, document_fills in _commit_order(i, _fills_by_document(i, fills)):
                result = await self._commit_payment_document(document, document_fills)
                if not result.get("success"):
                    return _partial(result, filled)
                filled += result.get("filled_fields", 0)
                written.append((document, document_fills))
            # A document's own check runs right after its writes. Providers reset controlled inputs
            # later, and a later document (brand detection on the number, a blur) can clear an
            # earlier one, so success is only reported once EVERY document still holds its values.
            await asyncio.sleep(_SETTLE_SECONDS)
            return _retention_result([(f, await self._payment_retention(d)) for d, f in written])
        except PaymentPolicyError:
            return _partial({"success": False, "error_type": "payment_fields_invalid"}, filled)
        except Exception:  # health: allow BLE001 -- CDP/transport text can carry values; reported as a fixed type, never retried
            return _partial({"success": False, "error_type": "payment_transport_uncertain"}, filled)
        finally:
            await self._release_payment_documents(i)

    async def _commit_payment_document(self, d: PaymentDocument, fills: list[dict]) -> dict:
        result = _value(
            await self._cdp(
                "Runtime.callFunctionOn",
                {
                    "objectId": d.object_id,
                    "functionDeclaration": "function(fills){return this.commit(fills)}",
                    "arguments": [{"value": fills}],
                    "returnByValue": True,
                },
                session_id=d.frame_session,
            )
        )
        if not isinstance(result, dict) or set(result) - {
            "success",
            "filled_fields",
            "error_type",
            "partially_filled",
        }:
            return {"success": False, "error_type": "payment_result_invalid"}
        return result

    async def _payment_retention(self, d: PaymentDocument) -> list | None:
        """Per written field ``{token, state, length}`` from the document's commit closure, or None when
        the document cannot answer (navigated, replaced, transport failure): its fields are then
        unknown and reported as replaced. Never retried, never a value."""
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
        for d in i.documents:
            await self._release_payment_object(d.frame_session, d.object_id)
        await self._release_payment_sessions(i.child_sessions)

    def discard_payment(self, i: PaymentInspection) -> None:
        from tools.browser_supervisor import _schedule

        i.consumed = True
        _schedule(self._release_payment_documents(i), self._loop, timeout=12)


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
  const doc = document, href = location.href;
  // Inputs in document order, including those inside open shadow roots (Unzer UI v2 renders every
  // card input in one); closed roots stay out of reach and therefore out of scope.
  const allInputs = () => {
    const out = [];
    const walk = root => {
      for (const n of root.querySelectorAll('*')) {
        if (n.tagName === 'INPUT') out.push(n);
        if (n.shadowRoot) walk(n.shadowRoot);
      }
    };
    walk(doc);
    return out;
  };
  const elements = allInputs();
  const describe = () => elements.map((e,index) => ({index, autocomplete:e.autocomplete,
    name:e.name+' '+e.id, label:(e.labels?Array.from(e.labels,l=>l.textContent).join(' '):'')+' '+
      (e.getAttribute('aria-label')||'')+' '+(e.placeholder||''),
    type:e.type, formIndex:e.form?Array.from(doc.forms).indexOf(e.form):null,
    maxLength:e.maxLength>0?e.maxLength:null}));
  // Honeypots and visually-hidden inputs are never fill targets: Shopify's PCI frames carry one
  // aria-hidden, tabindex=-1, 1px input per card token around the real field, and filling one
  // is a fraud signal.
  const visible = e => {
    if (e.disabled || e.readOnly || !e.isConnected || e.ownerDocument !== doc ||
        e.getAttribute('aria-hidden') === 'true' || e.tabIndex < 0 || e.hasAttribute('data-honeypot-field'))
      return false;
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    // Mollie keeps a transparent, click-through focus-trap input beside each real field.
    return e.getClientRects().length>0 && r.width > 2 && r.height > 2 && s.visibility !== 'hidden' &&
      Number(s.opacity) > 0 && s.pointerEvents !== 'none' &&
      (typeof e.checkVisibility !== 'function' ||
       e.checkVisibility({opacityProperty: true, visibilityProperty: true}));  // a transparent ancestor too
  };
  const entries = elements.map((e,index)=>({e,index})).filter(x=>visible(x.e) &&
    !['hidden','submit','button','reset','file','image','checkbox','radio'].includes(x.e.type));
  const selected = new Set(entries.map(x=>x.index));
  const signature = JSON.stringify(describe());
  const valid = () => document === doc && location.href === href &&
    JSON.stringify(describe()) === signature &&
    (now => now.length === elements.length && now.every((e,n)=>e === elements[n]))(allInputs()) &&
    entries.every(x=>visible(x.e));
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
  // One value in two spellings: providers re-format as they mask ("07/35" -> "07 / 35" or "0735",
  // grouped card numbers, upper-cased names). Anything else is a different value.
  const same = (a,b) => a.replace(/[\s\/.-]/g,'').toUpperCase() === b.replace(/[\s\/.-]/g,'').toUpperCase();
  const live = el => el.isConnected && el.ownerDocument === doc && allInputs().includes(el);
  let used = false, written = null;
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
        if (!page || !live(el)) return {token:f.token,state:'replaced',length:0};
        return {token:f.token,state:same(el.value,f.value)?'held':el.value?'changed':'empty',length:el.value.length};
      });
    },
    commit: fills => {
      if (used || !valid()) return {success:false,error_type:'payment_fields_changed'};
      used = true;
      if (!Array.isArray(fills) || !fills.length ||
          fills.some(f=>!selected.has(f.index) || typeof f.value !== 'string' || !f.value) ||
          new Set(fills.map(f=>f.index)).size !== fills.length)
        return {success:false,error_type:'payment_fields_invalid'};
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
      for (const f of ordered) {
        const el = elements[f.index];
        if (!live(el) || location.href !== href)
          return {success:false,error_type:'payment_fields_changed',filled_fields:attempted,partially_filled:attempted>0};
        attempted += 1;
        el.focus();
        setter.call(el,f.value);
        el.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText'}));
        el.dispatchEvent(new Event('change',{bubbles:true}));
        el.dispatchEvent(new FocusEvent('blur'));
        el.blur();
      }
      const held = fills.filter(f=>live(elements[f.index]) && same(elements[f.index].value,f.value)).length;
      return held === fills.length && location.href === href ? {success:true,filled_fields:held} :
        {success:false,error_type:'payment_validation_failed',filled_fields:held,partially_filled:true};
    }
  };
})()"""
