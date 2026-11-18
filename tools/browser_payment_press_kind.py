"""Which press through the pay gate is the approval's one Pay press (build C r2, moved here from
``browser_payment_surface``), and (build F, 2026-11-18 Sunday live retry) when a press whose caption only confirms
is a preparatory step instead: Unzer's payment-method modal has "BESTÄTIGEN" (it confirms the card and closes the
modal) while the page's "JETZT KAUFEN" places the order. Counting that confirm as the Pay press used the approval's
one press and refused the real one. Read by ``browser_payment_surface`` (``_click``) and ``browser_payment_done``
(the Pay caption still listed on a confirmation candidate). Only booleans leave the page.
"""

from __future__ import annotations

import logging
import re
from typing import Any

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
_PAY_NOUNS = re.compile(
    r"\b(payment|zahlung|paiement|pago|pagamento|order|bestellung|commande|pedido|ordine|bestelling|purchase|kauf|"
    r"achat|compra|acquisto|booking|buchung|réservation|reserva|prenotazione)\b", re.IGNORECASE)
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


def same_caption(a: Any, b: Any) -> bool:
    """Two captions read as the same control text (case and spacing aside)."""
    return isinstance(a, str) and isinstance(b, str) and " ".join(a.casefold().split()) == \
        " ".join(b.casefold().split())


# On the pressed control (the controls' isolated world of its document): the box it confirms in is the dialog it sits
# in, else the nearest ancestor below <body> that holds a card iframe (a provider's card frame: ``origins``). True when
# such a box exists and one of the listed purchase controls (``arr[i]`` for ``i`` in ``idx``) lies outside it.
_PREP_CHECK = r"""function(arr, idx, origins){
  const top = [document.body, document.documentElement];
  const cardBox = el => {
    for (let n = el.parentElement; n && !top.includes(n); n = n.parentElement) {
      for (const f of n.querySelectorAll('iframe')) {
        let o = ''; try { o = new URL(f.getAttribute('src') || '', location.href).origin; } catch (e) { o = ''; }
        if (origins.includes(o)) return n;
      }
    }
    return null;
  };
  const box = this.closest('__DIALOG__') || cardBox(this);
  if (!box || top.includes(box)) return false;
  return idx.some(i => { const el = arr[i]; return !!el && el !== this && el.isConnected && !box.contains(el); }); }"""


def _prep_check() -> str:
    from tools.browser_payment_surface_after_pay import _DIALOG_SEL

    return _PREP_CHECK.replace("__DIALOG__", _DIALOG_SEL)


def purchase_indices(controls: list, pressed: dict) -> list:
    """The listing indices of the other enabled controls in the pressed control's document that name the purchase."""
    return [c["_i"] for c in controls if c is not pressed and not c["disabled"] and strong_pay(c["text"])]


async def preparatory(sup: Any, sid: str, element: str, arr: str, idx: list, origins: list) -> bool:
    """build F: the pressed control confirms inside a dialog or the card's method box while a purchase control of the
    same document stays outside it (``_PREP_CHECK``). Any failure: False (the press stays the Pay press)."""
    if not idx:
        return False
    try:
        reply = await sup._cdp("Runtime.callFunctionOn", {
            "objectId": element, "functionDeclaration": _prep_check(),
            "arguments": [{"objectId": arr}, {"value": idx}, {"value": origins}], "returnByValue": True},
            session_id=sid)
    except Exception as exc:  # health: allow BLE001 -- unread: the press stays the approval's Pay press
        logger.info("payment_click: preparatory confirm not read (%s)", type(exc).__name__)
        return False
    return ((reply.get("result") or {}).get("result") or {}).get("value") is True
