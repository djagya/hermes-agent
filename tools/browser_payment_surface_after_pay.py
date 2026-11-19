"""The site rules the pay gate tells a bank's own step apart by after an admitted press (build C r4, 2026-11-18;
``cross_site``, a provider's site) and the pressed control's node id. Read by ``browser_payment_surface``
(``_pay_gate``), ``browser_payment_press_kind`` (``pay_node``) and ``browser_payment_detect`` (a shop page). Build G1
removed the one-press budget and with it the bank-step and confirmation-dialog exemptions that lived here.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("tools.browser_payment_surface")

_DIALOG_SEL = "[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open]"  # the occluder and overlay reads

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


async def _backend_id(sup: Any, sid: str, obj: str) -> Optional[int]:
    reply = await sup._cdp("DOM.describeNode", {"objectId": obj}, session_id=sid)
    value = (((reply.get("result") or {}).get("node")) or {}).get("backendNodeId")
    return value if type(value) is int else None


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
