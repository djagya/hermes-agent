"""Sealed temporary credentials read from a photographed letter.

An official letter (insurer, tax office, bank) often carries a one-off login: a customer or
insurance number plus a one-time password for one questionnaire. When the user sends a photo of
such a letter, the gateway screens it here BEFORE the image reaches the model
(:func:`seal_letter_images`): the auxiliary vision model reads the credentials twice, the secret
is kept in a process-local, TTL-bound store under an opaque ``letter-…`` handle and registered with
the model-egress redaction boundary, the cached photo is deleted, and the model receives only a
note with the handle, the organization, the portal printed in the letter, the identifier and a
secret-free summary. ``browser_vault action=letter_login`` then fills the CURRENT page from the
handle (tools/browser_vault_tool.py).

Nothing here is persisted: no vault item, no domain-bound password record. A gateway restart or
the TTL drops the handle, and the user resends the photo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent.vault_login_classifier import LoginControl, _normalize_text

logger = logging.getLogger(__name__)

LETTER_HANDLE_PREFIX = "letter-"
MAX_FAILED_RETRIES = 1  # one refill after the page itself showed the sign-in failed
MAX_REAUTHS = 3  # sign-ins renewed after a confirmed session expired
CONFIRM_WINDOW_S = 30 * 60  # a sign-in is confirmed within this long after its fill, or not at all
CHECKPOINT_MAX_AGE_S = 12 * 3600  # an older confirmed sign-in no longer proves the letter works
REAUTH_MIN_INTERVAL_S = 60.0
_DEFAULT_TTL_HOURS = 24.0
_MAX_PER_PROFILE = 16

_LOCK = threading.Lock()
_LETTERS: dict[str, dict[str, LetterCredential]] = {}  # profile home → handle → credential


@dataclass
class LetterCredential:
    handle: str
    organization: str
    purpose: str
    portal_urls: list[str]
    identifier_label: str
    identifier: str
    secret_label: str
    secret: str = field(repr=False)
    status: str  # "ready" | "ambiguous"
    expires_at: float
    summary: str = ""
    fills: int = 0
    # Sign-in lifecycle, kept apart from the fill count: "new" → "filled" (submitted or not, unknown)
    # → "signed_in" (the backend saw the login form gone on the letter's site) → "filled" again after a
    # renewed sign-in; "done" once the form the letter is for was sent, which retires the secret.
    state: str = "new"
    failed_retries: int = 0
    reauths: int = 0
    last_fill_at: float = 0.0
    fill_task: str = ""
    login_origin: str = ""
    signed_in_at: float = 0.0

    def public(self) -> dict[str, Any]:
        """Everything the model may see. Never the secret."""
        return {"handle": self.handle, "organization": self.organization, "purpose": self.purpose,
                "portal_urls": list(self.portal_urls), "identifier_label": self.identifier_label,
                "identifier": self.identifier, "secret_label": self.secret_label, "status": self.status,
                "fills": self.fills, "state": self.state, "reauths_left": MAX_REAUTHS - self.reauths,
                "expires_in_s": max(0, int(self.expires_at - time.time()))}


# ---------------------------------------------------------------------------
# Config + store
# ---------------------------------------------------------------------------

def _intake_cfg() -> dict[str, Any]:
    try:
        from hermes_cli.config import cfg_get, load_config
        return cfg_get(load_config(), "vault", "letter_intake", default={}) or {}
    except Exception:  # health: allow BLE001 -- unreadable config means intake off, the pre-existing behaviour
        return {}


def letter_intake_enabled() -> bool:
    return bool(_intake_cfg().get("enabled"))


def _ttl_seconds() -> float:
    try:
        hours = float(_intake_cfg().get("ttl_hours") or _DEFAULT_TTL_HOURS)
    except (TypeError, ValueError):
        hours = _DEFAULT_TTL_HOURS
    return max(0.1, min(hours, 24.0 * 14)) * 3600


def _scope() -> str:
    from hermes_constants import get_hermes_home
    return str(get_hermes_home())


def _bucket() -> dict[str, LetterCredential]:
    """The profile's letters with expired ones dropped and their secret zeroed. Caller holds _LOCK."""
    bucket = _LETTERS.setdefault(_scope(), {})
    now = time.time()
    for key in [k for k, v in bucket.items() if v.expires_at <= now]:
        bucket.pop(key).secret = ""
    return bucket


