"""dlz: model-facing start/submit/status/cancel for the owner-authenticated ``claude setup-token`` intake.

The model never sees the callback, code or token. Owner policy 2026-11-09: the whole flow may run in
chat: start/status return the CLI-issued, allowlisted authorize URL (plus the dashboard handoff link)
for Danil, and he pastes the one-time final localhost callback URL back in chat; ``submit`` hands it,
unechoed, to the same strict parse and one-shot delivery the dashboard page uses. The
broker and its intake live in ``agent.claude_setup_token``.
"""

from __future__ import annotations

from typing import Any

from tools.registry import registry, tool_error, tool_result

HANDOFF_PATH = "/claude-code/setup-token"  # hermes_cli.web_routers.claude_setup_token.PAGE_PATH


def _cfg() -> dict:
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly().get("claude_setup_token") or {}
    return cfg if isinstance(cfg, dict) else {}


def _handoff_base() -> str:
    """The owner's dashboard origin (``claude_setup_token.handoff_base_url``): HTTPS, or loopback."""
    from urllib.parse import urlsplit
    base = str(_cfg().get("handoff_base_url") or "").rstrip("/")
    try:
        parts = urlsplit(base)
    except ValueError:
        return ""
    if parts.username or parts.password or parts.query or parts.fragment:
        return ""
    if parts.scheme == "https" and parts.hostname:
        return base
    if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost"):
        return base
    return ""


def _owner_principal() -> str:
    """The dashboard principal allowed to complete flows: ``claude_setup_token.owner_principal``,
    defaulting to the loopback dashboard's session-token holder. A gated dashboard needs its
    ``auth:v1:...`` subject configured; any other principal is refused."""
    return str(_cfg().get("owner_principal") or "session-token")


def check_available() -> bool:
    """Off unless configured, and only inside a gateway that registered the protected intake."""
    from agent.claude_setup_token import get_broker
    try:
        return _cfg().get("enabled") is True and bool(_handoff_base()) and get_broker() is not None
    except Exception:
        return False


def _session_id(kwargs: dict) -> str:
    sid = kwargs.get("session_id")
    if not sid:
        from agent.vault_backends.unlock import get_current_session_id
        sid = get_current_session_id()
    return str(sid or "")


_NEXT = ("Send Danil the authorize_url in chat (or the handoff_url, the same step on his signed-in "
         "dashboard). After approving at Claude his browser lands on a page that does not load: he pastes "
         "that final http://localhost address back in chat (or on the dashboard page). Call submit with this "
         "flow and callback set to exactly that address. Do not repeat it. Then poll status.")


def _with_authorize(flow) -> dict:
    """dlz: owner policy 2026-11-09: the CLI-issued authorize URL (already parsed and allowlisted by
    ``parse_authorize_url``) is not secret and may go to Danil in chat while the callback is awaited.
    Callback, code and token never appear here."""
    out = flow.model_view()
    native = flow.native  # read once: a concurrent finish clears it
    if flow.state == "awaiting_callback" and native is not None:
        out["authorize_url"] = native.authorize_url
        out["next"] = _NEXT
    return out


def claude_setup_token(args: dict[str, Any], **kwargs) -> str:
    from agent.claude_setup_token import IntakeRejected, get_broker, profile_key
    broker = get_broker()
    if broker is None or not _handoff_base():
        return tool_error("claude_setup_token is only available in a gateway with the protected intake configured.")
    session_id = _session_id(kwargs)
    if not session_id:
        return tool_error("claude_setup_token needs a gateway session.")
    action, profile = str(args.get("action") or ""), profile_key()
    try:
        if action == "start":
            flow = broker.start(profile=profile, session_id=session_id, owner=_owner_principal())
            out = _with_authorize(flow)
            if "authorize_url" in out:
                out["handoff_url"] = f"{_handoff_base()}{HANDOFF_PATH}#h={flow.handoff_id}"
            return tool_result(out)
        flow_id = str(args.get("flow") or "")
        if action == "status":
            return tool_result(_with_authorize(broker.status(flow_id, profile=profile, session_id=session_id)))
        if action == "cancel":
            return tool_result(broker.cancel(flow_id, profile=profile, session_id=session_id).model_view())
        if action == "submit":
            # dlz: status only; the callback is never echoed, logged or kept past delivery.
            out = broker.submit_from_session(flow_id, args.get("callback"), profile=profile, session_id=session_id)
            return tool_result({"flow": flow_id, "state": out["state"], "reason": out["reason"]})
    except IntakeRejected as exc:
        return tool_error(f"claude_setup_token: {exc.code}")
    return tool_error("action must be start, submit, status or cancel")


CLAUDE_SETUP_TOKEN_SCHEMA = {
    "name": "claude_setup_token",
    "description": (
        "Create a new long-lived Claude subscription token (`claude setup-token`) when Danil explicitly "
        "asks for it. start runs the native CLI and returns authorize_url (send it to Danil in chat; "
        "status repeats it while the callback is awaited) and a handoff_url (the same step on his "
        "signed-in dashboard). After approving at Claude he pastes the final http://localhost address "
        "back to you in chat (or on the dashboard page). For a pasted address call submit with the "
        "flow handle and callback set to exactly that address; never repeat it back or store it. "
        "status/cancel take the flow handle. Results are status and opaque handles only: the token goes "
        "to a concealed pending store, is not installed as the runtime credential, and is not proven for "
        "model requests (both separate operator steps). Only this one-time callback may come from chat: "
        "never ask for or accept the resulting token, a password, API key or card data in chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "submit", "status", "cancel"]},
            "flow": {"type": "string", "description": "Flow handle from start (submit/status/cancel)."},
            "callback": {"type": "string", "maxLength": 4096,
                         "description": "submit only: the final http://localhost callback address Danil pasted "
                                        "in chat, exactly as sent."},
        },
        "required": ["action"],
    },
}


registry.register(
    name="claude_setup_token", toolset="claude_auth", schema=CLAUDE_SETUP_TOKEN_SCHEMA,
    handler=claude_setup_token, check_fn=check_available, emoji="🔑")
