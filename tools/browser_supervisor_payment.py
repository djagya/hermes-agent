"""Document-pinned payment operations on the existing supervisor socket.

No whole-checkout transaction is claimed. Exactly one document may contain
card controls; secrets never go through CLI eval or a model tool argument.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from agent.vault_login_classifier import LoginControl, classify_checkout_control
from agent.vault_payment_policy import PaymentPolicyError, https_origin

PSP_ORIGINS = ("https://js.stripe.com",)


@dataclass
class PaymentInspection:
    operation_id: str
    page_session: str
    page_id: str
    merchant_origin: str
    frame_origin: str
    frame_id: str
    frame_session: str
    object_id: str
    tree: tuple
    controls: tuple
    expires_at: float
    consumed: bool = False
    owner: Any = field(default=None, repr=False)
    child_sessions: dict = field(default_factory=dict, repr=False)

    def prompt(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "merchant_origin": self.merchant_origin,
            "target_origin": self.frame_origin,
            "fields": sorted(c.token for c in self.controls),
            "checks": [
                "HTTPS origins",
                "exact document and field references",
                "single card document",
            ],
            "merchant_reputation": "not established; verify the seller independently",
        }


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
        frame_origins: tuple = PSP_ORIGINS,
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
        with self._state_lock:
            tracked = dict(self._frames)
        pending = [t for t in targets if t.get("type") == "iframe"]
        while pending:
            progress = False
            for target in list(pending):
                fid = target["targetId"]
                old = tracked.get(fid)
                parent = old.parent_frame_id if old else None
                if not parent or parent not in {f[0] for f in frames}:
                    continue
                if len(frames) > 64:
                    raise PaymentPolicyError("Payment frame tree too large")
                parent_sid = child_sessions.get(parent, sid)
                # A stale bookkeeping entry cannot admit an unrelated iframe.
                await self._cdp(
                    "DOM.getFrameOwner", {"frameId": fid}, session_id=parent_sid
                )
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
            sid = (
                await self._cdp(
                    "Target.attachToTarget",
                    {"targetId": pages[0]["targetId"], "flatten": True},
                )
            )["result"]["sessionId"]
            await self._enable_page_domains(sid, timeout=5)
            with self._state_lock:
                self._page_session_id = sid
        page = (await self._cdp("Target.getTargetInfo", session_id=sid))["result"][
            "targetInfo"
        ]
        child_sessions = {}
        tree = await self._payment_tree(sid, child_sessions)
        merchant = https_origin(tree[0][3])
        if (origin and merchant != origin) or (bound and merchant not in bound):
            raise PaymentPolicyError("Merchant origin mismatch")
        operation = secrets.token_hex(16)
        candidates = []
        by_id = {f[0]: f for f in tree}
        try:
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
            if len(candidates) != 1:
                raise PaymentPolicyError(
                    "Missing, ambiguous or split payment documents"
                )
            fid, fsid, obj, frame_origin, controls = candidates[0]
            tokens = [c.token for c in controls]
            if len(tokens) != len(set(tokens)) or not {"cc-number", "cc-csc"} <= set(
                tokens
            ):
                raise PaymentPolicyError("Incomplete or ambiguous card fields")
            if not (
                "cc-exp" in tokens or {"cc-exp-month", "cc-exp-year"} <= set(tokens)
            ):
                raise PaymentPolicyError("Missing card expiry fields")
            if "cc-exp" in tokens and (
                "cc-exp-month" in tokens or "cc-exp-year" in tokens
            ):
                raise PaymentPolicyError("Ambiguous card expiry fields")
            if await self._payment_tree(sid, child_sessions) != tree:
                raise PaymentPolicyError("Payment page changed during inspection")
            return PaymentInspection(
                operation,
                sid,
                page["targetId"],
                merchant,
                frame_origin,
                fid,
                fsid,
                obj,
                tree,
                controls,
                time.monotonic() + 120,
                owner=self,
                child_sessions=child_sessions,
            )
        except Exception:
            for _, fsid, obj, _, _ in candidates:
                await self._release_payment_object(fsid, obj)
            await self._release_payment_sessions(child_sessions)
            raise

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
            return (
                _value(
                    await self._cdp(
                        "Runtime.callFunctionOn",
                        {
                            "objectId": i.object_id,
                            "functionDeclaration": "function(){return this.valid()}",
                            "returnByValue": True,
                        },
                        session_id=i.frame_session,
                    )
                )
                is True
            )
        except Exception:
            return False

    def commit_payment(self, inspected: PaymentInspection, fills: list[dict]) -> dict:
        from tools.browser_supervisor import _schedule

        return _schedule(self._commit_payment(inspected, fills), self._loop, timeout=20)

    async def _commit_payment(self, i: PaymentInspection, fills: list[dict]) -> dict:
        if i.consumed:
            return {"success": False, "error_type": "payment_consent_consumed"}
        if not await self._payment_is_current(i):
            i.consumed = True
            await self._release_payment_object(i.frame_session, i.object_id)
            await self._release_payment_sessions(i.child_sessions)
            return {"success": False, "error_type": "payment_page_changed"}
        # Consume before ingress; transport uncertainty is never a retry authorization.
        i.consumed = True
        try:
            result = _value(
                await self._cdp(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": i.object_id,
                        "functionDeclaration": "function(fills){return this.commit(fills)}",
                        "arguments": [{"value": fills}],
                        "returnByValue": True,
                    },
                    session_id=i.frame_session,
                )
            )
            if not isinstance(result, dict) or set(result) - {
                "success",
                "filled_fields",
                "error_type",
            }:
                return {"success": False, "error_type": "payment_result_invalid"}
            return result
        except Exception:
            return {"success": False, "error_type": "payment_transport_uncertain"}
        finally:
            await self._release_payment_object(i.frame_session, i.object_id)
            await self._release_payment_sessions(i.child_sessions)

    def discard_payment(self, i: PaymentInspection) -> None:
        from tools.browser_supervisor import _schedule

        i.consumed = True

        async def discard():
            await self._release_payment_object(i.frame_session, i.object_id)
            await self._release_payment_sessions(i.child_sessions)

        _schedule(discard(), self._loop, timeout=12)


_INSPECT = r"""(() => {
  const doc = document, href = location.href;
  const elements = Array.from(doc.querySelectorAll('input'));
  const describe = () => elements.map((e,index) => ({index, autocomplete:e.autocomplete,
    name:e.name+' '+e.id, label:(e.labels?Array.from(e.labels,l=>l.textContent).join(' '):'')+' '+
      (e.getAttribute('aria-label')||'')+' '+(e.placeholder||''),
    type:e.type, formIndex:e.form?Array.from(doc.forms).indexOf(e.form):null,
    maxLength:e.maxLength>0?e.maxLength:null}));
  const visible = e => !e.disabled && !e.readOnly && e.isConnected &&
    e.ownerDocument === doc && e.getClientRects().length>0 &&
    getComputedStyle(e).visibility !== 'hidden';
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
