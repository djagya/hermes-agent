"""Human-confirmed native card fill; no values cross the model-facing API."""

from __future__ import annotations

import json
import logging

from agent.vault_backends import BackendError
from agent.vault_login_classifier import match_expiry_option, select_checkout_fills
from agent.vault_payment_card import validate_card
from agent.vault_payment_policy import PaymentPolicyError, effective_frame_origins
from agent.vault_store import PAYMENT_FIELDS
from tools.browser_supervisor_payment import OPTIONAL_CARD_TOKENS

logger = logging.getLogger(__name__)


def _register_card_redaction(secret: dict) -> None:
    """Register the card secrets for redaction: the number (also grouped with spaces or dashes, as
    card inputs reformat it) and CVC. Holder name, expiry and billing postcode are not secrets on
    their own; the scrub is an exact-substring replace over every later tool result, so registering
    them would scrub common names, dates and postcodes from model-facing output."""
    from agent.redact import register_vault_redaction_value

    number = str(secret.get("card_number") or "")
    digits = "".join(ch for ch in number if ch.isdigit())
    groupings = [[digits[i : i + 4] for i in range(0, len(digits), 4)]]
    if len(digits) in (14, 15):  # Diners Club prints 4-6-4, American Express 4-6-5
        groupings.append([digits[:4], digits[4:10], digits[10:]])
    values = [number, digits, str(secret.get("cvc") or "")]
    values += [sep.join(groups) for groups in groupings for sep in (" ", "-")]
    for value in dict.fromkeys(values):
        if value:
            register_vault_redaction_value(value)


class ExpiryOptionUnmatched(Exception):
    """An expiry ``<select>`` has no single option for the card's month or year. Carries no value."""


def _document_fills(inspected, secret: dict) -> list | None:
    """Fills tagged with their card document, or None when a required field cannot be filled. An
    optional field (holder name, billing postcode) the saved card has no value for is skipped. An
    expiry ``<select>`` takes the value of its one matching option; zero or several matching options
    in ANY document raise ExpiryOptionUnmatched before anything is written anywhere."""
    fills = []
    for k, document in enumerate(inspected.documents):
        document_fills = select_checkout_fills(list(document.controls), secret, PAYMENT_FIELDS)
        filled = {f["index"] for f in document_fills}
        if any(c.control.index not in filled and c.token not in OPTIONAL_CARD_TOKENS for c in document.controls):
            return None
        controls = {c.control.index: c.control for c in document.controls}
        for f in document_fills:
            control = controls[f["index"]]
            if control.type == "select":
                option = match_expiry_option(f["token"], f["value"], control.options)
                if option is None:
                    raise ExpiryOptionUnmatched()
                f["value"] = option
        fills += [dict(f, document=k) for f in document_fills]
    return fills


def _set_control_capture_guard(route: dict) -> dict | None:
    """Ask browser-control to refuse capture on this lease's slot; a refusal result when it did not.

    Fail closed: any failure refuses the fill. Only a control without the route (404) may be let
    through, and only when the operator set ``HERMES_BROWSER_CAPTURE_GUARD_OPTIONAL`` for a rollout.
    """
    from tools.browser_control_route import capture_guard_optional, set_capture_guard

    ok, reason = set_capture_guard(str(route.get("lease") or ""), str(route.get("token") or ""))
    if ok:
        return None
    if reason == "control_without_capture_guard" and capture_guard_optional():
        logger.warning("card fill: browser-control has no capture guard route; local mark only (rollout)")
        return None
    logger.warning("card fill refused: browser-control capture guard not set (%s)", reason)
    return {
        "success": False,
        "error_type": "payment_capture_guard_unavailable",
        "error": "Browser control could not lock screen capture on this browser, so no card field was "
        "written. Nothing was submitted. Report this to Danil; do not retry on another route.",
    }


