"""Protected payment surface, runtime side (design v2 A9, Codex #5).

browser-control's CDP splice is the boundary (monolith ``payment_surface.py``,
contract ``cdp_methods`` in control-v1): model code in this container can read
every key, so only control can keep card values away from it. This module is
the runtime's half:

- :func:`claim_payment_surface` — the fill path's hook. Before any card value is
  resolved, the supervisor claims the card documents over its OWN socket with
  ``Hermes.claimPaymentSurface``. A refused or unconfirmed claim refuses the fill
  (``payment_surface_unavailable``); nothing is resolved or written.
- :func:`supervisor_route_refusal` — ``browser_cdp(frame_id=…)`` runs model commands
  over that same native socket, which control cannot tell apart from the runtime.
  This gate refuses there what control refuses on every other connection, and
  always refuses ``Hermes.*`` (model code must not claim as the runtime).
- :func:`payment_controls` / :func:`payment_click` — the two native actions that let a
  checkout continue inside a protected card document: list its buttons, links and
  checkboxes (never inputs or selects; digit runs masked), and make one trusted click
  on a listed control at its hit-tested centre.

The protected set is refreshed from control (``Hermes.paymentSurfaceStatus``), so it
lifts here when control's watcher sees the card frame navigate away. A failed refresh
keeps the last known set (fail closed).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

CLAIM_METHOD = "Hermes.claimPaymentSurface"
STATUS_METHOD = "Hermes.paymentSurfaceStatus"
UNAVAILABLE = "payment_surface_unavailable"
PROTECTED = "payment_surface_protected"
_CLAIM_TIMEOUT_S = 10.0
# payment_controls / payment_click: the caller waits _ACTION_TIMEOUT_S; the click's mousePressed is only
# dispatched before (start + _ACTION_TIMEOUT_S - _PRESS_MARGIN_S), so a press can never start after the
# caller gave up and reported that nothing was clicked.
_ACTION_TIMEOUT_S = 25.0
_PRESS_MARGIN_S = 5.0
_MAX_CONTROLS = 40
_MAX_TEXT = 60
_REF_RE = re.compile(r"c\d{1,3}-[0-9a-f]{10}")

# Mirrors control's guard tables (monolith payment_surface.py); keep in step.
_SESSION_DENY_DOMAINS = frozenset({"runtime", "dom", "domsnapshot", "accessibility", "css", "debugger",
                                   "indexeddb", "domstorage", "storage", "console"})
_SESSION_ALLOW = frozenset({"runtime.runifwaitingfordebugger"})
_SESSION_DENY_METHODS = frozenset({"input.inserttext", "input.imesetcomposition"})
_SLOT_DENY = frozenset({
    "network.getrequestpostdata", "network.getresponsebody", "network.getresponsebodyforinterception",
    "network.takeresponsebodyforinterceptionasstream", "network.setrequestinterception",
    "network.streamresourcecontent", "network.searchinresponsebody", "audits.getencodedresponse",
    "page.getresourcecontent", "page.searchinresource", "fetch.*",
    "target.sendmessagetotarget", "input.setinterceptdrags", "input.dispatchdragevent",
    "input.dispatchtouchevent", "input.emulatetouchfrommouseevent",
})
# The only Page.* allowed on a protected document's session (an allowlist, as control's
# PAGE_SAFE_ON_PROTECTED): drive the checkout, never inject a script (addScriptToEvaluateOnNewDocument
# with runImmediately), replace the document, create a world to evaluate in, or lift CSP.
_PAGE_SAFE_ON_PROTECTED = frozenset({
    "page.enable", "page.disable", "page.navigate", "page.reload", "page.stoploading", "page.getframetree",
    "page.getnavigationhistory", "page.bringtofront", "page.handlejavascriptdialog",
    "page.setlifecycleeventsenabled", "page.waitfordebugger",
})
_AUTO_ATTACH = frozenset({"target.setautoattach", "target.autoattachrelated"})
_PERMISSIONS = frozenset({"browser.grantpermissions", "browser.setpermission"})
_CLIP_KEYS = frozenset({"insert", "delete", "copy", "cut", "paste"})
_C0_SPACE = "".join(map(chr, range(0x21)))


class SurfaceRefused(RuntimeError):
    """The payment surface could not be claimed or used; carries a model-safe reason."""


@dataclass
class _Surface:
    frames: frozenset
    targets: frozenset
    oopif: bool
    ws: Any  # the supervisor socket the claim went over (native while it is the live one)


_LOCK = threading.Lock()
_SURFACES: weakref.WeakKeyDictionary[Any, _Surface] = weakref.WeakKeyDictionary()


def _refusal(error_type: str, message: str) -> dict:
    return {"success": False, "error_type": error_type, "error": message}


_UNAVAILABLE_TEXT = (
    "Browser control could not protect the card frames on this page, so no card was read or written. "
    "Nothing was submitted. Do not retry on another route; report it to the user."
)


def _uniq(items: list) -> list:
    return list(dict.fromkeys(items))


def _wait(coro, loop, *, timeout: float):
    """Run ``coro`` on the supervisor loop from a sync caller; on a timeout (or any failure to wait)
    the coroutine is cancelled, so a late CDP reply can never let it act after the caller reported."""
    from agent.async_utils import safe_schedule_threadsafe

    fut = safe_schedule_threadsafe(coro, loop)
    if fut is None:
        raise SurfaceRefused("supervisor loop unavailable")
    try:
        return fut.result(timeout=timeout)
    except BaseException:
        fut.cancel()
        raise


def _control_refused(exc: BaseException) -> bool:
    """Control answered the claim with its definitive refusal (the CDP error reply): it changed nothing.
    Anything else (timeout, cancel, transport, malformed reply) leaves control's state unknown."""
    return isinstance(exc, RuntimeError) and str(exc).startswith("CDP error on id=") and UNAVAILABLE in str(exc)


