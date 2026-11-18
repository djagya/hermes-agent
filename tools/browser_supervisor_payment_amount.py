"""The fill's amount check (dlz, design v2 B5): the page script that reads a checkout's visible totals, and the rule that
merges what it read in every witnessed document. Moved out of ``browser_supervisor_payment`` unchanged (build C r4:
file size); that module re-exports the names its callers import from it.
"""

from __future__ import annotations

from typing import Any

# dlz (design v2 B5): a transcription sanity check of the stated total against the document's visible text, never a
# verification. A token is an amount with two decimals, not part of a longer number. dlz (Codex B r2 #3, r3 #3): no
# grammar is parsed. On the token's own line, at most ONE unit may sit right beside it on each side (spaces between
# allowed): an ISO 4217 code (a whole word of 3 capitals) or a currency symbol with up to 3 letters before it ('$' and
# 'US$' are USD, 'CA$'/'C$' CAD, 'A$'/'AU$' AUD, 'NZ$' NZD, '€' EUR, '£' GBP; any other symbol, prefix or 3-capital
# word is an unknown unit). The 8 characters beyond each unit (spaces trimmed) are its context. dlz (Codex B r4 #2/#3):
# the token is bracketed when its line holds any bracket (Unicode open/close punctuation) other than balanced ASCII
# '(...)' pairs wholly left of it; a line longer than 1000 characters on either side is not judged, and (r5 #1) a text
# cut at 200000 characters is never `matched`. A clear amount has every unit equal to the stated currency, is not bracketed and not on an unjudged line, and has a
# context with no other 3-capital word, currency symbol, sign or dash, or digit. Anything else, a token without a unit
# included, is not clear. An occurrence of the stated total is a token in the stated currency with no sign or dash in
# its context and not bracketed. A total-labelled amount is the first token (with or without a unit) within 40
# characters after a total keyword on the same line; one that is not clear, a digit between the keyword and it, or
# (no token in reach) any digit in reach after the keyword (a total in a format not read, 'EUR 100', 'EUR 100.000')
# makes the page `unclear`, never `matched`.
# matched: some labelled amount, all of them clear and the stated total; ambiguous: the stated total appears, but
# unlabelled, unclear or beside a different labelled total; not_found: otherwise. pin() returns the state, the
# distinct clear labelled amounts (cents), the stated total's occurrences and `unclear`; valid() is the same
# document and URL reading the same pin.
_AMOUNT_WITNESS = r"""((cents, currency) => {
  const doc = document, href = location.href;
  const CODES = new Set('__CODES__'.split(' '));
  const AMOUNT = /(?<![\d.,])(\d{1,3}(?:[.,   ]\d{3})+|\d{1,7})[.,](\d{2})(?![\d.,]?\d)/g;
  const KEYWORD = /(?<![\p{L}\p{N}])(gesamtbetrag|amount due|order total|zu zahlen|endpreis|gesamt|summe|betrag|total)/giu;
  const UNIT_END = /(?<![\p{L}\p{N}])(?:[A-Z]{3}|\p{L}{0,3}\p{Sc})$/u;
  const UNIT_START = /^(?:[A-Z]{3}(?![\p{L}\p{N}])|\p{L}{0,3}\p{Sc})/u;
  const CODE = /(?<![\p{L}\p{N}])[A-Z]{3}(?![\p{L}\p{N}])/gu;
  const MARK = /[\p{Sc}\p{N}\p{Pd}−+±]/u, SIGN = /[\p{Pd}−+±]/u;
  const BRACKET = /[\p{Ps}\p{Pe}]/gu;
  const LINE = 1000, WINDOW = 8;
  const DOLLARS = {'': 'USD', US: 'USD', CA: 'CAD', C: 'CAD', A: 'AUD', AU: 'AUD', NZ: 'NZD'};
  const unit = m => {
    if (/^[A-Z]{3}$/.test(m)) return CODES.has(m) ? m : '?';
    const cs = Array.from(m), symbol = cs.pop(), prefix = cs.join('');
    if (symbol === '$') return DOLLARS[prefix] || '?';
    return prefix ? '?' : symbol === '€' ? 'EUR' : symbol === '£' ? 'GBP' : '?';
  };
  const codeIn = (s, from, to) => Array.from(s.matchAll(CODE)).some(c => c.index < to && c.index + 3 > from);
  // The token's line on each side (at most LINE characters), and whether it is unjudged: a side may run on past LINE.
  const sides = (text, start, end) => {
    const a = text.slice(Math.max(0, start - LINE), start), b = text.slice(end, end + LINE);
    const nl = Math.max(a.lastIndexOf('\n'), a.lastIndexOf('\r')), nr = b.search(/[\n\r]/);
    return [nl >= 0 ? a.slice(nl + 1) : a, nr >= 0 ? b.slice(0, nr) : b,
            (nl < 0 && start > LINE) || (nr < 0 && end + LINE < text.length)];
  };
  // dlz (Codex B r4 #2): any bracket on the token's line blocks it, except balanced ASCII '(...)' pairs wholly left
  // of it ('Gesamt (inkl. MwSt.) 22,66 €'): another bracket type, a mismatch, an open pair or any bracket right of it.
  const bracketed = (left, right) => {
    BRACKET.lastIndex = 0;
    if (BRACKET.test(right)) return true;
    let depth = 0;
    for (const m of left.matchAll(BRACKET)) {
      if (m[0] === '(') depth += 1;
      else if (m[0] === ')' && depth) depth -= 1;
      else return true;
    }
    return depth > 0;
  };
  const read = () => {
    const all = String((doc.body && doc.body.innerText) || ''), text = all.slice(0, 200000), cut = all.length > 200000;
    const amounts = [];
    for (const m of text.matchAll(AMOUNT)) {
      const start = m.index, end = start + m[0].length;
      const [left, right, unjudged] = sides(text, start, end);
      let l = left.trimEnd(), r = right.trimStart();
      const lu = UNIT_END.exec(l), ru = UNIT_START.exec(r);
      if (lu) l = l.slice(0, lu.index).trimEnd();
      if (ru) r = r.slice(ru[0].length).trimStart();
      const lw = l.slice(-WINDOW), rw = r.slice(0, WINDOW);
      const units = [lu && unit(lu[0]), ru && unit(ru[0])].filter(Boolean);
      const codes = codeIn(l, l.length - WINDOW, l.length) || codeIn(r, 0, WINDOW);
      const signed = SIGN.test(lw) || SIGN.test(rw) || bracketed(left, right);
      const marked = unjudged || codes || signed || MARK.test(lw) || MARK.test(rw);
      // A bare token (no unit) is never clear: first after a keyword, it makes the page unclear.
      amounts.push({start, value: Number(m[1].replace(/\D/g, '') + m[2]), stated: units.includes(currency) && !signed,
                    clear: units.length > 0 && !marked && units.every(u => u === currency)});
    }
    const labelled = new Set();
    let unclear = cut;  // dlz (Codex B r4 #3, r5 #1): a text cut at its size limit is never judged matched
    for (const k of text.matchAll(KEYWORD)) {
      const from = k.index + k[0].length;
      const first = amounts.find(a => a.start >= from);
      if (first && first.start - from <= 40 && !/[\n\r]/.test(text.slice(from, first.start))) {
        if (first.clear && !/\p{N}/u.test(text.slice(from, first.start))) labelled.add(first.value);
        else unclear = true;
      } else if (/\p{N}/u.test(text.slice(from, from + 40).split(/[\n\r]/)[0])) {
        unclear = true;  // a total in a format this check does not read
      }
    }
    const stated = amounts.filter(a => a.stated && a.value === cents).length;
    const distinct = Array.from(labelled).sort((a, b) => a - b);
    const state = !unclear && distinct.length && distinct.every(v => v === cents) ? 'matched' :
      stated ? 'ambiguous' : 'not_found';
    return {state, labelled: distinct.slice(0, 64), stated: Math.min(stated, 9999), unclear};
  };
  const pinned = JSON.stringify(read());
  return {pin: () => JSON.parse(pinned),
          valid: () => document === doc && location.href === href && JSON.stringify(read()) === pinned};
})""".replace("__CODES__", "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWL")
_PAGE_MATCH_STATES = ("matched", "ambiguous", "not_found")


def _valid_pin(pin: Any) -> bool:
    return (isinstance(pin, dict) and set(pin) == {"state", "labelled", "stated", "unclear"}
            and pin["state"] in _PAGE_MATCH_STATES and type(pin["unclear"]) is bool
            and isinstance(pin["labelled"], list) and len(pin["labelled"]) <= 64
            and all(type(v) is int and v >= 0 for v in pin["labelled"])
            and type(pin["stated"]) is int and pin["stated"] >= 0)


def _page_match(pins: list, cents: int) -> tuple:
    """``(state, where)`` over every witnessed document, by the same rule as one document: the union of labelled
    totals and the stated total's occurrences. ``where`` names the documents checked, in fixed words only."""
    labelled = {v for _place, pin in pins for v in pin["labelled"]}
    stated = sum(pin["stated"] for _place, pin in pins)
    unclear = any(pin["unclear"] for _place, pin in pins)
    state = "matched" if labelled == {cents} and not unclear else "ambiguous" if stated else "not_found"
    places = list(dict.fromkeys(place for place, _pin in pins))
    return state, " and ".join("the " + p for p in places)
