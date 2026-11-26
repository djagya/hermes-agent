"""A fake 1Password ``op`` for vault write tests, modelled on ``op --help`` only (not on Hermes's code).

- ``op item get <id> [--vault V] [--format json] [--reveal] [--otp]``: the item as JSON, never its passkey
  (op does not print one). ``--otp`` "outputs the primary one-time password": ASSUMED to be the item's first
  one-time-password field in field order (the real-op check of 2026-11-26 only saw items with one or with Hermes's
  field first); an empty one errors, the pessimistic reading, so enroll_totp's "empty field" outcome stays fail-safe.
- ``op item edit <id> [--vault V] [--generate-password[=recipe]] [assignment ...]``: an assignment
  ``[section.]field[type]=value`` (``\\`` escapes ``.``, ``=`` and ``\\`` in names) sets that one field, adding
  it when no field has that name. Names match ignoring spaces; the section is "optional unless multiple sections
  contain fields with the same name": without one, a uniquely named field in any section (or none) is the match,
  and several are an error. ``--template`` or data on stdin replace the whole item: the hidden passkey is gone,
  and the call is recorded as a violation.
- ``op item create --category login --vault V --title T --url U --generate-password=... username=<id>``.

Every call is logged with its argv (``calls()``); a ``fail`` file makes an edit fail echoing its argv on stderr
(as op quotes a bad assignment, with the key of each authenticator value as op reads it); a ``hang`` file makes
an edit sleep past any test timeout.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

GENERATED = "op-Generated-7%pw"

_SCRIPT = r'''#!/usr/bin/env python3
import base64, hashlib, hmac, json, os, re, select, struct, sys, time
from urllib.parse import parse_qs, urlsplit
here = os.path.dirname(os.path.abspath(__file__))
argv = sys.argv[1:]
stdin = sys.stdin.read() if select.select([sys.stdin], [], [], 0)[0] else ""
item_path, state_path = os.path.join(here, "item.json"), os.path.join(here, "state.json")
item = json.load(open(item_path, encoding="utf-8")) if os.path.exists(item_path) else {}
state = json.load(open(state_path, encoding="utf-8"))
state["calls"].append({"argv": argv, "stdin": bool(stdin)})

def save(code=0, out=None, err=None):
    json.dump(item, open(item_path, "w", encoding="utf-8"))
    json.dump(state, open(state_path, "w", encoding="utf-8"))
    if out is not None:
        print(out)
    if err is not None:
        sys.stderr.write(err + "\n")
    sys.exit(code)

VALUE_FLAGS = {"--vault", "--format", "--title", "--url", "--category", "--tags", "--template", "--fields"}
positional, flags, i = [], {}, 2
while i < len(argv):
    a = argv[i]
    if a in VALUE_FLAGS:
        flags[a] = argv[i + 1]; i += 2; continue
    if a.startswith("--"):
        name, _, value = a.partition("="); flags[name] = value; i += 1; continue
    positional.append(a); i += 1

def totp(value):
    q = parse_qs(urlsplit(value).query) if value.lower().startswith("otpauth://") else {"secret": [value]}
    key = "".join(q["secret"][0].split()).upper()
    digits, period = int(q.get("digits", ["6"])[0]), int(q.get("period", ["30"])[0])
    algo = q.get("algorithm", ["SHA1"])[0].lower()
    mac = hmac.new(base64.b32decode(key + "=" * (-len(key) % 8)), struct.pack(">Q", int(time.time()) // period),
                   getattr(hashlib, algo)).digest()
    n = (struct.unpack(">I", mac[mac[-1] & 15:][:4])[0] & 0x7FFFFFFF) % 10 ** digits
    return str(n).zfill(digits)

def split_unescaped(text, sep, limit=None):
    """Split ``text`` at unescaped ``sep`` (at most ``limit`` times); escapes are kept."""
    parts, cur, esc = [], "", False
    for at, ch in enumerate(text):
        if esc:
            cur += ch; esc = False
        elif ch == "\\":
            cur += ch; esc = True
        elif ch == sep:
            parts.append(cur); cur = ""
            if limit is not None and len(parts) == limit:
                return parts + [text[at + 1:]]
        else:
            cur += ch
    return parts + [cur]

TYPES = {"otp": "OTP", "password": "CONCEALED", "concealed": "CONCEALED", "text": "STRING", "string": "STRING",
         "email": "EMAIL", "url": "URL", "phone": "PHONE", "date": "DATE", "monthyear": "MONTH_YEAR"}

def assign(spec):
    head, value = split_unescaped(spec, "=", limit=1)
    ftype = None
    if head.endswith("]") and "[" in head:
        head, ftype = head[:head.rindex("[")], head[head.rindex("[") + 1:-1]
    names = [re.sub(r"\\(.)", r"\1", n) for n in split_unescaped(head, ".")]
    section, label = (names[0], names[1]) if len(names) == 2 else (None, names[0])
    fields = item.setdefault("fields", [])
    def same(a, b):
        return (a or "").replace(" ", "") == (b or "").replace(" ", "")
    def in_section(f):
        return section is None or same((f.get("section") or {}).get("label"), section)
    found = [f for f in fields if (same(f.get("label"), label) or f.get("id") == label) and in_section(f)]
    if len(found) > 1:
        return "[ERROR] more than one field matches %r: specify its section" % label
    match = found[0] if found else None
    if ftype == "delete":
        if match is not None:
            fields.remove(match)
        return
    if match is None:
        match = {"id": "f%d" % len(fields), "label": label, "type": "STRING", "value": ""}
        if section:
            match["section"] = {"id": "s-" + section, "label": section}
        fields.append(match)
    if ftype:
        match["type"] = TYPES[ftype]
    match["value"] = value

if argv[:2] == ["item", "get"]:
    if "--otp" in flags:
        otp = [f for f in item.get("fields", []) if f.get("type") == "OTP"]
        if not otp:
            save(1, err="[ERROR] item doesn't have a one-time password field")
        if not otp[0].get("value"):
            save(1, err="[ERROR] could not generate a one-time password: the field is empty")
        code = totp(otp[0]["value"])
        state["minted"].append(code)
        save(0, out=code)
    shown = json.loads(json.dumps(item))
    if "--reveal" not in flags:
        for f in shown.get("fields", []):
            if f.get("type") in ("CONCEALED", "OTP"):
                f.pop("value", None)
    save(0, out=json.dumps(shown))
if argv[:2] == ["item", "create"]:
    user = [a for a in positional if a.startswith("username=")][0].split("=", 1)[1]
    item = {"id": "new-item", "vault": {"id": "vault-h", "name": flags["--vault"]}, "category": "LOGIN",
            "title": flags.get("--title"), "urls": [{"href": flags.get("--url"), "primary": True}],
            "fields": [{"id": "username", "purpose": "USERNAME", "type": "STRING", "label": "username", "value": user},
                       {"id": "password", "purpose": "PASSWORD", "type": "CONCEALED", "label": "password",
                        "value": "op-Generated-7%pw"}]}
    save(0, out=json.dumps(item))
if argv[:2] == ["item", "edit"]:
    if os.path.exists(os.path.join(here, "hang")):
        time.sleep(20)
    if os.path.exists(os.path.join(here, "fail")):
        keys = [(parse_qs(urlsplit(v).query).get("secret") or [""])[0].upper() for v in
                (split_unescaped(spec, "=", limit=1)[-1] for spec in positional[1:]) if v.lower().startswith("otpauth://")]
        save(1, err="[ERROR] invalid field assignment: " + " ".join(positional[1:]) + " (keys: " + ", ".join(keys) + ")")
    if "--template" in flags or stdin:  # the template replaces the whole item: the passkey is not in it
        state["violations"].append(argv)
        state["passkey"] = None
        template = stdin or open(flags["--template"], encoding="utf-8").read()
        item = json.loads(template)
    if "--generate-password" in flags:
        for f in item.get("fields", []):
            if f.get("purpose") == "PASSWORD":
                f["value"] = "op-Generated-7%pw"
    before = json.loads(json.dumps(item))
    for spec in positional[1:]:
        error = assign(spec)
        if error:  # nothing of the call is applied
            item = before
            save(1, err=error)
    save(0, out=json.dumps(item))
save(2, err="[ERROR] unknown command")
'''


def install(folder: Path, item: dict, *, passkey: bool = True) -> SimpleNamespace:
    """Write the fake ``op`` into ``folder`` holding ``item`` (and a passkey ``item get`` never shows)."""
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / "op"
    exe.write_text(_SCRIPT, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    (folder / "item.json").write_text(json.dumps(item), encoding="utf-8")
    hidden = {"credentialId": "pk-1", "rpId": "shop.example"} if passkey else None
    (folder / "state.json").write_text(json.dumps({"calls": [], "violations": [], "minted": [], "passkey": hidden}),
                                       encoding="utf-8")

    def state() -> dict:
        return json.loads((folder / "state.json").read_text(encoding="utf-8"))

    def stored() -> dict:
        return json.loads((folder / "item.json").read_text(encoding="utf-8"))

    def use(new_item: dict) -> None:
        (folder / "item.json").write_text(json.dumps(new_item), encoding="utf-8")

    def flag(name: str) -> None:
        (folder / name).write_text("", encoding="utf-8")

    return SimpleNamespace(exe=exe, state=state, item=stored, use=use, flag=flag,
                           calls=lambda: state()["calls"], passkey=lambda: state()["passkey"])
