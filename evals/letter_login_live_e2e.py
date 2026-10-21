"""Live E2E: photographed letter → sealed credentials → sign-in → questionnaire, with the REAL auxiliary
vision model and a REAL headless Chromium driven over the supervisor CDP socket.

A synthetic insurer letter (Versichertennummer + Einmal-Kennwort, portal printed on it) is rendered to
PNG; a local portal serves a login form whose secret field is a plain type=text input labelled
"Einmal-Kennwort" and, after a correct sign-in, a questionnaire behind a session cookie. The run proves:
the photo is sealed (no pixels routed, photo deleted, note without the secret), letter_login fills both
fields, one submit signs in, the agent keeps working in the same browser session, a blind second fill is
refused, the confirmed sign-in is renewed once after the portal's session expires (only with the user's yes,
never through after_failed_login, never twice on one confirmation), a site not printed in the letter is
refused, a sent form retires the letter, and the secret is absent from every model-facing string and from
the logs written under HERMES_HOME during the run.

Run (needs auxiliary.vision configured in HERMES_HOME and a Chromium binary):
  HERMES_E2E_CHROME=/path/to/chrome <venv>/bin/python evals/letter_login_live_e2e.py
HERMES_E2E_FAKE_VISION=1 replaces the vision model with a synthetic read and keeps the real browser.
"""
from __future__ import annotations

import asyncio
import glob
import http.cookies
import http.server
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "Kq7xP2mT"
IDENT = "A234567895"
TASK = "letter-e2e"
STATE = {"attempts": 0, "sessions": set(), "answers": {}}

LOGIN = """<!doctype html><meta charset=utf-8><title>Anmeldung Fragebogen</title>
<h1>Fragebogen Familienversicherung</h1>{error}
<form method=post action=/fragebogen>
<label for=vsnr>Versichertennummer</label> <input id=vsnr name=vsnr type=text><br>
<label for=ekw>Einmal-Kennwort</label> <input id=ekw name=ekw type=text autocomplete=off><br>
<label for=q>Suche</label> <input id=q name=q type=search><br>
<button type=submit>Anmelden</button></form>"""
ANKETA = """<!doctype html><meta charset=utf-8><title>Fragebogen</title><h1>Angaben zum Einkommen</h1>
<form method=post action=/anketa><label for=inc>Monatliches Bruttoeinkommen</label> <input id=inc name=inc>
<button type=submit>Speichern</button></form>"""


