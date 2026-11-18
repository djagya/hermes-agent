"""Which press through the pay gate is the approval's one Pay press (build C r2, moved here from
``browser_payment_surface``), and (build F, 2026-11-18 Sunday live retry) when a press whose caption only confirms
is a preparatory step instead: Unzer's payment-method modal has "BESTÄTIGEN" (it confirms the card and closes the
modal) while the page's "JETZT KAUFEN" places the order. Counting that confirm as the Pay press used the approval's
one press and refused the real one.

build F round 2 (reviews: geometry let a real order press pass as preparatory): only on positive evidence. The
confirm's box is the nearest ancestor holding the owner ``<iframe>`` of a card frame this fill claimed (resolved by
frame id into the controls' isolated world, never found by geometry or ``src``); a purchase button (``purchase_button``)
of the same listing lies outside it. Read by ``browser_payment_surface`` (``_click``) and
``browser_payment_order_evidence`` (a purchase button still listed). Only booleans leave the page.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("tools.browser_payment_surface")

CAPTION_WITHHELD = "[caption withheld]"

# Words that name the purchase itself: a caption with one is a Pay press wherever it sits.
_STRONG = (r"pay|payer|pagar|paga|pague|buy|purchase|order now|place (?:your )?order|book|reserve|bezahlen|zahlen|"
           r"zahlungspflichtig|kostenpflichtig|kaufen|bestellen|buchen|commander|acheter|comprar|acquista|ordina")
# Words that only confirm or finish a step (the card's method, a form, the purchase): Pay only by context.
_CONFIRM = (r"submit|confirm|complete|finish|bestätigen|abschließen|absenden|valider|confirmer|confirmar|"
            r"conferma")
# build C r2 (P1): which presses through the pay gate count as the approval's one Pay press. A checkbox or radio
# never does; a button whose caption names a preparatory step (and no pay word) does not, nor does a link without a
# pay word. Everything else does, an unknown or withheld caption included (fails closed: at worst the real Pay is
# refused and handed to payment_done, never pressed twice).
_PAY_WORDS = re.compile(rf"\b({_STRONG}|{_CONFIRM})\b", re.IGNORECASE)
_STRONG_WORDS = re.compile(rf"\b({_STRONG})\b", re.IGNORECASE)
_CONFIRM_WORDS = re.compile(rf"\b({_CONFIRM})\b", re.IGNORECASE)
# A confirm word next to one of these names the purchase ("Zahlung bestätigen", "confirm order", "Kauf abschließen").
# build F r2: reservations, registrations, subscriptions, checkouts, donations and tickets are purchases too
# ("Confirm reservation", "Complete checkout", "Reservierung bestätigen", "Confirm subscription").
_PAY_NOUNS = re.compile(
    r"\b(payment|zahlung|paiement|pago|pagamento|order|bestellung|commande|pedido|ordine|bestelling|purchase|kauf|"
    r"achat|compra|acquisto|booking|bookings|buchung|buchungen|réservation|reservation|reservations|reservierung|"
    r"prenotazione|reserva|reservering|registration|anmeldung|registrierung|inscription|inscripción|iscrizione|"
    r"enrollment|enrolment|subscription|abonnement|abo|suscripción|abbonamento|checkout|kasse|donation|spende|"
    r"donación|donazione|ticket|tickets|billet|billets|entrada|entradas|biglietto|biglietti)\b", re.IGNORECASE)
# ... but not the payment method or card details ("Confirm payment method", "confirmer le moyen de paiement").
_METHOD_PHRASE = re.compile(
    r"\b(payment|card)\s+(method|methods|details|option|options|type|info|information)\b|"
    r"\b(moyen|méthode|mode|método|forma|metodo|modalità|mezzo)\s+(de|di)\s+(paiement|pago|pagamento)\b",
    re.IGNORECASE)
_PREP_WORDS = re.compile(
    r"\b(continue|next|weiter|review|prüfen|überprüfen|back|zurück|edit|change|ändern|bearbeiten|show|hide|"
    r"anzeigen|details|more|mehr|less|weniger|close|schließen|accept|akzeptieren|agree|zustimmen|apply|einlösen|"
    r"select|auswählen|wählen|add|hinzufügen|proceed|fortfahren|suivant|continuer|siguiente|continuar|avanti|"
    r"continua|cookies?)\b", re.IGNORECASE)


def _named_pay(role: str, caption: str) -> bool:
    """build C r3 (Q2): the caption itself names a pay step (a pay word, a readable caption)."""
    return role not in {"checkbox", "radio"} and isinstance(caption, str) and caption != CAPTION_WITHHELD \
        and bool(_PAY_WORDS.search(caption))


def _pay_press(role: str, caption: str) -> bool:
    """build C r2 (P1): whether a press through the pay gate is the approval's one Pay press (see ``_PAY_WORDS``)."""
    if role in {"checkbox", "radio"}:
        return False
    text = caption if isinstance(caption, str) else ""
    if _PAY_WORDS.search(text):
        return True
    if role == "link":
        return False
    return not (text != CAPTION_WITHHELD and _PREP_WORDS.search(text))


