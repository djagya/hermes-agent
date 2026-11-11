"""Human-confirmed native card fill; no values cross the model-facing API."""

from __future__ import annotations

import json
import logging
import re
import time

from agent.vault_backends import BackendError
from agent.vault_login_classifier import match_expiry_option, select_checkout_fills
from agent.vault_payment_card import validate_card
from agent.vault_payment_policy import (
    DESTINATION_OFF_HINT,
    PaymentDestinationError,
    PaymentPolicyError,
    destination_policy,
    effective_frame_origins,
)
from agent.vault_store import PAYMENT_FIELDS, revision_binding
from tools.browser_supervisor_payment import _COVERAGE_REFUSALS, OPTIONAL_CARD_TOKENS

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
    "The card fill did not finish cleanly (browser transport failed, the page answered in an unexpected shape, "
    "or the fill ran out of time), so some card fields may hold values and others not. " + _SUBMITTED_NOTHING
    + " Do not submit the checkout and do not retry; tell the user. Another fill needs a new approval."
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


# dlz (Track B): owner-approved fills on unreviewed hosted payment forms. Fixed texts, each naming the next step.
_APPROVAL_REQUIRED = (
    "Card fields on this checkout go to a payment form Hermes has not reviewed (receiving lists each origin and the "
    "fields it would get). Nothing was read or written. Tell the user which origins would receive the card. Then call "
    "fill again with cart (the items, quantities, total and currency exactly as the checkout shows them; the user sees "
    "them marked as stated by you, next to Hermes' page check) and approve_origins set to exactly approval_origins. "
    "That shows the user one approval card naming these origins; only the user's own button approves it."
)
_DESTINATION_CHANGED = (
    "approve_origins does not match the origins that would receive the card on the page now (approval_origins). "
    "Nothing was read or written and no approval was shown. Check the checkout, tell the user the current origins, "
    "then call fill again with approve_origins equal to approval_origins."
)
_DESTINATION_DISABLED = (
    "Owner-approved card fills on unreviewed payment forms were switched off, or their policy changed, while this "
    "fill was in progress, so no card field was written and nothing was submitted. Do not retry; tell the user."
)
_CART_CHANGED = (
    "The checkout's total text changed after the approval card was built (Hermes' page check no longer reads the "
    "same), so no card field was written and nothing was submitted. Check the cart, then call fill again with the "
    "current cart; it shows a new approval."
)
_CART_INVALID = (
    "cart must be {items: [{description: 1-80 characters, quantity: 1-99}] (1-8 items), total: a decimal with two "
    "places such as \"22.66\", currency: a three-letter code such as \"EUR\"}. Nothing was read or written. Call fill "
    "again with a valid cart, or without one."
)
_CART_TOTAL = re.compile(r"(0|[1-9][0-9]{0,6})\.[0-9]{2}")
_CART_CURRENCY = re.compile(r"[A-Z]{3}")


def _stated_cart(cart):
    """dlz (B5): the cart exactly as Sera stated it, as a ``StatedCart``, or None when it is missing or invalid.
    Nothing here is checked against the page except the total, separately, as a transcription sanity check."""
    from tools.payment_consent_prompt import CartItem, FactsInvalid, StatedCart

    if isinstance(cart, str):  # some model clients send a nested object as JSON text
        try:
            cart = json.loads(cart)
        except ValueError:
            return None
    if not isinstance(cart, dict) or set(cart) != {"items", "total", "currency"}:
        return None
    items, total, currency = cart["items"], cart["total"], cart["currency"]
    if not (isinstance(total, str) and _CART_TOTAL.fullmatch(total)
            and isinstance(currency, str) and _CART_CURRENCY.fullmatch(currency)):
        return None
    if not isinstance(items, list) or not 1 <= len(items) <= 8 or any(
            not isinstance(i, dict) or set(i) != {"description", "quantity"} or type(i["quantity"]) is not int
            or not isinstance(i["description"], str) or not 1 <= len(i["description"]) <= 80 for i in items):
        return None
    try:
        return StatedCart(items=tuple(CartItem(i["description"], i["quantity"]) for i in items), total=total,
                          currency=currency)
    except FactsInvalid:
        return None


def _cents(stated) -> tuple:
    return int(stated.total.replace(".", "")), stated.currency


