"""Human-confirmed native card fill; no values cross the model-facing API."""

from __future__ import annotations

import json
import logging
import time

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


def _set_control_capture_guard(route: dict, strict: bool = False) -> dict | None:
    """Ask browser-control to refuse capture on this lease's slot; a refusal result when it did not.

    Fail closed: any failure refuses the fill. Only a control without the route (404) may be let
    through, and only when the operator set ``HERMES_BROWSER_CAPTURE_GUARD_OPTIONAL`` for a rollout.
    dlz: ``strict`` (unreviewed destinations) ignores that rollout exception: the guard holds or no fill.
    """
    from tools.browser_control_route import capture_guard_optional, set_capture_guard

    ok, reason = set_capture_guard(str(route.get("lease") or ""), str(route.get("token") or ""))
    if ok:
        return None
    if reason == "control_without_capture_guard" and not strict and capture_guard_optional():
        logger.warning("card fill: browser-control has no capture guard route; local mark only (rollout)")
        return None
    logger.warning("card fill refused: browser-control capture guard not set (%s%s)", reason,
                   ", strict" if strict else "")
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
# dlz: once a field was written, "nothing was submitted" is not something Hermes can promise for the page.
_SUBMITTED_NOTHING = ("Hermes submitted nothing; page scripts may have acted on the filled fields. Check the page "
                      "before anything else.")
_NOT_RETAINED = (
    "Card fields were written, but after the page settled at least one no longer held its value "
    "(field_retention names which; states and lengths only). This is not a bank or issuer response. "
    + _SUBMITTED_NOTHING + " Do not submit the checkout and do not retry the fill; report it to the user. "
    "Another fill needs fresh consent."
)


def _consent_not_given(verdict: str, reason: str | None) -> dict:
    """The stop result when card-fill consent was not given; ``consent_outcome`` says whether the user
    refused, nobody answered in time, or the prompt could not be shown. None of these is an issuer or
    bank decline. dlz: only an explicit refusal is ``payment_declined`` (stop, never retry); a missing
    answer is ``payment_consent_missing``, which also stops, but a new fill (with its own new prompt)
    may follow once the user is asked."""
    if reason not in _CONSENT_OUTCOMES:  # a stubbed consent without a reason: infer from the verdict
        reason = "timeout" if verdict == "cancel" else "refused"
    if reason == "refused":
        return {
            "success": False,
            "error_type": "payment_declined",
            "consent_outcome": reason,
            "issuer_response": False,
            "error": _CONSENT_MESSAGES[reason] + " This is not a bank or issuer decline. No card value was read "
            "and no field was written. Stop; do not retry the fill.",
        }
    return {
        "success": False,
        "error_type": "payment_consent_missing",
        "consent_outcome": reason,
        "issuer_response": False,
        "error": _CONSENT_MESSAGES[reason] + " No card-fill approval was received; this is not a bank or issuer "
        "decline. No card value was read and no field was written. Stop here. A new fill needs a new consent "
        "prompt; ask the user once whether to try again.",
    }


# dlz: a next step for every typed refusal the model can get from fill_payment (B19).
_SUPERVISOR_REQUIRED = (
    "The card fill needs the supervised browser for this task, and it is not attached. Nothing was read or "
    "written. Open the checkout in the browser tools first, then call fill once more; if this repeats, report "
    "it to the user instead of filling by hand."
)
_NO_ORIGIN = (
    "This saved card is not bound to any merchant site, so it cannot be filled anywhere. Nothing was read or "
    "written. Ask the user to save the card with this merchant's site; never type card values yourself."
)
_PAGE_CHANGED = (
    "The checkout changed after it was inspected (navigation, re-render or a different frame), so no card "
    "field was written and nothing was submitted. Check the page, then call fill once more; it shows a new "
    "approval prompt."
)
_CONSENT_EXPIRED = (
    "The approval arrived (or the fill reached the page) after the 120 s inspection window closed, so no "
    "card field was written and nothing was submitted. Ask the user again: call fill once more for a new "
    "prompt and approve it in time."
)