def strong_pay(caption: Any) -> bool:
    """build F: the caption names the purchase: a strong word, or a confirm word with a purchase noun."""
    if not isinstance(caption, str) or caption == CAPTION_WITHHELD:
        return False
    if _STRONG_WORDS.search(caption):
        return True
    return bool(_CONFIRM_WORDS.search(caption) and _PAY_NOUNS.search(_METHOD_PHRASE.sub(" ", caption)))


def confirm_only(caption: Any) -> bool:
    """build F: a readable caption with a confirm word and nothing that names the purchase ("Bestätigen", "OK,
    confirm", "Confirm payment method")."""
    return (isinstance(caption, str) and caption != CAPTION_WITHHELD and bool(_CONFIRM_WORDS.search(caption))
            and not strong_pay(caption))


# build F r2: wallet, pay-later and gift-card buttons name a purchase but never the checkout's own order button
# ("Buy with Apple Pay" above a card form is no reason to read the form's "Submit" as a step).
_WALLET = re.compile(
    r"apple\s*pay|google\s*pay|\bg\s*pay\b|amazon\s*pay|samsung\s*pay|pay\s*pal|\bshop\s*pay\b|klarna|"
    r"\bpay\s*later\b|später\s*bezahlen|\bsofort\b|afterpay|clearpay|\baffirm\b|venmo|ratenkauf|\braten\b|"
    r"gutschein|geschenkkarte|geschenkgutschein|gift\s*cards?|gift\s*voucher|voucher|e-?gift|carte\s*cadeau|"
    r"bon\s*cadeau|tarjeta\s*regalo|buono\s*regalo|cadeaubon|cadeaukaart", re.IGNORECASE)


def purchase_button(control: dict) -> bool:
    """build F r2: a listed control that places the checkout's order: a button (not a link, checkbox or radio),
    enabled, whose caption names the purchase (``strong_pay``) and no wallet, pay-later or gift card."""
    text = control.get("text")
    return (control.get("role") == "button" and not control.get("disabled") and strong_pay(text)
            and not _WALLET.search(text))


def purchase_indices(controls: list, pressed: dict) -> list:
    """The listing indices of the other purchase buttons (``purchase_button``) in the pressed control's document."""
    return [c["_i"] for c in controls if c is not pressed and purchase_button(c)]


_MAX_OWNERS = 8  # card frames whose owner <iframe> the box check takes (a checkout has a handful)

# On the pressed control (the controls' isolated world of its document): its box is the nearest ancestor below <body>
# that contains one of ``owners`` (the owner <iframe> elements of this fill's card frames). True when such a box
# exists and one of the listed purchase buttons (``arr[i]`` for ``i`` in ``idx``) is connected and outside it.
PREP_CHECK = r"""function(arr, idx, ...owners){
  const top = [document.body, document.documentElement];
  const live = owners.filter(o => !!o && o.isConnected);
  let box = null;
  for (let n = this.parentElement; n && !top.includes(n); n = n.parentElement) {
    if (live.some(o => n.contains(o))) { box = n; break; }
  }
  if (!box) return false;
  return idx.some(i => { const el = arr[i]; return !!el && el !== this && el.isConnected && !box.contains(el); }); }"""