def put_letter(cred: LetterCredential) -> None:
    with _LOCK:
        bucket = _bucket()
        bucket[cred.handle] = cred
        while len(bucket) > _MAX_PER_PROFILE:
            bucket.pop(next(iter(bucket))).secret = ""


def get_letter(handle: str) -> Optional[LetterCredential]:
    with _LOCK:
        return _bucket().get(handle)


def list_letters() -> list[dict[str, Any]]:
    with _LOCK:
        return [c.public() for c in _bucket().values()]


_CONSUMED = ("letter_consumed", "The form this letter was for was already sent, so its password is retired. "
             "Nothing was written.")


def fill_refusal(cred: LetterCredential, *, after_failed_login: bool = False, session_expired: bool = False,
                 task: Optional[str] = None, origin: Optional[str] = None,
                 now: Optional[float] = None) -> Optional[tuple[str, str]]:
    """Why this fill may not run, as ``(error_type, message)``, else None. ``task``/``origin`` None
    skips the checks that need them (the cheap pre-check before the page is read)."""
    now = time.time() if now is None else now
    if cred.state == "done":
        return _CONSUMED
    if after_failed_login and session_expired:
        return ("invalid_reason", "Pass after_failed_login (the page showed the sign-in failed) or "
                "session_expired (a confirmed sign-in expired), not both.")
    if session_expired:
        if cred.state != "signed_in":
            return ("reauth_unproven", "No confirmed sign-in with this letter to renew: the last fill was never "
                    "confirmed with action=letter_signed_in, or a renewed sign-in is still unconfirmed. Read the "
                    "page; never refill after an unclear submit. Nothing was written.")
        if task is not None and task != cred.fill_task:
            return "reauth_unproven", "The sign-in was confirmed in another browser task. Nothing was written."
        if origin is not None and origin != cred.login_origin:
            return ("origin_mismatch", f"Renewing a sign-in works only on the login page it was confirmed for "
                    f"({cred.login_origin}). Nothing was written.")
        if now - cred.signed_in_at > CHECKPOINT_MAX_AGE_S:
            return ("reauth_stale", "The confirmed sign-in is too old to prove this letter still works. Tell the "
                    "user; they can sign in through a handoff. Nothing was written.")
        if cred.reauths >= MAX_REAUTHS:
            return ("attempts_exhausted", f"This letter's sign-in was already renewed {cred.reauths} times. Stop "
                    "and tell the user what the page shows.")
        if now - cred.last_fill_at < REAUTH_MIN_INTERVAL_S:
            return "rate_limited", "The last fill was moments ago. Read the page before signing in again."
        return None
    if not cred.fills:
        return None
    if cred.state == "signed_in":
        if after_failed_login:
            return ("reauth_required", "The last sign-in with this letter was confirmed, so a login form now means "
                    "the session expired, not that the password failed. Call again with session_expired=true.")
        return ("attempt_unclear", "This letter's sign-in was confirmed earlier. If the page shows the login form "
                "again because the session expired, call again with session_expired=true.")
    if not after_failed_login:
        return ("attempt_unclear", "This letter login was already filled. If the form was submitted, first "
                "check the page: signed in → call action=letter_signed_in and continue; still on the login form "
                "with an error → call again with after_failed_login=true. Never refill after a timeout without looking.")
    if cred.failed_retries >= MAX_FAILED_RETRIES:
        return ("attempts_exhausted", f"This letter login was already filled {cred.fills} times. Stop: more "
                "tries can lock the one-time password. Tell the user what the page shows.")
    return None