_CONSENT_EXPIRED_PARTIAL = (
    "The 120 s inspection window closed part-way through the fill: some card fields were written, the rest "
    "were not. " + _SUBMITTED_NOTHING + " Do not submit the checkout and do not retry; tell the user. Another "
    "fill needs a new approval."
)


_TRANSPORT_UNCERTAIN = (
    "The card fill did not finish cleanly (browser transport failed or the fill ran out of time), so some card "
    "fields may hold values and others not. " + _SUBMITTED_NOTHING + " Do not submit the checkout and do not "
    "retry; tell the user. Another fill needs a new approval."
)


_PAGE_CHANGED_PARTIAL = (
    "The checkout changed part-way through the fill (a field was hidden, replaced, relabelled or covered, a frame "
    "changed, or the page navigated), so the fill stopped: some card fields were written, the rest were not. "
    + _SUBMITTED_NOTHING + " Do not submit the checkout and do not retry; tell the user. Another fill needs a "
    "new approval."
)


_PARTIAL_STOPPED = (
    "The card fill stopped part-way: some card fields were written, the rest were not. " + _SUBMITTED_NOTHING
    + " Do not submit the checkout and do not retry; tell the user. Another fill needs a new approval."
)


_BROWSER_CHANGED = (
    "The browser this approval was for is no longer the one Hermes controls for this task (the lease ended, a "
    "human took over, or the browser connection was replaced), so no card field was written and nothing was "
    "submitted. Do not retry on your own; tell the user. Another fill needs a new approval."
)


_ITEM_CHANGED = (
    "The saved card changed before the fill. Nothing was resolved or written; list the vault and request "
    "fresh consent."
)


_ITEM_CHANGED_ON_READ = (
    "The saved card read for this fill is not the revision the approval was shown for (it was edited "
    "meanwhile), so it was dropped and no card field was written. Nothing was submitted. List the vault and "
    "request fresh consent."
)


_REVISION_UNAVAILABLE = (
    "The password manager did not report a revision for this saved card (no version or update time), so an "
    "edit between the approval and the fill could not be detected. Nothing was read or written. Tell the "
    "user; do not retry until the card item is fixed or re-saved."
)


_REVISION_UNAVAILABLE_ON_READ = (
    "The saved card read for this fill carried no revision comparable with the one the approval was shown for, "
    "so it was dropped and no card field was written. Nothing was submitted. Tell the user; do not retry on "
    "your own."
)


_REQUIRES_MANAGED = (
    "A card fill on an unreviewed payment destination needs the managed browser, where browser-control locks "
    "capture; this browser is not managed. Nothing was read or written. Tell the user; do not retry on another "
    "browser."
)


# Appended when a check after the card was read stops the fill: the value never reached the page.
_READ_NOT_WRITTEN = " The card was read inside Hermes for this fill and dropped; no field received it."


def _remaining_seconds(inspected) -> int:
    """Whole seconds left in the inspection window: the consent prompt's own deadline (at least 1)."""
    return max(1, int(inspected.expires_at - time.monotonic()))


def _binds_revisions(backend) -> bool:
    return getattr(backend, "payment_revisions", False) is True


def _payment_item_current(backend, meta) -> bool:
    """Inside ``fill_context``: the card still binds what consent was given for. dlz: every backend is
    re-read (metadata only, never a value); a backend with revisions must report the same revision the prompt
    was shown for. A 1Password listing may come from its 60 s metadata cache; the revision op reports with
    the revealed card is checked again in ``_resolve_card``."""
    from agent.vault_store import revision_verdict
    from tools.browser_vault_context import saved_origins

    fresh = backend.get_meta(meta.id)
    return (fresh is not None and fresh.kind == "payment" and fresh.label == meta.label
            and saved_origins(fresh) == saved_origins(meta)
            and tuple(fresh.payment_frame_origins) == tuple(meta.payment_frame_origins)
            and fresh.payment_scope == meta.payment_scope
            and (not _binds_revisions(backend)
                 or revision_verdict(meta.revision, getattr(fresh, "revision", "")) == "match"))


