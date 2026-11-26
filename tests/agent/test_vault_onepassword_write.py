"""1Password writes (J-0643, D-1085/D-1086): a new password or authenticator key reaches ``op`` only inside a
mode-600 template file in a private temp dir that is gone afterwards, never argv, and a failed write keeps no
copy of it in the error. A fake ``op`` records argv and what the template held."""

from __future__ import annotations

import json
import os
import stat

import pytest

from agent.vault_backends.base import BackendError
from agent.vault_backends.onepassword import OnePasswordLoginBackend, TotpAlreadySet, clear_list_cache

pytestmark = pytest.mark.platforms("posix")  # the fake op is a shebang script

SECRET = "Fresh-Pw_7%synthetic"
KEY_URI = "otpauth://totp/Shop:jane?secret=JBSWY3DPEHPK3PXP&issuer=Shop"

_FAKE_OP = r'''#!/usr/bin/env python3
import json, os, stat, sys
here = os.path.dirname(os.path.abspath(__file__))
argv = sys.argv[1:]
entry = {"argv": argv}
if "--template" in argv:
    path = argv[argv.index("--template") + 1]
    entry.update(path=path, template=open(path, encoding="utf-8").read(),
                 file_mode=stat.S_IMODE(os.stat(path).st_mode), dir_mode=stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode))
open(os.path.join(here, "op.log"), "a").write(json.dumps(entry) + "\n")
item_path = os.path.join(here, "item.json")
if os.path.exists(os.path.join(here, "fail")) and argv[1] in ("create", "edit"):  # op echoing the template
    values = [f.get("value", "") for f in json.loads(entry["template"]).get("fields", [])]
    sys.stderr.write("[ERROR] invalid field value: " + " ".join(values) + "\n"); sys.exit(1)
if argv[:2] == ["item", "get"]:
    print(open(item_path, encoding="utf-8").read()); sys.exit(0)
if argv[:2] == ["item", "create"]:
    created = dict(json.loads(entry["template"]), id="new-item", vault={"id": "vault-h", "name": "Hermes"})
    print(json.dumps(created)); sys.exit(0)  # real op echoes the values back
if argv[:2] == ["item", "edit"]:
    open(item_path, "w", encoding="utf-8").write(entry["template"])
    print(entry["template"]); sys.exit(0)
sys.exit(2)
'''

_ITEM = {"id": "abc", "title": "Shop", "category": "LOGIN", "vault": {"id": "vault-123"},
         "urls": [{"href": "https://shop.example/login", "primary": True}],
         "fields": [{"id": "username", "type": "STRING", "purpose": "USERNAME", "label": "username", "value": "jane"},
                    {"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "label": "password",
                     "value": "old-password"},
                    {"id": "notesPlain", "type": "STRING", "purpose": "NOTES", "label": "notesPlain", "value": "n"}]}


@pytest.fixture
def op(tmp_path, monkeypatch):
    exe = tmp_path / "op"
    exe.write_text(_FAKE_OP, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "item.json").write_text(json.dumps(_ITEM), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    clear_list_cache()
    backend = OnePasswordLoginBackend({"enabled": True, "binary_path": str(exe)})
    backend._service_token = "service-token"

    def calls():
        log = tmp_path / "op.log"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    yield backend, calls, tmp_path
    clear_list_cache()


def _private_template(call):
    assert call["file_mode"] == 0o600 and call["dir_mode"] == 0o700
    assert not os.path.exists(call["path"]) and not os.path.exists(os.path.dirname(call["path"]))
    return json.loads(call["template"])


def test_create_sends_the_password_only_in_a_private_template_that_is_removed(op):
    backend, calls, _ = op
    handle = backend.save_login("", origin="https://shop.example", password=SECRET, username="jane@x.test",
                                title="Shop")
    assert handle == "op:vault-h:new-item"
    (create,) = calls()
    assert create["argv"][:4] == ["item", "create", "--vault", "Hermes"]
    assert "--url" in create["argv"] and create["argv"][create["argv"].index("--url") + 1] == "https://shop.example"
    assert not any(SECRET in a for a in create["argv"])
    fields = {f["purpose"]: f["value"] for f in _private_template(create)["fields"]}
    assert fields == {"USERNAME": "jane@x.test", "PASSWORD": SECRET}


def test_update_sets_the_password_adds_the_page_origin_and_keeps_every_other_field(op):
    backend, calls, tmp_path = op
    handle = backend.save_login("op:vault-123:abc", origin="https://accounts.shop.example", password=SECRET)
    assert handle == "op:vault-123:abc"
    get, edit = calls()
    assert get["argv"][:5] == ["item", "get", "abc", "--vault", "vault-123"] and "--reveal" in get["argv"]
    assert edit["argv"][:5] == ["item", "edit", "abc", "--vault", "vault-123"]
    assert not any(SECRET in a for a in edit["argv"])
    saved = _private_template(edit)
    assert [f["value"] for f in saved["fields"]] == ["jane", SECRET, "n"]
    assert [u["href"] for u in saved["urls"]] == ["https://shop.example/login", "https://accounts.shop.example"]


@pytest.mark.parametrize("write", ["create", "update", "totp"])
def test_a_failed_write_keeps_no_copy_of_the_secret(op, write):
    backend, _, tmp_path = op
    (tmp_path / "fail").write_text("", encoding="utf-8")
    secret = KEY_URI if write == "totp" else SECRET
    with pytest.raises(BackendError) as failed:
        if write == "totp":
            backend.add_totp("op:vault-123:abc", secret)
        else:
            backend.save_login("op:vault-123:abc" if write == "update" else "", origin="https://shop.example",
                               password=secret, username="jane")
    exc = failed.value
    for text in (exc.detail, str(exc), exc.message(), json.dumps(exc.result())):
        assert secret not in text
    assert "invalid field value" in exc.detail  # op's own words are kept for classification


def test_totp_is_added_once_and_another_key_is_never_overwritten(op):
    backend, calls, tmp_path = op
    assert backend.add_totp("op:vault-123:abc", KEY_URI) is True
    otp = [f for f in _private_template(calls()[-1])["fields"] if f["type"] == "OTP"]
    assert [f["value"] for f in otp] == [KEY_URI]
    writes = len(calls())
    # the same key again (shown as spaced text this time): nothing written
    assert backend.add_totp("op:vault-123:abc", "jbsw y3dp ehpk 3pxp") is False
    assert [c["argv"][1] for c in calls()[writes:]] == ["get"]
    with pytest.raises(TotpAlreadySet):
        backend.add_totp("op:vault-123:abc", "KRSXG5CTMVRXEZLUKN2XAZLSKNSWC4TF")
    assert all(c["argv"][1] == "get" for c in calls()[writes:])
