"""Document-pinned payment operations on the existing supervisor socket.

No whole-checkout transaction is claimed. Card controls sit in one document, or are split
across documents on the merchant and at most ONE reviewed PSP origin with every card field
exactly once (Datatrans Secure Fields, Stripe split elements); secrets never go through CLI
eval or a model tool argument.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from agent.vault_login_classifier import LoginControl, classify_checkout_control
from agent.vault_payment_policy import (
    PAYMENT_PSP_ORIGINS,
    PaymentPolicyError,
    https_origin,
)


_MAX_CARD_DOCUMENTS = 4


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
    if not candidates or len(candidates) > _MAX_CARD_DOCUMENTS:
        raise PaymentPolicyError("Missing, ambiguous or split payment documents")
    if len({c[3] for c in candidates} - {merchant}) > 1:
        raise PaymentPolicyError("Card fields split across payment providers")
    tokens = [control.token for c in candidates for control in c[4]]
    if len(tokens) != len(set(tokens)) or not {"cc-number", "cc-csc"} <= set(tokens):
        raise PaymentPolicyError("Incomplete or ambiguous card fields")
    if not ("cc-exp" in tokens or {"cc-exp-month", "cc-exp-year"} <= set(tokens)):
        raise PaymentPolicyError("Missing card expiry fields")
    if "cc-exp" in tokens and ("cc-exp-month" in tokens or "cc-exp-year" in tokens):
        raise PaymentPolicyError("Ambiguous card expiry fields")
    return tuple(PaymentDocument(*c) for c in candidates)


def _fills_by_document(i: PaymentInspection, fills: list) -> list:
    """Group fills by their ``document`` index (default 0); every card document needs its fields."""
    groups: list = [[] for _ in i.documents]
    for f in fills:
        k = f.get("document", 0) if isinstance(f, dict) else None
        if type(k) is not int or not 0 <= k < len(groups):
            raise PaymentPolicyError("Fill targets an unknown document")
        groups[k].append({key: v for key, v in f.items() if key != "document"})
    if not all(groups):
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
            by_id = {f[0]: f for f in tree}
            for fid, _loader, _url, frame_origin, parent in tree:
                # Ignore unrelated ads, but never inspect/inject into an arbitrary PSP.
                if frame_origin not in (merchant, *allowed_frames):
                    continue
                https_origin(frame_origin)
                ancestor = parent
                while ancestor:
                    record = by_id.get(ancestor)
                    if record is None or record[3] not in (merchant, *allowed_frames):
                        raise PaymentPolicyError("Unapproved payment frame ancestry")
                    ancestor = record[4]
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
            for document, document_fills in zip(i.documents, _fills_by_document(i, fills)):
                result = await self._commit_payment_document(document, document_fills)
                if not result.get("success"):
                    return _partial(result, filled)
                filled += result.get("filled_fields", 0)
            return {"success": True, "filled_fields": filled}
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
        }:
            return {"success": False, "error_type": "payment_result_invalid"}
        return result

    async def _release_payment_documents(self, i: PaymentInspection) -> None:
        for d in i.documents:
            await self._release_payment_object(d.frame_session, d.object_id)
        await self._release_payment_sessions(i.child_sessions)

    def discard_payment(self, i: PaymentInspection) -> None:
        from tools.browser_supervisor import _schedule

        i.consumed = True
        _schedule(self._release_payment_documents(i), self._loop, timeout=12)


def _partial(result: dict, filled_before: int) -> dict:
    """A failed document after earlier documents of a split checkout already received values."""
    if not filled_before:
        return result
    return {**result, "filled_fields": filled_before + result.get("filled_fields", 0), "partially_filled": True}

_INSPECT = r"""(() => {
  const doc = document, href = location.href;
  const elements = Array.from(doc.querySelectorAll('input'));
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
    const r = e.getBoundingClientRect();
    return e.getClientRects().length>0 && r.width > 2 && r.height > 2 &&
      getComputedStyle(e).visibility !== 'hidden';
  };
  const entries = elements.map((e,index)=>({e,index})).filter(x=>visible(x.e) &&
    !['hidden','submit','button','reset','file','image','checkbox','radio'].includes(x.e.type));
  const selected = new Set(entries.map(x=>x.index));
  const signature = JSON.stringify(describe());
  const valid = () => document === doc && location.href === href &&
    JSON.stringify(describe()) === signature &&
    Array.from(doc.querySelectorAll('input')).every((e,n)=>e === elements[n]) &&
    doc.querySelectorAll('input').length === elements.length && entries.every(x=>visible(x.e));
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
  let used = false;
  return {
    describe:()=>describe().filter(x=>selected.has(x.index)), valid,
    commit: fills => {
      if (used || !valid()) return {success:false,error_type:'payment_fields_changed'};
      used = true;
      if (!Array.isArray(fills) || !fills.length ||
          fills.some(f=>!selected.has(f.index) || typeof f.value !== 'string' || !f.value) ||
          new Set(fills.map(f=>f.index)).size !== fills.length)
        return {success:false,error_type:'payment_fields_invalid'};
      // Native setters in an isolated world: no page-defined setters/event handlers
      // execute between the field validation and these writes. Events follow all writes.
      for (const f of fills) elements[f.index].style.setProperty('-webkit-text-security','disc','important');
      for (const f of fills) setter.call(elements[f.index],f.value);
      for (const f of fills) {
        elements[f.index].dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText'}));
        elements[f.index].dispatchEvent(new Event('change',{bubbles:true}));
      }
      const accepted = fills.every(f=>elements[f.index].value.replace(/[ -]/g,'') === f.value.replace(/[ -]/g,''));
      return accepted ? {success:true,filled_fields:fills.length} :
        {success:false,error_type:'payment_validation_failed',filled_fields:fills.length};
    }
  };
})()"""