def _resolve_card(backend, meta) -> tuple[dict | None, str]:
    """The card's raw payload and ``match``, or ``(None, "changed"|"unavailable")`` when the revision read is
    not provably the one the prompt was shown for (the value is dropped here, unwritten). A backend without
    revisions (plugins) relies on its own fill_context and the in-context metadata re-read."""
    from agent.vault_store import revision_verdict

    resolve = getattr(backend, "resolve_payment_secret", None)
    if callable(resolve):
        secret, revision = resolve(meta.id)
    else:  # duck-typed backends (tests, old plugins) without the revision-aware call
        secret, revision = backend.resolve_secret(meta.id), None
    if not _binds_revisions(backend):
        return secret, "match"
    verdict = revision_verdict(meta.revision, revision)
    return (secret, verdict) if verdict == "match" else (None, verdict)


def _route_lease(task_id: str) -> str:
    from tools.browser_use_cli import vault_route

    return str((vault_route(task_id) or {}).get("lease") or "")


def _ownership_refusal(vault, task_id: str, supervisor, inspected, lease: str) -> dict | None:
    """dlz: live ownership, asked of control (not the cached route copy): the lease snapshotted before the
    prompt is still this task's lease, control confirms the agent holds it, and the supervisor that inspected
    the page is the live one. A Bot Desktop human takeover refuses too. None = may proceed."""
    if vault._is_managed():
        changed = {"success": False, "error_type": "payment_browser_changed", "error": _BROWSER_CHANGED}
        # The route first: a new lease must not even get a supervisor attached on this path.
        if not lease or _route_lease(task_id) != lease or inspected.owner is not supervisor:
            return changed
        try:
            live = vault._managed_supervisor(task_id, admit=True)
        except vault._PageUnavailable:
            return changed
        if live is not supervisor:
            return changed
    if vault._bot_desktop_browser_session(task_id):
        from tools.bot_desktop import lease as desktop

        try:
            desktop.assert_agent_may_act()
        except desktop.HumanHasControl:
            return {"success": False, "error_type": "human_has_control", "error": _BROWSER_CHANGED}
    return None


def _expired() -> dict:
    return {"success": False, "error_type": "payment_consent_expired", "error": _CONSENT_EXPIRED}