async def _claim_scope(sup: Any, inspected: Any) -> tuple[list, list]:
    """(frames, targets) for the claim: each card document's frame and the target holding it."""
    infos = (await sup._cdp("Target.getTargets"))["result"]["targetInfos"]
    iframes = {t["targetId"] for t in infos if t.get("type") == "iframe"}
    frames, targets = [], []
    for d in inspected.documents:
        if d.frame_session == inspected.page_session:
            target = inspected.page_id
        else:
            owners = [k for k, v in inspected.child_sessions.items() if v == d.frame_session and k in iframes]
            if len(owners) != 1:
                raise SurfaceRefused("payment document owner unknown")
            target = owners[0]
        frames.append(d.frame_id)
        targets.append(target)
    return _uniq(frames), _uniq(targets)


async def _claim(sup: Any, inspected: Any) -> _Surface:
    frames, targets = await _claim_scope(sup, inspected)
    ws = sup._ws
    oopif = any(t != inspected.page_id for t in targets)
    # Register the requested scope BEFORE the claim CDP call: control starts protecting the splice as
    # soon as it confirms, so a concurrent browser_cdp read in that window must already be refused
    # here (defence in depth; control is the boundary). The provisional scope is a SUPERSET of what is
    # already protected: an extension never lifts an earlier fill's frames (Codex A3 r1 #3).
    with _LOCK:
        existing = _SURFACES.get(sup)
        if existing is None:
            provisional = _Surface(frozenset(frames), frozenset(targets), oopif, ws)
        else:
            provisional = _Surface(existing.frames | frozenset(frames), existing.targets | frozenset(targets),
                                   existing.oopif or oopif, ws)
        _SURFACES[sup] = provisional
    try:
        reply = await sup._cdp(CLAIM_METHOD, {"frames": frames, "targets": targets}, timeout=_CLAIM_TIMEOUT_S)
        result = reply.get("result") if isinstance(reply, dict) else None
        if not isinstance(result, dict) or result.get("claimed") is not True or sup._ws is not ws:
            raise SurfaceRefused("claim not confirmed")
        held_frames = frozenset(result.get("frames") or ())
        held_targets = frozenset(result.get("targets") or ())
        if not set(frames) <= held_frames or not set(targets) <= held_targets:
            raise SurfaceRefused("claim does not cover the card documents")
    except BaseException as exc:
        # Only control's own refusal proves nothing changed: restore what was protected before (an earlier
        # fill stays protected). On any uncertain failure keep the wider provisional scope; the next status
        # refresh (_surface) narrows it to control's real set or lifts it.
        if _control_refused(exc):
            with _LOCK:
                if _SURFACES.get(sup) is provisional:
                    if existing is None:
                        _SURFACES.pop(sup, None)
                    else:
                        _SURFACES[sup] = existing
        raise
    confirmed = _Surface(held_frames, held_targets, any(t != inspected.page_id for t in held_targets), ws)
    with _LOCK:
        _SURFACES[sup] = confirmed
    return confirmed