_EXPIRY_UNMATCHED = (
    "The expiry month or year list on this checkout has no single option matching the saved card, so no "
    "card field was written and nothing was submitted. Do not retry; ask the user to fill the card by hand "
    "or check the saved expiry."
)
_CONSENT_OUTCOMES = frozenset({"refused", "timeout", "unavailable"})
_CONSENT_MESSAGES = {
    "refused": "The user did not approve the card fill.",
    "timeout": "Nobody answered the card-fill approval in time.",
    "unavailable": "The card-fill approval could not be shown to the user.",
}
_NOT_RETAINED = (
    "Card fields were written, but after the page settled at least one no longer held its value "
    "(field_retention names which; states and lengths only). This is not a bank or issuer response and "
    "nothing was submitted. Do not submit the checkout and do not retry the fill; report it to the user. "
    "Another fill needs fresh consent."
)


def _consent_not_given(verdict: str, reason: str | None) -> dict:
    """The stop result when card-fill consent was not given. ``error_type`` stays ``payment_declined``
    (callers stop on it and never retry); ``consent_outcome`` says whether the user refused, nobody
    answered in time, or the prompt could not be shown. None of these is an issuer or bank decline."""
    if reason not in _CONSENT_OUTCOMES:  # a stubbed consent without a reason: infer from the verdict
        reason = "timeout" if verdict == "cancel" else "refused"
    return {
        "success": False,
        "error_type": "payment_declined",
        "consent_outcome": reason,
        "issuer_response": False,
        "error": _CONSENT_MESSAGES[reason] + " This is not a bank or issuer decline. No card value was read "
        "and no field was written. Stop; do not retry the fill.",
    }


def _payment_item_unchanged(backend, meta) -> bool:
    """Inside ``fill_context``: the card still binds what consent was given for. Only a backend with its own
    context is re-read (``tools.browser_vault_context.snapshot_refusal`` rule); built-in managers freeze nothing."""
    from tools.browser_vault_context import _has_own_context, saved_origins

    if not _has_own_context(backend):
        return True
    fresh = backend.get_meta(meta.id)
    return (fresh is not None and fresh.kind == "payment" and saved_origins(fresh) == saved_origins(meta)
            and tuple(fresh.payment_frame_origins) == tuple(meta.payment_frame_origins)
            and fresh.payment_scope == meta.payment_scope)


