"""Probe a hosted card form with the REAL native payment inspector (no purchase, no real card).

Serves a merchant fixture page over self-signed HTTPS (https://localhost:<port>/) in an isolated
headless Chromium, waits for the provider's sandbox SDK to render its card fields, then prints JSON:

- ``frames``: every frame's origin, path (query and fragment dropped), parent, title and rendered size;
- ``inputs``: per frame, each input's structural metadata (type, autocomplete, name/id, labels,
  placeholder, aria-label, visibility, whether it sits in a shadow root) -- never a value;
- ``inspection``: what ``inspect_payment`` admits for ``--origins`` (documents, origins, tokens) or
  its refusal text;
- with ``--fill``: the full native ``fill_payment`` path with a synthetic test card from a disposable
  local vault and auto-accepted consent, then the post-fill structural state (field counts, the
  provider's own validity classes/aria-invalid) and whether any form submitted.

Synthetic data only: the card is the provider's published test number, the vault is disposable and
lives in a temp HERMES_HOME. Never point this at a real merchant checkout.

Run: <venv>/bin/python evals/psp_probe.py --page fixture.html --origins https://a,https://b [--fill]
     [--card 4111111111111111] [--wait 12] [--chrome /path/to/chrome]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_DUMP = r"""(() => {
  const out = [];
  const walk = (root, shadow) => {
    for (const e of root.querySelectorAll('input, select, iframe')) {
      if (e.tagName === 'IFRAME') { out.push({tag: 'iframe', title: e.title || '', shadow}); continue; }
      const r = e.getBoundingClientRect();
      const labels = e.labels ? Array.from(e.labels, l => (l.textContent || '').trim()).join(' | ') : '';
      out.push({tag: e.tagName.toLowerCase(), type: e.type || '', autocomplete: e.autocomplete || '',
        name: e.name || '', id: e.id || '', labels, placeholder: e.getAttribute('placeholder') || '',
        aria: e.getAttribute('aria-label') || '', aria_invalid: e.getAttribute('aria-invalid'),
        cls: (e.className && String(e.className).slice(0, 120)) || '', tabindex: e.tabIndex,
        aria_hidden: e.getAttribute('aria-hidden'), maxlength: e.maxLength > 0 ? e.maxLength : null,
        inputmode: e.getAttribute('inputmode') || '', visible: e.getClientRects().length > 0 && r.width > 2 &&
          r.height > 2 && getComputedStyle(e).visibility !== 'hidden', has_value: !!e.value, shadow});
    }
    for (const host of root.querySelectorAll('*')) if (host.shadowRoot) walk(host.shadowRoot, true);
  };
  walk(document, false);
  return JSON.stringify({title: document.title, inputs: out, submitted: !!window.__probeSubmitted});
})()"""


def _cert(tmp: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    c, k = tmp / "cert.pem", tmp / "key.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return c, k


def _serve(page: Path, tmp: Path) -> tuple[ThreadingHTTPServer, str]:
    body = page.read_bytes()
    static = page.parent

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            data, ctype = body, "text/html; charset=utf-8"
            if path not in ("/", "/index.html"):
                f = (static / path.lstrip("/")).resolve()
                if static.resolve() in f.parents and f.is_file():
                    data = f.read_bytes()
                    ctype = "application/javascript" if f.suffix == ".js" else "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    cert, key = _cert(tmp)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"https://localhost:{srv.server_port}"


def _chrome(binary: str, profile: Path, url: str) -> tuple[subprocess.Popen, str]:
    proc = subprocess.Popen([binary, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
                             "--no-first-run", "--no-default-browser-check", "--site-per-process",
                             "--ignore-certificate-errors", "--window-size=1280,1600", url],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = profile / "DevToolsActivePort"
    for _ in range(200):
        if port_file.exists() and port_file.read_text().strip():
            break
        time.sleep(0.1)
    port = port_file.read_text().split()[0]
    with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as r:
        return proc, json.load(r)["webSocketDebuggerUrl"]


async def _dump(sup) -> dict:
    """Frame tree plus per-frame input metadata, read from each frame's own session."""
    from tools.browser_supervisor_payment import _value

    with sup._state_lock:
        sid = sup._page_session_id
    child: dict = {}
    tree = await sup._payment_tree(sid, child)
    frames, inputs = [], {}
    for fid, _loader, url, origin, parent in tree:
        parts = urlsplit(url)
        frames.append({"id": fid[:8], "origin": origin, "path": parts.path[:120], "parent": parent[:8]})
        fsid = child.get(fid, sid)
        try:
            ctx = (await sup._cdp("Page.createIsolatedWorld", {"frameId": fid, "worldName": "psp-probe"},
                                  session_id=fsid))["result"]["executionContextId"]
            raw = _value(await sup._cdp("Runtime.evaluate", {"expression": _DUMP, "contextId": ctx,
                                                             "returnByValue": True}, session_id=fsid))
            inputs[fid[:8]] = json.loads(raw)
        except Exception as exc:  # health: allow BLE001 -- a frame can vanish mid-probe; recorded by type
            inputs[fid[:8]] = {"error": type(exc).__name__}
    await sup._release_payment_sessions(child)
    return {"frames": frames, "inputs": inputs}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", required=True)
    ap.add_argument("--origins", default="")
    ap.add_argument("--fill", action="store_true")
    ap.add_argument("--card", default="4111111111111111")
    ap.add_argument("--cvc", default="737")
    ap.add_argument("--exp-month", default="03")
    ap.add_argument("--exp-year", default="2030")
    ap.add_argument("--holder", default="")
    ap.add_argument("--wait", type=float, default=12)
    ap.add_argument("--chrome", default=os.environ.get("HERMES_E2E_CHROME", ""))
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="psp-probe-"))
    os.environ["HERMES_HOME"] = str(tmp / "home")
    (tmp / "home").mkdir()
    chrome = args.chrome or shutil.which("google-chrome") or shutil.which("chromium")
    assert chrome, "pass --chrome"
    srv, merchant = _serve(Path(args.page).resolve(), tmp)
    proc, ws = _chrome(chrome, tmp / "profile", merchant + "/")
    from tools.browser_supervisor import SUPERVISOR_REGISTRY, _schedule

    out: dict = {"merchant": merchant}
    try:
        sup = SUPERVISOR_REGISTRY.get_or_start("psp-probe", ws)
        time.sleep(args.wait)
        out.update(_schedule(_dump(sup), sup._loop, timeout=60))
        origins = tuple(o for o in args.origins.split(",") if o)
        try:
            insp = sup.inspect_payment(merchant, frame_origins=origins)
            out["inspection"] = {"documents": [{"origin": d.origin, "tokens": [c.token for c in d.controls]}
                                               for d in insp.documents], "prompt": insp.prompt()}
            sup.discard_payment(insp)
        except Exception as exc:  # health: allow BLE001 -- any refusal is the probe result, recorded
            out["inspection"] = {"refused": type(exc).__name__, "error": str(exc)[:200]}
        if args.fill:
            out["fill"] = _fill(sup, merchant, origins, args)
            time.sleep(2)
            after = _schedule(_dump(sup), sup._loop, timeout=60)
            out["after_fill"] = {k: [{x: i.get(x) for x in ("type", "autocomplete", "name", "has_value",
                                                            "aria_invalid", "cls", "visible")}
                                     for i in v.get("inputs", []) if i.get("tag") == "input" and i.get("visible")]
                                 for k, v in after["inputs"].items()}
            out["submitted"] = any(v.get("submitted") for v in after["inputs"].values())
    finally:
        SUPERVISOR_REGISTRY.stop_all()
        proc.terminate()
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    text = json.dumps(out, indent=1, ensure_ascii=False)
    # Never print the synthetic card back. The CVC is too short to search for (placeholders such as
    # "123" or ids would match), and the dump never reads values, so the PAN check is the tripwire.
    assert args.card not in text, "card number leaked into probe output"
    print(text)
    return 0


def _fill(sup, merchant: str, origins: tuple, args) -> dict:
    from unittest.mock import patch

    from agent.vault_store import get_vault_store
    from tools import browser_vault_tool as bvt
    from tools.browser_vault_payment import fill_payment

    store = get_vault_store()
    card = {"card_number": args.card, "exp_month": args.exp_month, "exp_year": args.exp_year, "cvc": args.cvc}
    if args.holder:
        card["cardholder_name"] = args.holder
    meta = store.add_item("payment", "probe test card", card, origin=merchant)
    meta = store.get_meta(meta.id)
    from agent.vault_backends import backend_for_handle

    backend = backend_for_handle(meta.id)
    with patch.object(bvt, "_ensure_supervisor", return_value=sup), \
         patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"), \
         patch("agent.vault_payment_policy.PAYMENT_PSP_ORIGINS", origins), patch("tools.browser_vault_payment.effective_frame_origins", lambda o=(): origins):
        return json.loads(fill_payment(backend, meta, "psp-probe", merchant))


if __name__ == "__main__":
    raise SystemExit(main())
