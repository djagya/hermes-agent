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

from tests.waiting import wait_until

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
    # A re-render that hides the written number input and shows a fresh, empty one beside it.
    "hidden": {"number": "el.addEventListener('input',()=>setTimeout(()=>{el.style.display='none';"
               "const n=document.createElement('input');n.autocomplete='cc-number';el.after(n)},200))"},
    # The page hides the written number FRAME and shows a fresh, empty one beside it at the same path with another
    # query (the old one keeps its value; only the first document swaps itself).
    "frameswap": {"number": "if(!location.search)el.addEventListener('input',()=>setTimeout(()=>parent.postMessage('swap','*'),200))"},
    # ... and the same swap to a frame at ANOTHER path of the provider's origin (r1 F8: the approval binds the paths)
    "pathswap": {"number": "el.addEventListener('input',()=>setTimeout(()=>parent.postMessage('swap','*'),200))"},
    # A reset that lands later than one second after the last write.
    "late": {"number": "el.addEventListener('input',()=>setTimeout(()=>{el.value=''},1500))"},
    # The provider re-formats the expiry ("07/35" -> "07 / 35"), which is the same value.
    "expiry": {"expiry": "el.addEventListener('input',()=>setTimeout(()=>{const d=el.value.replace(/\\D/g,'');"
               "el.value=d.slice(0,2)+' / '+d.slice(2)},100))"},
    # ... or drops the separator altogether ("07/35" -> "0735").
    "expiry_digits": {"expiry": "el.addEventListener('input',()=>{el.value=el.value.replace(/\\D/g,'')})"},
}


_PP_GUEST_FORM = ('<label for="email">E-Mail</label><input id="email" name="email" autocomplete="email" value="a@b.example">'
                  '<label for="n">Kreditkartennummer</label><input id="n" name="cardnumber" type="tel" autocomplete="cc-number">'
                  '<label for="e">G\u00fcltig bis</label><input id="e" name="expiry-date" type="tel" autocomplete="cc-exp">'
                  '<label for="c">Pr\u00fcfnr.</label><input id="c" name="credit-card-security" type="tel" autocomplete="cc-csc">'
                  '<label for="p">Handy</label><input id="p" name="phone" type="tel" autocomplete="tel">')


def _paypal_page(path, origins):
    path = path.split("?")[0]  # a swapped frame carries a query
    parts = path.split("/")
    if path == "/pp-guest":
        return f'<iframe src="{origins["psp"]}/smart/buttons/g"></iframe>'
    if path == "/smart/buttons/g":
        return f'<button>PayPal</button><iframe src="{origins["psp"]}/smart/card-fields/g"></iframe>'
    if path == "/smart/card-fields/g":
        return _PP_GUEST_FORM
    if path.startswith("/smart/card-field/") and len(parts) == 5 and parts[3] in _PP_VARIANTS:
        variant, field = parts[3], parts[4]
        script = _PP_VARIANTS[variant].get(field, "")
        return (f'<input autocomplete="{_PP_FIELDS[field]}" id="{field}">'
                f'<script>const el=document.getElementById("{field}");{script}</script>')
    if path.startswith("/pp-") and path[4:] in _PP_VARIANTS:
        relay = ("<script>addEventListener('message',e=>{if(e.data==='brand')"
                 "for(let i=0;i<frames.length;i++)frames[i].postMessage('brand','*');"
                 "if(e.data==='swap')for(const f of document.querySelectorAll('iframe'))if(f.contentWindow===e.source)"
                 "{f.style.display='none';const n=document.createElement('iframe');"
                 "n.src=f.src.includes('/pathswap/')?f.src.replace('/pathswap/','/stable/'):f.src+'?swapped';f.after(n)}})</script>")
        return relay + "".join(
            f'<iframe src="{origins["psp"]}/smart/card-field/{path[4:]}/{f}"></iframe>' for f in _PP_FIELDS)
    return None


# PAYONE Client API hosted-iFrame shape (secure.pay1.de, field names from the Client API docs): one
# frame per field, expiry month and year as <select>; the brand select and the holder input stay on the
# merchant page. Synthetic: no PAYONE code runs. ``noyear`` lacks the card's expiry year.
_PAYONE_YEARS = {"ok": range(2026, 2041), "noyear": range(2026, 2035)}


def _payone_page(path, origins):
    parts = path.split("/")
    record = "<script>window.seen=[];el.addEventListener('input',()=>seen.push('input'));" \
             "el.addEventListener('change',()=>seen.push('change'))</script>"
    if path.startswith("/payone-field/") and len(parts) == 4 and parts[2] in _PAYONE_YEARS:
        field = parts[3]
        if field in ("cardpan", "cardcvc2"):
            return f'<input type="text" id="{field}" name="{field}"><script>const el=document.querySelector("input")</script>' + record
        options = {"cardexpiremonth": ["0"] + [str(m) for m in range(1, 13)],
                   "cardexpireyear": ["0"] + [str(y) for y in _PAYONE_YEARS[parts[2]]]}.get(field)
        if options is None:
            return None
        return (f'<select id="{field}" name="{field}">' + "".join(f"<option value='{o}'>{o}</option>" for o in options)
                + '</select><script>const el=document.querySelector("select")</script>' + record)
    if path.startswith("/payone-") and path[8:] in _PAYONE_YEARS:
        return ('<select id="cardtype" name="cardtype"><option value=""></option><option value="V">Visa</option><option value="M">Mastercard</option>'
                '</select><label for="cardholder">Cardholder</label><input id="cardholder" name="cardholder">'
                + "".join(f'<iframe src="{origins["psp"]}/payone-field/{path[8:]}/{f}"></iframe>'
                          for f in ("cardpan", "cardcvc2", "cardexpiremonth", "cardexpireyear")))
    return None


# dlz (Track B): a KPS-like unreviewed hosted form (secure.kps-payment.de/customer/wallet/pc/WalletInput.jsp shape,
# field names ASSUMED: the real KPS DOM was not captured). Merchant total text, the real form in one OOPIF, and
# three copies of the same form the user cannot see: display:none, clipped to nothing, covered by an overlay.
_KPS_FORM = (
    '<meta charset="utf-8"><div>Betrag: 22,66 EUR</div>'
    '<label for="h">Karteninhaber</label><input id="h" name="cardHolder">'
    '<label for="n">Kartennummer</label><input id="n" name="cardNumber">'
    '<label for="c">Prüfnummer</label><input id="c" name="cvc" maxlength="4">'
    '<select id="m" name="expMonth" aria-label="Ablaufmonat">'
    + "".join(f"<option>{m:02d}</option>" for m in range(1, 13)) + '</select>'
    '<select id="y" name="expYear" aria-label="Ablaufjahr">'
    + "".join(f"<option>{y}</option>" for y in range(2026, 2041)) + '</select>'
    '<input autocomplete="cc-number" aria-hidden="true" tabindex="-1" style="position:absolute;width:1px;height:1px">'
    "<script>window.seen=[];for(const el of document.querySelectorAll('input,select')){"
    "el.addEventListener('input',()=>seen.push(el.id+':input'));el.addEventListener('change',()=>seen.push(el.id+':change'))}"
    "</script>")


def _kps_page(path, origins):
    if path == "/customer/wallet/pc/WalletInput.jsp":
        return _KPS_FORM
    if path == "/kps-merchant":
        form = origins["psp"] + "/customer/wallet/pc/WalletInput.jsp"
        return ('<meta charset="utf-8"><p>2 × Erwachsene, Reihe 6</p><p>Gesamtbetrag 22,66 €</p>'
                f'<iframe id="kps" src="{form}?ref=x" style="width:560px;height:200px"></iframe>'
                f'<iframe src="{form}?ref=hidden" style="display:none"></iframe>'
                '<div style="display:flex">'
                f'<div style="overflow:hidden;width:0;height:0"><iframe src="{form}?ref=clipped"></iframe></div>'
                f'<div style="position:relative"><iframe src="{form}?ref=covered" style="width:280px;height:150px">'
                '</iframe><div style="position:absolute;inset:0;background:#fff"></div></div></div>')
    return None


