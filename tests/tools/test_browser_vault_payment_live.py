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
from datetime import datetime, timedelta, timezone
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
    now = datetime.now(timezone.utc)
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
            }.get(self.path, FIELDS)
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
            "browser_vault_fill", {"handle": card.id}, task_id="payment-live"
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
    for value in (CARD["card_number"], CARD["cvc"], "07/35"):
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
            assert out == {"success": True, "filled_fields": 3}
        else:
            assert not out["success"], mutation
        assert not sup.commit_payment(i, fills)["success"]
        if mutation == "stale_session":
            sup._page_session_id = i.page_session
        print("RECEIPT hosted_oopif", mutation, "=PASS replay=REFUSED")
