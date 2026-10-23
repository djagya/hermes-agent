"""Managed ``browser_exec`` page-dialog policy and reporting.

A page ``alert``/``confirm``/``prompt``/``beforeunload`` blocks the page's
JavaScript until someone answers it. Under the configured ``must_respond``
policy that was nobody for up to ``dialog_timeout_s`` (300 s): the model's
``js()`` calls timed out and it had no tool to answer (``browser_dialog`` is
hidden in Browser Use mode).

Managed mode arms a decider on the task's CDP supervisor for each exec:

- ``alert`` is closed (it has one button; closing it decides nothing);
- ``beforeunload`` is accepted (it only leaves the page) unless the call says
  ``on_dialog=dismiss``, and held on the first exec after a human handoff,
  where it would throw away what Danil typed;
- ``confirm``/``prompt`` are never accepted automatically. On an ordinary page
  they are dismissed (``auto``/``dismiss``) or held for an explicit answer
  (``on_dialog=accept``). On a consequential page they are always held: the
  ``shopping``, ``work:*`` or ``sensitive`` identity, a slot with a card-fill
  mark, a payment-provider frame, or buy/checkout/payment/order/delete/
  transfer/subscription/confirm/cancel wording (EN, DE, FR, ES, IT, NL) in the
  dialog's URL, the tab's top-level URL or the message.

A held dialog is answered with ``browser_control action=dialog`` after the
agent has read its text. The exec result carries ``dialogs``: what is still
pending and what was answered during the call.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ON_DIALOG_CHOICES = ("auto", "accept", "dismiss")

# Words that make a dialog consequential when they appear in the page URLs or the dialog
# text. Over-matching only costs an explicit answer; under-matching can accept an order.
_CONSEQUENTIAL_WORDS = (
    # English
    r"checkout|check-out|payment|\bpay\b|/pay|billing|purchase|\bbuy|\border|/order|subscri|unsubscri|"
    r"\bdelete|\bremove|discard|deactivate|close (?:my |your )?account|\bcancel|\bconfirm|transfer|refund|"
    r"\bcart\b|basket"
    # German
    r"|kasse|warenkorb|bestell|kaufen|\bkauf|kostenpflichtig|zahlung|bezahl|best[äa]tig|abschlie[ßs]|storn|"
    r"entfern|widerruf|l[öo]sch|k[üu]ndig|abonn|[üu]berweis"
    # French
    r"|paiement|payer|commande|acheter|achat|panier|supprimer|confirmer|annuler|d[ée]sabonn|virement"
    # Spanish
    r"|\bpago|pagar|pedido|comprar|\bcompra|carrito|eliminar|borrar|confirmar|cancelar|suscripci|transferencia"
    # Italian
    r"|pagamento|pagare|ordine|acquist|carrello|elimina|conferma|annulla|bonifico"
    # Dutch
    r"|betal|bestelling|kopen|winkelwagen|verwijder|bevestig|annuleer|overboeking|afrekenen"
)
_CONSEQUENTIAL_RE = re.compile(_CONSEQUENTIAL_WORDS, re.IGNORECASE)
_CONSEQUENTIAL_IDENTITIES = ("shopping", "sensitive")

def _origin(url: str) -> str:
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _payment_provider_origin(url: str) -> bool:
    origin = _origin(url)
    if not origin:
        return False
    try:
        from agent.vault_payment_policy import provider_for_origin

        return provider_for_origin(origin) is not None
    except Exception:  # health: allow BLE001 -- policy unavailable: treat as not a provider; keywords still apply
        return False


def consequential(dialog: Any, *, slot_marked: bool, identity: str) -> bool:
    """Whether a confirm/prompt must be held for an explicit answer."""
    ident = (identity or "").strip().lower()
    if slot_marked or ident in _CONSEQUENTIAL_IDENTITIES or ident.startswith("work:"):
        return True
    urls = [str(getattr(dialog, "url", "") or ""), str(getattr(dialog, "top_url", "") or "")]
    if any(_payment_provider_origin(url) for url in urls):
        return True
    text = " ".join(urls + [str(getattr(dialog, "message", "") or "")])
    return bool(_CONSEQUENTIAL_RE.search(text))


def make_decider(*, on_dialog: str, slot_marked: bool, identity: str,
                 after_handoff: bool = False) -> Callable[[Any], str]:
    choice = on_dialog if on_dialog in ON_DIALOG_CHOICES else "auto"

    def decide(dialog: Any) -> str:
        kind = str(getattr(dialog, "type", "") or "")
        if kind == "alert":
            return "accept"
        if kind == "beforeunload":
            if choice == "dismiss":
                return "dismiss"
            # Leaving only drops unsaved input; right after a handoff that input is Danil's.
            return "hold" if after_handoff else "accept"
        if consequential(dialog, slot_marked=slot_marked, identity=identity):
            return "hold"
        # A confirm/prompt is accepted only by an explicit answer after reading it.
        return "hold" if choice == "accept" else "dismiss"

    return decide


# Tasks whose next exec follows a human handoff (minted by browser_control, or a handback wake).
_after_handoff: set[str] = set()
_after_handoff_lock = threading.Lock()


def note_handoff(task_id: Optional[str]) -> None:
    with _after_handoff_lock:
        _after_handoff.add(task_id or "default")


def _take_handoff_flag(task_id: Optional[str]) -> bool:
    with _after_handoff_lock:
        return (task_id or "default") in _after_handoff


def _slot_marked(env: dict) -> bool:
    """Any card-fill mark on this lease's slot, after control-attested wipes are pruned (fail closed)."""
    try:
        from tools.browser_payment_privacy import capture_blocked_for_surface

        return capture_blocked_for_surface(str(env.get("HERMES_BROWSER_SLOT_ID") or ""),
                                           str(env.get("HERMES_BROWSER_GENERATION") or ""))
    except Exception:  # health: allow BLE001 -- unreadable marks: treat the page as consequential
        return True