def fill_payment(backend, meta, task_id: str, merchant_origin: str = "") -> str:
    from tools import browser_vault_tool as vault
    from tools.approval_prompt import request_elicitation_consent

    try:
        supervisor = vault._ensure_supervisor(task_id)
    except vault._PageUnavailable as exc:
        return json.dumps(exc.result())
    except Exception:  # health: allow BLE001 -- CDP attach errors can carry endpoint credentials; fixed type only
        return json.dumps({"success": False, "error_type": "supervisor_required"})
    if supervisor is None:
        return json.dumps({"success": False, "error_type": "supervisor_required"})
    inspected = None
    try:
        bound = tuple(meta.allowed_origins) or ((meta.origin,) if meta.origin else ())
        if not bound and meta.payment_scope != "confirmed_page":
            return json.dumps({"success": False, "error_type": "no_origin"})
        inspected = supervisor.inspect_payment(
            merchant_origin,
            bound_origins=bound,
            frame_origins=effective_frame_origins(meta.payment_frame_origins),
        )
        prompt = inspected.prompt()
        consent_detail: dict = {}
        consent = request_elicitation_consent(
            f"Fill '{meta.label}' on {prompt['merchant_origin']}",
            f"Card fields will be sent to {prompt['target_origin']}. HTTPS and the selected document/fields "
            "were checked. These checks do not establish seller legitimacy: verify the seller and the "
            "checkout terms independently. This one-use approval expires after 120 seconds; the native "
            "tool only fills and never submits payment.",
            surface="vault-payment",
            title="Approve card fill on this merchant?",
            outcome=consent_detail,
        )
        if consent != "accept":
            # Stop here: no secret resolved, no field written. Same error_type as before, so every caller
            # that stops on payment_declined still stops; consent_outcome says why.
            return json.dumps(_consent_not_given(consent, consent_detail.get("reason")))
        if not supervisor.payment_is_current(inspected):
            return json.dumps({"success": False, "error_type": "payment_page_changed"})
        # The card is resolved and written inside the backend's fill_context (a plugin backend's lock /
        # snapshot / manager session), entered only after consent; a plugin item that changed meanwhile
        # is refused before any value is resolved.
        from tools.browser_vault_context import fill_context

        with fill_context(backend):
            if not _payment_item_unchanged(backend, meta):
                return json.dumps({"success": False, "error_type": "payment_item_changed",
                                   "error": "The saved card changed before the fill. Nothing was resolved "
                                            "or written; list the vault and request fresh consent."})
            # Never resolve at inspection/list time, on decline or on stale consent.
            secret = validate_card(backend.resolve_secret(meta.id))
            _register_card_redaction(secret)
            try:
                fills = _document_fills(inspected, secret)
            except ExpiryOptionUnmatched:
                return json.dumps({
                    "success": False,
                    "error_type": "payment_expiry_option_unmatched",
                    "error": _EXPIRY_UNMATCHED,
                })
            if fills is None:
                return json.dumps({
                    "success": False,
                    "error_type": "payment_fields_incomplete",
                })
            # Re-admit at the write if a desktop user took control during consent.
            if vault._bot_desktop_browser_session(task_id):
                from tools.bot_desktop import lease

                lease.assert_agent_may_act()
            from tools.browser_payment_privacy import mark_payment_session

            surface = None
            if vault._is_managed():
                from tools.browser_use_cli import vault_route

                route = vault_route(task_id)
                # Control's CDP-level capture guard first; no card byte is written unless it holds.
                refused = _set_control_capture_guard(route)
                if refused:
                    return json.dumps(refused)
                surface = {"slot_id": route.get("slot_id", ""),
                           "browser_generation": route.get("browser_generation", ""),
                           "wipe_count": route.get("wipe_count", ""),
                           "profile_kind": route.get("profile_kind", "")}
            mark_payment_session(task_id, surface)
            result = supervisor.commit_payment(inspected, fills)
            if result.get("error_type") == "payment_fields_not_retained":
                result["error"] = _NOT_RETAINED
            result.update(
                kind="payment",
                backend=backend.name,
                origin=inspected.merchant_origin,
                target_origin=inspected.frame_origin,
                target_origins=list(inspected.target_origins),
                fields=sorted(f["token"] for f in fills),
                skipped_optional=sorted(set(prompt["fields"]) - {f["token"] for f in fills}),
                submitted=False,
                page_checks=prompt["checks"],
                merchant_reputation="human_review_required",
            )
            return json.dumps(result)
    except BackendError as exc:
        if type(exc) is BackendError:  # unclassified op failure: keep the opaque refusal below
            return json.dumps({
                "success": False,
                "error_type": "payment_fill_refused",
                "error": "Card resolution or page checks failed; nothing was submitted. Inspect the page and request fresh consent.",
            })
        return json.dumps(exc.result())
    except PaymentPolicyError as exc:
        return json.dumps({
            "success": False,
            "error_type": "payment_policy_refused",
            "error": str(exc) + "; inspect the checkout and request fresh consent.",
        })
    except Exception:
        # op/JS/transport exceptions can contain values. Do not interpolate them.
        return json.dumps({
            "success": False,
            "error_type": "payment_fill_refused",
            "error": "Card resolution or page checks failed; nothing was submitted. Inspect the page and request fresh consent.",
        })
    finally:
        if inspected is not None and not inspected.consumed:
            try:
                supervisor.discard_payment(inspected)
            except Exception:
                # Disconnected contexts have already lost their remote references.
                pass
