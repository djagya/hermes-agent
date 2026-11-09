"""Lease-bound, pinned browser bridges for operator-installed private adapters.

The model selects only a binding and action. The private binding pins the origin,
slot identity, vault item, selectors and read expressions. Every browser call
reuses the current task's already-held managed lease; this module never acquires
a replacement browser and never exports its endpoint or command token.

Bindings are data, not code: one JSON object at
``$HERMES_HOME/browser/managed-bridges.json`` mapping a bridge id to its binding.
The operator installs it owned by root and not group/other-writable, for example
``install -o root -g root -m 0644 bindings.json <HERMES_HOME>/browser/managed-bridges.json``.
The gateway runs unprivileged and owns ``HERMES_HOME``, so it can delete or move
that file but cannot create or edit one owned by root. The loader refuses any
other owner, a writable mode, a hard-linked file, and a path that resolves through
a symlink; then no bindings load and every bridge call fails closed. Residual: the
gateway could move a root-owned bindings file the operator left elsewhere in a
directory it can write (an old copy, another profile's) into place, so keep exactly
one installed copy per profile home.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from tools.browser_cdp_tool import _cdp_call, _run_async
from tools.browser_control_route import held_managed_cdp

_DISCOVERY_TIMEOUT_S = 5.0
_READ_RESULT_CAP = 2_000_000
_ARCHIVE_CAP = 25 * 1024 * 1024
_CHALLENGE_SELECTOR = (
    'iframe[src*="captcha" i], iframe[src*="challenge-platform" i], '
    '[class*="captcha" i], [id*="captcha" i], '
    'input[name*="otp" i], input[name*="one-time" i]'
)
# Compared after lowercasing and dropping "_" / "-", so accessToken == access_token.
_SENSITIVE_KEYS = {
    "authorization", "cookie", "cookies", "setcookie", "password", "secret", "clientsecret",
    "sessiontoken", "sessionid", "token", "accesstoken", "refreshtoken", "idtoken", "apikey",
    "bearer", "csrftoken", "xsrftoken",
}


class _BridgeError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_BINDINGS_CAP = 1_000_000
# The owner a bindings file must have; tests point this at their own uid.
_OPERATOR_UID = 0


def _bindings_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "browser" / "managed-bridges.json"


def _gateway_is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _load_bindings() -> dict[str, Any]:
    """Load the operator-installed bindings file, never a caller-selected path.

    Refused (no bindings) unless it is a regular, root-owned file without
    group/other write access, read without following a final symlink.
    """
    if _gateway_is_root():
        return {}  # a root gateway could write the file itself, so ownership proves nothing
    path = _bindings_path()
    try:
        # A symlinked browser/ (or file) would let the gateway point at any root-owned file.
        if os.path.realpath(path) != os.path.join(os.path.realpath(path.parent.parent), path.parent.name, path.name):
            return {}
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return {}
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != _OPERATOR_UID or info.st_nlink != 1
                or info.st_mode & 0o022 or info.st_size > _BINDINGS_CAP):
            return {}
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(_BINDINGS_CAP + 1)
    except OSError:
        return {}
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        bindings = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return bindings if isinstance(bindings, dict) and len(raw) <= _BINDINGS_CAP else {}


def _origin(value: Any) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
        return ""
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        return ""
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}" + (f":{port}" if port not in {None, 443} else "")


def _validated_binding(bindings: dict[str, Any], binding_id: str) -> dict[str, Any]:
    binding = bindings.get(binding_id)
    if not isinstance(binding, dict) or not _origin(binding.get("origin")):
        raise _BridgeError("unknown_binding")
    if not isinstance(binding.get("provider"), str) or not binding["provider"].strip():
        raise _BridgeError("unknown_binding")
    if not isinstance(binding.get("identity"), str) or not binding["identity"].strip():
        raise _BridgeError("unknown_binding")
    if binding.get("mode") not in {"research", "qa"}:
        raise _BridgeError("unknown_binding")
    return binding


def _resolve_credentials(binding: dict[str, Any]) -> dict[str, str]:
    from agent.redact import register_vault_redaction_value
    from agent.vault_backends import backend_for_handle

    handle = binding.get("vault_handle")
    if not isinstance(handle, str) or not handle:
        raise _BridgeError("credential_resolution_failed")
    try:
        backend = backend_for_handle(handle)
        meta = backend.get_meta(handle) if backend is not None else None
        if backend is None or meta is None:
            raise _BridgeError("credential_resolution_failed")
        expected = _origin(binding["origin"])
        allowed = {_origin(item) for item in (getattr(meta, "allowed_origins", None) or ())}
        if not allowed and meta.origin:
            allowed = {_origin(meta.origin)}
        username = str(getattr(meta, "identifier", "") or "")
        if getattr(meta, "kind", "") != "login" or not username or expected not in allowed:
            raise _BridgeError("credential_resolution_failed")
        password = backend.resolve_password(handle)
        if not password:
            raise _BridgeError("credential_resolution_failed")
    except _BridgeError:
        raise
    except Exception as exc:
        raise _BridgeError("credential_resolution_failed") from exc
    register_vault_redaction_value(username)
    register_vault_redaction_value(password)
    return {"username": username, "password": password}


def _login_expression(credentials: dict[str, str], binding: dict[str, Any]) -> str:
    selectors = binding.get("selectors")
    if not isinstance(selectors, dict):
        raise _BridgeError("login_fields_missing")
    username_selector = selectors.get("username")
    password_selector = selectors.get("password")
    if not isinstance(username_selector, str) or not isinstance(password_selector, str):
        raise _BridgeError("login_fields_missing")
    ionic = binding.get("ionic") is True
    form_selector = selectors.get("form")
    submit_selector = selectors.get("submit")
    if ionic and (not isinstance(form_selector, str) or not isinstance(submit_selector, str)):
        raise _BridgeError("login_fields_missing")

    parts = [
        "(() => {",
        f"if (window.location.origin !== {json.dumps(_origin(binding['origin']))}) "
        "return {ok:false,error:'origin_mismatch'};",
        f"if (document.querySelector({json.dumps(_CHALLENGE_SELECTOR)})) "
        "return {ok:false,error:'challenge_refused'};",
        f"const user=document.querySelector({json.dumps(username_selector)});",
        f"const pass=document.querySelector({json.dumps(password_selector)});",
    ]
    if ionic:
        parts += [
            f"const form=document.querySelector({json.dumps(form_selector)});",
            f"const submit=document.querySelector({json.dumps(submit_selector)});",
            "if(!user||!pass||!form||!submit)return {ok:false,error:'login_fields_missing'};",
        ]
    else:
        parts.append("if(!user||!pass)return {ok:false,error:'login_fields_missing'};")
    parts += [
        "const setValue=(element,value)=>{element.value=value;",
    ]
    if ionic:
        parts.append(
            "for(const name of ['ionInput','ionChange'])element.dispatchEvent(new CustomEvent(name,"
            "{detail:{value},bubbles:true,composed:true}));"
            "element.dispatchEvent(new CustomEvent('ionBlur',{bubbles:true,composed:true}));"
        )
    else:
        parts.append(
            "for(const name of ['input','change'])element.dispatchEvent(new Event(name,{bubbles:true}));"
        )
    parts += [
        "};",
        f"setValue(user,{json.dumps(credentials['username'])});",
        f"setValue(pass,{json.dumps(credentials['password'])});",
    ]
    if ionic:
        parts.append("if(!form.classList.contains('ng-valid'))return {ok:false,error:'form_state_rejected'};")
    parts.append("return {ok:true,form_valid:true};})()")
    return "".join(parts)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _BridgeError("cdp_unavailable")


def _fetch_json(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with build_opener(_NoRedirect).open(request, timeout=_DISCOVERY_TIMEOUT_S) as response:
            raw = response.read(_READ_RESULT_CAP + 1)
            if response.status != 200 or len(raw) > _READ_RESULT_CAP:
                raise _BridgeError("cdp_unavailable")
        return json.loads(raw.decode("utf-8"))
    except _BridgeError:
        raise
    except Exception as exc:
        raise _BridgeError("cdp_unavailable") from exc


def _lease_parts(version_url: str) -> tuple[Any, str]:
    try:
        parsed = urlsplit(version_url)
        token = (parse_qs(parsed.query).get("tok") or [""])[0]
    except ValueError as exc:
        raise _BridgeError("cdp_unavailable") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not token:
        raise _BridgeError("cdp_unavailable")
    if not re.fullmatch(r"/slot/[^/]+/json/version", parsed.path):
        raise _BridgeError("cdp_unavailable")
    return parsed, token


def _lease_url(version_url: str, suffix: str) -> str:
    parsed, _ = _lease_parts(version_url)
    path = parsed.path[: -len("/json/version")] + suffix
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _page_ws(version_url: str, target_id: str) -> str:
    parsed, token = _lease_parts(version_url)
    if not target_id or "/" in target_id:
        raise _BridgeError("cdp_unavailable")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, f"/devtools/page/{target_id}", f"tok={token}", ""))


def _browser_ws(version_url: str) -> str:
    discovery = _fetch_json(version_url)
    raw = str((discovery or {}).get("webSocketDebuggerUrl") or "") if isinstance(discovery, dict) else ""
    try:
        path = urlsplit(raw).path
    except ValueError:
        path = ""
    if not path.startswith("/devtools/"):
        raise _BridgeError("cdp_unavailable")
    parsed, token = _lease_parts(version_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, path, f"tok={token}", ""))


def _targets_from_lease(version_url: str) -> list[dict[str, Any]]:
    _lease_parts(version_url)
    try:
        listed = _fetch_json(_lease_url(version_url, "/json/list"))
        if not isinstance(listed, list):
            raise _BridgeError("cdp_unavailable")
        return [item for item in listed if isinstance(item, dict)]
    except _BridgeError:
        result = _run_async(_cdp_call(_browser_ws(version_url), "Target.getTargets", {}, None,
                                      _DISCOVERY_TIMEOUT_S))
        infos = result.get("targetInfos") if isinstance(result, dict) else None
        if not isinstance(infos, list):
            raise _BridgeError("cdp_unavailable")
        return [{"id": item.get("targetId"), "type": item.get("type"), "url": item.get("url")}
                for item in infos if isinstance(item, dict)]


def _exact_target(targets: list[dict[str, Any]], origin: str) -> dict[str, Any]:
    expected = _origin(origin)
    matches = {}
    for target in targets:
        target_id = str(target.get("id") or target.get("targetId") or "")
        try:
            actual = urlsplit(str(target.get("url") or ""))
            actual_origin = _origin(urlunsplit((actual.scheme, actual.netloc, "", "", "")))
        except ValueError:
            continue
        if target.get("type") == "page" and target_id and actual_origin == expected:
            matches[target_id] = target
    if not matches:
        raise _BridgeError("target_not_found")
    if len(matches) != 1:
        raise _BridgeError("target_ambiguous")
    return next(iter(matches.values()))


def _evaluate_expression(version_url: str, target: dict[str, Any], expression: str, *,
                         await_promise: bool, timeout_s: float) -> Any:
    target_id = str(target.get("id") or target.get("targetId") or "")
    params = {"expression": expression, "returnByValue": True, "awaitPromise": await_promise}
    try:
        result = _run_async(_cdp_call(_page_ws(version_url, target_id), "Runtime.evaluate", params,
                                      None, timeout_s))
    except Exception as exc:
        raise _BridgeError("cdp_evaluation_failed") from exc
    evaluation = result if isinstance(result, dict) else {}
    if evaluation.get("exceptionDetails"):
        raise _BridgeError("cdp_evaluation_failed")
    remote = evaluation.get("result", {})
    if not isinstance(remote, dict):
        raise _BridgeError("cdp_evaluation_failed")
    return remote.get("value")


def _assert_public(value: Any, depth: int = 0) -> None:
    if depth > 20:
        raise _BridgeError("response_refused")
    if isinstance(value, dict):
        for key, item in value.items():
            if re.sub(r"[_\-]", "", str(key).lower()) in _SENSITIVE_KEYS:
                raise _BridgeError("response_refused")
            _assert_public(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _assert_public(item, depth + 1)


def _public_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _BridgeError("bridge_failed")
    if not value.get("ok"):
        code = str(value.get("error") or "bridge_failed")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "bridge_failed"
        raise _BridgeError(code)
    _assert_public(value)
    result = {key: item for key, item in value.items() if key != "ok"}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > _READ_RESULT_CAP:
        raise _BridgeError("response_refused")
    return {"success": True, **result}


def _archive_destination(binding: dict[str, Any]) -> Path:
    """Today's pinned archive path, checked before the page export runs."""
    root = binding.get("archive_root")
    name = binding.get("archive_name")
    if not isinstance(root, str) or not os.path.isabs(root) or not isinstance(name, str):
        raise _BridgeError("archive_validation_failed")
    if not re.fullmatch(r"[a-z0-9-]{1,64}", name):
        raise _BridgeError("archive_validation_failed")
    date = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d")
    destination = Path(root).resolve() / date / "downloads" / f"{name}-{date}.zip"
    if os.path.lexists(destination):
        raise _BridgeError("archive_exists")
    return destination


