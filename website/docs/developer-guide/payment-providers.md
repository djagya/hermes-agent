---
title: Hosted card-field providers
description: Which payment providers' hosted card fields the native card fill admits, with the evidence behind each row.
---

# Hosted card-field providers

The native card fill (`browser_vault action=fill` on a payment item) writes card values into the
merchant page and into iframes on **reviewed provider origins** only. Each reviewed product is a
`PaymentProvider` row in `agent/vault_payment_policy.py`: its exact production card-field origins,
the path prefix of its card-field documents when the origin also serves other pages, and the most
card documents one checkout may hold. Script, API, wallet, 3DS, fraud and analytics origins are never
listed. Sandbox origins are never listed either: a sandbox frame must not receive a real card.

Stripe, Datatrans and Shopify are on by default. Every other row is **off until the operator names
it** in `config.yaml`:

```yaml
vault:
  payment_providers: [worldpay-hpp, mollie-components]
```

Unknown names are ignored. Card metadata (a vault item's `payment_frame_origins`) may only narrow the
reviewed set; an item naming an unreviewed origin is refused.

## Status vocabulary

- **discovered**: origins and layout from official docs or SDK source; nothing rendered.
- **fixture-tested**: a local page reproducing the documented structure passes the native path.
- **sandbox-native-tested**: the provider's real sandbox fields, rendered on a local HTTPS merchant
  page, admitted and filled by the real native path (`evals/psp_probe.py`), never submitted.
- **CI-verified / activated / production-observed**: CI receipt at the exact head; enabled in a live
  profile; seen working on a real merchant. No row below is production-observed.

Sandbox renders used only credentials the provider publishes for public testing (demo keys, demo
pages, official example repositories) and the provider's published test cards.

## Matrix (checked 2026-10-20)

| Row (`name`) | Product | Card-field origin: production (source) / sandbox (observed) | Topology | Status | Limitations |
|---|---|---|---|---|---|
| `stripe-elements` (default) | Stripe.js v3 Card Element, split elements, Payment Element | `js.stripe.com` (same SDK both) | Card/Payment Element: 1 document; split: 3 | sandbox-native-tested | Card and Payment Element failed before this change (see below). Payment Element document also holds Link fields, which are not filled. |
| `datatrans-secure-fields` (default) | Secure Fields 2.0 | `pay.datatrans.com` (docs) / `pay.sandbox.datatrans.com` | number and CVC frames, expiry on merchant | sandbox-native-tested (tokenize demo merchant) | Production host inferred from the script host. |
| `shopify-checkout` (default) | Shopify checkout | `checkout.pci.shopifyinc.com` | one frame per field, honeypots | fixture-tested | No public way to render it outside a store. |
| `worldpay-hpp` | Access Worldpay Hosted Payment Pages, iframe integration (same `/app/hpp` app as WPG HPP) | `payments.worldpay.com` (docs, incident) / `payments-test.worldpay.com`; path `/app/hpp/` | 1 document: number, holder, month, 2-digit year, CVC, billing | sandbox-native-tested (en, de) | No client-side validity signal; HPP validates on post. Docs warn traffic may move between domains; another host is refused, not guessed. WPG/Corporate Gateway HPP: discovered only. |
| `worldpay-access-checkout` | Access Checkout Web SDK v2.5 | `access.worldpay.com` (docs, SDK) / `try.access.worldpay.com`; path `/access-checkout/` | 3 frames | sandbox-native-tested | |
| `braintree-hosted-fields` | braintree-web 3.147 Hosted Fields | `assets.braintreegateway.com` (SDK `ASSETS_URLS`) / same; path `/web/` | 1 frame per field, up to 4 | sandbox-native-tested (official sample merchant token) | |
| `paypal-card-fields` | PayPal JS SDK `card-fields` 5.0 | `www.paypal.com` (SDK domain rule) / `www.sandbox.paypal.com`; path `/smart/card-field` | 4 frames, nested no-input children | sandbox-native-tested (`client-id=test`) | Same origin as PayPal wallet and login: only `/smart/card-field` documents are targets. Production host derived, not observed. Holder field is matched by its English placeholder. |
| `checkoutcom-frames` | Frames (framesv2 2.69) single and multi | `js.checkout.com` (SDK) / same; path `/framesv2/` | single: 1 document; multi: 3 frames + merchant holder | sandbox-native-tested | Checkout.com docs state Frames is deprecated as of 2026-06-30; still served. |
| `mollie-components` | mollie.js v1 Components | `js.mollie.com` (observed in test and live mode); path `/v1/component/` | 4 frames (holder, number, expiry, CVC) | sandbox-native-tested (official example profile id) | Exactly at the 4-document bound. |
| `unzer-ui-v2` | Unzer UI Components v2 card | `static-cc.unzer.com` (docs CSP, bundle) / `static-cc.test.unzer.com`; path `/v2/card-ui/` | 1 frame, inputs in open shadow roots, masked CVC | sandbox-native-tested (demo.unzer.com key) | Split first/last name holder is not filled. Click to Pay is excluded. |
| `unzer-ui-v1` | Unzer UI Components v1 (legacy) | `payment.unzer.com` (docs CSP) / `sbx-payment.unzer.com`; path `/v1/cardframe/` | 3 frames | sandbox-native-tested | Legacy `payment.heidelpay.com` not admitted (not observed). |
| `saferpay-fields` | Worldline Saferpay Fields 1.9 | `www.saferpay.com` (docs, library URL builder) / `test.saferpay.com`; path `/Fields/` | 4 frames | sandbox-native-tested (Saferpay's own demo shop page) | Rendered inside Saferpay's demo page, so one extra ancestry level was allowed for the run. |
| — | Adyen Web v6 Card / Custom Card | `checkoutshopper-live[-us\|-au\|-apse\|-in\|-nea].adyen.com` (SDK) / `checkoutshopper-test.adyen.com` | 3–4 field frames + same-origin fingerprint frame | discovered | Blocked: needs a test clientKey with the merchant origin allowed (no public key). Custom Card with a merchant holder is 5 documents. |
| — | Checkout.com Flow | `checkout-web-components.checkout.com` (SDK source) | split frames incl. holder | discovered | Blocked: payment session needs an account secret key. |
| — | Mollie hosted checkout, Adyen HPP/pay-by-link, Worldline Direct Hosted Checkout | full-page redirect | — | out of scope | Not an embedded target. |
| — | Nexi XPay Global / NPG Build v3 | `xpay.nexigroup.com` (SDK) / `xpaysandbox.nexigroup.com` | 5 frames incl. holder email | sandbox-native-tested: refused | Over the document bound and a write race; email no longer reads as holder. Needs its own scoped change. |
| — | PAYONE Client API hosted iFrames (legacy) | `secure.pay1.de` | 1 frame per field, expiry as `<select>` by default | sandbox-native-tested: refused | Blocked: `<select>` fill and PAYONE field ids not supported. |
| — | Worldline Direct Hosted Tokenization Page | `payment.direct.worldline-solutions.com` (docs) / `payment.preprod…` | 1 document, expiry `<select>` | fixture-tested: refused | Blocked: `<select>` fill; a live session needs a merchant API key. |
| — | Computop Paygate PaySSL | `www.computop-paygate.com` | 1 document, expiry `<select>` | fixture-tested: refused | Blocked: `<select>` fill, unlabelled CVC/holder; merchant keys needed. |
| — | Nexi Checkout (Nets Easy) embedded, Nexi XPay Italy legacy Build, PAYONE Commerce Platform HTP, Worldline Direct Hosted Fields, Ingenico Connect, Worldline Sips | see docs | — | discovered | Blocked: merchant credentials; no public render. |

Wallets (Apple Pay, Google Pay, PayPal buttons), BNPL, bank transfers, Click to Pay, stored-card
provisioning and recurring enrolment are excluded: their frames are on unlisted origins or excluded
paths, and frames without card inputs are never targets. 3DS and device challenges stay with the human.

## Engine changes that the sandbox runs required

- Inputs inside open shadow roots are inspected (Unzer v2).
- A transparent, click-through input (`opacity: 0` or `pointer-events: none`) is not visible
  (Mollie's focus trap duplicated every field).
- Fields are written one at a time with their own events, the card number last. Before, every value
  was written and the events fired afterwards: Stripe's Card and Payment Element re-render on each event
  and reset the fields written before it, and Braintree, Mollie and Worldpay Access Checkout re-shape
  the CVC once the number reveals the brand, which the next document's freshness check refused.
- Labels in German, French, Dutch, Spanish and Italian are recognised, and accented labels match at
  all (accents used to split words: `Prüfnummer` read as `pru fnummer`). `ccv`, a masked CVC input and
  holder-email exclusion were added.
- A two-character year field gets a two-digit year.
- A holder-name field is filled when the card has a holder name and left to the user otherwise,
  instead of refusing the whole fill.

## Re-running the evidence

`evals/psp_probe.py --page <fixture>/index.html --origins <sandbox field origin> --fill --card <test PAN>`
renders the fixture on `https://localhost:<port>/` in an isolated Chromium, dumps frame and input
structure (never values), runs the native inspection and fill, and reports the provider's own
validity markers and whether anything submitted. Live receipts for each structure above are in
`tests/tools/test_browser_vault_payment_live.py::test_provider_shapes_fill_once_without_submit`
(`HERMES_E2E_BROWSER=1`).