# On the listing array: every element at ``idx`` is still listed (connected, laid out, visible) and enabled.
# build F r3 (Opus final M1): installed on the listing array right before a preparatory press, in the controls' world:
# any form submit, unload or navigation that STARTS after the press marks it, so an order still in flight at the read
# (the new document not committed yet) re-books the press as the Pay press. Window/document events reach this world.
WATCH_LEAVE = r"""function(){ const s = {left: false}; const mark = () => { s.left = true; };
  window.addEventListener('beforeunload', mark, true); window.addEventListener('pagehide', mark, true);
  document.addEventListener('submit', mark, true);
  try { if (window.navigation) window.navigation.addEventListener('navigate', mark); } catch (e) {}
  this.__hermesPrepLeave = s; return true; }"""

STILL_LISTED = r"""function(idx){ const s = this.__hermesPrepLeave; if (!s || s.left) return false;
  return idx.every(i => { const el = this[i];
  if (!el || !el.isConnected) return false;
  const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
  return r.width >= 1 && r.height >= 1 && st.visibility === 'visible' && st.display !== 'none'
    && !el.disabled && el.getAttribute('aria-disabled') !== 'true'; }); }"""


def _top_children(tree: tuple, frames: frozenset) -> list:
    """For each claimed card frame in ``tree`` (rows ``(id, loader, url, origin, parent)``, the top first): the frame
    on its chain whose parent is the top document (its owner <iframe> is an element of the top document)."""
    parents = {row[0]: row[4] for row in tree}
    top, out = tree[0][0], []
    for fid in sorted(frames):
        hops = 0
        while fid in parents and parents[fid] and parents[fid] != top and hops < len(tree):
            fid, hops = parents[fid], hops + 1
        if parents.get(fid) == top and fid not in out:
            out.append(fid)
    return out[:_MAX_OWNERS]


async def card_owners(sup: Any, sid: str, ctx: Any, tree: tuple, frames: frozenset, into: list) -> None:
    """Resolve the owner <iframe> of each claimed card frame below the top document into the controls' isolated world
    ``ctx`` of the top document (session ``sid``), appending the object ids to ``into`` (the caller releases them).
    A frame whose owner does not resolve is left out (fewer owners: less is preparatory)."""
    if type(ctx) is not int:
        return
    for fid in _top_children(tree, frames):
        try:
            owner = (await sup._cdp("DOM.getFrameOwner", {"frameId": fid}, session_id=sid))["result"]
            obj = (await sup._cdp("DOM.resolveNode", {"backendNodeId": owner["backendNodeId"],
                                                      "executionContextId": ctx}, session_id=sid))["result"]
            into.append(obj["object"]["objectId"])
        except Exception as exc:  # health: allow BLE001 -- no owner: this frame gives no box (fails closed)
            logger.info("payment_click: card frame owner not resolved (%s)", type(exc).__name__)


async def preparatory(sup: Any, sid: str, element: str, arr: str, idx: list, owners: list) -> bool:
    """build F r2: the pressed control sits in the box of a claimed card frame's owner while a purchase button of the
    same listing lies outside it (``PREP_CHECK``). Any failure: False (the press stays the Pay press)."""
    if not idx or not owners:
        return False
    try:
        reply = await sup._cdp("Runtime.callFunctionOn", {
            "objectId": element, "functionDeclaration": PREP_CHECK,
            "arguments": [{"objectId": arr}, {"value": idx}, *({"objectId": o} for o in owners)],
            "returnByValue": True}, session_id=sid)
    except Exception as exc:  # health: allow BLE001 -- unread: the press stays the approval's Pay press
        logger.info("payment_click: preparatory confirm not read (%s)", type(exc).__name__)
        return False
    return ((reply.get("result") or {}).get("result") or {}).get("value") is True


