"""What a press after an approval's Pay press may be (build C r4, 2026-11-18): the site rules a bank's own step is told
apart by (``cross_site``, a provider's site), and the confirmation dialog the one follow-up press may be in (the
dialogs open before the Pay press, the dialog a control sits in). Read by ``browser_payment_surface`` (``_bank_step``,
``_confirm_possible``); the site rules moved here from it unchanged. Only node ids and verdicts leave the page.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("tools.browser_payment_surface")

_DIALOG_SEL = "[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open]"


def _origin_of(url: Any) -> str:
    from tools.browser_payment_done import _url_origin

    return _url_origin(url)


def _providers(sup: Any) -> frozenset:
    from tools import browser_payment_done as done

    with done._LOCK:
        payments = list((done._PAYMENTS.get(sup) or {}).values())
    return frozenset(o for p in payments for o in done._card_origins(p))


def _provider_site(sup: Any, url: Any) -> bool:
    """``url`` is on a recorded provider origin (exact origin, not the site).

    The exact origin, on purpose: a provider's own 3-D Secure host on the same site as its card frames
    (hooks.stripe.com beside js.stripe.com) is a bank step after Pay and must stay pressable."""
    return _origin_of(url) in _providers(sup)


# build C r4: a follow-up press in a new dialog is never a retry after an error (that needs a new approval)
_RETRY_WORDS = re.compile(r"\b(again|retry|try|erneut|nochmal|noch einmal|wiederholen|réessayer|reintentar)\b",
                          re.IGNORECASE)


_MAX_DIALOGS = 8
_OPEN_DIALOGS = r"""function(){
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width >= 1 && r.height >= 1 && st.visibility === 'visible' && st.display !== 'none' && st.opacity !== '0'; };
  const out = [];
  for (const el of this.ownerDocument.querySelectorAll('__DIALOG__')) {
    if (vis(el)) out.push(el);
    if (out.length > __MAX__) break;
  }
  return out; }""".replace("__DIALOG__", _DIALOG_SEL).replace("__MAX__", str(_MAX_DIALOGS))
_DIALOG_OF = "function(){ return this.closest('" + _DIALOG_SEL + "'); }"


async def _backend_id(sup: Any, sid: str, obj: str) -> Optional[int]:
    reply = await sup._cdp("DOM.describeNode", {"objectId": obj}, session_id=sid)
    value = (((reply.get("result") or {}).get("node")) or {}).get("backendNodeId")
    return value if type(value) is int else None


async def _open_dialogs(sup: Any, sid: str, element: str) -> Optional[frozenset]:
    """build C r4: the node ids of the dialogs open in the Pay control's document right before the Pay press (read in
    the controls' isolated world; only node ids leave it), or None (unread, more than ``_MAX_DIALOGS``): then no
    confirmation press follows this Pay. Never a reason to refuse the Pay press."""
    try:
        reply = await sup._cdp("Runtime.callFunctionOn", {"objectId": element, "functionDeclaration": _OPEN_DIALOGS,
                                                          "returnByValue": False}, session_id=sid)
        arr = ((reply.get("result") or {}).get("result") or {}).get("objectId")
        if not arr:
            return None
        try:
            return await _dialog_ids(sup, sid, arr)
        finally:
            await sup._release_payment_object(sid, arr)
    except Exception as exc:  # health: allow BLE001 -- unread: no confirmation press follows this Pay
        logger.info("payment_click: open dialogs not read (%s)", type(exc).__name__)
        return None


async def _dialog_ids(sup: Any, sid: str, arr: str) -> Optional[frozenset]:
    reply = await sup._cdp("Runtime.getProperties", {"objectId": arr, "ownProperties": True}, session_id=sid)
    props = (reply.get("result") or {}).get("result")
    if not isinstance(props, list):
        return None
    objs = [p["value"]["objectId"] for p in props if isinstance(p, dict) and str(p.get("name", "")).isdigit()
            and isinstance(p.get("value"), dict) and p["value"].get("objectId")]
    if len(objs) > _MAX_DIALOGS:
        return None
    ids = []
    for obj in objs:
        try:
            ids.append(await _backend_id(sup, sid, obj))
        finally:
            await sup._release_payment_object(sid, obj)
    return None if None in ids else frozenset(ids)


async def _new_dialog(sup: Any, gen: Any, sid: str, element: str) -> bool:
    """build C r4: the control sits in a dialog (``role=dialog``/``alertdialog``, ``aria-modal``, ``<dialog open>``)
    that was not open when this approval's Pay was pressed (its node is not one recorded then)."""
    reply = await sup._cdp("Runtime.callFunctionOn", {"objectId": element, "functionDeclaration": _DIALOG_OF,
                                                      "returnByValue": False}, session_id=sid)
    obj = ((reply.get("result") or {}).get("result") or {}).get("objectId")
    if not obj:
        return False
    try:
        node = await _backend_id(sup, sid, obj)
    finally:
        await sup._release_payment_object(sid, obj)
    return node is not None and node not in gen.pay_dialogs[2]


# r4 C4: public suffixes with two labels, so ``shop.co.uk`` is one site and ``bank.co.uk`` another. A suffix not
# listed makes two shops under it one site: their pages are read for the total (fails closed), never exempt.
_TWO_LABEL_SUFFIXES = frozenset("""
ac.uk co.uk gov.uk ltd.uk me.uk net.uk org.uk plc.uk sch.uk
com.au edu.au gov.au net.au org.au asn.au id.au
co.nz net.nz org.nz govt.nz ac.nz
co.jp ne.jp or.jp ac.jp go.jp ed.jp gr.jp lg.jp
co.kr or.kr ne.kr go.kr ac.kr
com.br net.br org.br gov.br
com.mx org.mx gob.mx com.ar com.co com.pe com.uy com.ve com.ec com.py com.bo
com.cn net.cn org.cn gov.cn com.hk org.hk com.tw org.tw com.sg org.sg com.my com.ph com.vn com.pk com.bd
co.in net.in org.in gen.in firm.in co.id or.id ac.id co.th in.th ac.th co.il org.il ac.il
co.za org.za com.tr org.tr com.sa com.eg com.ua com.pl net.pl org.pl com.gr com.cy com.mt
co.at or.at gv.at ac.at co.ke co.ug co.tz com.ng com.gh
""".split())


def _site(url: Any) -> Optional[str]:
    """The registrable domain of an https URL's host (last two labels, three under a listed two-label suffix), or
    None when it cannot be told (another scheme, an IP address, a single-label host)."""
    origin = _origin_of(url)
    if not origin.startswith("https://"):
        return None
    host = origin[len("https://"):].split(":")[0].rstrip(".").lower()
    labels = host.split(".")
    if host.startswith("[") or len(labels) < 2 or all(part.isdigit() for part in labels) or not all(labels):
        return None
    keep = 3 if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES else 2
    return ".".join(labels[-keep:])


def cross_site(url: Any, merchant: Any) -> bool:
    """Whether ``url`` is on another site than ``merchant``: both sites known and different. Same site, or either
    unknown: False (the page is then read for the total)."""
    a, b = _site(url), _site(merchant)
    return a is not None and b is not None and a != b
