"""Fill an unbound payment card into the current checkout.

1Password cards are not saved against one merchant. The live page origin is
the merchant. The user confirms the exact charge through the normal approval
surface; that grant allows this fill and one submit. Card values stay inside
the supervisor CDP socket.

Supported hosted fields: Stripe.js frames on ``https://js.stripe.com`` whose
src is embedded in the merchant page. Any other cross-origin card frame is a
refusal, not a place the card is typed.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from agent.payment_grant import (
    AuthResult,
    EnvelopeError,
    PaymentEnvelope,
    authorize,
    build_envelope,
    consume_submit,
    release_submit,
)
from agent.vault_login_classifier import (
    ClassifiedLoginControl,
    LoginControl,
    build_fill_js,
    build_inspection_js,
    classify_checkout_control,
    select_checkout_fills,
)
from agent.vault_store import PAYMENT_FIELDS, normalize_origin, scrub_secret_from_text

logger = logging.getLogger(__name__)

STRIPE_FRAME_ORIGIN = "https://js.stripe.com"
SUPPORTED_FRAME_ORIGINS = frozenset({STRIPE_FRAME_ORIGIN})

_REQUIRED_TOKENS = frozenset({"cc-number", "cc-csc"})

_IFRAME_SRC_JS = (
    '(() => JSON.stringify(Array.from(document.querySelectorAll("iframe"))'
    '.map((frame) => frame.src || "").filter(Boolean)))()'
)

_SUBMIT_JS = r"""(() => {
  const labelOf = (el) => ((el.innerText || el.value || el.getAttribute("aria-label") || ""))
    .replace(/\s+/g, " ").trim();
  const skip = /paypal|apple pay|google pay|gpay|klarna|sofort|giropay/i;
  const pay = /\b(pay now|pay|place order|complete purchase|buy now|confirm and pay|jetzt kaufen|kostenpflichtig bestellen|zahlungspflichtig bestellen|zahlen)\b/i;
  const visible = (el) => !el.disabled && el.getAttribute("aria-disabled") !== "true" && el.getClientRects().length > 0;
  const matches = Array.from(document.querySelectorAll("button, input[type=submit], [role=button]"))
    .filter((el) => visible(el) && !skip.test(labelOf(el)) && pay.test(labelOf(el)));
  if (matches.length !== 1) {
    return JSON.stringify({ clicked: false, reason: matches.length ? "ambiguous_submit" : "no_enabled_submit" });
  }
  matches[0].click();
  return JSON.stringify({ clicked: true });
})()"""

_OUTCOME_JS = r"""(() => {
  const text = ((document.body && document.body.innerText) || "").slice(0, 4000).toLowerCase();
  const declined = /card (was )?declined|payment failed|do not honor|insufficient funds|try another card|zahlung abgelehnt|karte abgelehnt/.test(text);
  const frames = Array.from(document.querySelectorAll("iframe")).map((frame) => frame.src || "");
  const threeDS = frames.some((src) => /hooks\.stripe\.com|3d_secure|three-?ds|acs\./i.test(src));
  return JSON.stringify({ declined: declined, threeDS: threeDS });
})()"""


def _url_key(url: str) -> str:
    """Identity of a frame URL without the fragment. Query stays: Stripe puts the element id there.
    A mismatch fails closed (unready) instead of writing into a different frame."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}{query}"


def classify_embedded_frames(
    iframe_srcs: Sequence[str],
    oopif_frames: Sequence[Dict[str, str]],
    *,
    merchant_origin: str,
) -> Dict[str, List[Any]]:
    """Match iframe srcs on the merchant page to attached out-of-process frames.

    ``supported`` frames are allowlisted providers with a live CDP session.
    ``unready`` are allowlisted srcs that are not attached yet.
    ``other`` are other cross-origin srcs (origin only, never the full URL).
    """
    sessions: Dict[str, Dict[str, str]] = {}
    for frame in oopif_frames:
        key = _url_key(str(frame.get("url") or ""))
        if key and frame.get("session_id") and frame.get("frame_id"):
            sessions.setdefault(key, frame)
    supported: List[Dict[str, str]] = []
    unready: List[str] = []
    other: List[str] = []
    seen_sessions: set[str] = set()
    seen_other: set[str] = set()
    for src in iframe_srcs:
        try:
            origin = normalize_origin(src)
        except Exception:
            continue
        if origin == merchant_origin:
            continue
        frame = sessions.get(_url_key(src))
        if origin in SUPPORTED_FRAME_ORIGINS:
            if frame and frame["session_id"] not in seen_sessions:
                seen_sessions.add(frame["session_id"])
                supported.append({
                    "frame_id": frame["frame_id"],
                    "session_id": frame["session_id"],
                    "origin": origin,
                })
            elif frame is None:
                unready.append(origin)
        elif origin not in seen_other:
            seen_other.add(origin)
            other.append(origin)
    return {"supported": supported, "unready": unready, "other": other[:5]}