def reserve_fill(handle: str, *, after_failed_login: bool = False, session_expired: bool = False,
                 task: str, origin: str) -> Optional[tuple[str, str]]:
    """Count one fill attempt atomically, right before the secret is written. Every dispatched
    fill counts, whatever its result. Returns ``(error_type, message)`` when it may not run."""
    with _LOCK:
        cred = _bucket().get(handle)
        if cred is not None and cred.state == "done":
            return _CONSUMED
        if cred is None or not cred.secret:
            return "letter_unknown", "The letter login expired. Ask the user to send the photo again."
        now = time.time()
        refused = fill_refusal(cred, after_failed_login=after_failed_login, session_expired=session_expired,
                               task=task, origin=origin, now=now)
        if refused:
            return refused
        if session_expired:
            cred.reauths += 1
        else:
            if cred.fills:
                cred.failed_retries += 1
            cred.fill_task, cred.login_origin = task, origin
        cred.fills += 1
        cred.state, cred.last_fill_at = "filled", now
        return None


def record_signed_in(handle: str, *, task: str) -> Optional[tuple[str, str]]:
    """The backend saw this task's page on the letter's site with no login form after a fill: keep it
    as the proof a later ``session_expired`` sign-in needs. Repeating it while signed in refreshes it."""
    with _LOCK:
        cred = _bucket().get(handle)
        if cred is not None and cred.state == "done":
            return _CONSUMED
        if cred is None or not cred.secret:
            return "letter_unknown", "The letter login expired. Ask the user to send the photo again."
        now = time.time()
        if cred.state not in ("filled", "signed_in"):
            return "nothing_to_confirm", "This letter was never filled, so there is no sign-in to confirm."
        if task != cred.fill_task:
            return "nothing_to_confirm", "This letter was filled in another browser task."
        if cred.state == "filled" and now - cred.last_fill_at > CONFIRM_WINDOW_S:
            return ("confirm_too_late", "The fill was too long ago to tie this page to it. If the session later "
                    "expires, the user signs in through a handoff.")
        cred.state, cred.signed_in_at = "signed_in", now
        return None


def retire_letter(handle: str) -> bool:
    """The form the letter was for was sent: zero the secret, keep the record so later calls say why."""
    with _LOCK:
        cred = _bucket().get(handle)
        if cred is None:
            return False
        cred.secret, cred.state = "", "done"
        return True


def clear_letters() -> None:
    with _LOCK:
        _LETTERS.pop(_scope(), None)


# ---------------------------------------------------------------------------
# Extraction (auxiliary vision model, two independent reads)
# ---------------------------------------------------------------------------

_EXTRACT_PROMPT = """You screen a user's photo for temporary login credentials before an assistant sees it.
Answer with ONE JSON object and nothing else.

If the image is NOT a letter/document that gives the reader a login secret (a password, one-time
password, initial password, PIN or access code used to sign in to a website or portal), answer:
{"has_login_secret": false}

Otherwise answer:
{"has_login_secret": true,
 "organization": "<issuing organization, as printed>",
 "purpose": "<what the login is for, e.g. the questionnaire it opens; short>",
 "portal_urls": ["<every website address printed for signing in, exactly as printed>"],
 "identifier_label": "<printed label of the user/customer/insurance number, e.g. Versichertennummer>",
 "identifier": "<that number or user name, exactly as printed>",
 "secret_label": "<printed label of the secret, e.g. Einmal-Kennwort>",
 "secret": "<the secret, character for character, as printed>",
 "uncertain": <true if ANY character of identifier or secret could be read two ways (O/0, l/1/I, S/5, B/8, rn/m) or is blurred, cut off or covered>,
 "summary": "<2-4 sentences on the rest of the letter: sender, date, deadline, what it asks the reader to do, reference numbers. NEVER include the secret.>"}

Copy the identifier and secret exactly: keep case, leading zeros and hyphens; never guess a character."""

_VERIFY_PROMPT = """Transcribe the sign-in details printed in this letter. Answer with ONE JSON object and nothing else:
{"identifier": "<the user/customer/insurance number exactly as printed>",
 "secret": "<the password / one-time password / PIN / access code exactly as printed>",
 "uncertain": <true if any character of either value is ambiguous, blurred or partly hidden>}
Copy every character as printed, including case, leading zeros and hyphens. Do not guess."""