# dlz (re-pin): a Datatrans-shaped hosted card form (pay.datatrans.com redirect/lightbox page, 11-12 SBB incident;
# its real DOM was not captured, so the re-render behaviours below are ASSUMED shapes of the class). The merchant
# page holds the lightbox frame; the lightbox page holds the brand chooser (Mastercard already chosen) and the
# expiry, with the card number and the CVC each in a Secure Fields frame on the same origin. ``single`` puts every
# field in the lightbox document. The first write into the expiry or the CVC re-renders the other one while it is
# still empty (the commit order of split documents follows the frame tree, not the DOM). Each variant is one way:
# - replace: the next field is replaced by a fresh, identical input (a re-mounted component);
# - relabel: the next field's placeholder changes (validation hint);
# - defer: the next field is hidden until the card number is in (brand detection), then shown again;
# - defer_ahead (round 2, Opus P2-2; single only, a card with a holder name): as defer, but the holder's write (the
#   first) hides the expiry while the CVC is next: the hidden field is not the next one in the queue. (A focus
#   listener cannot stand in: Chrome holds focus events back in a tab without window focus.)
# - duplicate / swap_role / navigate: what must still stop (a second visible CVC; the old CVC input re-described as
#   a promo code beside a new CVC; the field's frame changes its URL);
# - cover (round 2, Codex P1-1): the next field replaced by a fresh input under an overlay of the same document;
# - holder_swap (round 2, Codex P1-2; single only): the number input removed and the unfilled holder input (the
#   synthetic card has no holder name) relabelled as the card number.
_DTX_BRANDS = ('<div role="group" aria-label="Payment method"><button type="button" aria-pressed="true">Mastercard'
               '</button><button type="button" aria-pressed="false">Visa</button></div>')
_DTX_CVC = '<label for="cvv">Security code</label><input id="cvv" autocomplete="cc-csc" placeholder="CVC" maxlength="4">'
_DTX_EXP = '<label for="exp">Expiry date (MM/YY)</label><input id="exp" autocomplete="cc-exp" placeholder="MM/YY">'
_DTX_NUMBER = '<label for="num">Card number</label><input id="num" autocomplete="cc-number" inputmode="numeric">'
# Applied to field ``el`` (by id) when the trigger fires; ``undo`` when the card number is entered.
_DTX_MUTATIONS = {
    "replace": "const n=el.cloneNode(true);n.value='';el.replaceWith(n);",
    "relabel": "el.placeholder=el.placeholder+' (3 digits)';",
    "defer": "el.style.display='none';",
    "duplicate": "const n=el.cloneNode(true);n.id=el.id+'2';el.after(n);",
    "swap_role": "const n=el.cloneNode(true);el.autocomplete='off';el.placeholder='Promo code';el.id='promo';"
                 "if(el.labels)for(const l of el.labels)l.textContent='Promo code';el.after(n);",
    "navigate": "history.pushState({},'',location.pathname+'?step=2');",
    "defer_ahead": "el.style.display='none';",
    "cover": "const n=el.cloneNode(true);n.value='';el.replaceWith(n);const r=n.getBoundingClientRect(),"
             "o=document.createElement('div');o.style.cssText='position:fixed;background:#fff;left:'+r.left+'px;top:'"
             "+r.top+'px;width:'+r.width+'px;height:'+r.height+'px';document.body.appendChild(o);",
    "holder_swap": "document.getElementById('num').remove();document.getElementById('name').autocomplete='cc-number';",
}
_DTX_NAME = '<label for="name">Name on card</label><input id="name" autocomplete="cc-name">'
# The field whose write touches the expiry in the single layout (the CVC's, first in the fill order without a holder).
_DTX_TOUCHED_BY = {"defer_ahead": "name"}


def _dtx_script(target: str, variant: str) -> str:
    """React to 'touch' (the first write into the other field) on field ``target`` while it is still empty; show it
    again on 'number'."""
    act = _DTX_MUTATIONS[variant]
    return ("<script>let done=false;addEventListener('message',e=>{const el=document.getElementById('" + target + "');"
            "if(!el)return;if(e.data==='touch'&&!done&&!el.value){done=true;" + act + "}"
            "if(e.data==='number')el.style.display='';})</script>")


def _datatrans_page(path, origins):
    psp, parts = origins.get("psp", ""), path.split("/")
    if path.startswith("/dtx-") and path[5:] in _DTX_MUTATIONS:  # merchant: the lightbox frame
        return f'<iframe id="lb" src="{psp}/dtx/{path[5:]}/page" style="width:640px;height:460px"></iframe>'
    if path.startswith("/dtx1-") and path[6:] in _DTX_MUTATIONS:
        return f'<iframe id="lb" src="{psp}/dtx/{path[6:]}/single" style="width:640px;height:460px"></iframe>'
    if len(parts) != 4 or parts[1] != "dtx" or parts[2] not in _DTX_MUTATIONS:
        return None
    variant, kind = parts[2], parts[3]
    # Same-origin frames, events dispatched synchronously: each re-render lands inside the write that causes it,
    # so the fixture does not depend on message timing.
    send = "const send=(w,d)=>w.dispatchEvent(new MessageEvent('message',{data:d}));"
    relay = ("<script>" + send + "addEventListener('message',e=>{if(e.data==='number')for(let i=0;i<frames.length;"
             "i++)send(frames[i],e.data)})</script>")
    if kind == "page":  # expiry here; number and CVC in Secure Fields frames; writing the expiry touches the CVC
        return (_DTX_BRANDS + _DTX_EXP + relay + _dtx_script("exp", variant)
                + "<script>document.getElementById('exp').addEventListener('input',()=>{for(let i=0;i<frames.length;"
                "i++)send(frames[i],'touch')})</script>"
                + f'<iframe src="{psp}/dtx/{variant}/number" style="height:60px"></iframe>'
                + f'<iframe src="{psp}/dtx/{variant}/cvc" style="height:60px"></iframe>')
    if kind == "number":
        return _DTX_NUMBER + ("<script>" + send + "document.getElementById('num').addEventListener('input',()=>"
                              "send(parent,'number'))</script>")
    if kind == "cvc":
        return (_DTX_CVC + _dtx_script("cvv", variant) + "<script>" + send
                + "document.getElementById('cvv').addEventListener('input',()=>send(parent,'touch'))</script>")
    if kind == "single":  # one document: writing the CVC (first in the fill order) touches the expiry
        toucher = _DTX_TOUCHED_BY.get(variant, "cvv")
        return (_DTX_BRANDS + (_DTX_NAME if variant in ("holder_swap", "defer_ahead") else "") + _DTX_NUMBER + _DTX_CVC
                + _DTX_EXP + _dtx_script("exp", variant) + "<script>" + send
                + f"document.getElementById('{toucher}').addEventListener('input',()=>send(window,'touch'));"
                "document.getElementById('num').addEventListener('input',()=>send(window,'number'))</script>")
    return None


# dlz (fill-auto, 11-13): the three real stops of 11-12/13 as synthetic shapes (the real DOMs were not captured; each
# is the behaviour its stop record describes) and the changes that must still stop.
_FA_FIELDS = ('<label for="num">Card number</label><input id="num" autocomplete="cc-number">'
              '<label for="exp">Expiry date (MM/YY)</label><input id="exp" autocomplete="cc-exp" placeholder="MM/YY">'
              '<label for="c3">Security code</label><input id="c3" name="f3" maxlength="4">')
_FA_PROMO = '<label for="c4">Promo code</label><input id="c4" name="f4">'
# `log`: the fields whose value an input event changed (a kept field's no-op events change nothing and are not writes).
_FA_LOG = ("<script>window.log=[];const was=new WeakMap();document.addEventListener('input',e=>{"
           "if(e.target.value!==(was.get(e.target)||''))log.push(e.target.id);was.set(e.target,e.target.value)},true);"
           "const $=id=>document.getElementById(id),lab=id=>document.querySelector('label[for='+id+']');</script>")
