"""Human-confirmed native card fill; no values cross the model-facing API."""

from __future__ import annotations

import json

from agent.vault_login_classifier import select_checkout_fills
from agent.vault_payment_card import validate_card
from agent.vault_payment_policy import PAYMENT_PSP_ORIGINS, PaymentPolicyError
from agent.vault_store import PAYMENT_FIELDS

# Fill values whose later appearance in tool output would expose the card. The bare expiry month and
# year and the cardholder name are not secrets on their own and are short or common ("07", "2035",
# your own name): registering them would scrub every later date and name from model-facing output.
_REDACTED_TOKENS = frozenset({"cc-number", "cc-csc", "cc-exp"})


def _register_card_redaction(secret: dict, fills: list) -> None:
    from agent.redact import register_vault_redaction_value

    number = secret["card_number"]
    # Common card formatting is also scrubbed from ordinary browser text reads.
    groups = [number[n : n + 4] for n in range(0, len(number), 4)]
    for value in (number, secret["cvc"], " ".join(groups), "-".join(groups),
                  *(f["value"] for f in fills if f["token"] in _REDACTED_TOKENS)):
        register_vault_redaction_value(value)


def _document_fills(inspected, secret: dict) -> list | None:
    """Fills tagged with their card document, or None when a document's fields cannot all be filled."""
    fills = []
    for k, document in enumerate(inspected.documents):
        document_fills = select_checkout_fills(list(document.controls), secret, PAYMENT_FIELDS)
        if len(document_fills) != len(document.controls):
            return None
        fills += [dict(f, document=k) for f in document_fills]
    return fills


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
            frame_origins=meta.payment_frame_origins or PAYMENT_PSP_ORIGINS,
        )
        prompt = inspected.prompt()
        consent = request_elicitation_consent(
            f"Fill '{meta.label}' on {prompt['merchant_origin']}",
            f"Card fields will be sent to {prompt['target_origin']}. HTTPS and the selected document/fields "
            "were checked. These checks do not establish seller legitimacy: verify the seller and the "
            "checkout terms independently. This one-use approval expires after 120 seconds; the native "
            "tool only fills and never submits payment.",
            surface="vault-payment",
            title="Approve card fill on this merchant?",
        )
        if consent != "accept":
            return json.dumps({"success": False, "error_type": "payment_declined"})
        if not supervisor.payment_is_current(inspected):
            return json.dumps({"success": False, "error_type": "payment_page_changed"})
        # Never resolve at inspection/list time, on decline or on stale consent.
        secret = validate_card(backend.resolve_secret(meta.id))
        fills = _document_fills(inspected, secret)
        _register_card_redaction(secret, fills or [])
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

        mark_payment_session(task_id)
        result = supervisor.commit_payment(inspected, fills)
        result.update(
            kind="payment",
            backend=backend.name,
            origin=inspected.merchant_origin,
            target_origin=inspected.frame_origin,
            target_origins=list(inspected.target_origins),
            fields=prompt["fields"],
            submitted=False,
            page_checks=prompt["checks"],
            merchant_reputation="human_review_required",
        )
        return json.dumps(result)
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
