"""Payment slot lockdown, runtime side (lockdown design §1, contract ``cdp_methods`` in control-v1).

browser-control locks the slot when the runtime claims it: every non-native CDP connection to it is
closed, and while locked a non-native connection may only list and close tabs (``locked.commands``).
Control does not filter the runtime's own (native) supervisor socket, so this module is the lock for
everything the model can relay over that socket:

- :func:`claim_payment_surface` — the fill path's hook. Before any card value is resolved, the
  supervisor claims the payment tab (the page target holding the card documents; OOPIF card frames
  belong to it) with ``Hermes.claimPaymentSurface {tabs}``. A refused or unconfirmed claim refuses the
  fill (``payment_surface_unavailable``); nothing is resolved or written.
- :func:`slot_locked` / :func:`locked_refusal` — the one lock check for model-reachable calls on the
  native socket (``browser_cdp(frame_id=…)``, dialog answers and dialog text, the vault's non-card page
  operations). While locked only :data:`LOCKED_COMMANDS` without a session pass, which on this socket
  (every relayed call is session-scoped) means none. A call that was in flight when a claim started
  has its result withheld.
- :func:`payment_controls` / :func:`payment_click` — the native actions that let a checkout continue
  under the lock: list the buttons, links and checkboxes of the payment's card documents and of the
  payment tab's own top document (never inputs or selects; digit runs masked), and make one trusted
  click on a listed control at its hit-tested centre.

The lock lifts only when control's status proves the slot unlocked (the payment tab destroyed, a
wipe or a human clear); a failed or malformed status keeps it.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import weakref
from typing import Any, Optional

logger = logging.getLogger(__name__)

CLAIM_METHOD = "Hermes.claimPaymentSurface"
STATUS_METHOD = "Hermes.paymentSurfaceStatus"
UNAVAILABLE = "payment_surface_unavailable"
LOCKED = "payment_surface_locked"
# control-v1 ``cdp_methods.locked.commands`` (exact names); allowed only without a sessionId.
LOCKED_COMMANDS = frozenset({"Target.getTargets", "Target.getTargetInfo", "Target.setDiscoverTargets",
                             "Target.closeTarget", "Browser.getVersion"})
_MAX_CLAIM_TABS = 8
_MAX_STATUS_TABS = 64
_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_CLAIM_TIMEOUT_S = 10.0
# payment_controls / payment_click: the caller waits _ACTION_TIMEOUT_S; the click's mousePressed is only
# dispatched before (start + _ACTION_TIMEOUT_S - _PRESS_MARGIN_S), so a press can never start after the
# caller gave up and reported that nothing was clicked.
_ACTION_TIMEOUT_S = 25.0
_PRESS_MARGIN_S = 5.0
_MAX_CONTROLS = 40
_MAX_TOP_CONTROLS = 150  # a merchant page's header and footer links come before its Pay button
_MAX_TEXT = 60
_REF_RE = re.compile(r"c\d{1,3}-[0-9a-f]{10}")


class SurfaceRefused(RuntimeError):
    """The payment surface could not be claimed or used; carries a model-safe reason."""


_LOCK = threading.Lock()
# Supervisors whose claim was sent and not proven released (fail closed between claim and status).
_HELD: weakref.WeakSet = weakref.WeakSet()
# Supervisors with a claim on the wire: locked whatever a status read says meanwhile.
_CLAIMING: weakref.WeakSet = weakref.WeakSet()
# Bumped before every claim is sent: a relayed call whose reply straddles a claim is withheld, and a
# status read that started before a claim never clears the lock.
_EPOCH = [0]


def _refusal(error_type: str, message: str) -> dict:
    return {"success": False, "error_type": error_type, "error": message}


_UNAVAILABLE_TEXT = (
    "Browser control could not lock this browser for the card payment, so no card was read or written. "
    "Nothing was submitted. Do not retry on another route; report it to the user."
)
LOCKED_TEXT = (
    "The browser is locked for a card payment: page reads, scripts, input, screenshots and attaching to tabs "
    "are refused until the payment tab is closed. Tabs can still be listed (browser_cdp Target.getTargets / "
    "Target.getTargetInfo without frame_id; each tab's site (origin) only, no title) and closed "
    "(Target.closeTarget). Continue with "
    "browser_vault payment_controls / payment_click on the payment tab, and call payment_done after Pay: it "
    "hands the confirmation over and ends the lock."
)


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


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


def parse_claim_result(result: Any, tabs: list) -> bool:
    """Control confirmed the claim: exactly ``{claimed: true, locked: true, tabs}`` with every claimed
    tab among the slot's owning tabs. Anything else is unconfirmed."""
    return (isinstance(result, dict) and set(result) == {"claimed", "locked", "tabs"}
            and result["claimed"] is True and result["locked"] is True and isinstance(result["tabs"], list)
            and 0 < len(result["tabs"]) <= _MAX_STATUS_TABS and all(_valid_id(t) for t in result["tabs"])
            and set(tabs) <= set(result["tabs"]))