_FA_SCRIPTS = {
    # SBB / pay.datatrans.com: every write re-labels the other fields (validation text), on the same elements;
    # relabelNow() does it once more while the approval is open.
    "relabel": "let n=0;document.addEventListener('input',()=>{n++;for(const id of ['num','exp','c3'])"
               "lab(id).textContent=lab(id).textContent.split(' - ')[0]+($(id).value?'':' - required ('+n+')')});"
               "window.relabelNow=()=>{lab('num').textContent='Card number - please check'};",
    # A label-only change that moves a role: the CVC's and the promo field's labels swap on the first write.
    "relabel_swap": "let d=0;document.addEventListener('input',()=>{if(d++)return;lab('c3').textContent='Promo code';"
                    "lab('c4').textContent='Security code'});",
    # A re-mounted component: the first write replaces the (still empty) CVC input by a fresh, identical one.
    "replace": "let d=0;document.addEventListener('input',()=>{if(d++)return;const n=$('c3').cloneNode(true);"
               "$('c3').replaceWith(n)});",
    # Eurowings: brand detection after the card number clears the CVC a moment later (once).
    "retain": "let d=0;$('num').addEventListener('input',()=>{if(d++)return;setTimeout(()=>{$('c3').value='';was.set($('c3'),'')},300)});",
    # ... or re-mounts it empty.
    "retain_remount": "let d=0;$('num').addEventListener('input',()=>{if(d++)return;setTimeout(()=>{"
                      "const n=$('c3').cloneNode(true);n.value='';$('c3').replaceWith(n)},300)});",
    # r3 G3: the clear follows EVERY card number input event (30 ms / 300 ms later), a repeated no-op event included.
    "retain_every30": "$('num').addEventListener('input',()=>{setTimeout(()=>{$('c3').value='';was.set($('c3'),'')},30)});",
    "retain_every": "$('num').addEventListener('input',()=>{setTimeout(()=>{$('c3').value='';was.set($('c3'),'')},300)});",
    # A CVC that never keeps its value.
    "retain_never": "document.addEventListener('input',e=>{if(e.target.id==='c3')setTimeout(()=>{$('c3').value='';was.set($('c3'),'')},100)},true);",
    # Still stops: the next field hidden for good; the top document's URL changed.
    "hide": "let d=0;document.addEventListener('input',()=>{if(d++)return;$('c3').style.display='none'});",
    "navigate": "let d=0;document.addEventListener('input',()=>{if(d++)return;history.pushState({},'',location.pathname+'?step=2')});",
    "plain": "",
}
# FlixBus / assets.payrails.io: an unreviewed hosted form that re-renders while the approval is open. On 'rerender'
# from the merchant page the form is removed and, `delay` ms later, built again from fresh elements.
_FA_PAYRAILS = ('<meta charset="utf-8"><div>Total 22,66 EUR</div><div id="root">' + _FA_FIELDS.replace(
    'name="f3"', 'autocomplete="cc-csc"') + "</div>" + _FA_LOG + "<script>addEventListener('message',e=>{"
    "if(!e.data||e.data.t!=='rerender')return;const root=$('root'),html=root.innerHTML;root.innerHTML='';"
    "setTimeout(()=>{root.innerHTML=html;parent.postMessage('rerendered','*')},e.data.delay)})</script>")


def _fill_auto_page(path, origins):
    psp, parts = origins.get("psp", ""), path.split("?")[0].split("/")
    if len(parts) == 3 and parts[1] == "fa" and parts[2] in _FA_SCRIPTS:
        return _FA_FIELDS + _FA_PROMO + _FA_LOG + "<script>" + _FA_SCRIPTS[parts[2]] + "</script>"
    if path.startswith("/fa/payrails"):
        return _FA_PAYRAILS
    if path == "/fa-hosted":  # a reviewed hosted form in one frame
        return f'<div id="slot"><iframe id="card" src="{psp}/fa/plain" style="width:560px;height:200px"></iframe></div>'
    if path == "/fa-payrails":
        return ('<meta charset="utf-8"><p>2 x Adult</p><p>Gesamtbetrag 22,66 €</p>'
                f'<iframe id="pr" src="{psp}/fa/payrails?v=1" style="width:560px;height:220px"></iframe>'
                "<script>addEventListener('message',e=>{if(e.data==='rerendered')window.rerendered=true})</script>")
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
                or _payone_page(self.path, origins) or _kps_page(self.path, origins) \
                or _datatrans_page(self.path, origins) or _fill_auto_page(self.path, origins) \
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


@pytest.mark.parametrize("auto", [False, True])
def test_paypal_shaped_fields_report_settled_retention_truthfully(payment_browser, tmp_path, monkeypatch, caplog, auto):
    """Four hosted card-field frames on one provider origin. A value the provider drops after the
    per-document check (delayed reset, brand detection in another frame, reset on blur, a replaced
    node) must not come back as success; a stable fill and a re-formatted expiry must.
    dlz (11-13): ``auto`` (vault.payment_fill_auto): a value lost once (brand detection, a re-mounted or swapped
    field or frame) is written once more on a fresh inspection and then holds; a field that drops every write still
    ends as not retained after that one repair, and success is still only what every field holds after settling."""
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
    monkeypatch.setattr("tools.browser_vault_payment.payment_fill_auto_enabled", lambda: auto)

    def frame_values(variant):
        out = {}
        targets = _schedule(sup._cdp("Target.getTargets"), sup._loop, timeout=10)["result"]["targetInfos"]
        for t in targets:
            if t["type"] == "iframe" and f"/smart/card-field/{variant}/" in t["url"] and "?swapped" not in t["url"]:
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
        # No extra wait: the fill itself settles 2.0 s after the last write, past every scripted
        # reset (the latest fires at 1.5 s), so the frames are final when it returns.
        outcomes[variant] = (json.loads(raw), frame_values(variant), raw)
        print("OUTCOME", f"auto={auto}", variant, {k: v for k, v in outcomes[variant][0].items()
                                   if k in ("success", "error_type", "filled_fields", "field_retention", "fill_reinspect")},
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
            # ``frame_values`` reads the written frames only: a swapped-out one still holds its values.
            assert empty or variant in ("replace", "hidden", "frameswap", "pathswap"), (variant, out, values)
    assert outcomes["stable"][0]["success"] and outcomes["expiry"][0]["success"]
    assert outcomes["expiry_digits"][0]["success"] and outcomes["expiry_digits"][1]["expiry"] == "0735"
    assert outcomes["stable"][1] == {"name": "Ada Lovelace", "number": CARD["card_number"], "expiry": "07/35",
                                     "cvv": CARD["cvc"]}
    assert outcomes["expiry"][1]["expiry"] == "07 / 35"
    if not auto:
        assert outcomes["sync"][0]["error_type"] == "payment_validation_failed"
    assert outcomes["sync"][0]["partially_filled"] is True
    expected = {
        "delayed": {"cc-number": "empty", "cc-csc": "empty"},
        "brand": {"cc-csc": "empty"},
        "blur": {"cc-csc": "empty"},
        "replace": {"cc-number": "replaced"},
        "hidden": {"cc-number": "replaced"},
        "frameswap": {"cc-number": "replaced"},
        "pathswap": {"cc-number": "replaced"},
        "late": {"cc-number": "empty"},
    }
    if auto:
        # Lost once: repaired by the one extra pass. Dropped on every write: still not retained after it.
        for variant in ("brand", "replace", "hidden", "frameswap"):
            out = outcomes[variant][0]
            assert out["success"] and out["fill_reinspect"] == {"count": 1, "repairs": 1}, (variant, out)
            del expected[variant]
        expected["sync"] = {"cc-number": "empty"}
        # r1 F8: the fresh frame is at another path of the origin than any approved document: never adopted
        assert outcomes["pathswap"][0]["fill_reinspect"]["refused"] == "documents_changed", outcomes["pathswap"][0]
        assert outcomes["pathswap"][0]["fill_reinspect"]["count"] == 0
        del expected["pathswap"]
        for variant in expected:
            assert outcomes[variant][0]["fill_reinspect"] == {"count": 1, "repairs": 1, "refused": "budget"}, variant
        expected["pathswap"] = {"cc-number": "replaced"}
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
          "brand_reset=REFUSED blur_reset=REFUSED replaced_node=REFUSED hidden_node=REFUSED frame_swap=REFUSED "
          "late_reset=REFUSED no_submit=PASS capture_blocked=PASS")


