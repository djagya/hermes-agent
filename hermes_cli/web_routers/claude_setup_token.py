"""dlz: the owner's protected handoff for a gateway-owned ``claude setup-token`` run.

The page shows the CLI-issued Claude authorization link and takes the browser's final callback
address in one paste. Both API routes are dashboard-authenticated (``_require_token``: session token
or verified gated session), refuse cross-site Fetch Metadata, take JSON bodies only, and forward to
the gateway's private ``claude-setup-token`` control-socket verb, which owns validation and the one
delivery (``agent.claude_setup_token``). Nothing submitted is put in a URL, logged or echoed back.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from hermes_cli.web_deps import late

router = APIRouter()
_require_token = late("_require_token")

# Not under /api: a browser navigation carries no token header (loopback gate) and the gated
# middleware redirects a page path to sign-in instead of answering 401 JSON.
PAGE_PATH = "/claude-code/setup-token"
VIEW_PATH = "/api/providers/oauth/claude-code/setup-token/view"
SUBMIT_PATH = "/api/providers/oauth/claude-code/setup-token/submit"
_PAGE_HEADERS = {
    "Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Cross-Origin-Opener-Policy": "same-origin",
}
# Owner-facing reasons; anything else shows as a generic refusal.
_STATUS = {"not_found": 404, "not_owner": 403, "bad_request": 400, "unavailable": 503}


_MAX_BODY_BYTES = 8 * 1024


async def _body(request: Request, *fields: str) -> dict[str, str]:
    """Parsed here, after the auth guard, instead of by a pydantic model: a validation error must
    not echo the submitted callback back (FastAPI's 422 carries the rejected input)."""
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise HTTPException(413, "body too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "bad_request") from None
    if not isinstance(data, dict) or set(data) != set(fields):
        raise HTTPException(400, "bad_request")
    if not all(isinstance(data[f], str) and 0 < len(data[f]) <= 4096 for f in fields):
        raise HTTPException(400, "bad_request")
    return data


def _principal(request: Request) -> str:
    """Who the dashboard verified for this request, as a stable string."""
    if getattr(request.state, "hosted_http_authenticated", None) is True:
        raise HTTPException(403, "not available to hosted plugins")
    if getattr(request.state, "native_http_principal", None):
        return "native"
    session = getattr(request.state, "session", None)
    if session is not None:
        from gateway.session_identity import authenticated_subject
        return authenticated_subject({"user_id": session.user_id, "provider": session.provider,
                                      "issuer": getattr(session, "issuer", "")})
    return "session-token"


def _guard(request: Request) -> str:
    _require_token(request)
    site = request.headers.get("sec-fetch-site")
    if site is not None and site != "same-origin":
        raise HTTPException(403, "cross-site request refused")
    if not (request.headers.get("content-type") or "").lower().startswith("application/json"):
        raise HTTPException(415, "application/json required")
    return _principal(request)


def _ask_gateway(params: dict[str, Any]) -> dict[str, Any]:
    from gateway.control_socket import query_gateway_control
    from hermes_constants import get_process_hermes_home
    from agent.claude_setup_token import CONTROL_VERB
    answer = query_gateway_control(get_process_hermes_home(), CONTROL_VERB, params=params, timeout=30.0)
    if answer is None:
        raise HTTPException(503, "gateway_not_answering")
    if not answer.get("ok"):
        code = str(answer.get("error") or "refused")
        raise HTTPException(_STATUS.get(code, 409), code)
    return {k: v for k, v in answer.items() if k != "ok"}


def _profile_key(profile: Optional[str]) -> str:
    from agent.claude_setup_token import profile_key
    from hermes_cli.web_server_profiles import _profile_scope
    with _profile_scope(profile):
        return profile_key()


async def _forward(request: Request, profile: Optional[str], action: str, *fields: str) -> dict[str, Any]:
    principal = _guard(request)
    extra = await _body(request, *fields)
    key = await asyncio.to_thread(_profile_key, profile)
    return await asyncio.to_thread(_ask_gateway, {"action": action, "profile": key, "principal": principal, **extra})


@router.post(VIEW_PATH)
async def setup_token_view(request: Request, profile: Optional[str] = None):
    return await _forward(request, profile, "view", "handoff")


@router.post(SUBMIT_PATH)
async def setup_token_submit(request: Request, profile: Optional[str] = None):
    return await _forward(request, profile, "submit", "handoff", "callback")


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
<title>Claude setup token</title><style nonce="__NONCE__">
body{font:16px/1.45 system-ui,sans-serif;margin:0;padding:16px;background:#fafafa;color:#111;max-width:640px}
textarea{width:100%;box-sizing:border-box;min-height:7em;font:14px ui-monospace,monospace}
button,a.btn{display:inline-block;margin:8px 0;padding:10px 16px;font-size:16px}
#msg{white-space:pre-wrap}@media(prefers-color-scheme:dark){body{background:#111;color:#eee}}
</style></head><body><h1>Claude setup token</h1>
<p id="msg">Loading…</p>
<p id="step1" hidden><a id="auth" class="btn" target="_blank" rel="noopener noreferrer">1. Approve at Claude</a></p>
<form id="step2" hidden autocomplete="off"><p>2. After approving, the browser lands on a page that does not load
(<code>localhost</code>). Copy that whole address and paste it here (or send it in your chat with Hermes).</p>
<textarea id="cb" spellcheck="false" autocapitalize="off" autocomplete="off"></textarea>
<button type="submit">Send to Hermes</button></form>
<script nonce="__NONCE__">
(function(){
var cfg=__CFG__,tok=window.__HERMES_SESSION_TOKEN__;
var h=new URLSearchParams(location.hash.slice(1)).get("h")||"";
history.replaceState(null,"",location.pathname+location.search);
var msg=document.getElementById("msg");
function post(path,body){body.handoff=h;var hd={"Content-Type":"application/json"};
 if(tok)hd["X-Hermes-Session-Token"]=tok;
 return fetch(cfg.base+path+location.search,{method:"POST",credentials:"same-origin",cache:"no-store",
  referrerPolicy:"no-referrer",headers:hd,body:JSON.stringify(body)})
 .then(function(r){return r.json().catch(function(){return {}}).then(function(j){return [r.status,j]})});}
function show(s,j){msg.textContent=s+(j&&j.reason?" ("+j.reason+")":"");}
function view(){post(cfg.view,{}).then(function(a){var j=a[1];
 if(a[0]!==200){show("This handoff is not available: "+(j.detail||a[0]));return;}
 if(j.state==="awaiting_callback"&&j.authorize_url){var l=document.getElementById("auth");l.href=j.authorize_url;
  document.getElementById("step1").hidden=false;document.getElementById("step2").hidden=false;
  show("Waiting for your approval. Expires in "+j.expires_in_s+" s.");return;}
 document.getElementById("step1").hidden=true;document.getElementById("step2").hidden=true;
 show("State: "+j.state,j);if(["stored_pending","failed","cancelled","expired"].indexOf(j.state)<0)setTimeout(view,3000);});}
document.getElementById("step2").addEventListener("submit",function(e){e.preventDefault();
 var t=document.getElementById("cb"),v=t.value;t.value="";
 post(cfg.submit,{callback:v}).then(function(a){v="";var j=a[1];
  if(a[0]!==200){show("Not accepted: "+(j.detail||a[0])+". Nothing was sent.");return;}
  show("Sent once. State: "+j.state,j);setTimeout(view,2000);});});
if(!h){show("This link is incomplete.");}else{view();}
})();
</script></body></html>"""


@router.get(PAGE_PATH)
async def setup_token_page(request: Request):
    """The handoff page. It holds no flow data itself; the API calls above are authenticated."""
    _require_token_or_page_session(request)
    from hermes_cli.dashboard_auth.prefix import prefix_from_request
    nonce = secrets.token_urlsafe(16)
    base = prefix_from_request(request)
    cfg = json.dumps({"base": base, "view": VIEW_PATH, "submit": SUBMIT_PATH}).replace("</", "<\\/")
    html = _PAGE.replace("__NONCE__", nonce).replace("__CFG__", cfg)
    token_js = _session_token_script(request)
    if token_js:
        html = html.replace("<script nonce=", f"<script nonce=\"{nonce}\">{token_js}</script><script nonce=", 1)
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
           "img-src 'none'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'")
    return HTMLResponse(html, headers={**_PAGE_HEADERS, "Content-Security-Policy": csp})


def _require_token_or_page_session(request: Request) -> None:
    """Gated mode: the auth middleware already required a verified session for this page path.
    Loopback mode: the page is like the SPA shell, which carries the same token."""
    if getattr(request.app.state, "auth_required", False):
        _require_token(request)


def _session_token_script(request: Request) -> str:
    """The loopback SPA's session-token bootstrap, withheld exactly where the SPA withholds it."""
    from hermes_cli.web_deps import _server
    from hermes_cli.web_server_surface import policy
    state = request.app.state
    withheld = (bool(getattr(state, "auth_required", False))
                or bool(getattr(_server().app.state, "withhold_session_token", False))
                or not policy(state).html_token)
    return "" if withheld else f"window.__HERMES_SESSION_TOKEN__={json.dumps(_server()._SESSION_TOKEN)};"