def fill_payment(backend, meta, task_id: str, merchant_origin: str = "", *, strict: bool = False) -> str:
    """Native card fill after the owner's approval. ``strict`` (the unreviewed-destination feature): a managed
    browser is required, the capture guard's rollout exception does not apply, and each field is hit-tested
    before its setter. Reviewed fills keep ``strict=False``.

    Order after consent (design v2 A6): backend context -> item revision -> live ownership -> page freshness
    -> capture guard and local mark -> expiry -> read the card -> its revision -> fills -> commit fence
    (ownership again, expiry) -> commit with its own per-document and per-setter checks."""
    from tools import browser_vault_tool as vault
    from tools.approval_prompt import request_elicitation_consent

    if strict and not vault._is_managed():
        return json.dumps({"success": False, "error_type": "payment_destination_requires_managed_browser",
                           "error": _REQUIRES_MANAGED})
    try:
        supervisor = vault._ensure_supervisor(task_id)
    except vault._PageUnavailable as exc:
        return json.dumps(exc.result())
    except Exception:  # health: allow BLE001 -- CDP attach errors can carry endpoint credentials; fixed type only
        return json.dumps({"success": False, "error_type": "supervisor_required", "error": _SUPERVISOR_REQUIRED})
    if supervisor is None:
        return json.dumps({"success": False, "error_type": "supervisor_required", "error": _SUPERVISOR_REQUIRED})
    inspected = None
    try:
        bound = tuple(meta.allowed_origins) or ((meta.origin,) if meta.origin else ())
        if not bound and meta.payment_scope != "confirmed_page":
            return json.dumps({"success": False, "error_type": "no_origin", "error": _NO_ORIGIN})
        # dlz: a backend with revisions must name the card's revision now, or no prompt is shown at all.
        if _binds_revisions(backend) and not getattr(meta, "revision", ""):
            return json.dumps({"success": False, "error_type": "payment_item_revision_unavailable",
                               "error": _REVISION_UNAVAILABLE})
        # dlz: the lease this approval is for, snapshotted before the prompt (managed mode).
        lease = _route_lease(task_id) if vault._is_managed() else ""
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
            # dlz: the prompt closes with the inspection window, not the global approval timeout.
            timeout_seconds=_remaining_seconds(inspected),
        )
        if consent != "accept":
            # Stop here: no secret resolved, no field written. A refusal is payment_declined, a missing
            # answer payment_consent_missing; both stop, and consent_outcome says why.
            return json.dumps(_consent_not_given(consent, consent_detail.get("reason")))
        if time.monotonic() >= inspected.expires_at:
            return json.dumps(_expired())
        # The card is resolved and written inside the backend's fill_context (a plugin backend's lock /
        # snapshot / manager session), entered only after consent; an item that changed meanwhile is
        # refused before any value is resolved.
        from tools.browser_vault_context import fill_context

        with fill_context(backend):
            if not _payment_item_current(backend, meta):
                return json.dumps({"success": False, "error_type": "payment_item_changed", "error": _ITEM_CHANGED})
            # dlz: live ownership and page freshness inside the context, right before the card is read.
            refused = _ownership_refusal(vault, task_id, supervisor, inspected, lease)
            if refused:
                return json.dumps(refused)
            if not supervisor.payment_is_current(inspected):
                return json.dumps({"success": False, "error_type": "payment_page_changed", "error": _PAGE_CHANGED})
            # Capture lock (control's CDP guard, then the local mark) BEFORE the card is read: both are
            # set-only and harmless if the fill stops later, and no card byte is read unless they hold.
            from tools.browser_payment_privacy import mark_payment_session

            surface = None
            if vault._is_managed():
                from tools.browser_use_cli import vault_route

                route = vault_route(task_id)
                refused = _set_control_capture_guard(route, strict=strict)
                if refused:
                    return json.dumps(refused)
                surface = {"slot_id": route.get("slot_id", ""),
                           "browser_generation": route.get("browser_generation", ""),
                           "wipe_count": route.get("wipe_count", ""),
                           "profile_kind": route.get("profile_kind", "")}
            mark_payment_session(task_id, surface)
            # dlz: the freshness pass, the guard and the backend context can outlast the window: last check
            # before any secret is read.
            if time.monotonic() >= inspected.expires_at:
                return json.dumps(_expired())
            # Never resolve at inspection/list time, on decline or on stale consent.
            raw, verdict = _resolve_card(backend, meta)
            if raw is None:
                if verdict == "changed":
                    return json.dumps({"success": False, "error_type": "payment_item_changed",
                                       "error": _ITEM_CHANGED_ON_READ})
                return json.dumps({"success": False, "error_type": "payment_item_revision_unavailable",
                                   "error": _REVISION_UNAVAILABLE_ON_READ})
            secret = validate_card(raw)
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
            # Commit fence: a takeover or lease loss while the card was read stops here, before any write.
            refused = _ownership_refusal(vault, task_id, supervisor, inspected, lease)
            if refused:
                refused["error"] += _READ_NOT_WRITTEN
                return json.dumps(refused)
            if time.monotonic() >= inspected.expires_at:
                out = _expired()
                out["error"] += _READ_NOT_WRITTEN
                return json.dumps(out)
            result = supervisor.commit_payment(inspected, fills, strict=strict)
            if result.get("error_type") == "payment_fields_not_retained":
                result["error"] = _NOT_RETAINED
            elif result.get("error_type") in ("payment_page_changed", "payment_fields_changed"):
                # the commit's own freshness, per-document and per-setter checks
                result["error"] = _PAGE_CHANGED_PARTIAL if result.get("partially_filled") else _PAGE_CHANGED
            elif result.get("error_type") == "payment_consent_expired":
                result["error"] = _CONSENT_EXPIRED_PARTIAL if result.get("partially_filled") else _CONSENT_EXPIRED
            elif result.get("error_type") == "payment_transport_uncertain":
                result["error"] = _TRANSPORT_UNCERTAIN
            elif not result.get("success") and result.get("partially_filled"):
                result["error"] = _PARTIAL_STOPPED
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