def _supervisor(task_id: Optional[str]):
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY

        return SUPERVISOR_REGISTRY.get(task_id or "default")
    except Exception:  # health: allow BLE001 -- no supervisor module: nothing to arm
        return None


def arm(task_id: Optional[str], env: dict, *, on_dialog: str, identity: str) -> float:
    """Install this call's decider on the task's supervisor and watch every tab.
    Returns the call's start stamp for :func:`collect`."""
    started = time.time()
    supervisor = _supervisor(task_id)
    if supervisor is None:
        return started
    supervisor.dialog_decider = make_decider(on_dialog=on_dialog, slot_marked=_slot_marked(env), identity=identity,
                                             after_handoff=_take_handoff_flag(task_id))
    watch = getattr(supervisor, "watch_dialog_tabs", None)
    if callable(watch):
        watch()
    return started


def disarm(task_id: Optional[str], env: dict, *, identity: str, dispatched: bool = True) -> None:
    """Back to the default decider between calls (a call's ``on_dialog`` never outlives it). A call
    that reached the browser consumes the post-handoff flag."""
    if dispatched:
        with _after_handoff_lock:
            _after_handoff.discard(task_id or "default")
    supervisor = _supervisor(task_id)
    if supervisor is not None:
        supervisor.dialog_decider = make_decider(on_dialog="auto", slot_marked=_slot_marked(env), identity=identity,
                                                 after_handoff=_take_handoff_flag(task_id))


def collect(task_id: Optional[str], since: float) -> Optional[dict[str, Any]]:
    """``{"pending": [...], "handled": [...]}`` for the exec result, or None when no dialog fired."""
    supervisor = _supervisor(task_id)
    if supervisor is None:
        return None
    try:
        snap = supervisor.snapshot()
    except Exception:  # health: allow BLE001 -- a dead supervisor has nothing to report
        return None
    pending = []
    for dialog in snap.pending_dialogs:
        row = dialog.to_dict()
        row.pop("opened_at", None)
        row["url"] = _origin(getattr(dialog, "url", "")) or ""
        row["page"] = _origin(getattr(dialog, "top_url", "")) or ""
        pending.append(row)
    handled = []
    for record in snap.recent_dialogs:
        if record.opened_at < since - 1:
            continue
        row = record.to_dict()
        for key in ("opened_at", "closed_at"):
            row.pop(key, None)
        handled.append(row)
    if not pending and not handled:
        return None
    out: dict[str, Any] = {"pending": pending, "handled": handled}
    if pending:
        out["hint"] = (
            "A page dialog is waiting and blocks this tab's JavaScript. Read its message, then answer it "
            "with browser_control action=dialog dialog_action=accept|dismiss dialog_id=<id>. Never accept "
            "a payment, order or deletion dialog the user did not confirm for this exact action."
        )
    return out


def respond(task_id: Optional[str], *, action: str, dialog_id: str = "", prompt_text: Optional[str] = None) -> dict:
    """Answer a pending dialog on the task's supervisor (``browser_control action=dialog``)."""
    supervisor = _supervisor(task_id)
    if supervisor is None:
        return {"ok": False, "error": "no browser session is attached for this task; run browser_exec first"}
    return supervisor.respond_to_dialog(action, prompt_text=prompt_text, dialog_id=dialog_id or None)
