#!/usr/bin/env python3
"""Lean safety guard for Himalaya v2.

Public command policy:
- reads are allowed;
- mailbox/draft writes require HIMALAYA_WRITE_APPROVED=1;
- outbound requires a verified server-side draft plus HIMALAYA_SEND_APPROVED=1;
- destructive/provider-settings operations are absent from the permitted surface.

The guard intentionally fails closed on unknown commands.
"""
from __future__ import annotations

import email.policy
from email.parser import BytesParser
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

REAL = os.environ.get("HIMALAYA_REAL_BIN", "/usr/local/bin/himalaya.real")
WRITE_ENV = "HIMALAYA_WRITE_APPROVED"
SEND_ENV = "HIMALAYA_SEND_APPROVED"
APPROVED_FROM_ENV = "HIMALAYA_APPROVED_FROM"
APPROVED_HASH_ENV = "HIMALAYA_APPROVED_MESSAGE_SHA256"
APPROVED_DRAFT_ENV = "HIMALAYA_APPROVED_DRAFT_ID"
POLICY_PATH = Path(os.environ.get("HIMALAYA_GUARD_POLICY", "/opt/data/.config/himalaya/guard-policy.json"))
ORTIE = os.environ.get("ORTIE_BIN", "/opt/data/.local/bin/ortie")
ORTIE_CONFIG = os.environ.get("ORTIE_CONFIG", "/opt/data/.config/ortie/config.toml")
DEFAULT_GMAIL_BATCH_URL = "https://gmail.googleapis.com/batch"
DEFAULT_GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
GMAIL_BATCH_URL = os.environ.get("HIMALAYA_GMAIL_BATCH_URL", DEFAULT_GMAIL_BATCH_URL)
GMAIL_API_BASE = os.environ.get("HIMALAYA_GMAIL_API_BASE", DEFAULT_GMAIL_API_BASE)

GLOBAL_VALUE_OPTS = {
    "-c", "--config", "-a", "--account", "-b", "--backend",
    "--log-level", "--log", "--log-file",
}
GLOBAL_FLAG_OPTS = {"--json", "-h", "--help", "-V", "--version"}
GLOBAL_EQ_PREFIXES = (
    "--config=", "--account=", "--backend=", "--log-level=", "--log=", "--log-file=",
)
ALIASES = {
    "mbox": "mailbox", "ls": "list", "sr": "search", "msg": "message",
    "cp": "copy", "mv": "move", "fwd": "forward", "write": "compose",
    "save": "add", "dl": "download", "rm": "remove", "del": "delete",
    "sendas": "send-as", "filter": "filters", "delegate": "delegates",
    "autoforwarding": "auto-forwarding", "forwarding-address": "forwarding-addresses",
}

READ_SHARED = {
    ("account", "list"), ("account", "check"),
    ("mailbox", "list"),
    ("envelope", "list"), ("envelope", "search"),
    ("message", "read"),
    ("attachment", "list"), ("attachment", "download"),
}
READ_JMAP = {
    ("mailbox", "get"), ("mailbox", "query"),
    ("email", "get"), ("email", "query"), ("email", "read"),
    ("email", "export"), ("email", "parse"),
    ("thread", "get"), ("identity", "get"),
    ("submission", "get"), ("submission", "query"),
    ("vacation-response", "get"),
}
READ_GMAIL = {
    ("profile", "get"),
    ("labels", "list"), ("labels", "get"),
    ("messages", "list"), ("messages", "get"),
    ("attachments", "get"),
    ("drafts", "list"), ("drafts", "get"),
    ("threads", "list"), ("threads", "get"),
    ("history", "list"),
}
WRITE_GMAIL = {
    ("messages", "modify"), ("messages", "batch-modify"),
    ("threads", "modify"),
    ("labels", "create"), ("labels", "update"),
    ("drafts", "create"), ("drafts", "update"),
}
SEND_GMAIL = {("messages", "send")}
WRITE_JMAP = {
    ("mailbox", "create"), ("mailbox", "update"),
    ("email", "update"), ("email", "import"),
}
SEND_JMAP: set[tuple[str, str]] = set()
DENIED_JMAP = {
    ("query",),
    ("mailbox", "destroy"),
    ("email", "delete"),
    ("identity", "create"), ("identity", "update"), ("identity", "delete"),
    ("submission", "cancel"),
    ("vacation-response", "set"),
}
DENIED_GMAIL = {
    ("messages", "trash"), ("messages", "untrash"), ("messages", "delete"),
    ("messages", "batch-delete"), ("messages", "import"), ("messages", "insert"),
    ("threads", "trash"), ("threads", "untrash"), ("threads", "delete"),
    ("drafts", "send"), ("drafts", "delete"), ("labels", "delete"),
}
FORBIDDEN_LABELS = {"trash", "spam"}
ALLOWED_FLAGS = {"seen", "flagged"}


def die(message: str, *, code: int = 64) -> "NoReturn":
    print(f"himalaya: blocked by provider-native mail guard: {message}", file=sys.stderr)
    raise SystemExit(code)