def _origin_set(value) -> list | None:
    """``approve_origins`` as a list of distinct strings, or None when missing or malformed."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, list) or not value or len(value) > 8 or any(not isinstance(o, str) for o in value):
        return None
    return value if len(set(value)) == len(value) else None


def _is_coverage_refusal(exc: PaymentPolicyError) -> bool:
    """The reviewed pass found no complete reviewed card document (coverage), as opposed to a merchant or ancestry
    mismatch, a changed page or a typed refusal: only coverage may lead to the discovery pass."""
    return type(exc) is PaymentPolicyError and str(exc).split(" (", 1)[0] in _COVERAGE_REFUSALS


def _destination_gate(inspected, stated, approve_origins) -> dict | None:
    """dlz (B7, B8 steps 5-6): None when the stated cart is valid and ``approve_origins`` equals the one approval-origin
    set; else the typed result (no prompt, nothing read). ``approval_origins`` is EVERY origin that receives a card
    field, the merchant included when its document holds fields, in document order."""
    merchant = inspected.merchant_origin
    approval_origins = list(inspected.target_origins)
    receiving = []
    for origin in approval_origins:
        tokens = sorted({c.token for d in inspected.documents if d.origin == origin for c in d.controls})
        receiving.append({"origin": origin, "reviewed": "merchant" if origin == merchant
                          else origin not in inspected.unreviewed_origins, "fields": tokens})
    base = {"success": False, "merchant_origin": merchant, "approval_origins": approval_origins,
            "receiving": receiving, "documents": len(inspected.documents)}
    approve = _origin_set(approve_origins)
    needs = (["cart"] if stated is None else []) + (["approve_origins"] if approve is None else [])
    if needs:
        return {**base, "error_type": "payment_destination_approval_required", "needs": needs,
                "error": _APPROVAL_REQUIRED}
    if set(approve) != set(approval_origins):
        return {**base, "error_type": "payment_destination_changed", "error": _DESTINATION_CHANGED}
    return None


_CART_CHANGED_PARTIAL = (
    "The checkout's total text changed part-way through the fill (Hermes' page check no longer reads the same), so "
    "the fill stopped: some card fields were written, the rest were not. " + _SUBMITTED_NOTHING + " Do not submit the "
    "checkout and do not retry; tell the user. Another fill needs a new approval."
)
_DESTINATION_DISABLED_PARTIAL = (
    "Owner-approved card fills on unreviewed payment forms were switched off, or their policy changed, part-way "
    "through the fill, so it stopped: some card fields were written, the rest were not. " + _SUBMITTED_NOTHING
    + " Do not submit the checkout and do not retry; tell the user."
)


def _disabled(after_read: bool = False) -> dict:
    return {"success": False, "error_type": "payment_destination_disabled",
            "error": _DESTINATION_DISABLED + (_READ_NOT_WRITTEN if after_read else "")}


def _remaining_seconds(inspected) -> int:
    """Whole seconds left in the inspection window: the consent prompt's own deadline (at least 1)."""
    return max(1, int(inspected.expires_at - time.monotonic()))


def _binds_revisions(backend) -> bool:
    return getattr(backend, "payment_revisions", False) is True


def _bound_revision(backend, meta) -> str:
    """dlz (A8): the revision a fill binds, from metadata only: ``backend.payment_revision`` (1Password looks the
    item's ``version`` up when its listing has none), else the listed ``meta.revision``."""
    lookup = getattr(backend, "payment_revision", None)
    return str((lookup(meta) if callable(lookup) else getattr(meta, "revision", "")) or "")


def _payment_item_current(backend, meta, bound: str) -> bool:
    """Inside ``fill_context``: the card still binds what consent was given for. dlz: every backend is
    re-read (metadata only, never a value); a backend with revisions must report the revision ``bound`` the
    prompt was shown for, through the same lookup. The revision op reports with the revealed card is checked
    again in ``_resolve_card``."""
    from agent.vault_store import revision_verdict
    from tools.browser_vault_context import saved_origins

    fresh = backend.get_meta(meta.id)
    return (fresh is not None and fresh.kind == "payment" and fresh.label == meta.label
            and saved_origins(fresh) == saved_origins(meta)
            and tuple(fresh.payment_frame_origins) == tuple(meta.payment_frame_origins)
            and fresh.payment_scope == meta.payment_scope
            and (not _binds_revisions(backend)
                 or revision_verdict(bound, _bound_revision(backend, fresh)) == "match"))


def _resolve_card(backend, meta, bound: str) -> tuple[dict | None, str]:
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
    verdict = revision_verdict(bound, revision)
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


