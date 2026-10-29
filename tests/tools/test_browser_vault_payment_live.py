"""Real isolated Chromium + encrypted vault card fill. No external site/payment.

HERMES_E2E_BROWSER=1 scripts/run_tests.sh -m integration this file.
The TLS exemption is confined to this synthetic browser's disposable profile.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone, UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("HERMES_E2E_BROWSER") != "1",
        reason="explicit isolated browser opt-in required",
    ),
]

CARD = {
    "card_number": "4242424242424242",
    "exp_month": "07",
    "exp_year": "2035",
    "cvc": "837",
}
FIELDS = '<input autocomplete="cc-number"><input autocomplete="cc-exp"><input autocomplete="cc-csc">'


# Shopify checkout shape (checkout.pci.shopifyinc.com, build 09497de): one frame per card field;
# each holds its real input plus a 1px aria-hidden, tabindex=-1 honeypot for every other token.
# The Maestro-only issue date/number frames stay hidden in the merchant page until needed.
_SHOPIFY_FIELDS = {
    "number": ("cc-number", "Card number"), "expiry": ("cc-exp", "Expiration date (MM / YY)"),
    "verification_value": ("cc-csc", "Security code"), "name": ("cc-name", "Name on card"),
    "issue_date": ("off", "Issue date (MM / YY)"), "issue_number": ("off", "Issue number"),
}
_HONEYPOT_CSS = ("<style>.visually-hidden{position:absolute!important;width:1px!important;height:1px!important;"
                 "overflow:hidden!important;clip:rect(0 0 0 0)!important}</style>")


def _shopify_page(path, origins):
    if path.startswith("/shopify-field/"):
        real = path.rsplit("/", 1)[-1]
        inputs = []
        for field, (token, label) in _SHOPIFY_FIELDS.items():
            if field == real:
                inputs.append(f'<label for="{field}">{label}</label>'
                              f'<input autocomplete="{token}" id="{field}" name="{field}" data-current-field="{field}">')
            else:
                inputs.append(f'<input autocomplete="{token}" id="{field}" name="{field}" class="visually-hidden" '
                              'aria-hidden="true" data-honeypot-field tabindex="-1">')
        return _HONEYPOT_CSS + "".join(inputs)
    if path in ("/shopify-checkout", "/shopify-checkout-maestro"):
        frames = "".join(f'<iframe title="{label}" src="{origins["psp"]}/shopify-field/{field}"></iframe>'
                         for field, (_t, label) in list(_SHOPIFY_FIELDS.items())[:4])
        hidden = "" if path.endswith("maestro") else ' style="display:none"'
        issue = "".join(f'<iframe title="{label}" src="{origins["psp"]}/shopify-field/{field}"></iframe>'
                        for field, (_t, label) in list(_SHOPIFY_FIELDS.items())[4:])
        return frames + f"<div{hidden}>{issue}</div>"
    return None


# Structures observed on real provider sandboxes (website/docs/developer-guide/payment-providers.md),
# reduced to the behaviour that broke the native fill.
_RESHAPE_RELAY = "<script>addEventListener('message',e=>{if(e.data==='brand')for(const f of frames)f.postMessage('brand','*')})</script>"
_PROVIDER_PAGES = {
    # Controlled form (Stripe Card/Payment Element): every input event re-renders all fields from state
    # that each field only updates through its OWN event.
    "/controlled-frame": '<input autocomplete="cc-number" id="n"><input autocomplete="cc-exp" id="e">'
    '<input autocomplete="cc-csc" id="c"><script>const st={};for(const el of document.querySelectorAll("input"))'
    'el.addEventListener("input",()=>{st[el.id]=el.value;for(const o of document.querySelectorAll("input"))'
    'o.value=st[o.id]||""})</script>',
    # Brand detection on the number re-shapes the CVC in another frame (Braintree, Mollie, Worldpay Access).
    "/reshape-number": '<input autocomplete="cc-number"><script>document.querySelector("input")'
    '.addEventListener("input",()=>parent.postMessage("brand","*"))</script>',
    "/reshape-cvc": '<input autocomplete="cc-csc" maxlength="4"><script>addEventListener("message",'
    '()=>{document.querySelector("input").maxLength=3})</script>',
    # Mollie: a transparent, click-through focus trap beside the real field.
    "/decoy-number": '<input autocomplete="cc-number" id="cardNumber"><input id="hidden--cardNumber" '
    'autocomplete="false" style="position:absolute;opacity:0;pointer-events:none;width:300px;height:18px">',
    "/decoy-rest": '<input autocomplete="cc-exp"><input autocomplete="cc-csc">',
    # Unzer UI v2: every input inside an open shadow root, the CVC masked.
    "/shadow-frame": '<div id="h"></div><script>const r=document.getElementById("h").attachShadow({mode:"open"});'
    'r.innerHTML=\'<input name="cardNumber" aria-label="Card number"><input name="expiryDate" aria-label="Expiry date">'
    '<input type="password" name="cvc" placeholder="CVC" maxlength="4">\'</script>',
    # Worldpay HPP in German, labels only, separate month and a two-character year.
    "/worldpay-de": '<meta charset="utf-8"><label for="a">Kartennummer *</label><input id="a">'
    '<label for="b">Name des Karteninhabers *</label><input id="b">'
    '<label for="m">Ablaufmonat</label><input id="m" maxlength="2"><label for="y">Ablaufjahr</label><input id="y" maxlength="2">'
    '<label for="s">Prüfnummer</label><input id="s" maxlength="4">',
    # One provider origin serving card fields AND a wallet page with card-like inputs (PayPal).
    "/smart/buttons/w": FIELDS,
    "/smart/card-field/f": FIELDS,
}


# PayPal card fields shape (www.paypal.com/smart/card-field): name, number, expiry and CVV each in its
# own out-of-process frame. Each variant adds one way a provider can drop a value AFTER the native
# fill's immediate per-document check; ``el`` is the frame's only input. Synthetic: no PayPal code runs.
_PP_FIELDS = {"name": "cc-name", "number": "cc-number", "expiry": "cc-exp", "cvv": "cc-csc"}
_PP_CLEAR_LATER = "el.addEventListener('input',()=>setTimeout(()=>{el.value=''},300))"
_PP_VARIANTS = {
    "stable": {},
    # Controlled input that rejects the write inside its own input handler.
    "sync": {"number": "el.addEventListener('input',()=>{el.value=''})"},
    # Controlled inputs that reset from their own state a moment later (the reported symptom).
    "delayed": {"number": _PP_CLEAR_LATER, "cvv": _PP_CLEAR_LATER},
    # Brand detection on the number clears the CVC in another frame, after the CVC frame was checked.
    "brand": {"number": "el.addEventListener('input',()=>parent.postMessage('brand','*'))",
              "cvv": "addEventListener('message',e=>{if(e.data==='brand')el.value=''})"},
    # A field that clears itself shortly after it loses focus.
    "blur": {"cvv": "el.addEventListener('blur',()=>setTimeout(()=>{el.value=''},50))"},
    # A re-render that swaps the number input for a fresh, empty node.
    "replace": {"number": "el.addEventListener('input',()=>setTimeout(()=>{const n=document.createElement('input');"
                "n.autocomplete='cc-number';el.replaceWith(n)},200))"},
    # The provider re-formats the expiry ("07/35" -> "07 / 35"), which is the same value.
    "expiry": {"expiry": "el.addEventListener('input',()=>setTimeout(()=>{const d=el.value.replace(/\\D/g,'');"
               "el.value=d.slice(0,2)+' / '+d.slice(2)},100))"},
    # ... or drops the separator altogether ("07/35" -> "0735").
    "expiry_digits": {"expiry": "el.addEventListener('input',()=>{el.value=el.value.replace(/\\D/g,'')})"},
}


def _paypal_page(path, origins):
    parts = path.split("/")
    if path.startswith("/smart/card-field/") and len(parts) == 5 and parts[3] in _PP_VARIANTS:
        variant, field = parts[3], parts[4]
        script = _PP_VARIANTS[variant].get(field, "")
        return (f'<input autocomplete="{_PP_FIELDS[field]}" id="{field}">'
                f'<script>const el=document.getElementById("{field}");{script}</script>')
    if path.startswith("/pp-") and path[4:] in _PP_VARIANTS:
        relay = ("<script>addEventListener('message',e=>{if(e.data==='brand')"
                 "for(let i=0;i<frames.length;i++)frames[i].postMessage('brand','*')})</script>")
        return relay + "".join(
            f'<iframe src="{origins["psp"]}/smart/card-field/{path[4:]}/{f}"></iframe>' for f in _PP_FIELDS)
    return None


def _provider_page(path, origins):
    psp = origins.get("psp", "")
    merchants = {
        "/controlled": f'<iframe src="{psp}/controlled-frame"></iframe>',
        "/reshape": '<input autocomplete="cc-exp">' + _RESHAPE_RELAY
        + f'<iframe src="{psp}/reshape-cvc"></iframe><iframe src="{psp}/reshape-number"></iframe>',
        "/decoy": f'<iframe src="{psp}/decoy-number"></iframe><iframe src="{psp}/decoy-rest"></iframe>',
        "/shadow": f'<iframe src="{psp}/shadow-frame"></iframe>',
        "/worldpay": f'<iframe src="{psp}/worldpay-de"></iframe>',
        "/wallet": f'<iframe src="{psp}/smart/buttons/w"></iframe><iframe src="{psp}/smart/card-field/f"></iframe>',
    }
    return merchants.get(path) or _PROVIDER_PAGES.get(path)


@pytest.fixture
def payment_browser(tmp_path, record_property):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        chrome = str(mac) if mac.exists() else None
    if not chrome:
        pytest.fail("A real Chromium is required for this receipt")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic.test")])
    now = datetime.now(UTC)
    cert = (
        x509
        .CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    origins = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {
                "/split": '<input autocomplete="cc-number">',
                "/cvc": '<input autocomplete="cc-exp"><input autocomplete="cc-csc">',
                "/merchant": f'<iframe id="hosted" src="{origins["psp"]}/hosted"></iframe>',
                "/split-merchant": f'<iframe src="{origins["psp"]}/split"></iframe><iframe src="{origins["psp"]}/cvc"></iframe>',
                "/ambiguous": FIELDS + '<input autocomplete="cc-number">',
                # Datatrans Secure Fields shape: number and CVC each in their own PSP frame,
                # expiry on the merchant page.
                "/dt-number": '<input autocomplete="cc-number">',
                "/dt-cvc": '<input autocomplete="cc-csc">',
                "/dt-merchant": '<input autocomplete="cc-exp-month"><input autocomplete="cc-exp-year">'
                f'<iframe src="{origins["psp"]}/dt-number"></iframe><iframe src="{origins["psp"]}/dt-cvc"></iframe>',
                "/dt-duplicate": '<input autocomplete="cc-number"><input autocomplete="cc-exp">'
                f'<iframe src="{origins["psp"]}/dt-number"></iframe><iframe src="{origins["psp"]}/dt-cvc"></iframe>',
            }.get(self.path) or _shopify_page(self.path, origins) or _paypal_page(self.path, origins) \
                or _provider_page(self.path, origins) or FIELDS
            body = (
                '<!doctype html><form onsubmit="window.submitted=true;return false">'
                + body
                + "</form>"
            )
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)
    srv.socket = tls.wrap_socket(srv.socket, server_side=True)
    origins.update(
        merchant=f"https://localhost:{srv.server_port}",
        psp=f"https://127.0.0.1:{srv.server_port}",
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    profile = tmp_path / "chrome-profile"
    log = (tmp_path / "chromium.log").open("w")
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--site-per-process",
            "--ignore-certificate-errors",
            "--no-proxy-server",
        ],
        stdout=log,
        stderr=log,
    )
    try:
        deadline = time.monotonic() + 20
        while (
            not (profile / "DevToolsActivePort").exists()
            and time.monotonic() < deadline
        ):
            if proc.poll() is not None:
                pytest.fail("Isolated Chromium exited before CDP startup")
            time.sleep(0.1)
        port = int((profile / "DevToolsActivePort").read_text().splitlines()[0])
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            version = json.load(response)
        print(
            "BROWSER_BUILD", version["Browser"], "PROTOCOL", version["Protocol-Version"]
        )
        record_property("browser_build", version["Browser"])
        record_property("protocol_version", version["Protocol-Version"])
        supervisor = SUPERVISOR_REGISTRY.get_or_start(
            "payment-live", version["webSocketDebuggerUrl"]
        )
        yield supervisor, origins
    finally:
        SUPERVISOR_REGISTRY.stop_all()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        srv.shutdown()
        srv.server_close()
        log.close()


def navigate(sup, url):
    from tools.browser_supervisor import _schedule

    _schedule(
        sup._cdp("Page.navigate", {"url": url}, session_id=sup._page_session_id),
        sup._loop,
        timeout=10,
    )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if (
            sup.evaluate_runtime("location.href + '|' + document.readyState").get(
                "result"
            )
            == url + "|complete"
        ):
            time.sleep(0.6)
            return
        time.sleep(0.05)
    pytest.fail("Synthetic page did not load")


def test_native_encrypted_vault_fill_and_refusals(
    payment_browser, tmp_path, monkeypatch, caplog
):
    from agent.vault_store import VaultStore
    from tools.browser_vault_tool import browser_vault_fill, browser_vault_list
    from tools.registry import registry
    from agent.redact import redact_sensitive_text

    sup, origins = payment_browser
    store = VaultStore(base_dir=tmp_path / "vault")
    card = store.add_item("payment", "Synthetic card", CARD, origin=origins["merchant"])
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    navigate(sup, origins["merchant"] + "/checkout")
    listed = json.loads(browser_vault_list())
    assert any(i["handle"] == card.id and i["available"] for i in listed["items"])
    with patch(
        "tools.approval_prompt.request_elicitation_consent", return_value="accept"
    ):
        raw = registry.dispatch(
            "browser_vault", {"action": "fill", "handle": card.id}, task_id="payment-live"
        )
    result = json.loads(raw)
    assert (
        result["success"]
        and result["filled_fields"] == 3
        and result["submitted"] is False
    )
    assert sup.evaluate_runtime(
        "Array.from(document.querySelectorAll('input'),e=>e.value)"
    )["result"] == [CARD["card_number"], "07/35", CARD["cvc"]]
    assert sup.evaluate_runtime("!!window.submitted")["result"] is False
    assert (
        sup.evaluate_runtime(
            "Array.from(document.querySelectorAll('input'),e=>getComputedStyle(e).webkitTextSecurity)"
        )["result"]
        == ["disc"] * 3
    )
    from tools.browser_cdp_tool import browser_cdp
    from tools.browser_use_cli import browser_exec

    assert (
        json.loads(browser_cdp("Page.captureScreenshot", task_id="payment-live"))[
            "error_type"
        ]
        == "payment_screenshot_blocked"
    )
    assert (
        json.loads(browser_exec("capture_screenshot()", task_id="payment-live"))[
            "error_type"
        ]
        == "payment_screenshot_blocked"
    )
    from tools.computer_use.tool import handle_computer_use

    assert (
        json.loads(handle_computer_use({"action": "capture"}))["error_type"]
        == "payment_screenshot_blocked"
    )
    assert (
        json.loads(
            handle_computer_use({
                "action": "click",
                "coordinate": [0, 0],
                "capture_after": True,
            })
        )["error_type"]
        == "payment_screenshot_blocked"
    )
    assert (
        json.loads(browser_cdp("Page.captureScreenshot", task_id="sidecar-alias"))[
            "error_type"
        ]
        == "payment_screenshot_blocked"
    )
    from tools.computer_use.tool import _capture_response
    from tools.computer_use.backend import CaptureResult

    with patch(
        "tools.computer_use.tool._capture_view",
        side_effect=AssertionError("must refuse before persistence"),
    ):
        assert (
            json.loads(
                _capture_response(
                    CaptureResult(mode="som", width=1, height=1, png_b64="synthetic")
                )
            )["error_type"]
            == "payment_screenshot_blocked"
        )
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.browser_payment_privacy import payment_session_sensitive

    other_home = set_hermes_home_override(tmp_path / "profile-b")
    try:
        assert VaultStore().get_meta(card.id) is None
        assert not payment_session_sensitive("payment-live")
        assert redact_sensitive_text(CARD["cvc"], force=True) == CARD["cvc"]
    finally:
        reset_hermes_home_override(other_home)
    assert payment_session_sensitive("payment-live")
    assert CARD["cvc"] not in redact_sensitive_text(CARD["cvc"], force=True)
    # The expiry is not a secret on its own and is deliberately not registered for redaction
    # (browser_vault_payment._register_card_redaction): it would scrub common dates everywhere.
    for value in (CARD["card_number"], CARD["cvc"]):
        assert (
            value not in raw
            and value not in caplog.text
            and value not in redact_sensitive_text(value, force=True)
        )
    print(
        "RECEIPT native_encrypted_fill=PASS fields=3 no_submit=PASS text_privacy=PASS visual_mask=PASS"
    )

    for path in ("/ambiguous", "/split-merchant"):
        navigate(sup, origins["merchant"] + path)
        with patch(
            "tools.approval_prompt.request_elicitation_consent", return_value="accept"
        ) as consent:
            out = json.loads(browser_vault_fill(card.id, task_id="payment-live"))
        assert not out["success"] and not consent.called
    navigate(sup, origins["merchant"] + "/checkout")
    with patch(
        "tools.approval_prompt.request_elicitation_consent", return_value="decline"
    ):
        out = json.loads(browser_vault_fill(card.id, task_id="payment-live"))
    assert out["error_type"] == "payment_declined"
    assert (
        sup.evaluate_runtime(
            "Array.from(document.querySelectorAll('input'),e=>e.value)"
        )["result"]
        == [""] * 3
    )
    wrong = store.add_item(
        "payment", "Wrong binding", CARD, origin="https://other.test"
    )
    with patch("tools.approval_prompt.request_elicitation_consent") as consent:
        assert not json.loads(browser_vault_fill(wrong.id, task_id="payment-live"))[
            "success"
        ]
        assert not consent.called
    print(
        "RECEIPT ambiguity=PASS split_document=PASS declined_zero_write=PASS wrong_origin=PASS"
    )


def test_preloaded_second_tab_hosted_native_fill(
    payment_browser, tmp_path, monkeypatch
):
    from agent.vault_store import VaultStore
    from tools.browser_vault_tool import browser_vault_fill
    from tools.browser_supervisor import _schedule

    sup, origins = payment_browser
    _schedule(
        sup._cdp("Target.createTarget", {"url": origins["merchant"] + "/merchant"}),
        sup._loop,
        timeout=10,
    )
    time.sleep(1)
    with sup._state_lock:
        sup._frames.clear()  # Existing frames must be discovered without historical events.
    store = VaultStore(base_dir=tmp_path / "vault")
    card = store.add_item(
        "payment", "Synthetic hosted card", CARD, origin=origins["merchant"]
    )
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    # Metadata is fresh from storage; bind the synthetic PSP through the backend.
    original = store.get_meta

    def metadata(handle):
        from dataclasses import replace

        meta = original(handle)
        if meta:
            meta = replace(meta, payment_frame_origins=(origins["psp"],))
        return meta

    monkeypatch.setattr(store, "get_meta", metadata)
    # The synthetic PSP joins the REVIEWED list; card metadata alone may only narrow it.
    monkeypatch.setattr("agent.vault_payment_policy.PAYMENT_PSP_ORIGINS", (origins["psp"],))
    with patch(
        "tools.approval_prompt.request_elicitation_consent", return_value="accept"
    ):
        out = json.loads(
            browser_vault_fill(
                card.id, task_id="payment-live", merchant_origin=origins["merchant"]
            )
        )
    assert out["success"] and out["target_origin"] == origins["psp"]
    inspection = sup.inspect_payment(
        origins["merchant"], frame_origins=(origins["psp"],)
    )
    try:
        values = _schedule(
            sup._cdp(
                "Runtime.evaluate",
                {
                    "expression": "Array.from(document.querySelectorAll('input'), e=>e.value)",
                    "returnByValue": True,
                },
                session_id=inspection.frame_session,
            ),
            sup._loop,
            timeout=10,
        )
        assert values["result"]["result"]["value"] == [
            CARD["card_number"],
            "07/35",
            CARD["cvc"],
        ]
    finally:
        sup.discard_payment(inspection)
    print(
        "RECEIPT preloaded_second_tab=PASS hosted_native_vault_fill=PASS actual_values=PASS"
    )


def test_hosted_oopif_identity_races_and_single_use(payment_browser):
    from agent.vault_login_classifier import select_checkout_fills
    from agent.vault_store import PAYMENT_FIELDS

    sup, origins = payment_browser
    for mutation in (
        "normal",
        "replace",
        "navigate",
        "detach",
        "reembed",
        "expire",
        "stale_session",
    ):
        navigate(sup, origins["merchant"] + "/merchant")
        i = sup.inspect_payment(
            bound_origins=(origins["merchant"],), frame_origins=(origins["psp"],)
        )
        assert i.frame_origin == origins["psp"] and i.frame_session != i.page_session
        fills = select_checkout_fills(list(i.controls), CARD, PAYMENT_FIELDS)
        if mutation == "replace":
            from tools.browser_supervisor import _schedule

            _schedule(
                sup._cdp(
                    "Runtime.evaluate",
                    {
                        "expression": "document.querySelector('input').outerHTML='<input autocomplete=cc-number>'"
                    },
                    session_id=i.frame_session,
                ),
                sup._loop,
                timeout=10,
            )
        elif mutation in {"navigate", "detach", "reembed"}:
            expression = {
                "navigate": "document.querySelector('iframe').src += '?replacement'",
                "detach": "document.querySelector('iframe').remove()",
                "reembed": "const e=document.querySelector('iframe');e.replaceWith(e.cloneNode())",
            }[mutation]
            sup.evaluate_runtime(expression)
            time.sleep(0.3)
        elif mutation in {"expire", "stale_session"}:
            if mutation == "expire":
                i.expires_at = time.monotonic() - 1
            else:
                sup._page_session_id = "not-the-approved-session"
        out = sup.commit_payment(i, fills)
        if mutation == "normal":
            assert out == {"success": True, "filled_fields": 3, "retention_verified": True}
        else:
            assert not out["success"], mutation
        assert not sup.commit_payment(i, fills)["success"]
        if mutation == "stale_session":
            sup._page_session_id = i.page_session
        print("RECEIPT hosted_oopif", mutation, "=PASS replay=REFUSED")


def test_split_hosted_fields_fill_each_document_once(payment_browser, tmp_path, monkeypatch):
    """Number and CVC in separate PSP frames (OOPIFs) plus expiry on the merchant page: one consent
    names both origins, every document gets exactly its fields, and duplicates are refused."""
    from dataclasses import replace

    from agent.vault_store import VaultStore
    from tools.browser_supervisor import _schedule
    from tools.browser_vault_tool import browser_vault_fill

    sup, origins = payment_browser
    store = VaultStore(base_dir=tmp_path / "vault")
    card = store.add_item("payment", "Synthetic split card", CARD, origin=origins["merchant"])
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    original = store.get_meta
    monkeypatch.setattr(store, "get_meta", lambda handle: (
        replace(original(handle), payment_frame_origins=(origins["psp"],)) if original(handle) else None))
    # The synthetic PSP joins the REVIEWED list; card metadata alone may only narrow it.
    monkeypatch.setattr("agent.vault_payment_policy.PAYMENT_PSP_ORIGINS", (origins["psp"],))

    navigate(sup, origins["merchant"] + "/dt-duplicate")
    with patch("tools.approval_prompt.request_elicitation_consent", return_value="accept") as consent:
        out = json.loads(browser_vault_fill(card.id, task_id="payment-live", merchant_origin=origins["merchant"]))
    assert not out["success"] and not consent.called

    navigate(sup, origins["merchant"] + "/dt-merchant")
    with patch("tools.approval_prompt.request_elicitation_consent", return_value="accept") as consent:
        out = json.loads(browser_vault_fill(card.id, task_id="payment-live", merchant_origin=origins["merchant"]))
    assert out["success"] and out["filled_fields"] == 4 and out["submitted"] is False, out
    assert out["target_origins"] == [origins["merchant"], origins["psp"]]
    body = consent.call_args.args[1]
    assert origins["merchant"] in body and origins["psp"] in body
    inspection = sup.inspect_payment(origins["merchant"], frame_origins=(origins["psp"],))
    try:
        seen = {}
        for d in inspection.documents:
            reply = _schedule(sup._cdp("Runtime.evaluate", {
                "expression": "Array.from(document.querySelectorAll('input'), e => [e.autocomplete, e.value])",
                "returnByValue": True}, session_id=d.frame_session), sup._loop, timeout=10)
            seen.update(dict(reply["result"]["result"]["value"]))
        assert seen == {"cc-exp-month": "07", "cc-exp-year": "2035", "cc-number": CARD["card_number"],
                        "cc-csc": CARD["cvc"]}, seen
        assert len({d.frame_session for d in inspection.documents}) == 3
    finally:
        sup.discard_payment(inspection)
    assert sup.evaluate_runtime("!!window.submitted")["result"] is False
    print("RECEIPT split_hosted_fields=PASS documents=3 each_field_once=PASS duplicate_refused=PASS")

    # A frame swapped after consent: freshness covers every document, so nothing is written anywhere.
    from agent.vault_login_classifier import select_checkout_fills
    from agent.vault_store import PAYMENT_FIELDS

    for mutation in ("remove_cvc_frame", "navigate_number_frame"):
        navigate(sup, origins["merchant"] + "/dt-merchant")
        i = sup.inspect_payment(origins["merchant"], frame_origins=(origins["psp"],))
        fills = [dict(f, document=k) for k, d in enumerate(i.documents)
                 for f in select_checkout_fills(list(d.controls), CARD, PAYMENT_FIELDS)]
        sup.evaluate_runtime({
            "remove_cvc_frame": "document.querySelectorAll('iframe')[1].remove()",
            "navigate_number_frame": "document.querySelector('iframe').src += '?swapped'",
        }[mutation])
        time.sleep(0.3)
        out = sup.commit_payment(i, fills)
        assert not out["success"] and "partially_filled" not in out, (mutation, out)
        assert sup.evaluate_runtime("Array.from(document.querySelectorAll('input'), e => e.value)")["result"] == ["", ""]
        print("RECEIPT split_race", mutation, "=REFUSED zero_writes=PASS")


def test_shopify_card_fields_skip_honeypots_and_hidden_issue_frames(payment_browser, tmp_path, monkeypatch):
    """Shopify checkout: number, expiry, CVC and name each in their own PSP frame. Honeypot inputs and
    the hidden Maestro issue frames are never filled; a visible issue date is not taken for the expiry."""
    from dataclasses import replace

    from agent.vault_store import VaultStore
    from tools.browser_supervisor import _schedule
    from tools.browser_vault_tool import browser_vault_fill

    sup, origins = payment_browser
    store = VaultStore(base_dir=tmp_path / "vault")
    card = store.add_item("payment", "Synthetic Shopify card", {**CARD, "cardholder_name": "Ada Lovelace"},
                          origin=origins["merchant"])
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    original = store.get_meta
    monkeypatch.setattr(store, "get_meta", lambda handle: (
        replace(original(handle), payment_frame_origins=(origins["psp"],)) if original(handle) else None))
    # The synthetic PSP joins the REVIEWED list; card metadata alone may only narrow it.
    monkeypatch.setattr("agent.vault_payment_policy.PAYMENT_PSP_ORIGINS", (origins["psp"],))

    for path in ("/shopify-checkout", "/shopify-checkout-maestro"):
        navigate(sup, origins["merchant"] + path)
        with patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
            out = json.loads(browser_vault_fill(card.id, task_id="payment-live", merchant_origin=origins["merchant"]))
        assert out["success"] and out["filled_fields"] == 4 and out["target_origins"] == [origins["psp"]], (path, out)
        targets = _schedule(sup._cdp("Target.getTargets"), sup._loop, timeout=10)["result"]["targetInfos"]
        frames = [t for t in targets if t["type"] == "iframe" and "/shopify-field/" in t["url"]]
        assert len(frames) == 6
        for target in frames:
            field = target["url"].rsplit("/", 1)[-1]
            fsid = _schedule(sup._cdp("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}),
                             sup._loop, timeout=10)["result"]["sessionId"]
            try:
                values = _schedule(sup._cdp("Runtime.evaluate", {
                    "expression": "Object.fromEntries(Array.from(document.querySelectorAll('input'), e => [e.id, e.value]))",
                    "returnByValue": True}, session_id=fsid), sup._loop, timeout=10)["result"]["result"]["value"]
            finally:
                _schedule(sup._cdp("Target.detachFromTarget", {"sessionId": fsid}), sup._loop, timeout=10)
            want = {"number": CARD["card_number"], "expiry": "07/35", "verification_value": CARD["cvc"],
                    "name": "Ada Lovelace"}.get(field, "")
            assert values[field].replace(" ", "") == want.replace(" ", ""), (path, field, values)
            assert all(v == "" for k, v in values.items() if k != field), (path, field, "honeypot filled", values)
        print("RECEIPT shopify", path, "=PASS fields=4 honeypots_empty=PASS issue_frames_empty=PASS")



def test_provider_shapes_fill_once_without_submit(payment_browser, tmp_path, monkeypatch):
    """Each shape that broke on a real provider sandbox, filled through the native tool."""
    from dataclasses import replace

    from agent import vault_payment_policy as policy
    from agent.vault_store import VaultStore
    from tools.browser_vault_tool import browser_vault_fill

    sup, origins = payment_browser
    store = VaultStore(base_dir=tmp_path / "vault")
    card = store.add_item("payment", "Synthetic card", {**CARD, "cardholder_name": "Ada Lovelace"},
                          origin=origins["merchant"])
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    monkeypatch.setattr(policy, "PAYMENT_PSP_ORIGINS", (origins["psp"],))
    # The synthetic PSP is a reviewed provider whose card fields live under /smart/card-field only.
    monkeypatch.setattr(policy, "PAYMENT_PROVIDERS", policy.PAYMENT_PROVIDERS + (
        replace(policy.OPT_IN_PROVIDERS[0], name="synthetic", origins=(origins["psp"],), max_documents=4,
                paths=("/controlled-frame", "/reshape-cvc", "/reshape-number", "/decoy-number", "/decoy-rest", "/shadow-frame",
                       "/worldpay-de", "/smart/card-field")),))

    def fill(path):
        navigate(sup, origins["merchant"] + path)
        with patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
            out = json.loads(browser_vault_fill(card.id, task_id="payment-live", merchant_origin=origins["merchant"]))
        assert out["success"] and out["submitted"] is False and CARD["card_number"] not in json.dumps(out), (path, out)
        return out

    def frame_values(fragment):
        from tools.browser_supervisor import _schedule

        targets = _schedule(sup._cdp("Target.getTargets"), sup._loop, timeout=10)["result"]["targetInfos"]
        return [t for t in targets if t["type"] == "iframe" and fragment in t["url"]]

    assert fill("/controlled")["filled_fields"] == 3  # the Stripe Card/Payment Element failure
    assert fill("/reshape")["filled_fields"] == 3  # number written last, after the CVC frame
    assert fill("/decoy")["filled_fields"] == 3
    assert fill("/shadow")["filled_fields"] == 3
    out = fill("/worldpay")
    assert out["filled_fields"] == 5 and out["fields"] == ["cc-csc", "cc-exp-month", "cc-exp-year", "cc-name", "cc-number"]
    # The wallet page on the same origin holds a full card form and is never a target.
    assert fill("/wallet")["target_origins"] == [origins["psp"]] and len(frame_values("/smart/")) == 2


def test_paypal_shaped_fields_report_settled_retention_truthfully(payment_browser, tmp_path, monkeypatch, caplog):
    """Four hosted card-field frames on one provider origin. A value the provider drops after the
    per-document check (delayed reset, brand detection in another frame, reset on blur, a replaced
    node) must not come back as success; a stable fill and a re-formatted expiry must."""
    from dataclasses import replace

    from agent import vault_payment_policy as policy
    from agent.vault_store import VaultStore
    from tools.browser_cdp_tool import browser_cdp
    from tools.browser_supervisor import _schedule
    from tools.browser_vault_tool import browser_vault_fill

    sup, origins = payment_browser
    card_data = {**CARD, "cardholder_name": "Ada Lovelace"}
    store = VaultStore(base_dir=tmp_path / "vault")
    card = store.add_item("payment", "Synthetic card", card_data, origin=origins["merchant"])
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    monkeypatch.setattr(policy, "PAYMENT_PSP_ORIGINS", (origins["psp"],))
    monkeypatch.setattr(policy, "PAYMENT_PROVIDERS", policy.PAYMENT_PROVIDERS + (
        replace(policy.OPT_IN_PROVIDERS[0], name="synthetic-paypal", origins=(origins["psp"],), max_documents=4,
                paths=("/smart/card-field",)),))

    def frame_values(variant):
        out = {}
        targets = _schedule(sup._cdp("Target.getTargets"), sup._loop, timeout=10)["result"]["targetInfos"]
        for t in targets:
            if t["type"] == "iframe" and f"/smart/card-field/{variant}/" in t["url"]:
                fsid = _schedule(sup._cdp("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True}),
                                 sup._loop, timeout=10)["result"]["sessionId"]
                try:
                    reply = _schedule(sup._cdp("Runtime.evaluate", {
                        "expression": "document.querySelector('input').value", "returnByValue": True},
                        session_id=fsid), sup._loop, timeout=10)
                finally:
                    _schedule(sup._cdp("Target.detachFromTarget", {"sessionId": fsid}), sup._loop, timeout=10)
                out[t["url"].rsplit("/", 1)[-1]] = reply["result"]["result"]["value"]
        return out

    outcomes = {}
    for variant in _PP_VARIANTS:
        navigate(sup, origins["merchant"] + f"/pp-{variant}")
        with patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
            raw = browser_vault_fill(card.id, task_id="payment-live", merchant_origin=origins["merchant"])
        # No extra wait: the fill itself settles 1.0 s after the last write, past every scripted
        # reset (the delayed one fires at 300 ms), so the frames are final when it returns.
        outcomes[variant] = (json.loads(raw), frame_values(variant), raw)
        print("OUTCOME", variant, {k: v for k, v in outcomes[variant][0].items()
                                   if k in ("success", "error_type", "filled_fields", "field_retention")},
              {k: len(v) for k, v in outcomes[variant][1].items()})

    for variant, (out, values, raw) in outcomes.items():
        assert out["submitted"] is False, variant
        assert sup.evaluate_runtime("!!window.submitted")["result"] is False
        # Diagnostics carry states and lengths only, never a field value.
        for secret in (CARD["card_number"], "4242 4242", CARD["cvc"], "Ada Lovelace", "07/35"):
            assert secret not in raw, (variant, secret)
        empty = {f for f, v in values.items() if not v}
        if out["success"]:
            # Success is only reported when every written field still holds its value.
            assert not empty and out["filled_fields"] == 4 and out["retention_verified"] is True, (variant, out)
        else:
            assert empty or variant == "replace", (variant, out, values)
    assert outcomes["stable"][0]["success"] and outcomes["expiry"][0]["success"]
    assert outcomes["expiry_digits"][0]["success"] and outcomes["expiry_digits"][1]["expiry"] == "0735"
    assert outcomes["stable"][1] == {"name": "Ada Lovelace", "number": CARD["card_number"], "expiry": "07/35",
                                     "cvv": CARD["cvc"]}
    assert outcomes["expiry"][1]["expiry"] == "07 / 35"
    assert outcomes["sync"][0]["error_type"] == "payment_validation_failed"
    assert outcomes["sync"][0]["partially_filled"] is True
    expected = {
        "delayed": {"cc-number": "empty", "cc-csc": "empty"},
        "brand": {"cc-csc": "empty"},
        "blur": {"cc-csc": "empty"},
        "replace": {"cc-number": "replaced"},
    }
    for variant, dropped in expected.items():
        out = outcomes[variant][0]
        assert out["error_type"] == "payment_fields_not_retained" and out["partially_filled"] is True, (variant, out)
        assert out["field_retention"] == {**dict.fromkeys(_PP_FIELDS.values(), "held"), **dropped}, (variant, out)
        assert out["filled_fields"] == 4 - len(dropped), (variant, out)
        assert set(out["field_lengths"]) == set(dropped) and all(type(n) is int for n in out["field_lengths"].values())
    # The capture guard set before the secret write still holds after a fill that did not settle.
    assert json.loads(browser_cdp("Page.captureScreenshot", task_id="payment-live"))["error_type"] \
        == "payment_screenshot_blocked"
    for value in (CARD["card_number"], CARD["cvc"]):
        assert value not in caplog.text
    print("RECEIPT paypal_shape stable=PASS expiry_format=PASS expiry_digits=PASS sync_reset=REFUSED delayed_reset=REFUSED "
          "brand_reset=REFUSED blur_reset=REFUSED replaced_node=REFUSED no_submit=PASS capture_blocked=PASS")