def parse_lock_status(result: Any) -> tuple[str, frozenset]:
    """``Hermes.paymentSurfaceStatus`` as ``("locked", owning tabs)``, ``("unlocked", ∅)`` or
    ``("unknown", ∅)``. The one reader of the lock state. Exactly ``{locked, native, tabs, unverified}``:
    three bools and the sorted owning tab ids; ``locked`` must equal ``tabs or unverified`` and ``native``
    implies ``locked``. Any other shape, type or combination is unknown, never unlocked."""
    if not isinstance(result, dict) or set(result) != {"locked", "native", "tabs", "unverified"}:
        return "unknown", frozenset()
    locked, native, tabs, unverified = result["locked"], result["native"], result["tabs"], result["unverified"]
    if not (type(locked) is bool and type(native) is bool and type(unverified) is bool):
        return "unknown", frozenset()
    if not (isinstance(tabs, list) and len(tabs) <= _MAX_STATUS_TABS and all(_valid_id(t) for t in tabs)
            and len(set(tabs)) == len(tabs)):
        return "unknown", frozenset()
    if locked is not (bool(tabs) or unverified) or (native and not locked):
        return "unknown", frozenset()
    return ("locked", frozenset(tabs)) if locked else ("unlocked", frozenset())


def claim_payment_surface(supervisor: Any, inspected: Any) -> Optional[dict]:
    """The fill path's hook (managed mode, after consent, BEFORE ``resolve_secret``).

    Returns None when control confirmed this supervisor's claim of the payment tab; otherwise the
    refusal result to return as-is (``payment_surface_unavailable``). Never retried: a refusal means
    another connection holds the slot or control cannot verify the tab, and the fill must stop with
    nothing resolved. The local lock is set (and the epoch bumped) BEFORE the claim is sent, so a
    relayed call whose reply arrives during or after the claim is withheld.
    """
    loop = getattr(supervisor, "_loop", None)
    tab = getattr(inspected, "page_id", None)
    if loop is None or not loop.is_running() or not _valid_id(tab):
        return _refusal(UNAVAILABLE, _UNAVAILABLE_TEXT)
    with _LOCK:
        held_before = supervisor in _HELD
        _HELD.add(supervisor)
        _CLAIMING.add(supervisor)
        _EPOCH[0] += 1
    ws = getattr(supervisor, "_ws", None)
    try:
        reply = _wait(supervisor._cdp(CLAIM_METHOD, {"tabs": [tab]}, timeout=_CLAIM_TIMEOUT_S), loop,
                      timeout=_CLAIM_TIMEOUT_S + 5)
        result = reply.get("result") if isinstance(reply, dict) else None
        if not parse_claim_result(result, [tab]) or getattr(supervisor, "_ws", None) is not ws:
            raise SurfaceRefused("claim not confirmed")
    except Exception as exc:  # health: allow BLE001 -- CDP/transport text is not surfaced; fixed type only
        if _control_refused(exc) and not held_before:
            # Only control's own refusal proves nothing changed; any other failure keeps the lock here.
            with _LOCK:
                _HELD.discard(supervisor)
        logger.warning("card fill refused: payment surface claim failed (%s)", type(exc).__name__)
        return _refusal(UNAVAILABLE, _UNAVAILABLE_TEXT)
    finally:
        with _LOCK:
            _CLAIMING.discard(supervisor)
    # dlz (lockdown §2): the payment tab and its card documents, for browser_vault payment_done.
    from tools.browser_payment_done import record_payment

    record_payment(supervisor, inspected)
    return None