def _discover(supervisor, vault, reviewed: PaymentPolicyError, merchant_origin: str, bound: tuple,
              frame_origins: tuple, witness: dict):
    """dlz (Track B, B8 steps 1-4): after the reviewed pass refused with ``reviewed``: ``(inspection, policy)`` from
    the discovery pass, or a typed result dict. Raises the reviewed refusal again when it was not about coverage,
    when the switch is off (with its hint) or when discovery found no unreviewed receiving origin."""
    if not _is_coverage_refusal(reviewed):
        raise reviewed
    policy = destination_policy()
    if not policy[0]:
        raise PaymentPolicyError(f"{reviewed}; {DESTINATION_OFF_HINT}") from None
    if not vault._is_managed():
        return {"success": False, "error_type": "payment_destination_requires_managed_browser",
                "error": _REQUIRES_MANAGED}
    try:
        inspected = supervisor.inspect_payment(merchant_origin, bound_origins=bound, frame_origins=frame_origins,
                                               unreviewed=True, **witness)
    except PaymentDestinationError as found:
        coverage = getattr(reviewed, "coverage", "")
        if found.error_type == "payment_fields_unsupported" and coverage:
            # The reviewed pass's provider-level hint: registry origins and fixed text, never a page path (#8).
            found.detail["reviewed_refusal"] = coverage
        raise
    if not inspected.unreviewed_origins:
        # Discovery found no unreviewed receiving origin: the reviewed refusal stands as it was.
        supervisor.discard_payment(inspected)
        raise reviewed from None
    return inspected, policy


# dlz: the model-facing text for each commit stop: (nothing written, something written).
_COMMIT_TEXTS = {
    "payment_page_changed": (_PAGE_CHANGED, _PAGE_CHANGED_PARTIAL),
    "payment_fields_changed": (_PAGE_CHANGED, _PAGE_CHANGED_PARTIAL),
    "payment_consent_expired": (_CONSENT_EXPIRED, _CONSENT_EXPIRED_PARTIAL),
    "payment_transport_uncertain": (_TRANSPORT_UNCERTAIN, _TRANSPORT_UNCERTAIN),
    "payment_result_invalid": (_TRANSPORT_UNCERTAIN, _TRANSPORT_UNCERTAIN),
    "payment_cart_changed": (_CART_CHANGED, _CART_CHANGED_PARTIAL),
    "payment_destination_disabled": (_DESTINATION_DISABLED + _READ_NOT_WRITTEN, _DESTINATION_DISABLED_PARTIAL),
}


def _commit_text(result: dict) -> None:
    """Set the fixed text for a stopped commit; a partial stop always says that fields were written. dlz (Codex B r2
    #2): a write whose outcome is unknown (lost, failed or malformed answer) always says fields may hold values."""
    error_type = result.get("error_type")
    if not result.get("success") and result.get("write_uncertain"):
        result["error"] = _TRANSPORT_UNCERTAIN
    elif error_type == "payment_fields_not_retained":
        result["error"] = _NOT_RETAINED
    elif error_type in _COMMIT_TEXTS:
        result["error"] = _COMMIT_TEXTS[error_type][1 if result.get("partially_filled") else 0]
    elif not result.get("success") and result.get("partially_filled"):
        result["error"] = _PARTIAL_STOPPED


def _describe_result(result: dict, backend, inspected, fills: list, prompt: dict, bound_revision: str,
                     unreviewed: bool, stated) -> dict:
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
        # dlz: how strongly the card read was tied to the approved item (never a value): "version",
        # "record" (local digest), "updated_at_only" (1Password reported no version: weaker), "none".
        revision_binding=revision_binding(bound_revision),
    )
    if unreviewed:
        result.update(destination_reviewed=False, approval_origins=list(inspected.target_origins))
    if stated is not None:  # nothing after the commit may raise: the result must say what was written
        result.update(cart_stated_by="sera",
                      page_match=str((getattr(inspected, "page_match", None) or ("not_checked",))[0]))
    return result


def _page_refusal(supervisor, inspected, unreviewed: bool, policy) -> dict | None:
    """Inside the fill context, right before the capture guard: the page, the cart witnesses and (dlz, after
    those asynchronous checks) the destination policy still hold. None = may proceed."""
    if not supervisor.payment_is_current(inspected):
        return {"success": False, "error_type": "payment_page_changed", "error": _PAGE_CHANGED}
    # dlz (B5): the page check's pinned reading (total-labelled amounts, occurrences) is unchanged.
    if getattr(inspected, "witnesses", ()) and not supervisor.payment_cart_current(inspected):
        return {"success": False, "error_type": "payment_cart_changed", "error": _CART_CHANGED}
    if unreviewed and destination_policy() != policy:
        return _disabled()
    return None