def _complete(tokens: set[str]) -> bool:
    expiry = "cc-exp" in tokens or {"cc-exp-month", "cc-exp-year"} <= tokens
    return _REQUIRED_TOKENS <= tokens and expiry


def _merge_fills(
    groups: Sequence[Tuple[str, List[ClassifiedLoginControl]]],
    secret: Dict[str, str],
) -> Dict[str, List[Dict[str, Any]]]:
    """One control per card token, highest score, across the page and its frames."""
    best: Dict[str, Tuple[int, str, Dict[str, Any]]] = {}
    for target, classified in groups:
        score = {item.token: item.score for item in classified}
        for fill in select_checkout_fills(classified, secret, PAYMENT_FIELDS):
            token = str(fill["token"])
            rank = score.get(token, 0)
            current = best.get(token)
            if current is None or rank > current[0]:
                best[token] = (rank, target, fill)
    if "cc-exp" in best:
        best.pop("cc-exp-month", None)
        best.pop("cc-exp-year", None)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for _token, (_rank, target, fill) in best.items():
        grouped.setdefault(target, []).append(fill)
    return grouped


def _parse_eval(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def _controls_from_eval(result: Dict[str, Any]) -> Optional[List[LoginControl]]:
    if not result.get("success"):
        return None
    raw = _parse_eval(result.get("result"))
    if isinstance(raw, str):
        raw = _parse_eval(raw)
    if not isinstance(raw, list):
        return None
    return [LoginControl.from_dict(item) for item in raw if isinstance(item, dict)]


def _classify(controls: Sequence[LoginControl]) -> List[ClassifiedLoginControl]:
    out: List[ClassifiedLoginControl] = []
    for control in controls:
        classified = classify_checkout_control(control)
        if classified is not None:
            out.append(classified)
    return out


def _refuse(error_type: str, error: str, **extra: Any) -> str:
    body: Dict[str, Any] = {
        "success": False,
        "error_type": error_type,
        "error": error,
        "merchant_acceptance": "unverified",
    }
    body.update(extra)
    return json.dumps(body)


def _consent(message: str, description: str) -> str:
    from tools.approval_prompt import request_elicitation_consent

    return request_elicitation_consent(
        message,
        description,
        surface="vault-payment",
        title="Confirm this payment?",
    )


def _lease_epoch() -> int:
    from tools.bot_desktop.lease import assert_agent_may_act

    return int(assert_agent_may_act().epoch)


def _still_authorized(task_id: str, envelope: PaymentEnvelope) -> bool:
    from tools.bot_desktop.lease import AGENT, get
    from tools.browser_vault_tool import _current_page_origin

    lease = get()
    if lease.holder != AGENT or int(lease.epoch) != envelope.lease_epoch:
        return False
    return _current_page_origin(task_id) == envelope.merchant_origin


def _session_key() -> str:
    from tools.approval_context import get_current_session_key

    return get_current_session_key()


def _oopif_frames(supervisor: Any) -> List[Dict[str, str]]:
    with supervisor._state_lock:
        frames = list(supervisor._frames.values())
    out: List[Dict[str, str]] = []
    for frame in frames:
        if frame.is_oopif and frame.cdp_session_id and frame.url:
            out.append({
                "frame_id": frame.frame_id,
                "url": frame.url,
                "session_id": frame.cdp_session_id,
            })
    return out


def _eval_frame(supervisor: Any, session_id: str, expression: str) -> Dict[str, Any]:
    sup = supervisor.evaluate_runtime(expression, frame_session=session_id)
    if sup.get("ok"):
        return {"success": True, "result": sup.get("result")}
    return {"success": False, "error": str(sup.get("error") or "frame eval failed")}


def fill_unbound_card(
    *,
    handle: str,
    label: str,
    backend: Any,
    task_id: str,
    currency: object,
    amount: object,
    recipient: object,
    items: object,
    submit: bool,
) -> str:
    """Confirm, fill, and optionally submit one unbound card. Returns JSON."""
    from tools.bot_desktop.lease import HumanHasControl
    from tools.browser_vault_tool import (
        _current_page_origin,
        _ensure_supervisor,
        _eval_js,
        _eval_js_secret,
        _focus_bound_origin,
    )

    try:
        _focus_bound_origin(task_id, "", "payment")
        merchant = _current_page_origin(task_id)
        if not merchant or not merchant.startswith("https://"):
            return _refuse(
                "origin_mismatch", "Open the https checkout page before filling a card."
            )
        epoch = _lease_epoch()
        envelope = build_envelope(
            session_key=_session_key(),
            task_id=task_id,
            handle=handle,
            card_label=label,
            merchant_origin=merchant,
            currency=currency,
            amount=amount,
            recipient=recipient,
            items=items,
            lease_epoch=epoch,
        )
    except HumanHasControl as exc:
        return _refuse("human_has_control", str(exc))
    except EnvelopeError as exc:
        return _refuse("payment_envelope_invalid", str(exc))

    decision = authorize(envelope, consent=_consent)
    if decision.status != "accept":
        error_type = {
            "decline": "payment_declined",
            "cancel": "payment_cancelled",
            "replay": "payment_replay",
            "stale": "payment_stale",
        }.get(decision.status, "payment_declined")
        message = {
            "payment_declined": "The user did not confirm this payment. Do not retry; ask them instead.",
            "payment_cancelled": "The payment confirmation was not answered. Do not retry this turn.",
            "payment_replay": "This exact charge was already submitted. Do not submit it again. Check the merchant order before anything else.",
            "payment_stale": "The checkout confirmation is no longer valid. Nothing was written.",
        }[error_type]
        return _refuse(error_type, message)

    try:
        if not _still_authorized(task_id, envelope):
            return _refuse(
                "payment_stale",
                "The page or desktop control changed after confirmation. Nothing was written.",
            )
        supervisor = _ensure_supervisor(task_id)
        groups, frame_by_target, provider, blocked, nonce = _inspect(
            task_id, supervisor, merchant
        )
        if blocked:
            return blocked
        if not _still_authorized(task_id, envelope):
            return _refuse(
                "payment_stale",
                "The page or desktop control changed after confirmation. Nothing was written.",
            )
        from agent.vault_backends import UnlockRequired

        try:
            secret = {
                key: str(value)
                for key, value in backend.resolve_secret(handle).items()
                if value
            }
        except UnlockRequired:
            return _refuse(
                "unlock_required",
                f"{backend.display_name} locked again; call browser_vault_unlock.",
            )
    except HumanHasControl as exc:
        return _refuse("human_has_control", str(exc))
    except Exception as exc:
        return _refuse(
            "payment_unavailable", scrub_secret_from_text(str(exc), {})[:300]
        )

    try:
        return _write_and_maybe_submit(
            task_id=task_id,
            envelope=envelope,
            backend_name=str(getattr(backend, "name", "")),
            groups=groups,
            frame_by_target=frame_by_target,
            provider=provider,
            secret=secret,
            submit=submit,
            decision=decision,
            nonce=nonce,
            eval_secret=_eval_js_secret,
            eval_page=_eval_js,
        )
    except HumanHasControl as exc:
        return _refuse("human_has_control", str(exc))


def _inspect(
    task_id: str,
    supervisor: Any,
    merchant: str,
) -> Tuple[
    List[Tuple[str, List[ClassifiedLoginControl]]],
    Dict[str, Dict[str, str]],
    str,
    Optional[str],
    str,
]:
    from tools.browser_vault_tool import _eval_js

    nonce = secrets.token_hex(8)
    parent_controls = _controls_from_eval(_eval_js(task_id, build_inspection_js(nonce)))
    if parent_controls is None:
        return (
            [],
            {},
            "page",
            _refuse(
                "eval_failed",
                "Could not inspect the checkout page. Nothing was written.",
            ),
            nonce,
        )
    groups: List[Tuple[str, List[ClassifiedLoginControl]]] = [
        ("page", _classify(parent_controls))
    ]
    page_tokens = {item.token for item in groups[0][1]}
    iframe_raw = _parse_eval(_eval_js(task_id, _IFRAME_SRC_JS).get("result"))
    srcs = iframe_raw if isinstance(iframe_raw, list) else []
    oopif = _oopif_frames(supervisor) if supervisor is not None else []
    embedded = classify_embedded_frames(
        [str(src) for src in srcs if isinstance(src, str)],
        oopif,
        merchant_origin=merchant,
    )
    frame_by_target: Dict[str, Dict[str, str]] = {}
    for frame in embedded["supported"]:
        target = f"frame:{frame['frame_id']}"
        inspected = _controls_from_eval(
            _eval_frame(supervisor, frame["session_id"], build_inspection_js(nonce))
        )
        if inspected is None:
            if _complete(page_tokens):
                continue
            return (
                [],
                {},
                "stripe",
                _refuse(
                    "payment_frame_unready",
                    "A Stripe card frame is attached but could not be inspected. Nothing was written.",
                    provider="stripe",
                ),
                nonce,
            )
        groups.append((target, _classify(inspected)))
        frame_by_target[target] = frame
    frame_tokens = {
        item.token for _target, classified in groups[1:] for item in classified
    }
    visible = page_tokens | frame_tokens
    if not _complete(visible):
        if embedded["unready"]:
            return (
                [],
                {},
                "stripe",
                _refuse(
                    "payment_frame_unready",
                    "Stripe's card frame is on the page but not attached yet. Nothing was written.",
                    provider="stripe",
                ),
                nonce,
            )
        if embedded["supported"]:
            return (
                [],
                {},
                "stripe",
                _refuse(
                    "incomplete_card_form",
                    "The card form is missing number, expiry, or CVC. Nothing was written.",
                ),
                nonce,
            )
        if embedded["other"]:
            return (
                [],
                {},
                "page",
                _refuse(
                    "unsupported_payment_frame",
                    "Card fields are in an unsupported frame. Nothing was written.",
                    frame_origins=embedded["other"],
                ),
                nonce,
            )
        return (
            [],
            {},
            "page",
            _refuse(
                "no_card_fields",
                "No card fields were found on the checkout page. Nothing was written.",
            ),
            nonce,
        )
    provider = "stripe" if frame_by_target else "page"
    return groups, frame_by_target, provider, None, nonce


def _write_and_maybe_submit(
    *,
    task_id: str,
    envelope: PaymentEnvelope,
    backend_name: str,
    groups: List[Tuple[str, List[ClassifiedLoginControl]]],
    frame_by_target: Dict[str, Dict[str, str]],
    provider: str,
    secret: Dict[str, str],
    submit: bool,
    decision: AuthResult,
    nonce: str,
    eval_secret: Any,
    eval_page: Any,
) -> str:
    from agent.redact import register_vault_redaction_value

    for value in secret.values():
        register_vault_redaction_value(value)
    try:
        planned = _merge_fills(groups, secret)
        tokens = {str(fill["token"]) for fills in planned.values() for fill in fills}
        if not _complete(tokens):
            return _refuse(
                "incomplete_card_form",
                "The card form is missing number, expiry, or CVC. Nothing was written.",
            )
        if not _still_authorized(task_id, envelope):
            return _refuse(
                "payment_stale",
                "The page or desktop control changed after confirmation. Nothing was written.",
            )
        filled = 0
        for target, fills in planned.items():
            frame = frame_by_target.get(target)
            expected = frame["origin"] if frame else envelope.merchant_origin
            expression = build_fill_js(fills, expected_origin=expected, nonce=nonce)
            if frame:
                result = eval_secret(
                    task_id, expression, frame_session=frame["session_id"]
                )
            else:
                result = eval_secret(task_id, expression)
            if not result.get("success"):
                err = scrub_secret_from_text(
                    str(result.get("error") or "fill failed"), secret
                )
                body: Dict[str, Any] = {
                    "success": False,
                    "error": err,
                    "merchant_acceptance": "unverified",
                }
                if result.get("error_type"):
                    body["error_type"] = result["error_type"]
                return json.dumps(body)
            parsed = _parse_eval(result.get("result"))
            if isinstance(parsed, str):
                parsed = _parse_eval(parsed)
            if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
                return _refuse(
                    "origin_changed",
                    "The page navigated before the card could be entered. Nothing further was written.",
                )
            filled += int(parsed.get("filled", 0)) if isinstance(parsed, dict) else 0
        if filled <= 0:
            return _refuse(
                "eval_failed",
                "The card form did not accept the fill. Nothing was submitted.",
            )
        outcome, submit_consumed = _maybe_submit(task_id, envelope, submit, eval_page)
        out = {
            "success": True,
            "outcome": outcome,
            "filled_fields": filled,
            "backend": backend_name,
            "kind": "payment",
            "origin": envelope.merchant_origin,
            "fields": sorted(tokens),
            "provider": provider,
            "submit_consumed": submit_consumed,
            "authorization": "reused" if decision.reused else "confirmed",
            "merchant_acceptance": "unverified",
        }
        return json.dumps(out)
    finally:
        secret.clear()


def _maybe_submit(
    task_id: str, envelope: PaymentEnvelope, submit: bool, eval_page: Any
) -> Tuple[str, bool]:
    if not submit:
        return "filled", False
    if not _still_authorized(task_id, envelope):
        return "filled", False
    if not consume_submit(envelope):
        return "filled", False
    result = eval_page(task_id, _SUBMIT_JS)
    parsed = _parse_eval(result.get("result")) if result.get("success") else None
    if isinstance(parsed, str):
        parsed = _parse_eval(parsed)
    clicked = isinstance(parsed, dict) and bool(parsed.get("clicked"))
    if not clicked:
        release_submit(envelope)
        return "filled", False
    observed = _parse_eval(eval_page(task_id, _OUTCOME_JS).get("result"))
    if isinstance(observed, str):
        observed = _parse_eval(observed)
    if isinstance(observed, dict) and observed.get("declined"):
        return "declined", True
    if isinstance(observed, dict) and observed.get("threeDS"):
        return "needs_bank_approval", True
    return "ambiguous", True