def _write_archive(value: dict[str, Any], destination: Path) -> dict[str, Any]:
    encoded = value.pop("archive_base64", None)
    if not isinstance(encoded, str) or len(encoded) > ((_ARCHIVE_CAP + 2) // 3) * 4:
        raise _BridgeError("archive_validation_failed")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise _BridgeError("archive_validation_failed") from exc
    if not 4 <= len(raw) <= _ARCHIVE_CAP or not raw.startswith(b"PK"):
        raise _BridgeError("archive_validation_failed")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            pdf_count = sum(not item.is_dir() and item.filename.lower().endswith(".pdf")
                            for item in archive.infolist())
        if pdf_count != int(value.get("document_count", -1)):
            raise _BridgeError("archive_validation_failed")
    except _BridgeError:
        raise
    except Exception as exc:
        raise _BridgeError("archive_validation_failed") from exc

    directory = destination.parent
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.resolve() != directory:  # a symlinked date/downloads dir would escape the root
        raise _BridgeError("archive_validation_failed")
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{destination.stem}-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise _BridgeError("archive_exists") from exc
    finally:
        os.unlink(temporary)
    return {
        "success": True,
        "archive_file": str(destination),
        "archive_bytes": len(raw),
        "archive_sha256": hashlib.sha256(raw).hexdigest(),
        "pdf_count": pdf_count,
    }


def _held_target(binding: dict[str, Any], task_id: Optional[str]) -> tuple[str, dict[str, Any]]:
    if not task_id:
        raise _BridgeError("lease_not_held")
    env: dict[str, str] = {}
    error = held_managed_cdp(
        env,
        task_id=task_id,
        identity=binding["identity"],
        mode=binding["mode"],
    )
    if error:
        raise _BridgeError(str(error).split(":", 1)[0])
    endpoint = env.get("BU_CDP_URL", "")
    target = _exact_target(_targets_from_lease(endpoint), binding["origin"])
    return endpoint, target


def _guard_read_expression(expression: str, binding: dict[str, Any]) -> str:
    """Recheck the document at use, before any pinned async read starts."""
    return (
        "(() => {"
        f"if (window.location.origin !== {json.dumps(_origin(binding['origin']))}) "
        "return {ok:false,error:'origin_mismatch'};"
        f"if (document.querySelector({json.dumps(_CHALLENGE_SELECTOR)})) "
        "return {ok:false,error:'challenge_refused'};"
        f"return ({expression});"
        "})()"
    )


def run_managed_bridge(binding_id: str, action: str, *, task_id: Optional[str],
                       bindings: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Run one pinned operation inside the native browser/vault/lease owner."""
    try:
        binding = _validated_binding(_load_bindings() if bindings is None else bindings, binding_id)
        if action not in {"verify", "fill", "route", "capture", "archive"}:
            raise _BridgeError("usage_error")
        endpoint, target = _held_target(binding, task_id)
        credentials = _resolve_credentials(binding) if action in {"verify", "fill"} else None
        if action == "verify":
            return {"success": True, "credential_resolved": True}
        if action == "fill":
            expression = _login_expression(credentials or {}, binding)
            # Recheck after the vault call: a handoff minted meanwhile refuses before the fill.
            endpoint, target = _held_target(binding, task_id)
            value = _evaluate_expression(endpoint, target, expression,
                                         await_promise=False, timeout_s=10.0)
            _public_result(value)
            return {"success": True, "credentials_populated": True}
        expressions = binding.get("read_expressions")
        expression = expressions.get(action) if isinstance(expressions, dict) else None
        if not isinstance(expression, str) or not expression:
            raise _BridgeError("unknown_binding")
        destination = _archive_destination(binding) if action == "archive" else None
        expression = _guard_read_expression(expression, binding)
        value = _evaluate_expression(endpoint, target, expression, await_promise=True,
                                     timeout_s=180.0 if action == "archive" else 60.0)
        if destination is not None:
            if not isinstance(value, dict) or not value.get("ok"):
                _public_result(value)
            return _write_archive(value, destination)
        return _public_result(value)
    except _BridgeError as exc:
        return {"success": False, "error": exc.code}
    except Exception:  # health: allow BLE001 -- tool boundary; transport errors can carry the lease URL and token, so neither str(exc) nor a traceback may reach the model or logs
        return {"success": False, "error": "bridge_failed"}


def dispatch_browser_exec(*, binding_id: str, action: str, code: str, session: str,
                          timeout_s: int, default_timeout_s: int, local: bool,
                          mode: str, identity: str, qa_targets: Optional[list[str]],
                          on_dialog: str, task_id: Optional[str]) -> Optional[dict[str, Any]]:
    """Dispatch the narrow browser_exec bridge surface, or decline ordinary code."""
    if not binding_id and not action:
        return None
    overrides = (
        not binding_id or not action or bool(code and code.strip()) or bool(session) or local
        or bool(mode) or bool(identity) or bool(qa_targets) or on_dialog != "auto"
        or timeout_s != default_timeout_s
    )
    if overrides:
        return {"error": "bridge_override_refused"}
    return run_managed_bridge(binding_id, action, task_id=task_id)