def _guard_capture(vault, task_id: str, lease: str, strict: bool) -> tuple:
    """Capture lock (control's CDP guard for the approved lease only) on a managed browser: ``(refusal, surface)``."""
    if not vault._is_managed():
        return None, None
    from tools.browser_use_cli import vault_route

    route = vault_route(task_id) or {}
    # dlz: the guard protects the approved lease only: a route replaced meanwhile (another browser
    # operation during the freshness pass) is not the browser this approval was for.
    if str(route.get("lease") or "") != lease:
        return {"success": False, "error_type": "payment_browser_changed", "error": _BROWSER_CHANGED}, None
    refused = _set_control_capture_guard(route, strict=strict)
    if refused:
        return refused, None
    return None, {"slot_id": route.get("slot_id", ""), "browser_generation": route.get("browser_generation", ""),
                  "wipe_count": route.get("wipe_count", ""), "profile_kind": route.get("profile_kind", "")}


def _claim_and_readmit(vault, task_id: str, supervisor, inspected, lease: str) -> dict | None:
    """The last steps before the card is read. None = may proceed."""
    # dlz: A9: browser-control must confirm this supervisor's claim on the card documents before
    # any value exists in the page; another connection holding them refuses the fill.
    if vault._is_managed():
        from tools.browser_payment_surface import claim_payment_surface

        refused = claim_payment_surface(supervisor, inspected)
        if refused:
            return refused
        # dlz (Codex A3 r1 #7): the claim's verification can take seconds; the approval window is
        # checked again right after it, before the re-admission and any secret is resolved.
        if time.monotonic() >= inspected.expires_at:
            return _expired()
    # dlz (Codex A2 r3 #3): re-admit right before the card is read: the lease the guard was set for must
    # still be the live owner; a route replaced meanwhile refuses here, before any card byte is read.
    refused = _ownership_refusal(vault, task_id, supervisor, inspected, lease)
    if refused:
        return refused
    # dlz (Codex A3 r2 #2): the re-admission's status call can outlast the window too.
    return _expired() if time.monotonic() >= inspected.expires_at else None


