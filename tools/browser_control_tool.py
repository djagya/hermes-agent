"""``browser_control``: managed browser-control operations without the controller key in model code.

Managed mode (``HERMES_BROWSER_CONTROL_URL`` / ``browser.control_url``) gives
``browser_exec`` a leased slot. Releasing it, minting a human handoff link,
cancelling a native passkey prompt, moving files in and out of the slot and
answering a page dialog used to be hand-written ``urllib`` calls inside
``browser_exec`` code, carrying ``HERMES_BROWSER_CONTROL_KEY`` and the
lease's command token through model-written Python. This tool makes those
calls from Hermes itself: the key is read from the environment or
``HERMES_BROWSER_CONTROL_KEY_FILE`` by ``tools.browser_control_route`` and
the command token from the lease this task holds; neither is ever returned.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from tools.registry import registry, tool_error, tool_result

ACTIONS = ("status", "release", "cancel", "handoff", "native_cancel", "upload", "download", "dialog")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_FILE_TIMEOUT_S = 60.0
_DEFAULT_FILE_MAX_MB = 32
_SHA_HEADER = "X-Sha256"
# Fields of a control slot row the model may see. Tokens never ride /v1/status;
# lease ids are left out too: the model does not need them any more.
_STATUS_FIELDS = ("slot_id", "state", "owner", "identity", "mode", "kind", "busy_reason", "chrome_running",
                  "handback_due", "acquired_at", "last_gate_hit")


def _route():
    from tools import browser_control_route

    return browser_control_route


def _available() -> bool:
    try:
        route = _route()
    except ImportError:
        return False
    url = route.control_url()
    return bool(url) and not route.is_disabled_url(url) and url != route.CONFIG_UNREADABLE_URL


def _lease(task_id: Optional[str], identity: str = "", mode: str = "") -> tuple[Optional[dict], Optional[str]]:
    """The lease this task holds (filtered by identity/mode), or an error message."""
    leases = _route().held_leases_for_task(task_id)
    if identity:
        leases = [lease for lease in leases if lease.get("identity") == identity]
    if mode:
        leases = [lease for lease in leases if lease.get("mode") == mode]
    if not leases:
        return None, ("This task holds no browser lease" + (f" for identity {identity!r}" if identity else "")
                      + ". Run browser_exec first; it acquires the slot.")
    if len(leases) > 1:
        names = sorted({lease.get("identity") or "(research)" for lease in leases})
        return None, f"This task holds {len(leases)} leases ({', '.join(names)}); pass identity to pick one."
    return leases[0], None


def _lease_args(lease: dict) -> dict[str, str]:
    route = _route()
    return {"lease_id": lease.get(route.MANAGED_LEASE_ENV, ""), "command_token": lease.get(route.MANAGED_TOKEN_ENV, "")}


def _control_error(reply: dict) -> Optional[str]:
    if not isinstance(reply, dict):
        return "control returned a non-object"
    if reply.get("error"):
        detail = str(reply.get("detail") or "").strip()
        return f"{reply['error']}: {detail}" if detail else str(reply["error"])
    return None


def _workspace(task_id: Optional[str]) -> Optional[str]:
    from tools.browser_use_cli import _workspace_dir

    return _workspace_dir(task_id)


def _public_row(row: dict, mine: set[str]) -> dict[str, Any]:
    out = {k: row.get(k) for k in _STATUS_FIELDS if k in row}
    out["mine"] = row.get("slot_id") in mine
    return out


def _status(task_id: Optional[str]) -> str:
    route = _route()
    data = route.get_json("/v1/status")
    err = _control_error(data)
    if err:
        return tool_error(err)
    held = route.held_leases_for_task(task_id)
    mine = {lease.get(route.MANAGED_SLOT_ENV, "") for lease in held}
    slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}
    handoff = ((data.get("capabilities") or {}).get("handoff") or {}) if isinstance(data.get("capabilities"), dict) else {}
    return tool_result({
        "success": True,
        "slots": [_public_row(row, mine) for row in slots.values() if isinstance(row, dict)],
        "held_by_this_task": [{"slot_id": lease.get(route.MANAGED_SLOT_ENV, ""), "identity": lease.get("identity", ""),
                               "mode": lease.get("mode", "")} for lease in held],
        "queue_length": len(data.get("queue") or []),
        "handoff_available": bool(handoff.get("available")),
    })


def _release(task_id: Optional[str], identity: str, mode: str) -> str:
    lease, err = _lease(task_id, identity, mode)
    if err:
        return tool_error(err)
    route = _route()
    slot_id = lease.get(route.MANAGED_SLOT_ENV, "")
    failure = route.release_lease(dict(lease))
    if failure:
        return tool_error(failure, slot_id=slot_id)
    return tool_result({"success": True, "released": slot_id,
                        "note": "The slot is free; the next browser_exec acquires afresh."})


def _cancel(request_id: str) -> str:
    if not request_id:
        return tool_error("request_id is required: the queued_id (ticket) from a busy browser_exec result.")
    reply = _route().post_json("/v1/cancel", {"request_id": request_id})
    err = _control_error(reply)
    return tool_error(err) if err else tool_result({"success": True, "cancelled": request_id})


def _handoff(task_id: Optional[str], identity: str, mode: str, target_id: str) -> str:
    if not target_id:
        return tool_error("target_id is required: print(current_tab()['targetId']) in browser_exec on the tab "
                          "Danil must see, and pass it unchanged.")
    lease, err = _lease(task_id, identity, mode)
    if err:
        return tool_error(err)
    reply = _route().post_json("/v1/handoff", {**_lease_args(lease), "target_id": target_id})
    err = _control_error(reply)
    if err:
        return tool_error(err, target_id=target_id)
    workspace = _workspace(task_id)
    if workspace:
        try:
            Path(workspace, "handoff-target").write_text(target_id, encoding="utf-8")
        except OSError:
            workspace = None
    out = {"success": True, "url": reply.get("url"), "expires_in": reply.get("expires_in"), "target_id": target_id,
           "note": ("Send exactly this URL to Danil with the action needed, the lifetime and the Return to Sera "
                    "instruction. After he returns the browser, switch_tab(target_id) before anything else.")}
    if workspace:
        out["saved_target"] = str(Path(workspace, "handoff-target"))
    return tool_result(out)


def _native_cancel(task_id: Optional[str], identity: str, mode: str, target_id: str) -> str:
    if not target_id:
        return tool_error("target_id is required: the targetId of the tab showing the native passkey prompt.")
    lease, err = _lease(task_id, identity, mode)
    if err:
        return tool_error(err)
    reply = _route().post_json("/v1/native", {"action": "cancel_prompt", **_lease_args(lease), "target_id": target_id})
    err = _control_error(reply)
    return tool_error(err) if err else tool_result({"success": True, **reply})


def _file_max_bytes() -> int:
    try:
        return int(float(os.environ.get("BROWSER_FILE_MAX_MB") or _DEFAULT_FILE_MAX_MB) * 1024 * 1024)
    except ValueError:
        return _DEFAULT_FILE_MAX_MB * 1024 * 1024


def _file_url(kind: str, name: str, token: str) -> str:
    base = _route().control_url().rstrip("/")
    return f"{base}/v1/files/{kind}/{quote(name)}?{urlencode({'tok': token})}"


def _http_error(exc: HTTPError) -> dict[str, Any]:
    try:
        data = json.loads(exc.read().decode("utf-8") or "{}")
    except (ValueError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"status": exc.code, "error": str(data.get("error") or "unavailable"), "detail": str(data.get("detail") or "")}


def _upload(task_id: Optional[str], identity: str, mode: str, path: str, name: str) -> str:
    if not path:
        return tool_error("path is required: the local file to stage into the browser.")
    source = Path(path).expanduser()
    if not source.is_file():
        return tool_error(f"{path!r} is not a readable file on the Hermes side.")
    name = name or source.name
    if not _NAME_RE.match(name) or ".." in name:
        return tool_error("name must match [A-Za-z0-9._-]{1,128} with no '..' (it becomes /staging/<name>).")
    size = source.stat().st_size
    if size > _file_max_bytes():
        return tool_error(f"{size} bytes is over the slot file limit ({_file_max_bytes()} bytes).")
    lease, err = _lease(task_id, identity, mode)
    if err:
        return tool_error(err)
    body = source.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    token = _lease_args(lease)["command_token"]
    req = Request(_file_url("staging", name, token), data=body, method="PUT",
                  headers={_SHA_HEADER: digest, "Content-Type": "application/octet-stream"})
    try:
        with urlopen(req, timeout=_FILE_TIMEOUT_S) as resp:
            resp.read()
    except HTTPError as exc:
        failure = _http_error(exc)
        return tool_error(f"{failure['error']}: {failure['detail'] or 'upload refused'}", http_status=failure["status"])
    except OSError as exc:
        return tool_error(f"unavailable: control unreachable ({type(exc).__name__})")
    return tool_result({"success": True, "staged": f"/staging/{name}", "sha256": digest, "bytes": len(body),
                        "note": "Point the file input at the staged path with DOM.setFileInputFiles in browser_exec."})


def _download(task_id: Optional[str], identity: str, mode: str, name: str) -> str:
    if not name or not _NAME_RE.match(name) or ".." in name:
        return tool_error("name is required: the downloaded file's basename ([A-Za-z0-9._-]{1,128}).")
    workspace = _workspace(task_id)
    if not workspace:
        return tool_error("No browser workspace for this task; run browser_exec first.")
    lease, err = _lease(task_id, identity, mode)
    if err:
        return tool_error(err)
    token = _lease_args(lease)["command_token"]
    try:
        with urlopen(Request(_file_url("downloads", name, token), method="GET"), timeout=_FILE_TIMEOUT_S) as resp:
            body = resp.read()
            claimed = (resp.headers.get(_SHA_HEADER) or "").strip().lower()
    except HTTPError as exc:
        failure = _http_error(exc)
        if failure["status"] == 404 or (failure["status"] == 409 and "incomplete" in failure["detail"]):
            return tool_result({"success": False, "pending": True, "status": failure["status"],
                                "detail": failure["detail"] or "not there yet",
                                "note": "The download has not finished. Wait, then call download again."})
        return tool_error(f"{failure['error']}: {failure['detail'] or 'download refused'}", http_status=failure["status"])
    except OSError as exc:
        return tool_error(f"unavailable: control unreachable ({type(exc).__name__})")
    digest = hashlib.sha256(body).hexdigest()
    if not claimed or claimed != digest:
        return tool_error("Download bytes do not match control's X-Sha256; nothing was saved. Retry once.")
    target_dir = Path(workspace, "downloads")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    tmp = target.with_name(f".{name}.part")
    tmp.write_bytes(body)
    os.replace(tmp, target)
    return tool_result({"success": True, "path": str(target), "sha256": digest, "bytes": len(body)})


def _dialog(task_id: Optional[str], dialog_action: str, dialog_id: str, prompt_text: Optional[str]) -> str:
    if dialog_action not in {"accept", "dismiss"}:
        return tool_error("dialog_action must be 'accept' or 'dismiss'.")
    from tools import browser_exec_dialogs

    reply = browser_exec_dialogs.respond(task_id, action=dialog_action, dialog_id=dialog_id, prompt_text=prompt_text)
    if not reply.get("ok"):
        return tool_error(str(reply.get("error") or "dialog not answered"))
    return tool_result({"success": True, "answered": dialog_action, "dialog": reply.get("dialog")})


def browser_control(args: dict, task_id: Optional[str] = None) -> str:
    action = str(args.get("action") or "").strip()
    identity = str(args.get("identity") or "").strip()
    mode = str(args.get("mode") or "").strip()
    if action not in ACTIONS:
        return tool_error(f"action must be one of {', '.join(ACTIONS)}.")
    if action == "status":
        return _status(task_id)
    if action == "release":
        return _release(task_id, identity, mode)
    if action == "cancel":
        return _cancel(str(args.get("request_id") or "").strip())
    if action == "handoff":
        return _handoff(task_id, identity, mode, str(args.get("target_id") or "").strip())
    if action == "native_cancel":
        return _native_cancel(task_id, identity, mode, str(args.get("target_id") or "").strip())
    if action == "upload":
        return _upload(task_id, identity, mode, str(args.get("path") or "").strip(), str(args.get("name") or "").strip())
    if action == "download":
        return _download(task_id, identity, mode, str(args.get("name") or "").strip())
    prompt = args.get("prompt_text")
    return _dialog(task_id, str(args.get("dialog_action") or "").strip(), str(args.get("dialog_id") or "").strip(),
                   None if prompt is None else str(prompt))


BROWSER_CONTROL_SCHEMA = {
    "name": "browser_control",
    "description": (
        "Managed browser slot operations for the lease this task's browser_exec holds. The controller key and "
        "lease tokens stay inside Hermes. Actions: status (slots, which are yours, handoff availability); "
        "release (free your slot now); cancel (drop a queued acquire: request_id = queued_id from a busy "
        "result); handoff (single-use link for Danil to finish a CAPTCHA/login on exactly target_id, the "
        "targetId printed from current_tab() in browser_exec; returns url + expires_in); native_cancel (one "
        "Escape for Chrome's native passkey prompt on target_id); upload (local path -> /staging/<name> in "
        "the browser); download (finished file <name> from the browser's downloads -> your workspace, "
        "sha256-checked); dialog (answer a pending page dialog from a browser_exec result: dialog_action "
        "accept|dismiss, dialog_id, prompt_text for prompts). Pass identity when the task holds more than one lease."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "identity": {"type": "string", "description": "Which held lease (the identity used with browser_exec)."},
            "mode": {"type": "string", "enum": ["research", "qa"]},
            "request_id": {"type": "string", "description": "cancel: the queued_id from a busy result."},
            "target_id": {"type": "string", "description": "handoff / native_cancel: CDP targetId of the exact tab."},
            "path": {"type": "string", "description": "upload: local file to stage."},
            "name": {"type": "string", "description": "upload: staging name (default the file name); download: file name."},
            "dialog_action": {"type": "string", "enum": ["accept", "dismiss"]},
            "dialog_id": {"type": "string", "description": "dialog: id from the result's dialogs.pending."},
            "prompt_text": {"type": "string", "description": "dialog: text for a prompt() dialog."},
        },
        "required": ["action"],
    },
}


registry.register(
    name="browser_control",
    toolset="browser-use",
    schema=BROWSER_CONTROL_SCHEMA,
    handler=lambda args, **kw: browser_control(args, task_id=kw.get("task_id")),
    check_fn=_available,
    emoji="🧭",
)