class LetterExtractionError(Exception):
    """The screen could not run. Carries no model output, so it can never carry a secret."""


def _parse_reply(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    fence = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        data = json.loads(fence.group(0) if fence else text)
    except (json.JSONDecodeError, ValueError):
        raise LetterExtractionError("vision reply was not JSON") from None
    if not isinstance(data, dict):
        raise LetterExtractionError("vision reply was not a JSON object")
    return data


async def _ask_vision(image_path: str, prompt: str) -> dict[str, Any]:
    """One auxiliary vision call. Never logs or raises the reply text."""
    from tools import vision_tools as vt

    try:
        prepared = await vt._prepare_image(image_path, None, None, validate_decode=False)
    except Exception as exc:  # health: allow BLE001 -- re-raised typed; the cause text could carry image data
        raise LetterExtractionError(f"image could not be prepared ({type(exc).__name__})") from None
    try:
        data_url = await vt._run_encode_on_cpu_executor(vt._image_to_base64_data_url, prepared.path,
                                                        mime_type=prepared.mime)
        if len(data_url) > vt._MAX_BASE64_BYTES:
            data_url = await vt._resize_prepared(prepared, {})
        vt._load_auxiliary_client()
        kwargs = vt._aux_call_kwargs(vt._media_messages(prompt, "image_url", data_url), None, 120.0)
        kwargs["temperature"] = 0.0
        response = await vt.async_call_llm(**kwargs)
        reply = vt.extract_content_or_reasoning(response)
    except LetterExtractionError:
        raise
    except Exception as exc:  # health: allow BLE001 -- re-raised typed without the reply, which may hold the secret
        raise LetterExtractionError(f"vision call failed ({type(exc).__name__})") from None
    finally:
        vt._unlink_quietly(prepared.path)
    return _parse_reply(reply or "")


def _clean(value: Any, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


def _secret_value(value: Any) -> str:
    """Only surrounding whitespace is trimmed: a printed ``AB12-cd34`` is exactly those bytes."""
    return str(value or "").strip()


def _squash(text: str) -> str:
    return re.sub(r"[\s\-_.·]", "", text).casefold()


def _scrub(text: str, *values: str) -> str:
    """Text without the secret. Text that still holds it in another spelling (spaced, hyphenated,
    case-folded) is dropped whole rather than patched."""
    for v in values:
        if v and len(v) >= 3:
            text = text.replace(v, "[sealed]")
            if _squash(v) in _squash(text):
                return ""
    return text


async def extract_letter(image_path: str) -> Optional[LetterCredential]:
    """Screen one image. ``None`` = no login secret in it. Raises :class:`LetterExtractionError`
    when the screen cannot run. The secret is registered with the redaction boundary as soon as
    it is known, before anything else can echo it."""
    from agent.redact import register_vault_redaction_value

    first = await _ask_vision(image_path, _EXTRACT_PROMPT)
    flag = first.get("has_login_secret")
    if flag is False:
        return None
    if flag is not True:  # "true", missing key, any other shape: the screen did not answer
        raise LetterExtractionError("vision reply had no has_login_secret boolean")
    secret = _secret_value(first.get("secret"))
    identifier = _clean(first.get("identifier"), 120)
    register_vault_redaction_value(secret)
    second: dict[str, Any] = {}
    if secret:
        try:
            second = await _ask_vision(image_path, _VERIFY_PROMPT)
        except LetterExtractionError:
            second = {}
    check = _secret_value(second.get("secret"))
    register_vault_redaction_value(check)
    agrees = bool(secret) and check == secret and _clean(second.get("identifier"), 120) == identifier
    unsure = bool(first.get("uncertain")) or bool(second.get("uncertain"))
    # Every field reaches the model: one that holds the secret in any spelling is blanked, and an
    # identifier that IS the secret means the read mixed them up.
    def safe(value: str) -> str:
        return value if _scrub(value, secret, check) == value else ""
    mixed_up = bool(identifier) and _squash(identifier) in {_squash(secret), _squash(check)}
    status = "ready" if agrees and not unsure and not mixed_up and len(secret) >= 4 else "ambiguous"
    portal_urls = [safe(u) for u in (_clean(x, 200) for x in (first.get("portal_urls") or []) if isinstance(x, str))]
    return LetterCredential(
        handle=LETTER_HANDLE_PREFIX + secrets.token_hex(4),
        organization=safe(_clean(first.get("organization"), 120)),
        purpose=safe(_clean(first.get("purpose"), 200)),
        portal_urls=[u for u in portal_urls if u][:5],
        identifier_label=safe(_clean(first.get("identifier_label"), 60)),
        identifier=safe(identifier),
        secret_label=safe(_clean(first.get("secret_label"), 60)),
        secret=secret if status == "ready" else "",
        status=status,
        expires_at=time.time() + _ttl_seconds(),
        summary=_scrub(_clean(first.get("summary"), 800), secret, check),
    )


def render_note(cred: LetterCredential) -> str:
    lines = [
        "[The user sent a photo of a letter with temporary sign-in credentials. Hermes sealed the secret "
        "before you saw the image: you never see it, and you must not ask for, type, guess or repeat it.",
        f"letter handle: {cred.handle} (held in memory, not saved anywhere)",
        f"organization: {cred.organization or 'unknown'}",
    ]
    if cred.purpose:
        lines.append(f"purpose: {cred.purpose}")
    lines.append("portal printed in the letter: " + (", ".join(cred.portal_urls) or "none"))
    if cred.identifier:
        lines.append(f"identifier ({cred.identifier_label or 'user/customer number'}): {cred.identifier}")
    lines.append(f"secret ({cred.secret_label or 'password'}): sealed")
    if cred.summary:
        lines.append(f"letter summary: {cred.summary}")
    if cred.status == "ready":
        lines.append("To sign in: open the portal in the browser, reach its login form, then call browser_vault "
                     f"action=letter_login handle={cred.handle}. It fills the identifier and the secret; then submit once.")
    else:
        lines.append("status: AMBIGUOUS. The two reads of the credentials disagreed or a character was unclear, so "
                     "nothing was kept for filling. Do not try to sign in; ask the user for a sharper photo of the "
                     "credentials block, or offer a browser handoff so they sign in themselves.")
    return "\n".join(lines) + "]"


async def seal_letter_images(image_paths: list[str]) -> tuple[list[str], list[str]]:
    """Gateway intake: returns ``(images still to route, notes for the sealed ones)``.

    A sealed photo is deleted from the media cache, so no later tool call can re-read the secret.
    Fail closed: a photo the screen could not judge is withheld and deleted too (it may hold a
    password); the model is told to ask the user to resend it."""
    if not image_paths or not letter_intake_enabled():
        return list(image_paths or []), []
    kept, notes = [], []
    for path in image_paths:
        try:
            cred = await extract_letter(path)
        except LetterExtractionError as exc:
            logger.warning("letter intake: screen failed, photo withheld (%s)", exc)
            await _destroy(path)
            notes.append("[The user sent a photo, but Hermes could not check it for sign-in credentials, so it was "
                         "withheld and deleted. Tell the user and ask them to send it again in a moment.]")
            continue
        if cred is None:
            kept.append(path)
            continue
        put_letter(cred)
        notes.append(render_note(cred))
        await _destroy(path)
        logger.info("letter intake: sealed one letter as %s (status=%s)", cred.handle, cred.status)
    return kept, notes


async def _destroy(path: str) -> None:
    """Delete a withheld photo; if deletion fails, overwrite it so its pixels are gone either way."""
    def run() -> None:
        p = Path(path)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            with open(p, "wb"):
                pass
    try:
        await asyncio.to_thread(run)
    except OSError as exc:
        logger.error("letter intake: could not delete or blank a withheld photo (%s)", type(exc).__name__)


async def seal_into_message(message_text: str, image_paths: list[str], prepend, enrich=None) -> tuple[str, list[str]]:
    """Gateway hook: seal credential letters, put their notes in front of the message with ``prepend``
    (the gateway's media-prefix helper), and hand the remaining images to ``enrich`` when given."""
    kept, notes = await seal_letter_images(image_paths)
    if notes:
        message_text = prepend("\n\n".join(notes), message_text)
    if kept and enrich is not None:
        try:
            message_text = await enrich(message_text, kept)
        except Exception as exc:  # health: allow BLE001 -- the sealed notes must survive a failed description
            logger.warning("image enrichment failed after letter intake (%s)", type(exc).__name__)
    return message_text, kept


# ---------------------------------------------------------------------------
# Destination check
# ---------------------------------------------------------------------------

# Hosts where anyone can publish a subdomain: a letter printing one binds only that exact host.
_SHARED_SUFFIXES = (
    "github.io", "gitlab.io", "pages.dev", "workers.dev", "vercel.app", "netlify.app", "herokuapp.com",
    "web.app", "firebaseapp.com", "appspot.com", "blogspot.com", "azurewebsites.net", "cloudfront.net",
    "amazonaws.com", "onrender.com", "fly.dev", "glitch.me", "ngrok.io", "ngrok-free.app", "wixsite.com",
    "wordpress.com", "sharepoint.com", "trycloudflare.com", "notion.site", "r2.dev", "myshopify.com",
    "squarespace.com", "webflow.io", "framer.app", "carrd.co", "jimdosite.com", "strato-hosting.eu",
)
_TWO_LEVEL_TLDS = ("co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "co.at", "or.at", "gv.at", "co.jp", "com.br",
                   "co.nz", "com.tr", "co.za", "com.mx", "co.in", "com.cn", "com.pl", "com.es")


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit
    value = url.strip()
    if "://" not in value:
        value = "https://" + value
    try:
        return (urlsplit(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _site_root(host: str) -> str:
    """The printed host without ``www.``; subdomains of it are the same organization unless the
    host sits on a shared-hosting suffix or IS a public two-level suffix."""
    return host.removeprefix("www.")


def origin_allowed(page_origin: str, cred: LetterCredential) -> bool:
    """True when ``page_origin`` is HTTPS (loopback may be HTTP) and its host is a host printed in
    the letter or a subdomain of one. Exact-host only for shared-hosting suffixes."""
    from urllib.parse import urlsplit
    parts = urlsplit(page_origin)
    host = (parts.hostname or "").lower()
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if parts.scheme != "https" and not (loopback and parts.scheme == "http"):
        return False
    for printed in cred.portal_urls:
        printed_host = _host_of(printed)
        if not printed_host:
            continue
        if host == printed_host:
            return True
        root = _site_root(printed_host)
        shared = any(root == s or root.endswith("." + s) for s in _SHARED_SUFFIXES)
        if shared or root in _TWO_LEVEL_TLDS or root.count(".") < 1:
            continue
        if host == root or host.endswith("." + root):
            return True
    return False


# ---------------------------------------------------------------------------
# Field selection
# ---------------------------------------------------------------------------

_FILLABLE_TYPES = ("password", "text", "tel", "number", "")
_RE_SECRET = re.compile(
    r"kennwort|passwort|password|passcode|zugangscode|zugangsschlussel|freischaltcode|aktivierungscode|"
    r"access\s*code|activation\s*code|\bpin\b"
)
_RE_NOT_SECRET = re.compile(r"\b(?:new|neu|neues|confirm|bestatig\w*|wiederhol\w*|repeat|vergessen|forgot)\b")
_RE_IDENTIFIER = re.compile(
    r"versicherten\s*nummer|versichertennummer|kunden\s*nummer|kundennummer|mitglieds\s*nummer|benutzer\s*name|"
    r"benutzername|benutzerkennung|kennung|user\s*name|username|user\s*id|login|customer\s*number|member|"
    r"account|e\s*mail|aktenzeichen|steuernummer|identifikationsnummer"
)


def _search_text(c: LoginControl) -> str:
    return _normalize_text(" ".join(p for p in (c.name, c.label) if p))


def _label_match(c: LoginControl, printed_label: str) -> bool:
    want = _normalize_text(printed_label)
    if len(want) < 3:
        return False
    have = _search_text(c)
    return want in have or want.replace(" ", "") in have.replace(" ", "")


def _score_secret(c: LoginControl, cred: LetterCredential) -> int:
    if c.type not in _FILLABLE_TYPES:
        return 0
    text = _search_text(c)
    if _RE_NOT_SECRET.search(text) or "new-password" in c.autocomplete.lower():
        return 0
    if _label_match(c, cred.secret_label):
        return 95
    if c.type == "password":
        return 90
    return 75 if _RE_SECRET.search(text) else 0


def _score_identifier(c: LoginControl, cred: LetterCredential) -> int:
    if c.type not in ("text", "tel", "number", "email", ""):
        return 0
    if _label_match(c, cred.identifier_label):
        return 95
    if "username" in c.autocomplete.lower().split():
        return 85
    text = _search_text(c)
    if _RE_IDENTIFIER.search(text):
        return 70
    return 60 if c.type == "email" else 0


def _match_field(controls: list[LoginControl], spec: str) -> tuple[Optional[LoginControl], Optional[str]]:
    """Resolve an agent-named field: an exact name/id, else a label containing the text. Exactly one
    control may match; anything else is an error, never a guess."""
    want = spec.strip()
    exact = [c for c in controls if want and want.lower() in (p.lower() for p in c.name.split())]
    if not exact:
        norm = _normalize_text(want)
        exact = [c for c in controls if norm and norm in _normalize_text(c.label)]
    if len(exact) == 1:
        return exact[0], None
    return None, "field_not_found" if not exact else "field_ambiguous"


def _best(controls: list[LoginControl], score) -> tuple[Optional[LoginControl], bool]:
    """Highest-scoring control, and whether another control ties it (ambiguous)."""
    scored = sorted(((score(c), c) for c in controls), key=lambda sc: (-sc[0], sc[1].index))
    scored = [sc for sc in scored if sc[0] > 0]
    if not scored:
        return None, False
    return scored[0][1], len(scored) > 1 and scored[1][0] == scored[0][0]


def shows_login_form(controls: list[LoginControl], cred: LetterCredential) -> bool:
    """A visible control could take the letter's secret: the page is (still, or again) a login form."""
    return any(_score_secret(c, cred) for c in controls)


def select_letter_fills(controls: list[LoginControl], cred: LetterCredential, *, secret_field: str = "",
                        identifier_field: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Choose the secret control (required) and the identifier control (optional).

    Returns ``(fills, info)``. ``info["error_type"]`` set = refuse without writing:
    ``no_secret_field`` / ``secret_field_ambiguous`` / ``field_not_found`` / ``field_ambiguous``.
    An unclear identifier field is skipped (``info["identifier_filled"] = False``), not guessed."""
    info: dict[str, Any] = {}
    if secret_field:
        target, err = _match_field(controls, secret_field)
        if err:
            return [], {"error_type": err, "field": "secret_field"}
        if target is not None and target.type not in _FILLABLE_TYPES:
            return [], {"error_type": "field_not_fillable", "field": "secret_field"}
    else:
        target, tie = _best(controls, lambda c: _score_secret(c, cred))
        if target is None:
            return [], {"error_type": "no_secret_field"}
        if tie:
            return [], {"error_type": "secret_field_ambiguous"}
    fills = [{"index": target.index, "token": "letter-secret", "value": cred.secret}]
    others = [c for c in controls if c.index != target.index]
    ident: Optional[LoginControl] = None
    if cred.identifier:
        if identifier_field:
            ident, err = _match_field(others, identifier_field)
            if err:
                return [], {"error_type": err, "field": "identifier_field"}
        else:
            ident, tie = _best(others, lambda c: _score_identifier(c, cred))
            if tie:
                ident, info["identifier_hint"] = None, "several fields could take the identifier; pass identifier_field"
    if ident is not None:
        fills.insert(0, {"index": ident.index, "token": "username", "value": cred.identifier})
    info["identifier_filled"] = ident is not None
    return fills, info