def lock_status(supervisor: Any) -> str:
    """Control's answer for this supervisor's slot: ``locked`` | ``unlocked`` | ``unknown``. A proven
    unlock also clears the local lock, unless a claim started meanwhile."""
    start = epoch()
    try:
        reply = _wait(supervisor._cdp(STATUS_METHOD, {}, timeout=5), supervisor._loop, timeout=7)
        state, _tabs = parse_lock_status(reply.get("result") if isinstance(reply, dict) else None)
    except Exception:  # health: allow BLE001 -- an unanswered status proves nothing
        state = "unknown"
    if state == "unlocked":
        with _LOCK:
            if _EPOCH[0] == start and supervisor not in _CLAIMING:
                _HELD.discard(supervisor)
    return state


def _managed() -> bool:
    try:
        from tools.browser_vault_tool import _is_managed

        return bool(_is_managed())
    except Exception:  # health: allow BLE001 -- unknown mode: treat as managed (fail closed)
        return True


def slot_locked(supervisor: Any) -> bool:
    """Whether model-reachable calls on this supervisor's (native) socket must be refused. Locked when
    a claim from this process is not proven released, when control says locked, and on a managed
    browser whenever control's answer is unknown. A browser without browser-control (no lock exists)
    is unlocked unless this process claimed it."""
    start = epoch()
    state = lock_status(supervisor)
    with _LOCK:
        if supervisor in _CLAIMING or _EPOCH[0] != start:
            return True
        held = supervisor in _HELD
    if state == "unknown":
        return held or _managed()
    return state == "locked"


def epoch() -> int:
    with _LOCK:
        return _EPOCH[0]


def locked_refusal(supervisor: Any, method: str = "", session_id: Optional[str] = None) -> Optional[dict]:
    """``payment_surface_locked`` for a model-reachable call while the slot is locked, else None.
    ``method``/``session_id`` name a relayed CDP command (``locked.commands`` without a session pass);
    a non-CDP action (dialog answer, vault page operation) passes neither and is refused while locked."""
    if method in LOCKED_COMMANDS and not session_id:
        return None
    return _refusal(LOCKED, LOCKED_TEXT) if slot_locked(supervisor) else None


def locked_withheld() -> dict:
    """A relayed call whose reply arrived after a card payment claimed the slot: never returned."""
    return _refusal(LOCKED, "A card payment locked this browser while the call was running, so its result was "
                            "withheld. " + LOCKED_TEXT)


def supervisor_route_refusal(supervisor: Any, frame_id: str, method: str, params: Any,
                             session_id: Optional[str] = None) -> Optional[dict]:
    """Refusal for ``browser_cdp(frame_id=…)`` on the native socket (``session_id``: where it lands),
    or None to send it."""
    if str(method or "").lower().startswith("hermes."):
        return _refusal(LOCKED, "browser-control methods cannot be sent through browser_cdp.")
    return locked_refusal(supervisor, str(method or ""), session_id)


# ------------------------------------------------------------ native actions

# Runs in a fresh isolated world of one protected document. Lists clickable controls and never an
# editable field: text inputs, selects, textareas and contenteditable are skipped, and so is any
# control that contains one or labels one. Returns the elements array (by reference) so the caller
# describes or clicks by index without the page seeing the call.
_LISTER_SRC = r"""(() => {
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
})()"""
_LISTER = _LISTER_SRC.replace("%d", str(_MAX_CONTROLS))
_TOP_LISTER = _LISTER_SRC.replace("%d", str(_MAX_TOP_CONTROLS))

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


async def _documents(sup: Any) -> tuple:
    with sup._state_lock:
        page_sid = sup._page_session_id if sup._active else None
    if not page_sid:
        raise SurfaceRefused("supervised browser required")
    child_sessions: dict = {}
    tree = await sup._payment_tree(page_sid, child_sessions)
    from tools.browser_payment_done import card_frame_ids, top_document

    # The payment's card documents (claimed by this supervisor), then, since under the slot lock agent CDP
    # cannot click anything, the payment tab's own top document (merchant page, 3-D Secure redirect) unless
    # it is a card frame.
    frames = card_frame_ids(sup)
    docs = [(f[0], child_sessions.get(f[0], page_sid), f[0]) for f in tree if f[0] in frames]
    top = await top_document(sup, page_sid, tree)
    if top is not None and top[0] not in {d[0] for d in docs}:
        docs.append(top)
    return page_sid, tree, child_sessions, docs


