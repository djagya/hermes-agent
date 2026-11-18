"""Why a settled shop document after Pay is the order confirmation, or why it is not yet (build F round 2,
2026-11-18 Sunday live retry and its reviews). Read by ``browser_payment_done._probe`` on a document that passed
``_safe_top`` and the value-free snapshot.

- A separate value-free scan of the whole rendered text (title and visible text, no form control, editable region,
  frame or hidden subtree; not the bounded snapshot) answers two booleans: order wording found, failure wording found.
- Failure or negation wording ("Zahlung fehlgeschlagen", "keine Bestellung aufgegeben", "payment declined") is never
  a confirmation, on any path.
- The pressed Pay control itself (its document, ``backendNodeId`` resolving to a connected, rendered element) still
  there: not a confirmation. A recommendation with the same caption on a real thank-you page is another element.
- A checkout-like document (the checkout's own document, the Pay press's document, or a document at either one's
  origin + path) needs positive order wording AND no enabled purchase button listed. Any other shop document is a
  ``new_document``.

Every "not yet" carries a fixed reason code (``REASONS``) that payment_done reports when the wait ends.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger("tools.browser_payment_done")

# What payment_confirmation_not_reached says the tab showed last (fixed codes, never page text).
REASONS = ("pay_control_shown", "failure_wording", "no_order_wording", "purchase_button_shown", "not_shop_page",
           "loading", "page_unread")

# Phrase sources shared by Python (tests, the fake pages) and the page script. ``%L%`` is a letter or digit (Python
# ``[^\W_]``, JavaScript ``[\p{L}\p{N}]`` with the u flag): JavaScript's ``\b`` is ASCII-only ("échoué").
_NOUN = (r"order|purchase|booking|reservation|payment|bestellung|buchung|reservierung|zahlung|einkauf|kauf|auftrag|"
         r"commande|réservation|paiement|achat|pedido|reserva|pago|compra|ordine|prenotazione|pagamento|acquisto|"
         r"bestelling|betaling|boeking|reservering|aankoop")
# Words allowed between the noun and its verb: an order number, auxiliaries, "bei uns", "successfully". No future,
# condition or negation ("will be placed", "wird bestätigt", "once", "not"): those never read as done.
_FILL = (r"#?\s?[a-z0-9-]*\d[a-z0-9-]*|nr\.?|no\.|n°|has|have|been|is|was|were|now|successfully|already|ist|wurde|"
         r"wurden|bei|uns|soeben|hiermit|a|été|est|bien|ha|sido|fue|está|è|stato|stata|werd|succesvol|met succes")
_VERB = (r"confirmed|placed|received|complete|completed|successful|succeeded|accepted|processed|eingegangen|"
         r"bestätigt|erhalten|aufgegeben|abgeschlossen|erfolgreich|angenommen|ausgeführt|durchgeführt|confirmée|"
         r"confirmé|validée|validé|enregistrée|enregistré|reçue|reçu|effectuée|effectué|réussie|réussi|confirmado|"
         r"confirmada|realizado|realizada|recibido|recibida|completado|completada|procesado|procesada|confermato|"
         r"confermata|ricevuto|ricevuta|completato|completata|riuscito|riuscita|effettuato|effettuata|geplaatst|"
         r"ontvangen|bevestigd|gelukt|geslaagd|voltooid")
_THANKS = (r"(?:thank you|thanks) for your (?:order|purchase|booking|reservation|payment)|"
           r"(?:vielen )?dank(?:e)? für (?:ihre|deine|ihren|deinen) (?:bestellung|buchung|reservierung|einkauf|kauf|"
           r"auftrag|zahlung)|merci (?:pour|de) votre (?:commande|achat|réservation)|"
           r"gracias por (?:tu|su) (?:pedido|compra|reserva)|grazie per (?:il tuo |il vostro |l['’])?(?:ordine|acquisto)|"
           r"bedankt voor (?:je|jouw|uw) (?:bestelling|aankoop|boeking)")
_NUMBER = (r"bestellnummer|auftragsnummer|buchungsnummer|order number|order no\.|booking number|booking reference|"
           r"confirmation number|numéro de commande|número de pedido|numero d['’]ordine|numero ordine|bestelnummer|"
           r"ordernummer")
ORDER_SRC = (rf"(?<!%L%)(?:(?:{_NOUN})(?:\s+(?:{_FILL})){{0,4}}\s+(?:{_VERB})|{_THANKS}|{_NUMBER})(?!%L%)")
FAILURE_SRC = (
    r"(?<!%L%)(?:fehlgeschlagen|abgelehnt|abgebrochen|zahlungsfehler|keine bestellung|"
    r"nicht\s+(?:%L%+\s+){0,2}?(?:erfolgreich|aufgegeben|abgeschlossen|durchgeführt|ausgeführt|bearbeitet|bestätigt|"
    r"möglich)|failed|declined|unsuccessful|not\s+(?:been\s+)?(?:placed|completed|complete|processed|successful|"
    r"confirmed|accepted)|no order|error|échoué|échouée|échec|refusé|refusée|n['’]a pas abouti|fallido|fallida|"
    r"rechazado|rechazada|non\s+(?:è\s+)?(?:riuscito|riuscita|andato a buon fine)|rifiutato|rifiutata|mislukt|"
    r"geweigerd|niet gelukt)(?!%L%)")


def _py(src: str) -> re.Pattern:
    return re.compile(src.replace("%L%", r"[^\W_]"), re.IGNORECASE)


ORDER = _py(ORDER_SRC)
FAILURE = _py(FAILURE_SRC)

# Runs in a fresh isolated world of the candidate document: the title and every visible text node (open shadow roots
# included; form controls, editable regions, frames, scripts and hidden subtrees skipped, as in the snapshot) up to
# far larger bounds than the snapshot's excerpt. Only two booleans leave the page.
SCAN_JS = r"""(() => {
  const ORDER = new RegExp(%ORDER%, 'iu'), FAILURE = new RegExp(%FAILURE%, 'iu');
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'INPUT', 'TEXTAREA', 'SELECT', 'OPTION',
    'OPTGROUP', 'DATALIST', 'IFRAME', 'FRAME', 'FRAMESET', 'OBJECT', 'EMBED', 'CANVAS']);
  const shown = (el) => { const s = getComputedStyle(el); return !s || (s.display !== 'none' && s.visibility !== 'hidden'); };
  const out = [String(document.title || '')]; let size = 0, budget = 400000;
  const walk = (node, depth) => {
    if (size >= 4000000 || --budget < 0 || depth > 200) return;
    if (node.nodeType === 3) { const t = String(node.data); size += t.length; out.push(t); return; }
    if (node.nodeType !== 1 && node.nodeType !== 11) return;
    let tag = '';
    if (node.nodeType === 1) {
      tag = String(node.tagName).toUpperCase();
      if (SKIP.has(tag) || node.isContentEditable === true || !shown(node)) return;
    }
    let kids = node.childNodes || [];
    if (node.nodeType === 1 && node.shadowRoot) kids = [node.shadowRoot];
    else if (tag === 'SLOT' && typeof node.assignedNodes === 'function') {
      const assigned = node.assignedNodes({flatten: true});
      if (assigned.length) kids = assigned;
    }
    for (let i = 0; i < kids.length; i++) walk(kids[i], depth + 1);
  };
  const root = document.body || document.documentElement;
  if (root) walk(root, 0);
  const text = out.join(' ').replace(/\s+/g, ' ');
  return {order: ORDER.test(text), failure: FAILURE.test(text)};
})()""".replace("%ORDER%", json.dumps(ORDER_SRC.replace("%L%", r"[\p{L}\p{N}]"))).replace(
    "%FAILURE%", json.dumps(FAILURE_SRC.replace("%L%", r"[\p{L}\p{N}]")))

# On the pressed Pay element (resolved by its backendNodeId): still connected and rendered.
_SHOWN = r"""function(){ if (!this.isConnected) return false; const r = this.getBoundingClientRect();
  const st = getComputedStyle(this);
  return r.width >= 1 && r.height >= 1 && st.visibility === 'visible' && st.display !== 'none'; }"""


def wording(title: str, text: str) -> dict:
    """The scan's answer for a title and text (the page script's regexes, in Python): tests and fakes."""
    joined = " ".join(f"{title} {text}".split())
    return {"order": bool(ORDER.search(joined)), "failure": bool(FAILURE.search(joined))}


async def _scan(sup: Any, sid: str, frame_id: str) -> Optional[dict]:
    """``{"order": bool, "failure": bool}`` from ``SCAN_JS``, or None when it is not exactly that (unread)."""
    try:
        ctx = (await sup._cdp("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "hermes-payment-done"},
                              session_id=sid))["result"]["executionContextId"]
        reply = (await sup._cdp("Runtime.evaluate", {"expression": SCAN_JS, "contextId": ctx, "returnByValue": True},
                                session_id=sid, timeout=10))["result"]
    except Exception as exc:  # health: allow BLE001 -- unread: no confirmation on this probe (fails closed)
        logger.info("payment_done: order wording not read (%s)", type(exc).__name__)
        return None
    value = (reply.get("result") or {}).get("value") if not reply.get("exceptionDetails") else None
    if not (isinstance(value, dict) and set(value) == {"order", "failure"}
            and all(type(v) is bool for v in value.values())):
        return None
    return value


async def _pay_control_shown(sup: Any, sid: str, frame_id: str, node: int) -> bool:
    """Whether the pressed Pay element (``node``, a backendNodeId of this document) is still connected and rendered.
    A node that no longer resolves is gone; a resolved node whose state cannot be read counts as still there."""
    try:
        ctx = (await sup._cdp("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "hermes-payment-done"},
                              session_id=sid))["result"]["executionContextId"]
        obj = (await sup._cdp("DOM.resolveNode", {"backendNodeId": node, "executionContextId": ctx},
                              session_id=sid))["result"]["object"]["objectId"]
    except Exception as exc:  # health: allow BLE001 -- a node that does not resolve is not on the page any more
        logger.info("payment_done: pressed Pay control not resolved (%s)", type(exc).__name__)
        return False
    try:
        reply = (await sup._cdp("Runtime.callFunctionOn", {"objectId": obj, "functionDeclaration": _SHOWN,
                                                           "returnByValue": True}, session_id=sid))["result"]
        return (reply.get("result") or {}).get("value") is not False
    except Exception as exc:  # health: allow BLE001 -- unread state of a resolved node: still there (fails closed)
        logger.info("payment_done: pressed Pay control not read (%s)", type(exc).__name__)
        return True
    finally:
        await sup._release_payment_object(sid, obj)


async def _purchase_button_listed(sup: Any, sid: str, row: tuple) -> bool:
    """An enabled purchase button among the controls payment_controls lists on the document (captions only, as
    listed for the model; never an input value)."""
    from tools.browser_payment_press_kind import purchase_button
    from tools.browser_payment_surface import _listing

    arr, controls = await _listing(sup, row[0], sid, f"{row[0]}|{row[1]}", row[1], True, "")
    await sup._release_payment_object(sid, arr)
    return any(purchase_button(c) for c in controls)


async def order_evidence(sup: Any, sid: str, row: tuple, checkout: bool, pay: Optional[dict]) -> tuple:
    """``(evidence, reason)``: ``("confirmation_text" | "new_document", None)`` for a confirmation, else
    ``(None, reason code)``. ``row``: the candidate's top frame row; ``checkout``: it is checkout-like (the caller
    knows the checkout's loaders and URLs); ``pay``: the Pay press of this lock (``pay_press``), or None."""
    scan = await _scan(sup, sid, row[0])
    if scan is None:
        return None, "page_unread"
    if scan["failure"]:
        return None, "failure_wording"
    if (pay is not None and isinstance(pay.get("node"), int) and (pay.get("frame"), pay.get("loader")) == row[:2]
            and await _pay_control_shown(sup, sid, row[0], pay["node"])):
        return None, "pay_control_shown"
    if not checkout:
        return "new_document", None
    if not scan["order"]:
        return None, "no_order_wording"
    if await _purchase_button_listed(sup, sid, row):
        return None, "purchase_button_shown"
    return "confirmation_text", None