def claim_payment_surface(supervisor: Any, inspected: Any) -> Optional[dict]:
    """The fill path's hook (managed mode, after consent, BEFORE ``resolve_secret``).

    Returns None when control confirmed this supervisor's claim over the card documents;
    otherwise the refusal result to return as-is (``payment_surface_unavailable``). Never
    retried: a refusal means another connection holds the surface or control cannot verify
    the frames, and the fill must stop with nothing resolved.
    """
    loop = getattr(supervisor, "_loop", None)
    if loop is None or not loop.is_running():
        return _refusal(UNAVAILABLE, _UNAVAILABLE_TEXT)
    try:
        _wait(_claim(supervisor, inspected), loop, timeout=_CLAIM_TIMEOUT_S + 5)
    except Exception as exc:  # health: allow BLE001 -- CDP/transport text is not surfaced; fixed type only
        logger.warning("card fill refused: payment surface claim failed (%s)", type(exc).__name__)
        return _refusal(UNAVAILABLE, _UNAVAILABLE_TEXT)
    return None


def _refresh(supervisor: Any) -> tuple[Optional[_Surface], bool]:
    """(the supervisor's protected set refreshed from control, whether THIS refresh succeeded and control
    named this connection the native one). A failed or malformed refresh keeps the last known set (fail
    closed) and is never native."""
    with _LOCK:
        known = _SURFACES.get(supervisor)
    if known is None:
        return None, False
    try:
        reply = _wait(supervisor._cdp(STATUS_METHOD, {}, timeout=5), supervisor._loop, timeout=7)
        status = reply.get("result") if isinstance(reply, dict) else None
        if not isinstance(status, dict):
            raise SurfaceRefused("malformed status")
    except Exception:  # health: allow BLE001 -- control unreachable: keep the protection (fail closed)
        return known, False
    if status.get("protected") is not True:
        with _LOCK:
            if _SURFACES.get(supervisor) is known:
                _SURFACES.pop(supervisor, None)
        return None, False
    known.frames = frozenset(status.get("frames") or known.frames)
    known.targets = frozenset(status.get("targets") or known.targets)
    return known, status.get("native") is True


def _surface(supervisor: Any) -> Optional[_Surface]:
    """The supervisor's protected set, refreshed from control; the last known set on failure."""
    return _refresh(supervisor)[0]


def protected_frames(supervisor: Any) -> frozenset:
    found = _surface(supervisor)
    return found.frames if found else frozenset()


# ------------------------------------------------------------- browser_cdp gate


def _names_frame(value: Any, frames: frozenset, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"frameId", "parentFrameId"} and isinstance(item, str) and item in frames:
                return True
            if isinstance(item, (dict, list)) and _names_frame(item, frames, depth + 1):
                return True
    elif isinstance(value, list):
        return any(_names_frame(item, frames, depth + 1) for item in value[:64])
    return False