def approved(name: str) -> bool:
    return os.environ.get(name) == "1"


def normalize(word: str) -> str:
    low = word.lower()
    return ALIASES.get(low, low)


def strip_globals(args: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token in GLOBAL_VALUE_OPTS:
            if i + 1 >= len(args):
                die(f"missing value after {token}")
            i += 2
            continue
        if token in GLOBAL_FLAG_OPTS or token.startswith(GLOBAL_EQ_PREFIXES):
            i += 1
            continue
        out.append(token)
        i += 1
    return out


def global_value(args: list[str], *names: str) -> str | None:
    value = None
    for i, token in enumerate(args):
        for name in names:
            if token == name and i + 1 < len(args):
                value = args[i + 1]
            elif token.startswith(name + "="):
                value = token.split("=", 1)[1]
    return value


def command_path(args: list[str]) -> tuple[str, tuple[str, ...], list[str]]:
    clean = strip_globals(args)
    if not clean:
        die("interactive wizard is not exposed")
    family = normalize(clean[0])
    if family in {"jmap", "gmail"}:
        if len(clean) < 2:
            die(f"{family} requires a resource")
        resource = normalize(clean[1])
        if family == "gmail" and resource == "settings":
            if len(clean) < 4:
                die("gmail settings requires an exact sub-resource and action")
            sub = normalize(clean[2])
            action = normalize(clean[3])
            return family, (resource, sub, action), clean[4:]
        if len(clean) < 3:
            die(f"{family} {resource} requires an action")
        action = normalize(clean[2])
        return family, (resource, action), clean[3:]
    if len(clean) < 2:
        die(f"{family} requires an action")
    action = normalize(clean[1])
    return family, (action,), clean[2:]


def option_values(tokens: list[str], names: Iterable[str]) -> list[str]:
    names = set(names)
    values: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in names:
            if i + 1 >= len(tokens):
                die(f"missing value after {t}")
            values.append(tokens[i + 1])
            i += 2
            continue
        for name in names:
            if t.startswith(name + "="):
                values.append(t.split("=", 1)[1])
        i += 1
    return values


def has_option(tokens: list[str], *names: str) -> bool:
    return any(t in names or any(t.startswith(n + "=") for n in names) for t in tokens)


def reject_trash_spam(tokens: list[str]) -> None:
    values = option_values(
        tokens,
        {
            "--to", "--mailbox", "--mailbox-id", "--add-label", "--remove-label",
            "--label", "--add-mailbox", "--remove-mailbox", "--mailboxes",
        },
    )
    for value in values:
        pieces = {p.strip().casefold() for p in value.replace(",", " ").split() if p.strip()}
        if pieces & FORBIDDEN_LABELS:
            die("TRASH/SPAM transitions are absent from the allowed surface")


def validate_flags(tokens: list[str]) -> None:
    flags = option_values(tokens, {"-f", "--flag"})
    if not flags:
        die("flag operation requires --flag seen or --flag flagged")
    for raw in flags:
        for flag in raw.replace(",", " ").split():
            if flag.casefold().lstrip("\\") not in ALLOWED_FLAGS:
                die(f"flag {flag!r} is not permitted")


def validate_jmap_write(resource: str, action: str, tokens: list[str]) -> None:
    reject_trash_spam(tokens)
    if resource == "email" and action == "update":
        if has_option(tokens, "--add-mailbox", "--remove-mailbox", "--mailboxes"):
            die("raw JMAP mailbox-ID updates are not exposed; use guarded shared mailbox operations")
        kws = option_values(tokens, {"--add-keyword", "--remove-keyword", "--keywords"})
        for raw in kws:
            for kw in raw.replace(",", " ").split():
                if kw.casefold() not in {"$seen", "$flagged"}:
                    die(f"JMAP keyword {kw!r} is not permitted")
    if resource == "email" and action == "import":
        if has_option(tokens, "--upload-only"):
            die("upload-only blobs are not part of the guarded draft surface")
        kws = option_values(tokens, {"--keyword"})
        if any(kw.casefold() != "$draft" for raw in kws for kw in raw.replace(",", " ").split()):
            die("JMAP import permits only the $draft keyword")
        if not has_option(tokens, "--mailbox-id"):
            die("JMAP draft import requires an explicit Drafts mailbox ID")


def validate_shared(action: str, tokens: list[str]) -> str:
    # Returns read/write/send.
    if action in {"list", "check", "search", "read", "download"}:
        return "read"
    if action in {"move", "copy"}:
        reject_trash_spam(tokens)
        if not has_option(tokens, "--to"):
            die(f"message {action} requires --to")
        return "write"
    if action in {"send"}:
        return "send"
    if action in {"compose", "reply", "forward"}:
        if has_option(tokens, "--send"):
            die("render/review first, then send the approved RFC 5322 artifact with message send")
        if has_option(tokens, "--save"):
            saves = option_values(tokens, {"--save"})
            if any(v.casefold() != "drafts" for v in saves):
                die("composer may save only to the Drafts alias")
            return "write"
        return "read"  # local MIME rendering only
    if action == "add":
        boxes = option_values(tokens, {"-m", "--mailbox"})
        if boxes != ["drafts"]:
            die("message add is exposed only for the canonical Drafts alias")
        if has_option(tokens, "--send"):
            die("combined save-and-send is not exposed; save or send the reviewed artifact separately")
        return "write"
    die(f"shared action {action!r} is not exposed")


def require_authority(kind: str) -> None:
    if kind == "write" and not approved(WRITE_ENV):
        die(f"mailbox/draft write requires {WRITE_ENV}=1 for this exact command")
    if kind == "send" and not approved(SEND_ENV):
        die(f"outbound requires {SEND_ENV}=1 for this exact command")


def load_policy() -> dict:
    try:
        data = json.loads(POLICY_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read sender policy {POLICY_PATH}: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
        die("sender policy has an invalid schema")
    return data


def approved_sender(args: list[str]) -> tuple[str, str, dict, dict]:
    account = global_value(args, "-a", "--account")
    expected = os.environ.get(APPROVED_FROM_ENV, "").strip()
    if not account:
        die("outbound requires an explicit -a/--account")
    if not expected:
        die(f"outbound requires {APPROVED_FROM_ENV}=<approved address>")
    policy = load_policy()
    account_policy = policy["accounts"].get(account)
    if not isinstance(account_policy, dict):
        die(f"account {account!r} is absent from sender policy")
    senders = account_policy.get("senders")
    if not isinstance(senders, dict):
        die(f"account {account!r} has no commissioned senders")
    match = next((value for address, value in senders.items() if address.casefold() == expected.casefold()), None)
    if not isinstance(match, dict):
        die(f"sender {expected!r} is not commissioned for account {account!r}")
    return account, expected, account_policy, match


def approved_draft_id() -> str:
    draft_id = os.environ.get(APPROVED_DRAFT_ENV, "").strip()
    if not draft_id:
        die(f"outbound requires {APPROVED_DRAFT_ENV}=<verified remote Drafts id>")
    if len(draft_id) > 256 or not draft_id.replace("-", "").replace("_", "").isalnum():
        die(f"{APPROVED_DRAFT_ENV} contains an invalid draft id")
    return draft_id


def raw_message_bytes(tokens: list[str]) -> tuple[bytes, bool]:
    if has_option(tokens, "--save"):
        die("provider-native sends already persist Sent; --save is not exposed")
    positional: list[str] = []
    after_separator = False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--":
            after_separator = True
            i += 1
            continue
        if not after_separator and token.startswith("-"):
            die(f"unexpected send option {token!r}")
        positional.append(token)
        i += 1
    if not positional:
        return sys.stdin.buffer.read(), True
    if len(positional) == 1 and Path(positional[0]).is_file():
        return Path(positional[0]).read_bytes(), False
    return " ".join(positional).encode(), False


def canonicalize_crlf(raw: bytes) -> bytes:
    # Himalaya v2's MessageArg canonicalizes bare LF to CRLF before the
    # provider sees the message. Approve and hash those exact provider-bound
    # bytes rather than the Unix file representation.
    canonical = bytearray()
    previous = None
    for byte in raw:
        if byte == 0x0A and previous != 0x0D:
            canonical.append(0x0D)
        canonical.append(byte)
        previous = byte
    canonical_bytes = bytes(canonical)
    return bytes(canonical)


def verify_approved_message(raw: bytes, expected_from: str) -> bytes:
    canonical_bytes = canonicalize_crlf(raw)
    expected_hash = os.environ.get(APPROVED_HASH_ENV, "").strip().lower()
    actual_hash = hashlib.sha256(canonical_bytes).hexdigest()
    if not expected_hash:
        die(f"outbound requires {APPROVED_HASH_ENV}=<approved RFC 5322 SHA-256>")
    if expected_hash != actual_hash:
        die("RFC 5322 bytes differ from the approved outbound artifact")
    try:
        parsed = BytesParser(policy=email.policy.default).parsebytes(canonical_bytes, headersonly=True)
        addresses = [a.addr_spec for h in parsed.get_all("from", []) for a in h.addresses]
    except Exception as exc:
        die(f"cannot parse outbound From header: {exc}")
    if len(addresses) != 1 or addresses[0].casefold() != expected_from.casefold():
        die("outbound From header differs from the approved identity")
    return canonical_bytes


def message_id_header(raw: bytes) -> str:
    try:
        parsed = BytesParser(policy=email.policy.default).parsebytes(raw, headersonly=True)
    except Exception as exc:
        die(f"cannot parse outbound Message-ID header: {exc}")
    value = str(parsed.get("message-id", "")).strip().strip("<>")
    if not value:
        die("outbound reviewed artifact requires a stable Message-ID header")
    return value


def jmap_message_id(value: object) -> str:
    """Return one canonical JMAP Message-ID, otherwise fail closed."""
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], str):
        return ""
    return value[0].strip().strip("<>")