def fill_payment(backend, meta, task_id: str, merchant_origin: str = "", *, strict: bool = False,
                 cart=None, approve_origins=None) -> str:
    """Native card fill after the owner's approval. ``strict`` (the unreviewed-destination feature): a managed
    browser is required, the capture guard's rollout exception does not apply, and each field is hit-tested
    before its setter. Reviewed fills keep ``strict=False``; an unreviewed destination always runs strict.

    dlz (Track B, design v2 B8): when the reviewed inspection refuses for coverage and
    ``vault.payment_destination_consent`` is on, a discovery inspection also admits unreviewed hosted frames by
    structure. Without a valid ``cart`` and ``approve_origins`` equal to its approval-origin set it returns
    ``payment_destination_approval_required`` (nothing read, no prompt); with them, the owner's one approval card
    names every receiving origin (UNREVIEWED marked), the cart as stated by Sera and the page check, and the fill
    continues with the strict checks, the topology/cart/absence bindings and the switch re-read after the accept,
    in context, after the page checks, at the commit fence and inside the commit (between documents and on every
    paced resume).

    Order after consent (design v2 A6): backend context -> item revision -> live ownership -> page freshness
    -> capture guard (for the approved lease only) and local mark -> expiry -> live ownership again -> read the
    card -> its revision -> fills -> commit fence (ownership again, expiry) -> commit with its own per-document
    and per-setter checks."""
    from tools import browser_vault_tool as vault
    from tools.payment_consent_prompt import (
        UNAVAILABLE_FACTS, PaymentConsent, facts_for_inspection, request_payment_approval)

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
        bound_revision = _bound_revision(backend, meta) if _binds_revisions(backend) else ""
        if _binds_revisions(backend) and not bound_revision:
            return json.dumps({"success": False, "error_type": "payment_item_revision_unavailable",
                               "error": _REVISION_UNAVAILABLE})
        # dlz: the lease this approval is for, snapshotted before the prompt (managed mode).
        lease = _route_lease(task_id) if vault._is_managed() else ""
        frame_origins = effective_frame_origins(meta.payment_frame_origins)
        stated = _stated_cart(cart) if cart is not None else None
        witness = {"cart": _cents(stated)} if stated is not None else {}
        policy = None  # (enabled, generation) captured for the discovery inspection (B6)
        try:
            inspected = supervisor.inspect_payment(merchant_origin, bound_origins=bound, frame_origins=frame_origins,
                                                   **witness)
        except PaymentPolicyError as exc:
            found = _discover(supervisor, vault, exc, merchant_origin, bound, frame_origins, witness)
            if isinstance(found, dict):
                return json.dumps(found)
            inspected, policy = found
        unreviewed = bool(getattr(inspected, "unreviewed_origins", ()))
        strict = strict or unreviewed
        if unreviewed:
            gate = _destination_gate(inspected, stated, approve_origins)
            if gate is not None:
                return json.dumps(gate)
            # dlz: the commit re-reads the switch between documents and on every paced resume.
            inspected.policy_check = lambda: destination_policy() == policy
        elif cart is not None and stated is None:
            return json.dumps({"success": False, "error_type": "payment_cart_invalid", "error": _CART_INVALID})
        prompt = inspected.prompt()
        # dlz: the owner-only payment card (tools.payment_consent_prompt): every fact or no prompt.
        try:
            facts = facts_for_inspection(inspected, card_label=meta.label, cart=stated)
        except Exception:  # health: allow BLE001 -- facts out of bounds: no prompt, no fill
            facts = None
        consent = (request_payment_approval(facts, timeout_seconds=_remaining_seconds(inspected)) if facts is not None
                   else PaymentConsent("unavailable", "unavailable", UNAVAILABLE_FACTS))
        if not consent.accepted:
            # Stop here: no secret resolved, no field written. A refusal is payment_declined, a missing
            # answer payment_consent_missing; both stop, and consent_outcome says why.
            refusal = _consent_not_given(consent.verdict, consent.reason)
            if consent.detail:
                refusal["error"] += " " + consent.detail
            return json.dumps(refusal)
        if time.monotonic() >= inspected.expires_at:
            return json.dumps(_expired())
        # dlz (B6): the switch and its policy generation, re-read after the accept.
        if unreviewed and destination_policy() != policy:
            return json.dumps(_disabled())
        # The card is resolved and written inside the backend's fill_context (a plugin backend's lock /
        # snapshot / manager session), entered only after consent; an item that changed meanwhile is
        # refused before any value is resolved.
        from tools.browser_vault_context import fill_context

        with fill_context(backend):
            if not _payment_item_current(backend, meta, bound_revision):
                return json.dumps({"success": False, "error_type": "payment_item_changed", "error": _ITEM_CHANGED})
            if unreviewed and destination_policy() != policy:  # B6, A6 step 7
                return json.dumps(_disabled())
            # dlz: live ownership and page freshness inside the context, right before the card is read.
            refused = (_ownership_refusal(vault, task_id, supervisor, inspected, lease)
                       or _page_refusal(supervisor, inspected, unreviewed, policy))
            if refused:
                return json.dumps(refused)
            # Capture lock (control's CDP guard, then the local mark) BEFORE the card is read: both are
            # set-only and harmless if the fill stops later, and no card byte is read unless they hold.
            from tools.browser_payment_privacy import mark_payment_session

            refused, surface = _guard_capture(vault, task_id, lease, strict)
            if refused:
                return json.dumps(refused)
            mark_payment_session(task_id, surface)
            # dlz: the freshness pass, the guard and the backend context can outlast the window: last check
            # before any secret is read.
            if time.monotonic() >= inspected.expires_at:
                return json.dumps(_expired())
            refused = _claim_and_readmit(vault, task_id, supervisor, inspected, lease)
            if refused:
                return json.dumps(refused)
            # Never resolve at inspection/list time, on decline or on stale consent.
            raw, verdict = _resolve_card(backend, meta, bound_revision)
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
            if unreviewed and destination_policy() != policy:  # B6, A6 step 16: the commit fence
                return json.dumps(_disabled(after_read=True))
            result = supervisor.commit_payment(inspected, fills, strict=strict)
            _commit_text(result)
            return json.dumps(_describe_result(result, backend, inspected, fills, prompt, bound_revision,
                                               unreviewed, stated))
    except BackendError as exc:
        if type(exc) is BackendError:  # unclassified op failure: keep the opaque refusal below
            return json.dumps({
                "success": False,
                "error_type": "payment_fill_refused",
                "error": "Card resolution or page checks failed; nothing was submitted. Inspect the page and request fresh consent.",
            })
        return json.dumps(exc.result())
    except PaymentDestinationError as exc:
        # dlz: typed refusals carry fixed text and sanitized detail (origins, field tokens, counts) only.
        return json.dumps({**exc.detail, "success": False, "error_type": exc.error_type, "error": str(exc)})
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