class _Portal(http.server.BaseHTTPRequestHandler):
    def _session(self) -> str:
        jar = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
        return jar["sid"].value if "sid" in jar else ""

    def _send(self, body: str, status: int = 200, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path.startswith("/anketa") and self._session() in STATE["sessions"]:
            return self._send(ANKETA)
        self._send(LOGIN.format(error=""))

    def do_POST(self):
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
        if self.path.startswith("/anketa") and self._session() in STATE["sessions"]:
            STATE["answers"].update({k: v[0] for k, v in form.items()})
            return self._send(ANKETA)
        STATE["attempts"] += 1
        if form.get("vsnr") == [IDENT] and form.get("ekw") == [SECRET]:
            sid = secrets.token_hex(8)
            STATE["sessions"].add(sid)
            return self._send("", 303, {"Location": "/anketa", "Set-Cookie": f"sid={sid}; Path=/; HttpOnly"})
        self._send(LOGIN.format(error="<p class=error>Anmeldung fehlgeschlagen</p>"), 401)

    def log_message(self, *args):
        pass


def _render_letter(path: Path, portal: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    fonts = glob.glob("/usr/share/fonts/**/DejaVuSans.ttf", recursive=True) + glob.glob("/System/Library/Fonts/Supplemental/Arial.ttf")
    font = ImageFont.truetype(fonts[0], 30) if fonts else ImageFont.load_default(size=30)
    img = Image.new("RGB", (1400, 1100), "white")
    d = ImageDraw.Draw(img)
    lines = [
        "Beispiel Krankenkasse  ·  Postfach 12 34  ·  20001 Hamburg", "", "Herrn Max Mustermann", "Musterweg 5, 10115 Berlin",
        "", "Hamburg, 14. Oktober 2026", "", "Ihr Fragebogen zur Familienversicherung und zum Einkommen", "",
        "bitte füllen Sie den Fragebogen bis zum 30. November 2026 online aus.",
        f"Melden Sie sich dazu an unter: {portal}", "",
        f"Versichertennummer:   {IDENT}", f"Einmal-Kennwort:      {SECRET}", "",
        "Das Einmal-Kennwort gilt nur für diesen Fragebogen.", "", "Mit freundlichen Grüßen", "Ihre Beispiel Krankenkasse",
    ]
    for i, line in enumerate(lines):
        d.text((80, 60 + i * 50), line, fill="black", font=font)
    img.save(path)


def _chrome(work: Path, url: str) -> tuple[subprocess.Popen, str]:
    binary = os.environ.get("HERMES_E2E_CHROME") or next(iter(sorted(glob.glob("/opt/hermes/tools/chromium-*/chrome-linux64/chrome"))), "")
    assert binary, "set HERMES_E2E_CHROME to a Chromium binary"
    profile = work / "profile"
    proc = subprocess.Popen([binary, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
                             "--remote-debugging-port=0", f"--user-data-dir={profile}", url],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = profile / "DevToolsActivePort"
    for _ in range(100):
        if port_file.exists() and port_file.read_text().strip():
            break
        time.sleep(0.1)
    port = port_file.read_text().split()[0]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as r:
        return proc, json.load(r)["webSocketDebuggerUrl"]


def _wait(sup, expr: str, want, tries: int = 50):
    value = None
    for _ in range(tries):
        value = sup.evaluate_runtime(expr).get("result")
        if value == want:
            return value
        time.sleep(0.2)
    return value


def main() -> int:
    from agent import vault_letter
    from tools import browser_vault_tool as bvt
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    home = Path(os.environ.get("HERMES_HOME") or tempfile.mkdtemp())
    work = Path(tempfile.mkdtemp(prefix="letter-e2e-", dir=home / "cache" if (home / "cache").is_dir() else None))
    started = time.time()
    seen: list[str] = []  # every model-facing string of the run
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Portal)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    portal = f"http://127.0.0.1:{srv.server_address[1]}/fragebogen"
    chrome = None
    try:
        photo = work / "letter.png"
        _render_letter(photo, portal)
        vault_letter._intake_cfg = lambda: {"enabled": True}
        if os.environ.get("HERMES_E2E_FAKE_VISION") == "1":
            reads = iter([{"has_login_secret": True, "organization": "Beispiel Krankenkasse", "purpose": "Fragebogen",
                           "portal_urls": [portal], "identifier_label": "Versichertennummer", "identifier": IDENT,
                           "secret_label": "Einmal-Kennwort", "secret": SECRET, "uncertain": False, "summary": ""},
                          {"identifier": IDENT, "secret": SECRET, "uncertain": False}])

            async def fake_vision(path, prompt):
                return next(reads)
            vault_letter._ask_vision = fake_vision
        kept, notes = asyncio.run(vault_letter.seal_letter_images([str(photo)]))
        seen += notes
        print("note:\n" + "\n".join(notes))
        assert kept == [] and len(notes) == 1, (kept, notes)
        assert not photo.exists(), "sealed photo still on disk"
        handle = notes[0].split("letter handle: ")[1].split()[0]
        cred = vault_letter.get_letter(handle)
        assert cred.status == "ready", f"extraction not clean: {cred.public()}"
        assert cred.identifier == IDENT, cred.public()
        print("1. sealed:", json.dumps(cred.public(), ensure_ascii=False))

        chrome, ws = _chrome(work, portal)
        bvt._is_managed = lambda: False
        sup = SUPERVISOR_REGISTRY.get_or_start(task_id=TASK, cdp_url=ws, dialog_policy="must_respond", dialog_timeout_s=30)
        for _ in range(50):  # Chromium opens the portal tab a moment after the debugger is up
            if sup.focus_page("", accept=bvt._TAB_PROBES["letter"]).get("ok"):
                break
            time.sleep(0.2)
        assert _wait(sup, "document.readyState", "complete") == "complete"

        raw = bvt.browser_vault_letter_login(handle, task_id=TASK)
        seen.append(raw)
        out = json.loads(raw)
        print("2. letter_login:", out)
        assert out["success"] and out["filled_fields"] == 2 and out["identifier_filled"], out
        assert _wait(sup, "document.querySelector('#q').value", "") == ""  # the search box stays untouched

        again = bvt.browser_vault_letter_login(handle, task_id=TASK)
        seen.append(again)
        assert json.loads(again)["error_type"] == "attempt_unclear", again
        print("3. blind second fill refused")

        sup.evaluate_runtime("document.querySelector('form').requestSubmit()")
        assert _wait(sup, "location.pathname", "/anketa") == "/anketa", "sign-in did not reach the questionnaire"
        assert STATE["attempts"] == 1, STATE
        sup.evaluate_runtime("document.querySelector('#inc').value = '3200'; document.querySelector('form').requestSubmit()")
        for _ in range(50):
            if STATE["answers"]:
                break
            time.sleep(0.2)
        assert STATE["answers"] == {"inc": "3200"}, STATE["answers"]
        print("4. one submit signed in; questionnaire answered in the same browser session")

        def call(fn, *args, **kw):
            raw = fn(*args, **kw)
            seen.append(raw)
            return json.loads(raw)

        assert call(bvt.browser_vault_letter_signed_in, handle, task_id=TASK)["success"]
        STATE["sessions"].clear()  # the portal's session expires
        sup.evaluate_runtime("location.href = '/anketa'")
        assert _wait(sup, "!!document.querySelector('#ekw')", True), "expired session did not show the login form"
        _wait(sup, "document.readyState", "complete")
        assert call(bvt.browser_vault_letter_login, handle, task_id=TASK,
                    after_failed_login=True)["error_type"] == "reauth_required"
        bvt._confirm_letter_reuse = lambda *a: False
        cred.last_fill_at -= 120
        assert call(bvt.browser_vault_letter_login, handle, task_id=TASK,
                    session_expired=True)["error_type"] == "reuse_declined"
        assert _wait(sup, "document.querySelector('#ekw').value", "") == ""
        bvt._confirm_letter_reuse = lambda *a: True
        out = call(bvt.browser_vault_letter_login, handle, task_id=TASK, session_expired=True)
        assert out["success"] and out["filled_fields"] == 2, out
        assert call(bvt.browser_vault_letter_login, handle, task_id=TASK,
                    session_expired=True)["error_type"] == "reauth_unproven"
        sup.evaluate_runtime("document.querySelector('form').requestSubmit()")
        assert _wait(sup, "location.pathname", "/anketa") == "/anketa", "renewed sign-in did not reach the questionnaire"
        assert STATE["attempts"] == 2, STATE
        assert call(bvt.browser_vault_letter_signed_in, handle, task_id=TASK)["success"]
        print("5. expired session renewed once with the user's yes; failed-login path and a second renewal refused")

        other = f"http://localhost:{srv.server_address[1]}/fragebogen"  # not printed in the letter
        cred.fills, cred.state = 0, "new"
        sup.evaluate_runtime(f"location.href = {json.dumps(other)}")
        _wait(sup, "location.host", f"localhost:{srv.server_address[1]}")
        _wait(sup, "document.readyState", "complete")
        wrong = bvt.browser_vault_letter_login(handle, task_id=TASK)
        seen.append(wrong)
        assert json.loads(wrong)["error_type"] == "origin_unverified", wrong
        assert _wait(sup, "document.querySelector('#ekw').value", "") == ""
        print("6. site not printed in the letter refused, nothing written")

        assert call(bvt.browser_vault_letter_done, handle)["success"]
        assert call(bvt.browser_vault_letter_login, handle, task_id=TASK,
                    session_expired=True)["error_type"] == "letter_consumed"
        assert vault_letter.get_letter(handle).secret == ""
        print("7. a sent form retires the letter")

        seen.append(bvt.browser_vault_list())
        leaks = [s[:80] for s in seen if SECRET in s]
        logs = [p for p in glob.glob(str(home / "logs" / "**" / "*"), recursive=True)
                if os.path.isfile(p) and os.path.getmtime(p) >= started]
        log_leaks = [p for p in logs if SECRET.encode() in Path(p).read_bytes()]
        assert not leaks and not log_leaks, (leaks, log_leaks)
        print(f"8. secret absent from {len(seen)} model-facing strings and {len(logs)} log files touched by the run")
        print("E2E OK")
        return 0
    finally:
        SUPERVISOR_REGISTRY.stop_all()
        if chrome is not None:
            chrome.terminate()
            chrome.wait(10)
        srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