def jmap_secret(account_policy: dict) -> str:
    path = Path(str(account_policy.get("secret_file", "")))
    try:
        stat = path.lstat()
    except OSError as exc:
        die(f"cannot read commissioned JMAP token file: {exc}")
    if path.is_symlink() or not path.is_file() or stat.st_mode & 0o077:
        die("commissioned JMAP token file is missing, symlinked, or too permissive")
    token = path.read_text().strip()
    if not token:
        die("commissioned JMAP token file is empty")
    return token


def json_request(url: str, token: str, *, data: bytes | None = None, content_type: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    request = Request(url, data=data, method="POST" if data is not None else "GET", headers=headers)
    try:
        with urlopen(request, timeout=60) as response:
            body = response.read()
    except Exception as exc:
        die(f"JMAP transport outcome is uncertain; do not retry before checking Sent: {exc}", code=75)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        die("JMAP returned non-JSON; outcome is uncertain, do not retry before checking Sent", code=75)
    if not isinstance(parsed, dict):
        die("JMAP returned an invalid JSON object; outcome is uncertain", code=75)
    return parsed


def jmap_method(response: dict, tag: str) -> tuple[str, dict]:
    for item in response.get("methodResponses", []):
        if isinstance(item, list) and len(item) == 3 and item[2] == tag and isinstance(item[1], dict):
            return str(item[0]), item[1]
    die(f"JMAP response omitted method tag {tag}; outcome is uncertain", code=75)


def jmap_email_state(api_url: str, token: str, account_id: str, email_id: str) -> tuple[dict, dict]:
    payload = {
        "using": ["urn:ietf:params:jmap:mail"],
        "methodCalls": [["Email/get", {"accountId": account_id, "ids": [email_id], "properties": ["mailboxIds", "keywords"]}, "verify"]],
    }
    name, args = jmap_method(
        json_request(api_url, token, data=json.dumps(payload).encode(), content_type="application/json"),
        "verify",
    )
    if name != "Email/get" or not args.get("list"):
        die("submitted JMAP email could not be verified; do not retry")
    email_state = args["list"][0]
    return email_state.get("mailboxIds", {}), email_state.get("keywords", {})


def jmap_verify_remote_draft(api_url: str, token: str, account_id: str,
                             draft_id: str, drafts_mailbox_id: str, raw: bytes) -> None:
    payload = {
        "using": ["urn:ietf:params:jmap:mail"],
        "methodCalls": [["Email/get", {
            "accountId": account_id,
            "ids": [draft_id],
            "properties": ["mailboxIds", "keywords", "messageId"],
        }, "draft-preflight"]],
    }
    name, args = jmap_method(
        json_request(api_url, token, data=json.dumps(payload).encode(), content_type="application/json"),
        "draft-preflight",
    )
    if name != "Email/get" or not args.get("list"):
        die("approved remote JMAP draft does not exist")
    state = args["list"][0]
    mailbox_ids = state.get("mailboxIds", {})
    keywords = state.get("keywords", {})
    if not mailbox_ids.get(drafts_mailbox_id) or not keywords.get("$draft"):
        die("approved remote JMAP message is not currently in Drafts")
    remote_message_id = jmap_message_id(state.get("messageId"))
    if not remote_message_id or remote_message_id != message_id_header(raw):
        die("approved remote JMAP draft does not match the reviewed Message-ID")


def jmap_consume_approved_draft(api_url: str, token: str, account_id: str,
                                draft_id: str) -> None:
    payload = {
        "using": ["urn:ietf:params:jmap:mail"],
        "methodCalls": [["Email/set", {
            "accountId": account_id,
            "destroy": [draft_id],
        }, "consume-draft"]],
    }
    name, args = jmap_method(
        json_request(api_url, token, data=json.dumps(payload).encode(), content_type="application/json"),
        "consume-draft",
    )
    destroyed = args.get("destroyed", [])
    if name != "Email/set" or args.get("notDestroyed") or draft_id not in destroyed:
        die("approved remote JMAP draft could not be consumed; message was not submitted")


def jmap_send_approved(raw: bytes, account_policy: dict, sender_policy: dict,
                       approved_remote_draft_id: str) -> int:
    token = jmap_secret(account_policy)
    session_url = str(account_policy.get("session_url", "https://api.fastmail.com/jmap/session"))
    session = json_request(session_url, token)
    mail_cap = "urn:ietf:params:jmap:mail"
    submission_cap = "urn:ietf:params:jmap:submission"
    try:
        account_id = session["primaryAccounts"][mail_cap]
        api_url = session["apiUrl"]
        upload_url = session["uploadUrl"].replace("{accountId}", account_id)
        drafts_id = account_policy["mailboxes"]["drafts"]
        sent_id = account_policy["mailboxes"]["sent"]
        identity_id = sender_policy["identity_id"]
    except (KeyError, TypeError, AttributeError):
        die("commissioned JMAP session/policy lacks account, mailbox, or identity data")

    jmap_verify_remote_draft(
        api_url, token, account_id, approved_remote_draft_id, drafts_id, raw,
    )
    jmap_consume_approved_draft(
        api_url, token, account_id, approved_remote_draft_id,
    )

    upload = json_request(upload_url, token, data=raw, content_type="message/rfc822")
    blob_id = upload.get("blobId")
    if not blob_id:
        die("JMAP upload returned no blobId; message was not submitted")

    payload = {
        "using": [mail_cap, submission_cap],
        "methodCalls": [
            ["Email/import", {
                "accountId": account_id,
                "emails": {"outgoing": {"blobId": blob_id, "mailboxIds": {drafts_id: True}, "keywords": {"$draft": True}}},
            }, "import"],
            ["EmailSubmission/set", {
                "accountId": account_id,
                "create": {"outgoing": {"identityId": identity_id, "emailId": "#outgoing"}},
                "onSuccessUpdateEmail": {"#outgoing": {
                    f"mailboxIds/{drafts_id}": None,
                    f"mailboxIds/{sent_id}": True,
                    "keywords/$draft": None,
                    "keywords/$seen": True,
                }},
            }, "submit"],
        ],
    }
    response = json_request(api_url, token, data=json.dumps(payload).encode(), content_type="application/json")
    import_name, imported = jmap_method(response, "import")
    submission_name, submitted = jmap_method(response, "submit")
    if import_name != "Email/import" or imported.get("notCreated"):
        die("JMAP Email/import rejected the outbound message; it was not submitted")
    if submission_name != "EmailSubmission/set" or submitted.get("notCreated"):
        die("JMAP EmailSubmission/set rejected the outbound message; it was not sent")
    try:
        email_id = imported["created"]["outgoing"]["id"]
        submitted["created"]["outgoing"]
    except (KeyError, TypeError):
        die("JMAP submission response lacked creation receipts; outcome is uncertain", code=75)

    mailbox_ids, keywords = jmap_email_state(api_url, token, account_id, email_id)
    filed = bool(mailbox_ids.get(sent_id)) and not mailbox_ids.get(drafts_id) and not keywords.get("$draft")
    if not filed:
        repair = {
            "using": [mail_cap],
            "methodCalls": [["Email/set", {
                "accountId": account_id,
                "update": {email_id: {
                    f"mailboxIds/{drafts_id}": None,
                    f"mailboxIds/{sent_id}": True,
                    "keywords/$draft": None,
                    "keywords/$seen": True,
                }},
            }, "repair"]],
        }
        repair_name, repaired = jmap_method(
            json_request(api_url, token, data=json.dumps(repair).encode(), content_type="application/json"),
            "repair",
        )
        if repair_name != "Email/set" or repaired.get("notUpdated"):
            print("himalaya: message was sent but could not be filed in Sent; do not resend", file=sys.stderr)
            return 0
        mailbox_ids, keywords = jmap_email_state(api_url, token, account_id, email_id)
        if not (mailbox_ids.get(sent_id) and not mailbox_ids.get(drafts_id) and not keywords.get("$draft")):
            print("himalaya: message was sent but Sent filing could not be verified; do not resend", file=sys.stderr)
            return 0
    print("Message successfully sent and filed in Sent")
    return 0


def parse_batch_args(args: list[str]) -> tuple[str, str, list[str]]:
    account = global_value(args, "-a", "--account")
    if account not in {"fastmail", "azoth", "gmail", "gmail-work"}:
        die("batch-read requires explicit -a fastmail|azoth|gmail|gmail-work")
    clean = strip_globals(args)
    if not clean or normalize(clean[0]) != "batch-read":
        die("invalid batch-read invocation")
    fmt = "metadata"
    ids: list[str] = []
    i = 1
    while i < len(clean):
        token = clean[i]
        if token == "--format":
            if i + 1 >= len(clean):
                die("batch-read --format requires a value")
            fmt = clean[i + 1].lower()
            i += 2
            continue
        if token.startswith("--format="):
            fmt = token.split("=", 1)[1].lower()
            i += 1
            continue
        if token.startswith("-"):
            die(f"unknown batch-read option {token!r}")
        ids.append(token)
        i += 1
    if fmt not in {"minimal", "metadata", "full", "raw"}:
        die("batch-read format must be minimal, metadata, full, or raw")
    if not 1 <= len(ids) <= 50:
        die("batch-read requires 1–50 message IDs")
    if any(not value.replace("-", "").replace("_", "").isalnum() for value in ids):
        die("batch-read message IDs contain invalid characters")
    return account, fmt, ids


def ortie_token(account: str) -> str:
    ortie_account = {"gmail": "gmail", "gmail-work": "gmail-work"}[account]
    try:
        result = subprocess.run(
            [ORTIE, "-c", ORTIE_CONFIG, "-a", ortie_account, "token", "show", "--auto-refresh"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        die(f"Ortie could not provide a Gmail token: {detail.strip()}")
    token = result.stdout.splitlines()[0].strip() if result.stdout else ""
    if not token:
        die("Ortie returned an empty Gmail token")
    return token


def gmail_api_request(token: str, target: str, *, data: bytes | None = None,
                      uncertain_after_commit: bool = False) -> dict:
    request = Request(
        target,
        data=data,
        method="POST" if data is not None else "GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            body = response.read()
    except Exception as exc:
        if uncertain_after_commit:
            die(
                f"Gmail send outcome is uncertain; do not retry before checking Sent: {exc}",
                code=75,
            )
        die(f"approved remote Gmail draft could not be verified before send: {exc}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        if uncertain_after_commit:
            die("Gmail send returned non-JSON; do not retry before checking Sent", code=75)
        die("approved remote Gmail draft returned non-JSON")
    if not isinstance(payload, dict):
        if uncertain_after_commit:
            die("Gmail send returned an invalid object; do not retry before checking Sent", code=75)
        die("approved remote Gmail draft returned an invalid object")
    return payload


def gmail_decode_raw(value: object, *, uncertain_after_commit: bool = False) -> bytes:
    code = 75 if uncertain_after_commit else 64
    if not isinstance(value, str) or not value:
        die("Gmail message omitted raw RFC 5322 bytes", code=code)
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        die(f"Gmail message returned invalid base64url: {exc}", code=code)


def gmail_api_base(token: str) -> str:
    """Allow local API substitution only for the isolated test token."""
    configured = GMAIL_API_BASE.rstrip("/")
    if configured == DEFAULT_GMAIL_API_BASE:
        return configured
    parsed = urlsplit(configured)
    local = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if os.environ.get("HIMALAYA_GUARD_TEST_MODE") != "1" or token != "test-access-token" or not local:
        die("Gmail API endpoint override is permitted only in isolated loopback tests")
    return configured


def gmail_address_fingerprint(message, name: str) -> tuple[tuple[str, str], ...]:
    addresses: list[tuple[str, str]] = []
    for header in message.get_all(name, []):
        for address in getattr(header, "addresses", ()):
            addresses.append((str(address.display_name), str(address.addr_spec).casefold()))
    return tuple(addresses)


def gmail_message_ids(message, name: str) -> tuple[str, ...]:
    value = " ".join(str(item) for item in message.get_all(name, []))
    return tuple(match.strip() for match in re.findall(r"<([^<>]+)>", value))


def gmail_mime_fingerprint(part) -> tuple:
    content_type = part.get_content_type().casefold()
    params = tuple(sorted(
        (str(key).casefold(), str(value).casefold() if str(key).casefold() == "charset" else str(value))
        for key, value in (part.get_params(header="content-type", failobj=[]) or [])
        if str(key).casefold() not in {content_type, "boundary"}
    ))
    disposition = part.get_content_disposition() or ""
    filename = part.get_filename() or ""
    content_id = str(part.get("content-id", "")).strip()
    if part.is_multipart():
        payload = tuple(gmail_mime_fingerprint(child) for child in part.iter_parts())
    else:
        decoded = part.get_payload(decode=True)
        if decoded is None:
            value = part.get_payload()
            decoded = value.encode(part.get_content_charset() or "utf-8") if isinstance(value, str) else b""
        if content_type.startswith("text/"):
            decoded = decoded.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            # Gmail API raw retains the RFC transport terminator while the
            # CLI read-back may omit it. Ignore exactly one final line break;
            # additional blank lines remain material.
            if decoded.endswith(b"\n"):
                decoded = decoded[:-1]
        payload = hashlib.sha256(decoded).hexdigest()
    return content_type, params, disposition, filename, content_id, payload


def gmail_semantic_fingerprint(
    raw: bytes, *, include_bcc: bool = True, include_message_id: bool = True,
) -> tuple:
    message = BytesParser(policy=email.policy.default).parsebytes(raw)
    address_names = ["from", "sender", "reply-to", "to", "cc"]
    if include_bcc:
        address_names.append("bcc")
    addresses = tuple((name, gmail_address_fingerprint(message, name)) for name in address_names)
    subject = str(message.get("subject", ""))
    message_id = str(message.get("message-id", "")).strip().strip("<>") if include_message_id else ""
    reply_ids = gmail_message_ids(message, "in-reply-to")
    reference_ids = gmail_message_ids(message, "references")
    return addresses, subject, message_id, reply_ids, reference_ids, gmail_mime_fingerprint(message)


def gmail_assert_semantic_match(reviewed: bytes, candidate: bytes, *,
                                context: str, uncertain_after_commit: bool = False,
                                include_bcc: bool = True,
                                include_message_id: bool = True) -> None:
    try:
        matches = gmail_semantic_fingerprint(
            reviewed, include_bcc=include_bcc, include_message_id=include_message_id,
        ) == gmail_semantic_fingerprint(
            candidate, include_bcc=include_bcc, include_message_id=include_message_id,
        )
    except Exception as exc:
        die(
            f"{context} could not be parsed for semantic verification: {exc}",
            code=75 if uncertain_after_commit else 64,
        )
    if not matches:
        die(
            f"{context} differs materially from the reviewed outbound artifact",
            code=75 if uncertain_after_commit else 64,
        )


def gmail_is_reply(raw: bytes) -> bool:
    message = BytesParser(policy=email.policy.default).parsebytes(raw, headersonly=True)
    return bool(gmail_message_ids(message, "in-reply-to") or gmail_message_ids(message, "references"))


def gmail_has_literal_message_id(raw: bytes) -> bool:
    message = BytesParser(policy=email.policy.default).parsebytes(raw, headersonly=True)
    values = gmail_message_ids(message, "message-id")
    return len(values) == 1 and bool(re.fullmatch(r"[^<>\s]+@[^<>\s]+", values[0]))


def gmail_verified_remote_draft(account: str, draft_id: str, raw: bytes) -> tuple[str, str, str]:
    token = ortie_token(account)
    api_base = gmail_api_base(token)
    target = (
        f"{api_base}/users/me/drafts/"
        f"{quote(draft_id, safe='')}?format=raw"
    )
    payload = gmail_api_request(token, target)
    try:
        message = payload["message"]
        labels = set(message.get("labelIds", []))
        message_id = str(message["id"])
        thread_id = str(message["threadId"])
    except (KeyError, TypeError, AttributeError):
        die("approved remote Gmail draft returned an invalid raw object")
    if "DRAFT" not in labels:
        die("approved remote Gmail message is not currently in Drafts")
    if not message_id or not thread_id:
        die("approved remote Gmail draft omitted message or thread identity")
    remote_raw = canonicalize_crlf(gmail_decode_raw(message.get("raw")))
    gmail_assert_semantic_match(raw, remote_raw, context="approved remote Gmail draft")
    return token, message_id, thread_id


def gmail_send_approved_draft(account: str, draft_id: str, raw: bytes) -> int:
    token, _, expected_thread_id = gmail_verified_remote_draft(
        account, draft_id, raw,
    )
    api_base = gmail_api_base(token)
    send_target = f"{api_base}/users/me/drafts/send"
    sent = gmail_api_request(
        token,
        send_target,
        data=json.dumps({"id": draft_id}).encode(),
        uncertain_after_commit=True,
    )
    sent_id = str(sent.get("id", ""))
    sent_thread_id = str(sent.get("threadId", ""))
    if not sent_id or not sent_thread_id:
        die("Gmail draft send returned no message/thread identity; do not retry", code=75)
    if gmail_is_reply(raw) and sent_thread_id != expected_thread_id:
        die(
            "Gmail sent message detached from the approved draft thread; do not retry",
            code=75,
        )
    verify_target = (
        f"{api_base}/users/me/messages/"
        f"{quote(sent_id, safe='')}?format=raw"
    )
    verified = gmail_api_request(token, verify_target, uncertain_after_commit=True)
    labels = set(verified.get("labelIds", []))
    verified_thread_id = str(verified.get("threadId", ""))
    if "SENT" not in labels or verified_thread_id != sent_thread_id:
        die(
            "Gmail sent state or thread continuity could not be verified; do not retry",
            code=75,
        )
    verified_raw = canonicalize_crlf(gmail_decode_raw(
        verified.get("raw"), uncertain_after_commit=True,
    ))
    gmail_assert_semantic_match(
        raw,
        verified_raw,
        context="Gmail Sent message",
        uncertain_after_commit=True,
        include_bcc=False,
        # Gmail legitimately regenerates RFC Message-ID at drafts.send. The
        # stable binding is the exact internal message id returned by POST.
        include_message_id=False,
    )
    if not gmail_has_literal_message_id(verified_raw):
        die("Gmail Sent message has no single literal Message-ID; do not retry", code=75)
    consumed_target = (
        f"{api_base}/users/me/drafts/"
        f"{quote(draft_id, safe='')}?format=minimal"
    )
    consumed_request = Request(
        consumed_target,
        method="GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urlopen(consumed_request, timeout=60):
            die("Gmail sent message was verified but the approved draft still exists; do not retry", code=75)
    except HTTPError as exc:
        if exc.code != 404:
            die(
                f"Gmail sent message was verified but draft consumption is uncertain ({exc.code}); do not retry",
                code=75,
            )
    except SystemExit:
        raise
    except Exception as exc:
        die(
            f"Gmail sent message was verified but draft consumption is uncertain: {exc}; do not retry",
            code=75,
        )
    print(f"Gmail draft `{draft_id}` sent as message `{sent_id}` in thread `{sent_thread_id}`")
    return 0


def parse_inner_http(payload: bytes) -> tuple[int, object]:
    head, separator, body = payload.partition(b"\r\n\r\n")
    if not separator:
        head, separator, body = payload.partition(b"\n\n")
    first = head.splitlines()[0].decode("ascii", errors="replace") if head else ""
    pieces = first.split()
    if len(pieces) < 2 or not pieces[1].isdigit():
        return 0, {"parseError": "invalid inner HTTP response"}
    status = int(pieces[1])
    try:
        parsed_body: object = json.loads(body) if body.strip() else None
    except json.JSONDecodeError:
        parsed_body = body.decode("utf-8", errors="replace")
    return status, parsed_body


def gmail_batch_read(account: str, fmt: str, ids: list[str]) -> int:
    boundary = "himalaya_batch_" + os.urandom(12).hex()
    chunks: list[bytes] = []
    headers = ["Subject", "From", "To", "Cc", "Date", "Message-ID"]
    for index, message_id in enumerate(ids):
        params: list[tuple[str, str]] = [("format", fmt)]
        if fmt == "metadata":
            params.extend(("metadataHeaders", value) for value in headers)
        target = f"/gmail/v1/users/me/messages/{quote(message_id, safe='')}?{urlencode(params)}"
        chunks.append(
            (
                f"--{boundary}\r\n"
                "Content-Type: application/http\r\n"
                f"Content-ID: <item-{index}>\r\n\r\n"
                f"GET {target} HTTP/1.1\r\n\r\n"
            ).encode()
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    request = Request(
        GMAIL_BATCH_URL,
        data=b"".join(chunks),
        method="POST",
        headers={
            "Authorization": f"Bearer {ortie_token(account)}",
            "Content-Type": f"multipart/mixed; boundary={boundary}",
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            response_type = response.headers.get("Content-Type", "")
            response_body = response.read()
    except Exception as exc:
        die(f"Gmail batch transport failed: {exc}")
    mime = BytesParser(policy=email.policy.default).parsebytes(
        f"MIME-Version: 1.0\r\nContent-Type: {response_type}\r\n\r\n".encode() + response_body
    )
    if not mime.is_multipart():
        die("Gmail batch response was not multipart")
    output: list[dict] = []
    for index, part in enumerate(mime.iter_parts()):
        payload = part.get_payload(decode=True) or b""
        status, body = parse_inner_http(payload)
        message_id = ids[index] if index < len(ids) else None
        output.append({"id": message_id, "status": status, "ok": 200 <= status < 300, "body": body})
    if len(output) != len(ids):
        die(f"Gmail batch response count mismatch: expected {len(ids)}, got {len(output)}")
    print(json.dumps(output, ensure_ascii=False))
    return 0 if all(item["ok"] for item in output) else 1


def run_batch_read(args: list[str]) -> int:
    account, fmt, ids = parse_batch_args(args)
    if account in {"gmail", "gmail-work"}:
        return gmail_batch_read(account, fmt, ids)
    forwarded = ["-a", account]
    config = global_value(args, "-c", "--config")
    if config:
        forwarded[:0] = ["-c", config]
    if "--json" in args:
        forwarded.append("--json")
    return subprocess.run([REAL, *forwarded, "jmap", "email", "get", *ids]).returncode


def validate_policy(args: list[str]) -> str:
    if any(a in {"-h", "--help", "-V", "--version"} for a in args):
        return "read"
    family, path, tail = command_path(args)

    if family == "jmap":
        if path in DENIED_JMAP:
            die(f"jmap {' '.join(path)} is hard-denied")
        if path in READ_JMAP:
            return "read"
        if path in WRITE_JMAP:
            validate_jmap_write(path[0], path[1], tail)
            return "write"
        if path in SEND_JMAP:
            if has_option(tail, "--mail-from", "--rcpt-to"):
                die("JMAP envelope overrides are not exposed")
            return "send"
        die(f"jmap {' '.join(path)} is not exposed")

    if family == "gmail":
        if path[0] == "settings":
            if path[1] == "send-as" and path[2] in {"list", "get"}:
                return "read"
            die(f"gmail {' '.join(path)} is hard-denied")
        pair = (path[0], path[1])
        if pair in DENIED_GMAIL:
            die(f"gmail {' '.join(pair)} is hard-denied")
        if pair in READ_GMAIL:
            return "read"
        if pair in WRITE_GMAIL:
            reject_trash_spam(tail)
            return "write"
        if pair in SEND_GMAIL:
            return "send"
        die(f"gmail {' '.join(pair)} is not exposed")

    if family == "flag":
        action = path[0]
        if action not in {"add", "remove"}:
            die(f"flag {action} is not exposed")
        validate_flags(tail)
        return "write"

    pair = (family, path[0])
    if pair in READ_SHARED:
        return "read"
    if family == "message":
        return validate_shared(path[0], tail)
    die(f"command {' '.join(pair)} is not exposed")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        die("interactive mode is disabled")
    if not Path(REAL).is_file() or not os.access(REAL, os.X_OK):
        print(f"himalaya: real binary missing or not executable: {REAL}", file=sys.stderr)
        raise SystemExit(127)
    clean = strip_globals(args)
    if clean and normalize(clean[0]) == "batch-read":
        raise SystemExit(run_batch_read(args))
    kind = validate_policy(args)
    require_authority(kind)
    stdin_data = None
    if kind == "send":
        family, path, tail = command_path(args)
        account, expected_from, account_policy, sender_policy = approved_sender(args)
        remote_draft_id = approved_draft_id()
        raw, _ = raw_message_bytes(tail)
        canonical_raw = verify_approved_message(raw, expected_from)
        if account_policy.get("backend") == "jmap":
            raise SystemExit(jmap_send_approved(
                canonical_raw, account_policy, sender_policy, remote_draft_id,
            ))
        raise SystemExit(gmail_send_approved_draft(
            account, remote_draft_id, canonical_raw,
        ))
    result = subprocess.run([REAL, *args], input=stdin_data)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