def test_payone_shaped_select_expiry_fills_once_or_refuses_without_writes(payment_browser, tmp_path, monkeypatch, caplog):
    """PAYONE-shaped hosted fields: number, CVC, expiry month and year each in their own provider frame,
    month and year as <select>. A matching year fills once without submit and passes the settled retention
    check; a year list without the card's year refuses before any document is written."""
    from dataclasses import replace

    from agent import vault_payment_policy as policy
    from agent.vault_store import VaultStore
    from tools.browser_supervisor import _schedule
    from tools.browser_vault_tool import browser_vault_fill

    sup, origins = payment_browser
    store = VaultStore(base_dir=tmp_path / "vault")
    card = store.add_item("payment", "Synthetic card", {**CARD, "cardholder_name": "Ada Lovelace"},
                          origin=origins["merchant"])
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    monkeypatch.setattr(policy, "PAYMENT_PSP_ORIGINS", (origins["psp"],))
    # Four provider frames plus the merchant's holder input: five documents.
    monkeypatch.setattr(policy, "PAYMENT_PROVIDERS", policy.PAYMENT_PROVIDERS + (
        replace(policy.OPT_IN_PROVIDERS[0], name="synthetic-payone", origins=(origins["psp"],), max_documents=5,
                paths=("/payone-field",)),))

    def frame_state(variant):
        out = {}
        targets = _schedule(sup._cdp("Target.getTargets"), sup._loop, timeout=10)["result"]["targetInfos"]
        for t in targets:
            if t["type"] == "iframe" and f"/payone-field/{variant}/" in t["url"]:
                fsid = _schedule(sup._cdp("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True}),
                                 sup._loop, timeout=10)["result"]["sessionId"]
                try:
                    reply = _schedule(sup._cdp("Runtime.evaluate", {
                        "expression": "[el.value, seen]", "returnByValue": True}, session_id=fsid), sup._loop, timeout=10)
                finally:
                    _schedule(sup._cdp("Target.detachFromTarget", {"sessionId": fsid}), sup._loop, timeout=10)
                out[t["url"].rsplit("/", 1)[-1]] = reply["result"]["result"]["value"]
        return out

    def merchant_values():
        return sup.evaluate_runtime("[cardtype.value, cardholder.value]")["result"]

    navigate(sup, origins["merchant"] + "/payone-ok")
    with patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
        raw = browser_vault_fill(card.id, task_id="payment-live", merchant_origin=origins["merchant"])
    out = json.loads(raw)
    assert out["success"] and out["retention_verified"] is True and out["filled_fields"] == 5, out
    assert out["submitted"] is False and sup.evaluate_runtime("!!window.submitted")["result"] is False
    assert out["fields"] == ["cc-csc", "cc-exp-month", "cc-exp-year", "cc-name", "cc-number"]
    state = frame_state("ok")
    assert {f: v[0] for f, v in state.items()} == {"cardpan": CARD["card_number"], "cardcvc2": CARD["cvc"],
                                                   "cardexpiremonth": "7", "cardexpireyear": "2035"}, state
    assert state["cardexpiremonth"][1] == state["cardexpireyear"][1] == ["input", "change"]
    assert merchant_values() == ["", "Ada Lovelace"]  # the brand select is never a card field
    for secret in (CARD["card_number"], CARD["cvc"]):
        assert secret not in raw and secret not in caplog.text
    print("RECEIPT payone_shape select_expiry=PASS retention=PASS no_submit=PASS")

    navigate(sup, origins["merchant"] + "/payone-noyear")
    with patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
        raw = browser_vault_fill(card.id, task_id="payment-live", merchant_origin=origins["merchant"])
    out = json.loads(raw)
    assert out["success"] is False and out["error_type"] == "payment_expiry_option_unmatched", out
    assert CARD["card_number"] not in raw and CARD["cvc"] not in raw
    state = frame_state("noyear")
    assert len(state) == 4 and all(v[1] == [] for v in state.values()), state  # no event reached any frame
    assert state["cardpan"][0] == state["cardcvc2"][0] == "" and merchant_values() == ["", ""]
    assert state["cardexpiremonth"][0] == "0" and state["cardexpireyear"][0] == "0"  # untouched defaults
    assert sup.evaluate_runtime("!!window.submitted")["result"] is False
    print("RECEIPT payone_shape missing_year=REFUSED zero_writes=PASS")


def test_paypal_guest_card_form_inside_buttons_fills_once(payment_browser, tmp_path, monkeypatch):
    """The Buttons guest form nests /smart/card-fields inside /smart/buttons on the provider origin.
    Admitted only because the reviewed row lists /smart/buttons as an ancestor path; the email and
    phone inputs beside the card fields are never written."""
    from dataclasses import replace

    from agent import vault_payment_policy as policy
    from agent.vault_store import VaultStore
    from tools.browser_supervisor import _schedule
    from tools.browser_vault_tool import browser_vault_fill

    sup, origins = payment_browser
    store = VaultStore(base_dir=tmp_path / "vault")
    card = store.add_item("payment", "Synthetic card", dict(CARD), origin=origins["merchant"])
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    monkeypatch.setattr(policy, "PAYMENT_PSP_ORIGINS", (origins["psp"],))
    row = replace(policy.OPT_IN_PROVIDERS[0], name="synthetic-paypal-guest", origins=(origins["psp"],),
                  max_documents=4, paths=("/smart/card-field", "/smart/card-fields"), ancestor_paths=())

    def fill(provider):
        monkeypatch.setattr(policy, "PAYMENT_PROVIDERS", policy.DEFAULT_PROVIDERS + (provider,))
        navigate(sup, origins["merchant"] + "/pp-guest")
        with patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
            return json.loads(browser_vault_fill(card.id, task_id="payment-live", merchant_origin=origins["merchant"]))

    def form_values():
        targets = _schedule(sup._cdp("Target.getTargets"), sup._loop, timeout=10)["result"]["targetInfos"]
        # /smart/card-fields is same-origin with /smart/buttons, so it lives in the buttons frame's process.
        frame = next(t for t in targets if t["type"] == "iframe" and t["url"].endswith("/smart/buttons/g"))
        fsid = _schedule(sup._cdp("Target.attachToTarget", {"targetId": frame["targetId"], "flatten": True}),
                         sup._loop, timeout=10)["result"]["sessionId"]
        try:
            reply = _schedule(sup._cdp("Runtime.evaluate", {
                "expression": "Object.fromEntries([...document.querySelector('iframe').contentDocument"
                              ".querySelectorAll('input')].map(i=>[i.id,i.value]))",
                "returnByValue": True}, session_id=fsid), sup._loop, timeout=10)
        finally:
            _schedule(sup._cdp("Target.detachFromTarget", {"sessionId": fsid}), sup._loop, timeout=10)
        return reply["result"]["result"]["value"]

    refused = fill(row)
    assert refused["success"] is False and "ancestry" in refused["error"].lower(), refused
    assert form_values()["n"] == ""

    out = fill(replace(row, ancestor_paths=("/smart/buttons",)))
    assert out["success"] is True and out["submitted"] is False and out["filled_fields"] == 3, out
    assert CARD["card_number"] not in json.dumps(out) and CARD["cvc"] not in json.dumps(out)
    values = form_values()
    assert values["n"] == CARD["card_number"] and values["c"] == CARD["cvc"] and values["e"]
    assert values["email"] == "a@b.example" and values["p"] == ""
    assert sup.evaluate_runtime("!!window.submitted")["result"] is False


def _kps_fill(payment_browser, tmp_path, monkeypatch, on_consent=None, auto=True):
    """The managed-mode seams (lease route, capture guard, A9 claim) faked around a real Chromium page; the
    unreviewed-destination switch on. Returns (first result, second result, consent kwargs, frame state)."""
    from agent.vault_store import VaultStore
    from tools.browser_supervisor import _schedule
    from tools.browser_vault_tool import browser_vault_fill

    sup, origins = payment_browser
    store = VaultStore(base_dir=tmp_path / "vault")
    card = store.add_item("payment", "Synthetic card", {**CARD, "cardholder_name": "Ada Lovelace"},
                          origin=origins["merchant"])
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    monkeypatch.setattr("tools.browser_vault_tool._is_managed", lambda: True)
    monkeypatch.setattr("tools.browser_vault_tool._managed_supervisor", lambda task_id, admit=False: sup)
    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr("tools.browser_use_cli.vault_route", lambda task_id: {"lease": "live", "token": "t"})
    monkeypatch.setattr("tools.browser_control_route.set_capture_guard", lambda lease, token: (True, ""))
    monkeypatch.setattr("tools.browser_payment_surface.claim_payment_surface", lambda s, i: None)
    monkeypatch.setattr("tools.browser_vault_payment.destination_policy", lambda: (True, "live"))
    monkeypatch.setattr("tools.browser_vault_payment.payment_fill_auto_enabled", lambda: auto)
    asked = []

    def consent(message, description, **kwargs):
        asked.append(kwargs)
        if on_consent:
            on_consent(sup, origins)
        return "accept"

    navigate(sup, origins["merchant"] + "/kps-merchant")
    with patch("tools.approval_prompt.request_elicitation_consent", side_effect=consent):
        first = browser_vault_fill(card.id, task_id="payment-live", merchant_origin=origins["merchant"])
        second = browser_vault_fill(card.id, task_id="payment-live", merchant_origin=origins["merchant"],
                                    cart={"items": [{"description": "Adult ticket", "quantity": 2}],
                                          "total": "22.66", "currency": "EUR"},
                                    approve_origins=[origins["psp"]])
    state = {}
    for t in _schedule(sup._cdp("Target.getTargets"), sup._loop, timeout=10)["result"]["targetInfos"]:
        if t["type"] == "iframe" and "/WalletInput.jsp" in t["url"]:
            fsid = _schedule(sup._cdp("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True}),
                             sup._loop, timeout=10)["result"]["sessionId"]
            try:
                reply = _schedule(sup._cdp("Runtime.evaluate", {
                    "expression": "[[...document.querySelectorAll('input,select')].map(e=>e.value), seen]",
                    "returnByValue": True}, session_id=fsid), sup._loop, timeout=10)
            finally:
                _schedule(sup._cdp("Target.detachFromTarget", {"sessionId": fsid}), sup._loop, timeout=10)
            assert "value" in reply["result"]["result"], (t["url"], reply["result"].get("exceptionDetails", {}).get("text"),
                                                          reply["result"]["result"].get("description", "")[:200])
            state[t["url"].rsplit("=", 1)[-1]] = reply["result"]["result"]["value"]
    return json.loads(first), second, asked, state, sup


def test_kps_like_unreviewed_form_fills_once_after_owner_approval(payment_browser, tmp_path, monkeypatch, caplog):
    """v1 §9 test 7 with the v2 cart: the first call names the unreviewed origin and reads nothing; the second shows
    one owner card and fills only the visible form, each select once, without submit; hidden, clipped and covered
    copies stay empty."""
    first, raw, asked, state, sup = _kps_fill(payment_browser, tmp_path, monkeypatch)
    _sup, origins = payment_browser
    assert first["error_type"] == "payment_destination_approval_required", first
    assert first["approval_origins"] == [origins["psp"]] and first["needs"] == ["cart", "approve_origins"]
    assert first["receiving"] == [{"origin": origins["psp"], "reviewed": False,
                                   "fields": ["cc-csc", "cc-exp-month", "cc-exp-year", "cc-name", "cc-number"]}]
    out = json.loads(raw)
    assert out["success"] and out["retention_verified"] is True and out["destination_reviewed"] is False, out
    assert out["page_match"] == "matched" and out["submitted"] is False
    assert len(asked) == 1 and asked[0]["payment"]["unreviewed"] is True
    assert asked[0]["payment"]["page_match"]["state"] == "matched"
    values, seen = state["x"]
    assert values[:3] == ["Ada Lovelace", CARD["card_number"], CARD["cvc"]] and values[3:5] == ["07", "2035"]
    assert values[5] == ""  # the honeypot
    assert seen.count("m:change") == 1 and seen.count("y:change") == 1
    for copy in ("hidden", "clipped", "covered"):
        assert all(v in ("", "01", "2026") for v in state[copy][0]) and state[copy][1] == [], (copy, state[copy])
    assert sup.evaluate_runtime("!!window.submitted")["result"] is False
    for secret in (CARD["card_number"], CARD["cvc"]):
        assert secret not in raw and secret not in caplog.text
    print("RECEIPT kps_like unreviewed=APPROVAL_REQUIRED_THEN_FILLED copies=EMPTY page_match=matched no_submit=PASS")


def test_kps_like_form_navigated_during_consent_writes_nothing(payment_browser, tmp_path, monkeypatch):
    """v1 §9 test 8: the approved iframe navigates while the owner's card is open: payment_page_changed, nothing
    written anywhere. dlz (11-13): this is the fill with vault.payment_fill_auto off; armed, a frame loaded again on
    the approved origin is inspected again and filled (test_fill_auto_unreviewed_form_re_rendered_during_the_approval,
    variant reload)."""
    from tools.browser_supervisor import _schedule

    def swap(sup, origins):
        _schedule(sup._cdp("Runtime.evaluate", {
            "expression": f"document.getElementById('kps').src={json.dumps(origins['psp'])}"
                          "+'/customer/wallet/pc/WalletInput.jsp?ref=x2'"}, session_id=sup._page_session_id),
            sup._loop, timeout=10)

        def navigated():  # the approved iframe's new document is live (in the page's tree or as its own target)
            tree = json.dumps(_schedule(sup._cdp("Page.getFrameTree", session_id=sup._page_session_id), sup._loop,
                                        timeout=10))
            return "ref=x2" in tree or "ref=x2" in json.dumps(_schedule(sup._cdp("Target.getTargets"), sup._loop,
                                                                        timeout=10))

        wait_until(navigated, what="the approved iframe navigated")

    _first, raw, _asked, state, _sup = _kps_fill(payment_browser, tmp_path, monkeypatch, on_consent=swap, auto=False)
    out = json.loads(raw)
    assert out["error_type"] == "payment_page_changed", out
    assert all(v in ("", "01", "2026") for values, _seen in state.values() for v in values), state
    print("RECEIPT kps_like navigated_during_consent=REFUSED zero_writes=PASS")


def _dtx_values(sup):
    """id -> value of every input in the Datatrans-shaped lightbox document and its same-origin field frames."""
    from tools.browser_supervisor import _schedule

    targets = _schedule(sup._cdp("Target.getTargets"), sup._loop, timeout=10)["result"]["targetInfos"]
    lightbox = [t for t in targets if t["type"] == "iframe" and "/dtx/" in t["url"]]
    assert len(lightbox) == 1, lightbox
    fsid = _schedule(sup._cdp("Target.attachToTarget", {"targetId": lightbox[0]["targetId"], "flatten": True}),
                     sup._loop, timeout=10)["result"]["sessionId"]
    try:
        reply = _schedule(sup._cdp("Runtime.evaluate", {
            "expression": "JSON.stringify([document, ...Array.from(frames, f => f.document)].flatMap(d => "
                          "Array.from(d.querySelectorAll('input'), e => [e.id, e.value])))",
            "returnByValue": True}, session_id=fsid), sup._loop, timeout=10)
    finally:
        _schedule(sup._cdp("Target.detachFromTarget", {"sessionId": fsid}), sup._loop, timeout=10)
    assert "exceptionDetails" not in reply["result"], reply
    return dict(json.loads(reply["result"]["result"]["value"]))


@pytest.mark.parametrize("layout", ["split", "single"])
def test_datatrans_shaped_re_render_is_re_pinned_inside_one_approval(payment_browser, tmp_path, monkeypatch, caplog,
                                                                      layout):
    """dlz (11-12 SBB/Datatrans), with re-pin switched on (vault.payment_repin; off by default, see the next test):
    a field the hosted form re-renders after the first write (replaced, re-described, or hidden until the card number
    is in) is re-pinned and written inside the same approval; what must still stop stops with nothing more written,
    and the stop names its check. Never submitted."""
    import logging
    from dataclasses import replace

    from agent.vault_store import VaultStore
    from tools.browser_vault_tool import browser_vault_fill

    sup, origins = payment_browser
    store = VaultStore(base_dir=tmp_path / "vault")
    card = store.add_item("payment", "Synthetic Datatrans card", CARD, origin=origins["merchant"])
    holder = store.add_item("payment", "Synthetic Datatrans card with holder", {**CARD, "cardholder_name": "JANE DOE"},
                            origin=origins["merchant"])
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    original = store.get_meta
    monkeypatch.setattr(store, "get_meta", lambda handle: (
        replace(original(handle), payment_frame_origins=(origins["psp"],)) if original(handle) else None))
    monkeypatch.setattr("agent.vault_payment_policy.PAYMENT_PSP_ORIGINS", (origins["psp"],))
    monkeypatch.setattr("tools.browser_vault_payment.payment_repin_enabled", lambda: True)
    monkeypatch.setattr("tools.browser_vault_payment.payment_fill_auto_enabled", lambda: False)  # re-pin needs it off
    prefix = "/dtx-" if layout == "split" else "/dtx1-"
    full = {"exp": "07/35", "num": CARD["card_number"], "cvv": CARD["cvc"]}

    def fill(variant, item=card):
        navigate(sup, origins["merchant"] + prefix + variant)
        caplog.clear()
        with caplog.at_level(logging.INFO), \
                patch("tools.approval_prompt.request_elicitation_consent", return_value="accept") as consent:
            out = json.loads(browser_vault_fill(item.id, task_id="payment-live", merchant_origin=origins["merchant"]))
        assert consent.call_count == 1 and out["submitted"] is False, (variant, out)
        for secret in (CARD["card_number"], CARD["cvc"], "07/35"):
            assert secret not in json.dumps(out) and secret not in caplog.text, (variant, secret)
        assert sup.evaluate_runtime("!!window.submitted")["result"] is False
        print("RESULT datatrans", layout, variant, json.dumps({k: out.get(k) for k in (
            "success", "error_type", "filled_fields", "fill_repins", "fill_check", "field_retention")}))
        return out

    for variant in ("replace", "relabel", "defer") + (("defer_ahead",) if layout == "single" else ()):
        ahead = variant == "defer_ahead"
        out = fill(variant, holder if ahead else card)
        assert out["success"] and out["filled_fields"] == 3 + ahead and out.get("fill_repins", 0) >= 1 + ahead, \
            (variant, out)  # defer_ahead: re-pinned when the expiry is hidden, and again once the number is in
        values = _dtx_values(sup)
        expected = {**full, **({"name": "JANE DOE"} if ahead else {})}
        assert {k: values.get(k) for k in expected} == expected, (variant, values)
        print("RECEIPT datatrans", layout, variant, "=FILLED repins", out["fill_repins"])

    stops = {"duplicate": "fields_replaced", "swap_role": "fields_replaced", "navigate": "navigated",
             "cover": "hit_test", **({"holder_swap": "fields_replaced"} if layout == "single" else {})}
    refusals = {"duplicate": "token_twice", "swap_role": "role_changed", "holder_swap": "role_changed"}
    for variant, reason in stops.items():
        out = fill(variant)
        assert not out["success"] and out["filled_fields"] == 1 and out["partially_filled"] is True, (variant, out)
        values = _dtx_values(sup)
        # Nothing after the stop: only the first field written holds a value; the touched one, any copy of it and
        # the number stay empty.
        written = [k for k, v in values.items() if v]
        assert len(written) == 1 and values[written[0]] == full.get(written[0]), (variant, values)
        if layout == "split" and variant == "navigate":
            # the CVC frame's URL changed: the between-document scope check stops it before the closure runs
            assert out["error_type"] == "payment_page_changed", out
        else:
            check = out["fill_check"]
            assert out["error_type"] == "payment_fields_changed" and check["reason"] == reason, (variant, out)
            assert check["token"] == {"exp": "cc-csc", "cvv": "cc-exp"}[written[0]], out
            assert check["documents"] == (3 if layout == "split" else 1), out
            assert "card fill stopped" in caplog.text and reason in caplog.text, caplog.text
            assert check.get("repin_refused") == refusals.get(variant), out
        print("RECEIPT datatrans", layout, variant, "=STOPPED", out.get("fill_check", out["error_type"]))


# Re-pin switched off: where each Datatrans-shaped variant stops (the failed check of the stop record). A covered
# replacement stops as a replaced field: nothing is re-pinned, so its exposure is never the question.
_DTX_OFF = {"replace": "fields_replaced", "relabel": "fields_relabeled", "defer": "field_hidden",
            "defer_ahead": "field_hidden", "duplicate": "fields_replaced", "swap_role": "fields_replaced",
            "navigate": "navigated", "cover": "fields_replaced", "holder_swap": "fields_replaced"}


@pytest.mark.parametrize("layout", ["split", "single"])
def test_datatrans_shaped_re_render_stops_the_fill_while_re_pin_is_switched_off(payment_browser, tmp_path, monkeypatch,
                                                                                caplog, layout):
    """vault.payment_repin off (the default): every re-render of the hosted form stops the fill at the first changed
    field, as before re-pinning existed: the one field written before it keeps its value, nothing is written after
    it, nothing is re-pinned; the stop record and the log line name the check. Never submitted. The RECEIPT lines
    hold what the base (4ecd5a7afb) reports for the same pages (no stop record there)."""
    import logging
    from dataclasses import replace

    from agent.vault_store import VaultStore
    from tools.browser_vault_tool import browser_vault_fill

    sup, origins = payment_browser
    store = VaultStore(base_dir=tmp_path / "vault")
    card = store.add_item("payment", "Synthetic Datatrans card", CARD, origin=origins["merchant"])
    holder = store.add_item("payment", "Synthetic Datatrans card with holder", {**CARD, "cardholder_name": "JANE DOE"},
                            origin=origins["merchant"])
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    original = store.get_meta
    monkeypatch.setattr(store, "get_meta", lambda handle: (
        replace(original(handle), payment_frame_origins=(origins["psp"],)) if original(handle) else None))
    monkeypatch.setattr("agent.vault_payment_policy.PAYMENT_PSP_ORIGINS", (origins["psp"],))
    monkeypatch.setattr("tools.browser_vault_payment.payment_repin_enabled", lambda: False, raising=False)
    monkeypatch.setattr("tools.browser_vault_payment.payment_fill_auto_enabled", lambda: False)  # the fill before 11-13
    full = {"exp": "07/35", "num": CARD["card_number"], "cvv": CARD["cvc"], "name": "JANE DOE"}
    for variant, reason in _DTX_OFF.items():
        if layout == "split" and variant in ("defer_ahead", "holder_swap"):
            continue  # single-document variants
        navigate(sup, origins["merchant"] + ("/dtx-" if layout == "split" else "/dtx1-") + variant)
        caplog.clear()
        with caplog.at_level(logging.INFO), \
                patch("tools.approval_prompt.request_elicitation_consent", return_value="accept") as consent:
            item = holder if variant == "defer_ahead" else card
            out = json.loads(browser_vault_fill(item.id, task_id="payment-live", merchant_origin=origins["merchant"]))
        assert consent.call_count == 1 and out["submitted"] is False, (variant, out)
        for secret in (CARD["card_number"], CARD["cvc"], "07/35"):
            assert secret not in json.dumps(out) and secret not in caplog.text, (variant, secret)
        assert sup.evaluate_runtime("!!window.submitted")["result"] is False
        values = _dtx_values(sup)
        written = sorted(k for k, v in values.items() if v)
        print("RECEIPT datatrans-off", layout, variant, json.dumps({
            "success": out.get("success"), "error_type": out.get("error_type"), "filled_fields": out.get("filled_fields"),
            "partially_filled": out.get("partially_filled"), "written": written}, sort_keys=True))
        assert not out["success"] and out["filled_fields"] == 1 and out["partially_filled"] is True, (variant, out)
        assert len(written) == 1 and values[written[0]] == full[written[0]], (variant, values)  # nothing after the stop
        assert "fill_repins" not in out and "re-pin(s)" not in caplog.text, (variant, out)
        if layout == "split" and variant == "navigate":
            assert out["error_type"] == "payment_page_changed", out  # the between-document scope check
            continue
        assert out["error_type"] == "payment_fields_changed", (variant, out)
        check = out["fill_check"]  # the stop record: kept with re-pin off
        assert check["reason"] == reason and "repin_refused" not in check, (variant, out)
        assert "card fill stopped" in caplog.text and reason in caplog.text, caplog.text
        print("RECORD datatrans-off", layout, variant, json.dumps(check, sort_keys=True))


# --- dlz (fill-auto, 11-13): one approval, several inspections -------------------------------------------------------

_FA_FULL = {"num": CARD["card_number"], "exp": "07/35", "c3": CARD["cvc"]}


def _fa_setup(payment_browser, tmp_path, monkeypatch, *, hosted=False, auto=True, wait=2.0):
    from dataclasses import replace

    from agent.vault_store import VaultStore

    sup, origins = payment_browser
    store = VaultStore(base_dir=tmp_path / "vault")
    card = store.add_item("payment", "Synthetic card", CARD, origin=origins["merchant"])
    monkeypatch.setattr("agent.vault_store.get_vault_store", lambda: store)
    if hosted:
        original = store.get_meta
        monkeypatch.setattr(store, "get_meta", lambda handle: (
            replace(original(handle), payment_frame_origins=(origins["psp"],)) if original(handle) else None))
        monkeypatch.setattr("agent.vault_payment_policy.PAYMENT_PSP_ORIGINS", (origins["psp"],))
    monkeypatch.setattr("tools.browser_vault_payment.payment_fill_auto_enabled", lambda: auto)
    monkeypatch.setattr("tools.browser_supervisor_payment_auto._REINSPECT_WAIT_SECONDS", wait)
    return sup, origins, card


def _fa_fill(sup, origins, card, caplog, on_consent=None, **arguments):
    """One fill with the owner accepting; ``on_consent`` runs while the approval is open. The result, checked for
    card values (none in it or in the log)."""
    import logging

    from tools.browser_vault_tool import browser_vault_fill

    def consent(*_a, **_k):
        if on_consent:
            on_consent()
        return "accept"

    caplog.clear()
    with caplog.at_level(logging.INFO), \
            patch("tools.approval_prompt.request_elicitation_consent", side_effect=consent) as asked:
        raw = browser_vault_fill(card.id, task_id="payment-live", merchant_origin=origins["merchant"], **arguments)
    out = json.loads(raw)
    leaked = [n for n, secret in enumerate((CARD["card_number"], CARD["cvc"], "07/35")) if secret in raw
              or secret in caplog.text]
    assert not leaked and asked.call_count <= 1 and out.get("submitted") in (False, None), (leaked, sorted(out))
    return out


def _fa_states(pairs, log):
    """Per field id: held / empty / other (never a value), and how often each field got an input event."""
    states = {k: "held" if v == _FA_FULL.get(k) else "empty" if not v else "other" for k, v in pairs}
    return states, {k: log.count(k) for k in dict(pairs)}


def _fa_top(sup):
    read = sup.evaluate_runtime("JSON.stringify([[...document.querySelectorAll('input')].map(e=>[e.id,e.value]),"
                                "window.log||[]])")["result"]
    return _fa_states(*json.loads(read))


def _fa_frame(sup):
    """The same reading from the one hosted /fa/ frame (None when there is none)."""
    from tools.browser_supervisor import _schedule

    targets = _schedule(sup._cdp("Target.getTargets"), sup._loop, timeout=10)["result"]["targetInfos"]
    frames = [t for t in targets if t["type"] == "iframe" and "/fa/" in t["url"]]
    if len(frames) != 1:
        return None
    fsid = _schedule(sup._cdp("Target.attachToTarget", {"targetId": frames[0]["targetId"], "flatten": True}),
                     sup._loop, timeout=10)["result"]["sessionId"]
    try:
        reply = _schedule(sup._cdp("Runtime.evaluate", {
            "expression": "JSON.stringify([[...document.querySelectorAll('input')].map(e=>[e.id,e.value]),window.log||[]])",
            "returnByValue": True}, session_id=fsid), sup._loop, timeout=10)
    finally:
        _schedule(sup._cdp("Target.detachFromTarget", {"sessionId": fsid}), sup._loop, timeout=10)
    return _fa_states(*json.loads(reply["result"]["result"]["value"]))


_HELD = {"num": "held", "exp": "held", "c3": "held", "c4": "empty"}


@pytest.mark.parametrize("auto", [True, False])
def test_fill_auto_label_only_changes_never_stop_the_fill(payment_browser, tmp_path, monkeypatch, caplog, auto):
    """SBB / pay.datatrans.com (11-12, 11-13): every write re-labels the other fields with validation text, on the
    same elements, and one label changes while the approval is open. Armed: every field is written exactly once and no
    re-inspection is used (a label-only change is classified again in place). Switched off (the fill before 11-13):
    the label change during the approval refuses the fill, and without it the first write stops it."""
    sup, origins, card = _fa_setup(payment_browser, tmp_path, monkeypatch, auto=auto)
    navigate(sup, origins["merchant"] + "/fa/relabel")
    out = _fa_fill(sup, origins, card, caplog, on_consent=lambda: sup.evaluate_runtime("relabelNow()"))
    states, writes = _fa_top(sup)
    print(f"RECEIPT fill-auto relabel auto={auto}", json.dumps({k: out.get(k) for k in (
        "success", "error_type", "filled_fields", "fill_reinspect", "fill_check")}, sort_keys=True))
    if auto:
        assert out["success"] and out["filled_fields"] == 3 and out["retention_verified"] is True, out
        assert "fill_reinspect" not in out and states == _HELD, (out.get("fill_reinspect"), states)
        assert writes == {"num": 1, "exp": 1, "c3": 1, "c4": 0}, writes
        return
    assert out["error_type"] == "payment_page_changed" and set(states.values()) == {"empty"}, (out, states)
    navigate(sup, origins["merchant"] + "/fa/relabel")
    out = _fa_fill(sup, origins, card, caplog)
    assert out["error_type"] == "payment_fields_changed" and out["filled_fields"] == 1, out
    assert out["fill_check"]["reason"] == "fields_relabeled" and out["fill_check"]["keys"] == ["label"], out


@pytest.mark.parametrize("auto", [True, False])
def test_fill_auto_a_field_that_lost_its_value_is_written_once_more(payment_browser, tmp_path, monkeypatch, caplog,
                                                                     auto):
    """Eurowings (11-13): the CVC is empty after the page settled (cleared, or re-mounted, a moment after the card
    number went in). Armed: one repair pass on a fresh inspection writes it again, the fields that kept their value
    are not written twice, and a CVC that never keeps its value still stops after that one pass. Switched off: the
    fill stops as payment_fields_not_retained."""
    sup, origins, card = _fa_setup(payment_browser, tmp_path, monkeypatch, auto=auto)
    for variant in ("retain", "retain_remount", "retain_every30", "retain_every", "retain_never"):
        navigate(sup, origins["merchant"] + "/fa/" + variant)
        out = _fa_fill(sup, origins, card, caplog)
        states, writes = _fa_top(sup)
        print("RECEIPT fill-auto", variant, f"auto={auto}", json.dumps({k: out.get(k) for k in (
            "success", "error_type", "filled_fields", "fill_reinspect", "field_retention")}, sort_keys=True))
        if auto and variant != "retain_never":
            assert out["success"] and out["filled_fields"] == 3 and states == _HELD, (variant, out, states)
            assert out["fill_reinspect"] == {"count": 1, "repairs": 1}, (variant, out)
            assert writes["num"] == 1 and writes["exp"] == 1 and writes["c3"] >= 1, (variant, writes)
            assert "1 re-inspection(s), 1 for lost values" in caplog.text
            continue
        assert out["error_type"] == "payment_fields_not_retained" and out["partially_filled"] is True, (variant, out)
        assert out["field_retention"]["cc-csc"] in ("empty", "replaced") and states["c3"] == "empty", (variant, out)
        assert out.get("fill_reinspect") == ({"count": 1, "repairs": 1, "refused": "budget"} if auto else None), out
        assert writes["num"] == 1 and writes["c3"] == (2 if auto else 1), (variant, writes)


def test_fill_auto_continues_on_a_re_mounted_field_and_keeps_its_stops(payment_browser, tmp_path, monkeypatch, caplog):
    """Armed, one document. A field replaced by a fresh, identical input under the fill: inspected again, filled.
    Still stopping: a label change that moves a role (the CVC's and a promo field's labels swap: role_changed, the
    promo field never gets the CVC), a field hidden for good (no complete form within the wait), and a changed top
    URL (navigated). Each stop keeps what was written before it and writes nothing after."""
    sup, origins, card = _fa_setup(payment_browser, tmp_path, monkeypatch)
    navigate(sup, origins["merchant"] + "/fa/replace")
    out = _fa_fill(sup, origins, card, caplog)
    states, writes = _fa_top(sup)
    assert out["success"] and out["fill_reinspect"] == {"count": 1, "repairs": 0}, out
    # c3: the write on the old input, then (r1 F14) the no-op input event on its re-mounted clone, which Chromium
    # clones WITH the value: it is kept, not written again. The page log counts events that changed a value.
    assert states == _HELD and writes == {"num": 1, "exp": 1, "c3": 2, "c4": 0}, (states, writes)
    for variant, refused in (("relabel_swap", "role_changed"), ("hide", "incomplete"), ("navigate", "navigated")):
        navigate(sup, origins["merchant"] + "/fa/" + variant)
        out = _fa_fill(sup, origins, card, caplog)
        states, writes = _fa_top(sup)
        print("RECEIPT fill-auto stop", variant, json.dumps({k: out.get(k) for k in (
            "success", "error_type", "filled_fields", "fill_reinspect", "fill_check")}, sort_keys=True))
        assert out["error_type"] == "payment_fields_changed" and out["partially_filled"] is True, (variant, out)
        again = out["fill_reinspect"]  # `seen`: the card field tokens per origin of the refused reading, when it got one
        assert out["filled_fields"] == 1 and (again["count"], again["repairs"], again["refused"]) == (0, 0, refused), \
            (variant, out)
        assert set(again) <= {"count", "repairs", "refused", "seen"} and set(again.get("seen", {})) <= {origins["merchant"]}
        # The one field written before the change keeps its value; the card number (last) and the promo field
        # never get one (relabel_swap: the CVC went into its field before that field's label became the promo's).
        assert sorted(states.values()) == ["empty", "empty", "empty", "held"], (variant, states)
        assert states["num"] == "empty" and states["c4"] == "empty", (variant, states)
        assert sum(writes.values()) == 1 and "re-inspection" in caplog.text, (variant, writes)


def test_fill_auto_card_fields_on_another_origin_after_the_approval_stop(payment_browser, tmp_path, monkeypatch,
                                                                         caplog):
    """Armed, a reviewed hosted form in a frame. While the approval is open the frame is removed and the same card
    fields appear in the merchant's own document, an origin the approval did not name for them: nothing is written."""
    sup, origins, card = _fa_setup(payment_browser, tmp_path, monkeypatch, hosted=True)
    navigate(sup, origins["merchant"] + "/fa-hosted")
    moved = "document.getElementById('slot').innerHTML=" + json.dumps(_FA_FIELDS.replace('name="f3"', 'autocomplete="cc-csc"'))
    out = _fa_fill(sup, origins, card, caplog, on_consent=lambda: sup.evaluate_runtime(moved))
    assert out["error_type"] == "payment_page_changed" and not out.get("partially_filled"), out
    # r1 F1: the origin the card fields moved to is page-chosen and not in the approval: only counted, never named
    assert out["fill_reinspect"] == {"count": 0, "repairs": 0, "refused": "origin_changed", "seen": {"other": 1}}, out
    assert origins["merchant"] not in json.dumps(out) and "it saw other 1" in caplog.text
    assert set(_fa_top(sup)[0].values()) == {"empty"} and "re-inspection refused (origin_changed)" in caplog.text
    # The same form left alone fills (the hosted frame is the approved destination).
    navigate(sup, origins["merchant"] + "/fa-hosted")
    out = _fa_fill(sup, origins, card, caplog)
    assert out["success"] and "fill_reinspect" not in out and _fa_frame(sup)[0] == _HELD | {"c4": "empty"}, out


@pytest.mark.parametrize("variant", ["rerender", "slow", "reload", "off"])
def test_fill_auto_unreviewed_form_re_rendered_during_the_approval(payment_browser, tmp_path, monkeypatch, caplog,
                                                                   variant):
    """FlixBus / assets.payrails.io (11-13): an owner-approved unreviewed hosted form re-renders while the approval is
    open (fresh elements in the same document, at once or 1.5 s later, or the frame's document loaded again). Armed:
    the form is inspected again after the approval, by the discovery inspection, and filled once, strict checks
    included. Switched off: payment_page_changed, nothing written."""
    from tools.browser_supervisor import _schedule

    sup, origins, card = _fa_setup(payment_browser, tmp_path, monkeypatch, auto=variant != "off", wait=10.0)
    monkeypatch.setattr("tools.browser_vault_tool._is_managed", lambda: True)
    monkeypatch.setattr("tools.browser_vault_tool._managed_supervisor", lambda task_id, admit=False: sup)
    monkeypatch.setattr("tools.browser_vault_tool._bot_desktop_browser_session", lambda task_id: False)
    monkeypatch.setattr("tools.browser_use_cli.vault_route", lambda task_id: {"lease": "live", "token": "t"})
    monkeypatch.setattr("tools.browser_control_route.set_capture_guard", lambda lease, token: (True, ""))
    monkeypatch.setattr("tools.browser_payment_surface.claim_payment_surface", lambda s, i: None)
    monkeypatch.setattr("tools.browser_vault_payment.destination_policy", lambda: (True, "live"))
    navigate(sup, origins["merchant"] + "/fa-payrails")

    def rerender():
        if variant == "reload":
            sup.evaluate_runtime("document.getElementById('pr').src=" + json.dumps(origins["psp"] + "/fa/payrails?v=2"))
            wait_until(lambda: "v=2" in json.dumps(_schedule(sup._cdp("Target.getTargets"), sup._loop, timeout=10))
                       or "v=2" in json.dumps(_schedule(sup._cdp("Page.getFrameTree", session_id=sup._page_session_id),
                                                        sup._loop, timeout=10)), what="the frame loaded again")
            return
        delay = 1500 if variant == "slow" else 0
        sup.evaluate_runtime(f"document.getElementById('pr').contentWindow.postMessage({{t:'rerender',delay:{delay}}},'*')")
        if not delay:
            wait_until(lambda: sup.evaluate_runtime("window.rerendered===true")["result"] is True,
                       what="the form re-rendered")

    first = _fa_fill(sup, origins, card, caplog)
    assert first["error_type"] == "payment_destination_approval_required", first
    out = _fa_fill(sup, origins, card, caplog, on_consent=rerender,
                   cart={"items": [{"description": "Adult ticket", "quantity": 2}], "total": "22.66", "currency": "EUR"},
                   approve_origins=[origins["psp"]])
    if variant == "slow":
        wait_until(lambda: sup.evaluate_runtime("window.rerendered===true")["result"] is True, what="the form came back")
    states, writes = _fa_frame(sup)
    print("RECEIPT fill-auto payrails", variant, json.dumps({k: out.get(k) for k in (
        "success", "error_type", "filled_fields", "fill_reinspect", "destination_reviewed")}, sort_keys=True))
    if variant == "off":
        assert out["error_type"] == "payment_page_changed" and set(states.values()) == {"empty"}, (out, states)
        return
    assert out["success"] and out["filled_fields"] == 3 and out["destination_reviewed"] is False, out
    assert states == {"num": "held", "exp": "held", "c3": "held"}, states
    assert writes == {"num": 1, "exp": 1, "c3": 1} and "re-inspected after the approval" in caplog.text, writes
    assert out["fill_reinspect"] == {"count": 1, "repairs": 0}, out  # the one after the approval; none under the fill


# What the armed fill does with each Datatrans-shaped re-render (the fixtures of the re-pin tests above): filled, with
# the number of re-inspections it took, or stopped, with the reason the re-inspection gave.
_DTX_AUTO = {
    "relabel": ("filled", 0), "replace": ("filled", 1), "navigate": ("filled", 1), "cover": ("covered", "hit_test"),
    "defer": ("stopped", "incomplete"), "defer_ahead": ("stopped", "incomplete"),
    "duplicate": ("stopped", "incomplete"), "swap_role": ("stopped", "role_changed"),
    "holder_swap": ("stopped", "fields_changed"),
}


@pytest.mark.parametrize("layout", ["split", "single"])
def test_fill_auto_datatrans_shaped_re_renders(payment_browser, tmp_path, monkeypatch, caplog, layout):
    """Armed, on the Datatrans-shaped hosted form (one lightbox document, or the number and the CVC in their own
    frames): a relabelled field never uses a re-inspection; a re-mounted field and a frame whose URL changed in place
    are inspected again and filled; a re-mounted field under an overlay of its own reviewed document is inspected
    again and not written (hit test); a field hidden until the card number is in, a second CVC, an old field re-described as a promo code beside its copy, and a holder
    field turned into the card number all still stop, with nothing written after the change."""
    from agent.vault_store import VaultStore

    sup, origins, card = _fa_setup(payment_browser, tmp_path, monkeypatch, hosted=True)
    store = VaultStore(base_dir=tmp_path / "vault")
    holder = store.add_item("payment", "Synthetic card with holder", {**CARD, "cardholder_name": "JANE DOE"},
                            origin=origins["merchant"])
    full = {"exp": "07/35", "num": CARD["card_number"], "cvv": CARD["cvc"], "name": "JANE DOE"}
    for variant, (outcome, detail) in _DTX_AUTO.items():
        if layout == "split" and variant in ("defer_ahead", "holder_swap"):
            continue  # single-document variants
        navigate(sup, origins["merchant"] + ("/dtx-" if layout == "split" else "/dtx1-") + variant)
        out = _fa_fill(sup, origins, holder if variant == "defer_ahead" else card, caplog)
        values = _dtx_values(sup)
        states = {k: "held" if v == full.get(k.rstrip("2")) else "empty" if not v else "other" for k, v in values.items()}
        print("RECEIPT fill-auto datatrans", layout, variant, json.dumps({k: out.get(k) for k in (
            "success", "error_type", "filled_fields", "fill_reinspect", "fill_check")}, sort_keys=True))
        if outcome == "filled":
            assert out["success"] and out["filled_fields"] == 3, (variant, out)
            assert out.get("fill_reinspect", {"count": 0}).get("count") == detail, (variant, out)
            assert all(states[k] == "held" for k in ("exp", "num", "cvv")), (variant, states)
            continue
        if outcome == "covered":
            # r1 F4: a field the first inspection never saw (re-mounted during the approval) must be exposed, on a
            # reviewed destination too: the re-mounted expiry under the overlay is never written, nor the number after it
            assert not out["success"] and out["error_type"] == "payment_fields_changed", (variant, out)
            assert out["fill_check"]["reason"] == detail and out["fill_check"]["token"] in ("cc-exp", "cc-csc"), (variant, out)
            held = [k for k, v in states.items() if v == "held"]
            assert out["filled_fields"] == 1 and len(held) == 1 and states.get("num", "empty") == "empty", (variant, states)
            continue
        assert not out["success"] and out["fill_reinspect"]["refused"] == detail, (variant, out)
        assert out["fill_reinspect"]["count"] == 0 and out["partially_filled"] is True, (variant, out)
        held = sorted(k for k, v in states.items() if v == "held")
        assert len(held) == 1 and "other" not in states.values(), (variant, states)
        assert states.get("num", "empty") == "empty" and states.get("name", "empty") in ("empty", "held"), (variant, states)
