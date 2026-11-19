"""Which presses through the pay gate are Pay-like (build C r2, moved here from ``browser_payment_surface``), and
which listed controls are purchase buttons (build F). Build G1 (owner direction 2026-11-19: detect the payment instead
of limiting presses): a Pay-like press is never refused for being another press; it only makes Hermes look for order
evidence afterwards (``browser_payment_detect``) and answers ``pay_pressed``. ``purchase_button`` is read by
``browser_payment_order_evidence`` (a checkout that still offers its purchase button is no confirmation).
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
# build C r2 (P1), G1: which presses through the pay gate are Pay-like (answered pay_pressed, followed by the payment
# detection). A checkbox or radio never is; a button whose caption names a preparatory step (and no pay word) is not,
# nor is a link without a pay word. Everything else is, an unknown or withheld caption included (fails closed: an
# icon-only Pay is still followed by the detection).
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


def _pay_press(role: str, caption: str) -> bool:
    """build C r2 (P1): whether a press through the pay gate is Pay-like (see ``_PAY_WORDS``)."""
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


async def pay_node(sup: Any, sid: str, element: str, clicked: tuple) -> tuple:
    """build F r2: ``(frame id, loader, backendNodeId or None, origin + path)`` of the control a Pay-like press goes
    to, kept on the lock record for payment_done and the payment detection (the identity veto)."""
    from tools.browser_payment_done import _checkout_key
    from tools.browser_payment_surface_after_pay import _backend_id

    try:
        node = await _backend_id(sup, sid, element)
    except Exception as exc:  # health: allow BLE001 -- unread: payment_done cannot tell this element by identity
        logger.info("payment_click: pressed control's node not read (%s)", type(exc).__name__)
        node = None
    return clicked[0], clicked[1], node, _checkout_key(clicked[2])