async def _listing(sup: Any, fid: str, sid: str, key: str) -> tuple:
    """(array object id, [control dicts]) for one listed document; ``key`` binds its refs."""
    ctx = (await sup._cdp("Page.createIsolatedWorld", {"frameId": fid, "worldName": "hermes-payment-controls"},
                          session_id=sid))["result"]["executionContextId"]
    lister = _LISTER if key == fid else _TOP_LISTER  # a payment tab's top document has its own ref key
    arr = (await sup._cdp("Runtime.evaluate", {"expression": lister, "contextId": ctx, "returnByValue": False},
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
        controls.append({"ref": _ref(key, index, role, text), "role": role, "text": text,
                         "disabled": row.get("disabled") is True, "_i": index})
    return arr, controls


async def _list_all(sup: Any) -> tuple:
    page_sid, tree, child_sessions, docs = await _documents(sup)
    listed = []
    for fid, sid, key in docs:
        arr, controls = await _listing(sup, fid, sid, key)
        listed.append((fid, sid, arr, controls))
    return page_sid, tree, child_sessions, listed


async def _release(sup: Any, child_sessions: dict, listed: list) -> None:
    for _fid, sid, arr, _c in listed:
        await sup._release_payment_object(sid, arr)
    await sup._release_payment_sessions(child_sessions)


def _public(controls: list) -> list:
    return [{k: v for k, v in c.items() if not k.startswith("_")} for c in controls]


async def _controls(sup: Any) -> dict:
    _page_sid, _tree, child_sessions, listed = await _list_all(sup)
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


async def _click(sup: Any, ref: str, progress: list, deadline: float) -> dict:
    page_sid, tree, child_sessions, listed = await _list_all(sup)
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
    from tools.browser_payment_done import has_payment

    if not has_payment(supervisor):
        return _refusal("payment_surface_none", "No card payment by Hermes is open on this browser connection: "
                                                "use the browser tools as usual.")
    # Taken before the wait starts, so the press deadline always falls before the caller's timeout.
    deadline = time.monotonic() + _ACTION_TIMEOUT_S - _PRESS_MARGIN_S
    try:
        return _wait(coro_factory(deadline), supervisor._loop, timeout=_ACTION_TIMEOUT_S)
    except SurfaceRefused as exc:
        if progress:
            return _uncertain_click()
        return _refusal(UNAVAILABLE, f"The payment page could not be used ({exc}); nothing was clicked.")
    except Exception as exc:  # health: allow BLE001 -- CDP text can quote the page; fixed type only
        logger.info("payment surface action failed: %s", type(exc).__name__)
        if progress:
            return _uncertain_click()
        return _refusal(UNAVAILABLE, "The payment page could not be read by the native action; nothing "
                                     "was clicked. If the native browser connection was replaced, finish on the "
                                     "merchant page or ask the user.")


def _uncertain_click() -> dict:
    return _refusal("payment_click_uncertain", "Outcome uncertain: a press may have landed (the click was being "
                    "sent when the action failed or ran out of time). Do NOT click again or retry; call "
                    "browser_vault payment_done to see where the payment tab ended up, or ask the user whether the "
                    "purchase went through.")


def payment_controls(supervisor: Any) -> dict:
    """``browser_vault action=payment_controls``: ``{ref, role, text, disabled}`` per control."""
    return _run(supervisor, lambda _deadline: _controls(supervisor))


def payment_click(supervisor: Any, ref: str) -> dict:
    """``browser_vault action=payment_click ref=…``: one trusted click on a listed control."""
    if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
        return _refusal("payment_control_unknown", "ref must be one returned by payment_controls.")
    progress: list = []
    return _run(supervisor, lambda deadline: _click(supervisor, ref, progress, deadline), progress)