def _clipboard_input(low: str, params: dict) -> bool:
    if low == "input.dispatchkeyevent":
        if params.get("commands"):
            return True
        mods = params.get("modifiers")
        mods = mods if isinstance(mods, int) and not isinstance(mods, bool) else 0
        key, code = str(params.get("key") or "").lower(), str(params.get("code") or "").lower()
        if mods & (2 | 4):
            return True
        if key in _CLIP_KEYS or code in _CLIP_KEYS:
            return bool(mods & 8) or key in {"copy", "cut", "paste"}
        return False
    if low == "input.dispatchmouseevent":
        buttons = params.get("buttons")
        return params.get("button") == "middle" or (
            params.get("type") == "mouseMoved" and isinstance(buttons, int) and buttons != 0)
    return False


def _plain_navigation(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    flat = "".join(ch for ch in url if ch not in "\t\n\r").lstrip(_C0_SPACE)
    return (flat.split(":", 1)[0].lower() if ":" in flat else "") in {"http", "https", "about"}


def _denial(low: str, params: dict, *, protected_session: bool, surface: _Surface) -> str:
    domain = low.split(".", 1)[0]
    if low in _SLOT_DENY or f"{domain}.*" in _SLOT_DENY:
        return "network bodies, interception and nested sessions are refused while a payment frame is protected"
    if low == "target.attachtotarget" and params.get("targetId") in surface.targets:
        return "attaching to a protected payment target is refused"
    if low in _AUTO_ATTACH and surface.oopif:
        return "auto-attach is refused while a payment frame is protected"
    if low in _PERMISSIONS and "clipboard" in json.dumps(params).lower():
        return "clipboard permissions are refused while a payment frame is protected"
    if _clipboard_input(low, params):
        return "copy, paste and drag input is refused while a payment frame is protected"
    if low == "page.navigate" and not _plain_navigation(params.get("url")):
        return "only http(s) and about: navigations are allowed while a payment frame is protected"
    if low != "page.navigate" and _names_frame(params, surface.frames):
        return "commands naming a protected payment frame are refused"
    if protected_session and low not in _SESSION_ALLOW and (
            domain in _SESSION_DENY_DOMAINS or low in _SESSION_DENY_METHODS):
        return "reads of a protected payment document are refused; use browser_vault payment_controls"
    if protected_session and domain == "page" and low not in _PAGE_SAFE_ON_PROTECTED:
        return "script injection and content ops on a protected payment document are refused"
    return ""


def _session_protected(supervisor: Any, frame_id: str, session_id: Optional[str], surface: _Surface) -> bool:
    """True when the call lands in a protected document's process. A same-process frame inside a
    card OOPIF shares the OOPIF's session, so the session decides, not the frame id the model named.
    A protected target whose session the supervisor does not know makes every session protected."""
    if frame_id in surface.targets:
        return True
    with supervisor._state_lock:
        records = dict(supervisor._frames)
    sessions = set()
    for target in surface.targets:
        record = records.get(target)
        sid = getattr(record, "cdp_session_id", None) if record is not None else None
        if not sid:
            return True
        sessions.add(sid)
    return session_id in sessions


def supervisor_route_refusal(supervisor: Any, frame_id: str, method: str, params: Any,
                             session_id: Optional[str] = None) -> Optional[dict]:
    """Refusal for ``browser_cdp(frame_id=…)`` on the native socket (``session_id``: where it lands),
    or None to send it."""
    low = str(method or "").lower()
    if low.startswith("hermes."):
        return _refusal(PROTECTED, "browser-control methods cannot be sent through browser_cdp.")
    surface = _surface(supervisor)
    if surface is None:
        return None
    reason = _denial(low, params if isinstance(params, dict) else {},
                     protected_session=_session_protected(supervisor, frame_id, session_id, surface),
                     surface=surface)
    if not reason:
        return None
    return _refusal(PROTECTED, f"{reason}. The card frame is protected after a card fill until it navigates "
                               "away; continue with browser_vault payment_controls / payment_click or the "
                               "merchant page.")


# ------------------------------------------------------------ native actions

# Runs in a fresh isolated world of one protected document. Lists clickable controls and never an
# editable field: text inputs, selects, textareas and contenteditable are skipped, and so is any
# control that contains one or labels one. Returns the elements array (by reference) so the caller
# describes or clicks by index without the page seeing the call.
_LISTER = r"""(() => {
  const EDIT = 'input:not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]),select,textarea,[contenteditable=""],[contenteditable=true]';
  const SEL = 'button,a[href],[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],summary,'
    + 'input[type=checkbox],input[type=radio],input[type=submit],input[type=button]';
  const out = [];
  for (const el of document.querySelectorAll(SEL)) {
    if (el.matches(EDIT) || el.querySelector(EDIT)) continue;
    if (el.tagName === 'LABEL' && el.control && el.control.matches(EDIT)) continue;
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    if (r.width < 1 || r.height < 1 || st.visibility !== 'visible' || st.display === 'none') continue;
    out.push(el);
    if (out.length >= %d) break;
  }
  return out;
})()""".replace("%d", str(_MAX_CONTROLS))

_DESCRIBE = r"""function(){ return this.map((el, i) => {
  const t = (el.type || '').toLowerCase(); const role = (el.getAttribute('role') || '').toLowerCase();
  const kind = (t === 'checkbox' || role === 'checkbox') ? 'checkbox' : (t === 'radio' || role === 'radio') ? 'radio'
    : (el.tagName === 'A' || role === 'link') ? 'link' : 'button';
  const input = el.tagName === 'INPUT' && (t === 'submit' || t === 'button' || t === 'reset');
  const raw = el.getAttribute('aria-label') || (input ? el.value : el.innerText) || el.title || '';
  return {i, role: kind, text: String(raw).replace(/\s+/g, ' ').trim(),
          disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true')};
}); }"""

_HIT = r"""function(){ this.scrollIntoView({block: 'center', inline: 'center'});
  const r = this.getBoundingClientRect(); const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const hit = document.elementFromPoint(x, y); return !!hit && (hit === this || this.contains(hit)); }"""


def _mask(text: str) -> str:
    return re.sub(r"\d{4,}", lambda m: "•" * len(m.group(0)), text)[:_MAX_TEXT]


def _ref(frame_id: str, index: int, role: str, text: str) -> str:
    digest = hashlib.sha256(f"{frame_id}|{index}|{role}|{text}".encode()).hexdigest()[:10]
    return f"c{index}-{digest}"


async def _documents(sup: Any, surface: _Surface) -> tuple:
    with sup._state_lock:
        page_sid = sup._page_session_id if sup._active else None
    if not page_sid:
        raise SurfaceRefused("supervised browser required")
    child_sessions: dict = {}
    tree = await sup._payment_tree(page_sid, child_sessions)
    docs = [(f[0], child_sessions.get(f[0], page_sid)) for f in tree if f[0] in surface.frames]
    return page_sid, tree, child_sessions, docs


async def _listing(sup: Any, fid: str, sid: str) -> tuple:
    """(array object id, [control dicts]) for one protected document."""
    ctx = (await sup._cdp("Page.createIsolatedWorld", {"frameId": fid, "worldName": "hermes-payment-controls"},
                          session_id=sid))["result"]["executionContextId"]
    arr = (await sup._cdp("Runtime.evaluate", {"expression": _LISTER, "contextId": ctx, "returnByValue": False},
                          session_id=sid))["result"]["result"].get("objectId")
    if not arr:
        raise SurfaceRefused("payment document unavailable")
    rows = (await sup._cdp("Runtime.callFunctionOn", {"objectId": arr, "functionDeclaration": _DESCRIBE,
                                                      "returnByValue": True}, session_id=sid))["result"]["result"]
    controls = []
    for row in rows.get("value") or []:
        text = _mask(str(row.get("text") or ""))
        role = row.get("role") if row.get("role") in {"button", "link", "checkbox", "radio"} else "button"
        index = int(row.get("i") or 0)
        controls.append({"ref": _ref(fid, index, role, text), "role": role, "text": text,
                         "disabled": row.get("disabled") is True, "_i": index})
    return arr, controls


async def _list_all(sup: Any, surface: _Surface) -> tuple:
    page_sid, tree, child_sessions, docs = await _documents(sup, surface)
    listed = []
    for fid, sid in docs:
        arr, controls = await _listing(sup, fid, sid)
        listed.append((fid, sid, arr, controls))
    return page_sid, tree, child_sessions, listed


async def _release(sup: Any, child_sessions: dict, listed: list) -> None:
    for _fid, sid, arr, _c in listed:
        await sup._release_payment_object(sid, arr)
    await sup._release_payment_sessions(child_sessions)


def _public(controls: list) -> list:
    return [{k: v for k, v in c.items() if not k.startswith("_")} for c in controls]


async def _controls(sup: Any, surface: _Surface) -> dict:
    _page_sid, _tree, child_sessions, listed = await _list_all(sup, surface)
    try:
        controls = [c for _f, _s, _a, cs in listed for c in cs]
        return {"success": True, "documents": len(listed), "controls": _public(controls)}
    finally:
        await _release(sup, child_sessions, listed)


def _owner_target(sid: str, page_sid: str, child_sessions: dict, iframes: set) -> Optional[str]:
    if sid == page_sid:
        return None
    owners = [k for k, v in child_sessions.items() if v == sid and k in iframes]
    if len(owners) != 1:
        raise SurfaceRefused("payment document owner unknown")
    return owners[0]


async def _page_point(sup: Any, sid: str, element: str, page_sid: str, tree: tuple,
                      child_sessions: dict) -> tuple:
    """The element's centre in page coordinates, plus the outermost OOPIF owner node (or None)."""
    quads = (await sup._cdp("DOM.getContentQuads", {"objectId": element}, session_id=sid))["result"]["quads"]
    if not quads:
        raise SurfaceRefused("control not rendered")
    q = quads[0]
    x, y = sum(q[0::2]) / 4, sum(q[1::2]) / 4
    infos = (await sup._cdp("Target.getTargets"))["result"]["targetInfos"]
    iframes = {t["targetId"] for t in infos if t.get("type") == "iframe"}
    parents = {f[0]: f[4] for f in tree}
    outer = None
    target = _owner_target(sid, page_sid, child_sessions, iframes)
    while target is not None:
        parent = parents.get(target)
        if not parent:
            raise SurfaceRefused("payment frame owner unknown")
        parent_sid = child_sessions.get(parent, page_sid)
        owner = (await sup._cdp("DOM.getFrameOwner", {"frameId": target}, session_id=parent_sid))["result"]
        box = (await sup._cdp("DOM.getBoxModel", {"backendNodeId": owner["backendNodeId"]},
                              session_id=parent_sid))["result"]["model"]["content"]
        x, y = x + box[0], y + box[1]
        if parent_sid == page_sid:
            outer = owner["backendNodeId"]
        target = _owner_target(parent_sid, page_sid, child_sessions, iframes)
    return x, y, outer


async def _click(sup: Any, surface: _Surface, ref: str, progress: list, deadline: float) -> dict:
    page_sid, tree, child_sessions, listed = await _list_all(sup, surface)
    element = None
    try:
        found = [(fid, sid, arr, c) for fid, sid, arr, cs in listed for c in cs if c["ref"] == ref]
        if len(found) != 1:
            return _refusal("payment_control_unknown", "That control is not listed now; call payment_controls again.")
        _fid, sid, arr, control = found[0]
        if control["disabled"]:
            return _refusal("payment_control_disabled", "That control is disabled; nothing was clicked.")
        element = (await sup._cdp("Runtime.callFunctionOn", {
            "objectId": arr, "functionDeclaration": "function(i){return this[i]}",
            "arguments": [{"value": control["_i"]}], "returnByValue": False}, session_id=sid))["result"]["result"].get(
                "objectId")
        if not element:
            raise SurfaceRefused("control gone")
        hit = (await sup._cdp("Runtime.callFunctionOn", {"objectId": element, "functionDeclaration": _HIT,
                                                         "returnByValue": True}, session_id=sid))["result"]["result"]
        if hit.get("value") is not True:
            return _refusal("payment_control_covered", "Another element covers that control; nothing was clicked.")
        x, y, outer = await _page_point(sup, sid, element, page_sid, tree, child_sessions)
        if outer is not None:
            at = (await sup._cdp("DOM.getNodeForLocation", {"x": round(x), "y": round(y)},
                                 session_id=page_sid))["result"].get("backendNodeId")
            if at != outer:
                return _refusal("payment_control_covered", "Another element covers that control; nothing was clicked.")
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            params = {"type": kind, "x": x, "y": y}
            if kind != "mouseMoved":
                params.update(button="left", clickCount=1)
            if kind == "mousePressed":
                # Nothing awaits between this check and the dispatch: past the deadline the caller may
                # already have reported "nothing was clicked", so the press must not start.
                if time.monotonic() >= deadline:
                    raise SurfaceRefused("the action ran out of time before the click")
                # From here a failure cannot promise the click did not land (duplicate-submit risk).
                progress.append(True)
            await sup._cdp("Input.dispatchMouseEvent", params, session_id=page_sid)
        return {"success": True, "clicked": ref, "role": control["role"], "text": control["text"]}
    finally:
        if element:
            await sup._release_payment_object(sid, element)
        await _release(sup, child_sessions, listed)


def _run(supervisor: Any, coro_factory, progress: list | None = None) -> dict:
    surface, native = _refresh(supervisor)
    if surface is None:
        return _refusal("payment_surface_none", "No protected card frame on this page: use browser_exec as usual.")
    if not native:
        # Only the connection control confirmed (just now) as the surface's native owner may act inside it.
        return _refusal(UNAVAILABLE, "Browser control did not confirm that this connection holds the protected "
                                     "card frame, so the native action did not run; nothing was clicked. Finish "
                                     "on the merchant page or ask the user.")
    # Taken before the wait starts, so the press deadline always falls before the caller's timeout.
    deadline = time.monotonic() + _ACTION_TIMEOUT_S - _PRESS_MARGIN_S
    try:
        return _wait(coro_factory(surface, deadline), supervisor._loop, timeout=_ACTION_TIMEOUT_S)
    except SurfaceRefused as exc:
        if progress:
            return _uncertain_click()
        return _refusal(UNAVAILABLE, f"The protected card frame could not be used ({exc}); nothing was clicked.")
    except Exception as exc:  # health: allow BLE001 -- CDP text can quote the page; fixed type only
        logger.info("payment surface action failed: %s", type(exc).__name__)
        if progress:
            return _uncertain_click()
        return _refusal(UNAVAILABLE, "The protected card frame could not be read by the native action; nothing "
                                     "was clicked. If the native browser connection was replaced, finish on the "
                                     "merchant page or ask the user.")


def _uncertain_click() -> dict:
    return _refusal("payment_click_uncertain", "Outcome uncertain: a press may have landed (the click was being "
                    "sent when the action failed or ran out of time). Do NOT click again or retry; look at the "
                    "page (browser_exec screenshot is blocked, so read text) or ask the user whether the purchase "
                    "went through.")


def payment_controls(supervisor: Any) -> dict:
    """``browser_vault action=payment_controls``: ``{ref, role, text, disabled}`` per control."""
    return _run(supervisor, lambda s, _deadline: _controls(supervisor, s))


def payment_click(supervisor: Any, ref: str) -> dict:
    """``browser_vault action=payment_click ref=…``: one trusted click on a listed control."""
    if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
        return _refusal("payment_control_unknown", "ref must be one returned by payment_controls.")
    progress: list = []
    return _run(supervisor, lambda s, deadline: _click(supervisor, s, ref, progress, deadline), progress)