async def still_listed(sup: Any, sid: str, arr: str, idx: list) -> bool:
    """build F r2: after a preparatory press, every purchase button listed before it is still listed and enabled.
    Unread: False (the press is re-booked as the Pay press)."""
    try:
        reply = await sup._cdp("Runtime.callFunctionOn", {"objectId": arr, "functionDeclaration": STILL_LISTED,
                                                          "arguments": [{"value": idx}], "returnByValue": True},
                               session_id=sid)
    except Exception as exc:  # health: allow BLE001 -- unread: the preparatory press counts as the Pay press
        logger.info("payment_click: purchase buttons not read after the confirm (%s)", type(exc).__name__)
        return False
    return ((reply.get("result") or {}).get("result") or {}).get("value") is True




async def prep_args(sup: Any, gen: Any, clicked: tuple, control: dict, doc_controls: list, at: tuple,
                    held: list) -> Optional[tuple]:
    """build F r2: whether a Pay-like press is the approval's one preparatory press instead: its caption only confirms
    (``confirm_only``), it is on the merchant's top document (``clicked[3]``), the approval has a lock record with no
    Pay press and no preparatory press yet, and ``preparatory`` holds. Returns ``(purchase indices, owner objects)``
    to read it again before the press and after it, or None (the Pay press: fails closed). ``at``: ``(session,
    element, listing array, frame tree)``; ``held``: receives the resolved owner objects (the caller releases them)."""
    if not (clicked[3] and confirm_only(control["text"])) or gen is None or gen.paid or gen.prep_used:
        return None
    from tools.browser_payment_done import card_frame_ids

    sid, element, arr, tree = at
    idx = purchase_indices(doc_controls, control)
    if not idx:
        return None
    await card_owners(sup, sid, control.get("_ctx"), tree, card_frame_ids(sup), held)
    return (idx, list(held)) if await preparatory(sup, sid, element, arr, idx, held) else None


async def watch_leave(sup: Any, sid: str, arr: str) -> bool:
    """build F r3: install ``WATCH_LEAVE`` before a preparatory press. False: not installed (the caller keeps the press
    the Pay press: an unwatched submit cannot be taken back)."""
    try:
        reply = await sup._cdp("Runtime.callFunctionOn", {"objectId": arr, "functionDeclaration": WATCH_LEAVE,
                                                          "returnByValue": True}, session_id=sid)
    except Exception as exc:  # health: allow BLE001 -- unwatched: the preparatory press stays the Pay press
        logger.info("payment_click: leave watch not installed (%s)", type(exc).__name__)
        return False
    return ((reply.get("result") or {}).get("result") or {}).get("value") is True


async def prep_verdict(sup: Any, sid: str, arr: str, prep: tuple, effect: Optional[dict],
                       started: Optional[tuple] = None) -> Optional[str]:
    """build F r2: after a preparatory press, why it counts as the Pay press after all (``navigated``: the top
    document changed; ``purchase_button_gone``: a purchase button listed before it is gone or disabled;
    ``effect_unread``; a submit, unload or navigation that started after the press also reads as
    ``purchase_button_gone`` via ``STILL_LISTED``), or None: it stays a preparatory step."""
    if not isinstance(effect, dict):
        return "effect_unread"
    if effect.get("navigated") is not False:
        return "navigated"
    # build F r3 (Codex P1): Chrome's own navigation-start events (``started`` = (top frame id, press time)): a submit
    # still in flight at the read, which a page script could hide from the in-page leave watch
    seen = getattr(sup, "load_started_since", None)
    if started is not None and (seen is None or seen(*started)):
        return "navigated"
    return None if await still_listed(sup, sid, arr, prep[0]) else "purchase_button_gone"


async def pay_node(sup: Any, sid: str, element: str, clicked: tuple) -> tuple:
    """build F r2: ``(frame id, loader, backendNodeId or None, origin + path)`` of the control a Pay (or preparatory)
    press goes to, kept on the lock record for payment_done."""
    from tools.browser_payment_done import _checkout_key
    from tools.browser_payment_surface_after_pay import _backend_id

    try:
        node = await _backend_id(sup, sid, element)
    except Exception as exc:  # health: allow BLE001 -- unread: payment_done cannot tell this element by identity
        logger.info("payment_click: pressed control's node not read (%s)", type(exc).__name__)
        node = None
    return clicked[0], clicked[1], node, _checkout_key(clicked[2])
