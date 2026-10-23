"""Managed ``browser_exec`` page-dialog policy and reporting.

A page ``alert``/``confirm``/``prompt``/``beforeunload`` blocks the page's
JavaScript until someone answers it. Under the configured ``must_respond``
policy that was nobody for up to ``dialog_timeout_s`` (300 s): the model's
``js()`` calls timed out and it had no tool to answer (``browser_dialog`` is
hidden in Browser Use mode).

Managed mode arms a decider on the task's CDP supervisor for each exec:

- ``alert`` is closed (it has one button; closing it decides nothing);
- on an ordinary page ``confirm``/``prompt`` are dismissed and
  ``beforeunload`` is accepted (the navigation the code asked for goes on),
  or every dialog follows the call's ``on_dialog`` (``accept``/``dismiss``);
- on a consequential page (this slot holds a card-fill mark, the
  ``sensitive`` identity, a payment-provider frame, or payment/checkout/
  delete/transfer wording in the URL or message) nothing but an alert is
  answered automatically, whatever ``on_dialog`` says: the dialog stays
  pending and is reported, and the agent answers it explicitly with
  ``browser_control action=dialog`` after reading its text.

The exec result carries ``dialogs``: what is still pending and what was
answered during the call.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ON_DIALOG_CHOICES = ("auto", "accept", "dismiss")

# Words that make a dialog consequential when they appear in the page URL or
# the dialog text (English and German: the slots browse German sites).
_CONSEQUENTIAL_RE = re.compile(
    r"checkout|payment|\bpay\b|/pay|billing|purchase|\border\b|/order|bestell|kaufen|kostenpflichtig|zahlung|bezahl|"
    r"subscription|abonnement|\bdelete\b|l[öo]schen|remove account|transfer|[üu]berweis|"
    r"cancel (?:my )?(?:account|subscription|order)|k[üu]ndig|confirm (?:payment|purchase|order)",
    re.IGNORECASE,
)


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
    """Whether a dialog must be held for an explicit answer."""
    if slot_marked or (identity or "").strip().lower() == "sensitive":
        return True
    url = str(getattr(dialog, "url", "") or "")
    if _payment_provider_origin(url):
        return True
    text = f"{url} {getattr(dialog, 'message', '') or ''}"
    return bool(_CONSEQUENTIAL_RE.search(text))


def make_decider(*, on_dialog: str, slot_marked: bool, identity: str) -> Callable[[Any], str]:
    choice = on_dialog if on_dialog in ON_DIALOG_CHOICES else "auto"

    def decide(dialog: Any) -> str:
        kind = str(getattr(dialog, "type", "") or "")
        if kind == "alert":
            return "accept"
        if consequential(dialog, slot_marked=slot_marked, identity=identity):
            return "hold"
        if choice in {"accept", "dismiss"}:
            return choice
        return "accept" if kind == "beforeunload" else "dismiss"

    return decide


def _slot_marked(env: dict) -> bool:
    """Any card-fill mark on this lease's slot (raw marks, no control lookup: fail closed)."""
    try:
        from tools.browser_payment_privacy import slot_has_mark

        return slot_has_mark(str(env.get("HERMES_BROWSER_SLOT_ID") or ""))
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
    supervisor.dialog_decider = make_decider(on_dialog=on_dialog, slot_marked=_slot_marked(env), identity=identity)
    watch = getattr(supervisor, "watch_dialog_tabs", None)
    if callable(watch):
        watch()
    return started


def disarm(task_id: Optional[str], env: dict, *, identity: str) -> None:
    """Back to the default decider between calls (a call's ``on_dialog`` never outlives it)."""
    supervisor = _supervisor(task_id)
    if supervisor is not None:
        supervisor.dialog_decider = make_decider(on_dialog="auto", slot_marked=_slot_marked(env), identity=identity)


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
