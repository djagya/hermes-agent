"""The inspect-and-commit closure that ``tools.browser_supervisor_payment`` pins in each card document.

JavaScript only, run in an isolated world of the card document; no Python logic lives here. The facade fills
``__LIMITS__`` / ``__EXPOSED__`` for the reviewed and the discovery inspection.
"""

from __future__ import annotations

_INSPECT_TEMPLATE = r"""(() => {
  // dlz (B2, B3): null on the reviewed path; the discovery inspection's limits and exposure test otherwise.
  const LIM = __LIMITS__;
  const exposedFn = LIM ? __EXPOSED__ : null;
  // dlz: the document's monotonic clock at creation; commit deadlines count from here (_renderer_deadline_ms).
  const born = performance.now();
  // dlz: and its wall clock. Commit requires BOTH elapsed times inside the deadline: the monotonic one
  // pauses while a remote browser's machine sleeps (macOS ticks exclude sleep), the wall one does not;
  // a wall-clock step alone cannot reopen the window; a constant browser/host offset cancels out.
  const bornWall = Date.now();
  const doc = document, href = location.href;
  // Inputs and selects in document order, including those inside open shadow roots (Unzer UI v2
  // renders every card input in one); closed roots stay out of reach and therefore out of scope. A
  // select is only ever an expiry month or year (PAYONE, Worldline HTP, Computop).
  // dlz (B3): discovery stops at LIM.inputs fields or LIM.shadow nested shadow roots and marks the overflow.
  let overflow = false;
  const allInputs = () => {
    const out = [];
    const walk = (root, depth) => {
      if (LIM && depth > LIM.shadow) { overflow = true; return; }
      for (const n of root.querySelectorAll('*')) {
        if (n.tagName === 'INPUT' || n.tagName === 'SELECT') {
          out.push(n);
          if (LIM && out.length > LIM.inputs) { overflow = true; return; }
        }
        if (n.shadowRoot) walk(n.shadowRoot, depth + 1);
        if (overflow) return;
      }
    };
    walk(doc, 0);
    return out;
  };
  const cut = v => LIM ? String(v).slice(0, LIM.text) : v;
  // dlz: the pinned field set; a commit's re-pin (adopt) may replace it once the supervisor re-proved it.
  let elements = allInputs();
  const isSelect = e => e.tagName === 'SELECT';
  // A select's options (months, years: never a secret) are part of the description, so a list that
  // is re-populated after consent fails valid() like any other changed field.
  const describeOne = (e,index) => ({index, autocomplete:cut(e.autocomplete),
    name:cut(e.name+' '+e.id), label:cut((e.labels?Array.from(e.labels,l=>l.textContent).join(' '):'')+' '+
      (e.getAttribute('aria-label')||'')+' '+(e.placeholder||'')),
    type:isSelect(e)?(e.multiple?'select-multiple':'select'):e.type,
    formIndex:e.form?Array.from(doc.forms).indexOf(e.form):null,
    maxLength:e.maxLength>0?e.maxLength:null,
    ...(isSelect(e)?{options:Array.from(e.options,o=>[o.value,o.text.slice(0,64),o.disabled]).slice(0,400)}:{})});
  const describe = () => elements.map(describeOne);
  // Honeypots and visually-hidden inputs are never fill targets: Shopify's PCI frames carry one
  // aria-hidden, tabindex=-1, 1px input per card token around the real field, and filling one
  // is a fraud signal.
  const visible = e => !(e.disabled || e.readOnly || !e.isConnected || e.ownerDocument !== doc ||
      e.getAttribute('aria-hidden') === 'true' || e.tabIndex < 0 || e.hasAttribute('data-honeypot-field')) &&
    shown(e);
  // Laid out and visible to the user; also what the settled retention check requires of a written field.
  const shown = e => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    // Mollie keeps a transparent, click-through focus-trap input beside each real field.
    return e.getClientRects().length>0 && r.width > 2 && r.height > 2 && s.visibility !== 'hidden' &&
      Number(s.opacity) > 0 && s.pointerEvents !== 'none' &&
      (typeof e.checkVisibility !== 'function' ||
       e.checkVisibility({opacityProperty: true, visibilityProperty: true}));  // a transparent ancestor too
  };
  const skipped = ['hidden','submit','button','reset','file','image','checkbox','radio','select-multiple'];
  // dlz (B2): discovery also drops a field the user is not actually shown (clipped, covered, offscreen,
  // transparent), so a hidden copy is no candidate and cannot make the real form ambiguous. dlz (Codex B r1 #7):
  // true, false or null; a fillable-looking field whose exposure is unknown makes the reading unknown, never hidden.
  const exposure = e => skipped.includes(e.type) || !visible(e) ? false : exposedFn ? exposedFn(e) : true;
  const judge = list => list.map((e,index)=>({e,index,x:exposure(e)}));
  const judged = judge(elements);
  const unknown = judged.some(x => x.x !== true && x.x !== false);
  let entries = judged.filter(x=>x.x === true);
  let selected = new Set(entries.map(x=>x.index));
  // dlz (Codex B r1 #2): discovery binds the exposed set: a field hidden at inspection and shown now (a revealed
  // duplicate) or one whose exposure became unknown changes the document.
  const unexposed = () => !LIM || elements.every((e,n)=>selected.has(n) || exposure(e) === false);
  let signature = JSON.stringify(describe());
  // dlz: the owner chain this document can reach synchronously (same-origin ancestors, pinned now) and the
  // top URL when readable: page code run by focus can hide a parent iframe or rewrite the top URL
  // (history.replaceState) without touching this document. Cross-origin ancestors are out of reach of
  // this document's scripts, and out of this check (between documents, control re-checks the owners).
  const scopeChain = [];
  for (let w = globalThis, k = 0; k < 8; k++) {
    let fe = null, pw = null, ph = null;
    try { fe = w.frameElement; pw = w.parent; ph = fe && pw ? pw.location.href : null; } catch (_) { fe = null; }
    if (!fe || !pw || pw === w || typeof ph !== 'string') break;
    scopeChain.push({fe, pw, ph});
    w = pw;
  }
  const readTop = () => { try { const t = globalThis.top; return t && t !== globalThis ? String(t.location.href) : null; }
                          catch (_) { return null; } };
  const topHref = readTop();
  const ownerShown = e => {
    const v = e.ownerDocument && e.ownerDocument.defaultView;
    if (!e.isConnected || !v) return false;
    const r = e.getBoundingClientRect(), s = v.getComputedStyle(e);
    return e.getClientRects().length > 0 && r.width > 2 && r.height > 2 && s.visibility !== 'hidden' &&
      Number(s.opacity) > 0 && (typeof e.checkVisibility !== 'function' ||
        e.checkVisibility({opacityProperty: true, visibilityProperty: true}));
  };
  const chain = () => scopeChain.every(c => { try { return c.pw.location.href === c.ph && ownerShown(c.fe); }
                                             catch (_) { return false; } });
  const topSame = () => topHref === null || readTop() === topHref;
  const scope = () => chain() && topSame();
  // dlz: which part of the pinned state no longer holds (null: all of it), checked in this order. Only fixed
  // reason codes leave this world, never a value, label or name.
  const sameSet = now => now.length === elements.length && now.every((e,n)=>e === elements[n]);
  const why = () => document !== doc || location.href !== href || !topSame() ? 'navigated' :
    !chain() ? 'frame_changed' : !sameSet(allInputs()) ? 'fields_replaced' :
    JSON.stringify(describe()) !== signature ? 'fields_relabeled' :
    !(entries.every(x=>visible(x.e)) && unexposed()) ? 'field_hidden' : null;
  const valid = () => why() === null;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
  const selectSetter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set;
  // A select fill names the value of exactly one enabled option, chosen on the inspected options.
  const oneOption = (el,v) => Array.from(el.options).filter(o=>o.value === v && !o.disabled).length === 1 &&
    Array.from(el.options).filter(o=>o.value === v).length === 1;
  // One value in two spellings: providers re-format as they mask ("07/35" -> "07 / 35" or "0735",
  // grouped card numbers, upper-cased names). Anything else is a different value.
  const same = (a,b) => a.replace(/[\s\/.-]/g,'').toUpperCase() === b.replace(/[\s\/.-]/g,'').toUpperCase();
  const live = el => el.isConnected && el.ownerDocument === doc && allInputs().includes(el);
  // dlz: strict mode (unreviewed destinations): the focused field is what the user would hit at its centre.
  const hits = el => {
    const r = el.getBoundingClientRect(), root = el.getRootNode ? el.getRootNode() : doc;
    const at = (root && typeof root.elementFromPoint === 'function' ? root : doc)
      .elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return at === el;
  };
  let used = false, written = null, paused = null, candidate = null;
  // One step of the commit; a finished (or never started) commit answers changed, never a second run. `answer`
  // is the supervisor's reply to a re-pin request (pins or null), undefined otherwise.
  const advance = answer => {
    const step = paused ? paused.next(answer) : {done: true, value: {success:false,error_type:'payment_fields_changed',filled_fields:0}};
    if (step.done) paused = null;
    return step.value;
  };
  // dlz (re-pin): re-renders the commit may survive; navigation, a changed frame chain and a covered field never.
  const REPINNABLE = ['fields_replaced', 'fields_relabeled', 'field_hidden', 'detached', 'hidden', 'deferred'];
  const MAX_REPINS = 3;
  // dlz: the fixed descriptor keys that differ between two descriptions (names only, never their values).
  const keysOf = (a, b) => Object.keys(a).filter(k => JSON.stringify(a[k]) !== JSON.stringify(b && b[k]));
  // dlz: the re-pin candidate, as the supervisor will classify it: this document's exposed fields now.
  const exposedNow = () => {
    const els = allInputs(), j = judge(els);
    return {els, entries: j.filter(x=>x.x === true), unsure: j.some(x => x.x !== true && x.x !== false)};
  };
  // dlz: the exact field set the supervisor described, still in place and described the same; then adopted.
  const adopt = (pins, plan) => {
    const c = candidate;
    candidate = null;
    if (!c || !pins || typeof pins !== 'object' || why() === 'navigated' || !chain()) return false;
    const now = exposedNow();
    const exposed = new Set(now.entries.map(x=>x.index));
    // the same elements, described the same, and exactly the fields exposed when the supervisor read them
    if (!(now.els.length === c.els.length && now.els.every((e,n)=>e === c.els[n])) ||
        JSON.stringify(now.els.map(describeOne)) !== c.sig ||
        exposed.size !== c.exposed.length || c.exposed.some(n => !exposed.has(n))) return false;
    const indexes = plan.map(f => pins[f.token]);
    if (plan.some(f => !Object.prototype.hasOwnProperty.call(pins, f.token)) ||
        indexes.some(n => typeof n !== 'number' || (n !== -1 && !exposed.has(n))) ||
        new Set(indexes.filter(n => n !== -1)).size !== indexes.filter(n => n !== -1).length ||
        plan.some((f, k) => indexes[k] === -1 && (f.done || f.token === 'cc-number'))) return false;  // never deferred
    elements = now.els;
    entries = now.entries;
    selected = exposed;
    signature = JSON.stringify(describe());
    plan.forEach((f, k) => { f.index = indexes[k];
      if (f.index !== -1) elements[f.index].style.setProperty('-webkit-text-security','disc','important'); });
    return true;
  };
  // dlz (B3): the description, or the overflow marker (too many fields, nested shadow roots, or too large), or
  // the unknown marker (a field whose exposure cannot be judged).
  const bounded = (d, unsure) => overflow || (LIM && JSON.stringify(d).length > LIM.bytes) ? {overflow:true} :
    unsure ? {unknown:true} : d;
  return {
    describe:()=>bounded(describe().filter(x=>selected.has(x.index)), unknown), valid,
    // dlz (B4, discovery only): the fields shown now, for a card-free document's absence check; null once the
    // document or its URL changed.
    scan: () => {
      if (!LIM || document !== doc || location.href !== href) return null;
      const now = allInputs().map((e,index)=>({e,index,x:exposure(e)}));
      return bounded(now.filter(x=>x.x === true).map(x=>describeOne(x.e,x.index)),
                     now.some(x => x.x !== true && x.x !== false));
    },
    // dlz (re-pin): only while a commit waits on a re-pin answer, in the same document, URL and same-origin
    // chain: this document's exposed fields now (the inspection's own description and limits) and, per field
    // token, the new index of the element pinned for it so far (-1: gone). The answer is single-use.
    rebind: () => {
      candidate = null;
      if (!paused || !written || why() === 'navigated' || !chain()) return null;
      const now = exposedNow();
      const fields = bounded(now.entries.map(x=>describeOne(x.e,x.index)), now.unsure);
      if (!Array.isArray(fields)) return fields;
      candidate = {els: now.els, sig: JSON.stringify(now.els.map(describeOne)), exposed: now.entries.map(x=>x.index)};
      const kept = {};
      for (const f of written) kept[f.token] = f.index === -1 ? -1 : now.els.indexOf(elements[f.index]);
      return {fields, kept};
    },
    // After every document was written and the page had time to settle: does each written field still
    // hold its value? States and lengths only; a value never leaves this world. Answers once.
    retained: () => {
      const w = written;
      written = null;
      if (!w) return null;
      const page = document === doc && location.href === href;
      return w.filter(f => f.done).map(f => {
        const el = f.index === -1 ? null : elements[f.index];
        // dlz: a field the provider hid in favour of a fresh input is not the one the user sees.
        if (!page || !el || !live(el) || !shown(el)) return {token:f.token,state:'replaced',length:0};
        return {token:f.token,state:same(el.value,f.value)?'held':el.value?'changed':'empty',length:el.value.length};
      });
    },
    commit: (fills, deadline, strict, paced) => {
      if (used) return {success:false,error_type:'payment_fields_changed',filled_fields:0};
      used = true;
      // dlz: a document that changed since the inspection (or since an earlier document's writes) stops here,
      // unless the change is a re-render: then the first field's checks ask for a re-pin before any write.
      const start = why();
      if (start && !REPINNABLE.includes(start))
        return {success:false,error_type:'payment_fields_changed',filled_fields:0,
                check:{token:String((Array.isArray(fills) && fills[0] && fills[0].token) || 'other'),reason:start,step:1}};
      // dlz: `deadline` is ms after `born` on this document's monotonic clock (never Date.now(): a wall-clock
      // step must not reopen the window); checked here and again before every setter. Not a number: late.
      const late = () => !(typeof deadline === 'number' && performance.now() - born < deadline &&
                           Date.now() - bornWall < deadline);
      if (late()) return {success:false,error_type:'payment_consent_expired',filled_fields:0};
      if (!Array.isArray(fills) || !fills.length ||
          fills.some(f=>!selected.has(f.index) || typeof f.value !== 'string' || !f.value) ||
          new Set(fills.map(f=>f.index)).size !== fills.length ||
          new Set(fills.map(f=>String(f.token||''))).size !== fills.length)
        return {success:false,error_type:'payment_fields_invalid',filled_fields:0};
      // Every select target is checked before ANY write in this document: zero or several matching
      // options leave the whole document untouched.
      const selects = fills.filter(f=>isSelect(elements[f.index]));
      if (selects.some(f=>!['cc-exp-month','cc-exp-year'].includes(f.token)))
        return {success:false,error_type:'payment_fields_invalid',filled_fields:0};
      if (selects.some(f=>!oneOption(elements[f.index],f.value)))
        return {success:false,error_type:'payment_expiry_option_unmatched',filled_fields:0};
      // Native setters in an isolated world, one field at a time with its own events, the card number
      // last: a controlled form (Stripe Card/Payment Element) re-renders on each input event and would
      // reset sibling fields written before the event they never saw; brand detection on the number
      // re-shapes the CVC (Braintree, Mollie). Writes only reach the pinned element references.
      for (const f of fills) elements[f.index].style.setProperty('-webkit-text-security','disc','important');
      // dlz: the plan, one entry per field: its token, value and CURRENT pin (a re-pin moves it; -1: deferred
      // until the card number is in), and whether it was written.
      const plan = fills.map(f=>({index:f.index,token:String(f.token||''),value:f.value,done:false}));
      written = plan;
      const queue = plan.slice().sort((a,b)=>(a.token==='cc-number')-(b.token==='cc-number'));
      const later = [];
      let attempted = 0, repins = 0, last = null;
      const changed = () => ({success:false,error_type:'payment_fields_changed',filled_fields:attempted,
                              partially_filled:attempted>0,...(last ? {check:last} : {})});
      const expired = () => ({success:false,error_type:'payment_consent_expired',filled_fields:attempted,
                              partially_filled:attempted>0});
      // dlz: what the failed check saw, for the record: reason code, field token, write step, and for a changed
      // description the descriptor KEYS that changed; `fields` names the pinned tokens involved ('other': none).
      const pinnedToken = n => (plan.find(f => f.index === n) || {token: 'other'}).token;
      const involved = reason => {
        const now = reason === 'fields_relabeled' ? describe() : null, before = now && JSON.parse(signature);
        const hit = reason === 'fields_replaced' ? plan.filter(f => f.index !== -1 && !allInputs().includes(elements[f.index]))
          : reason === 'field_hidden' ? entries.filter(x => !visible(x.e)).map(x => ({token: pinnedToken(x.index)}))
          : now ? now.map((d, n) => keysOf(d, before[n]).length ? {token: pinnedToken(n)} : null).filter(Boolean) : [];
        const fields = [...new Set(hit.map(f => f.token))].sort();
        const keys = now ? [...new Set(now.flatMap((d, n) => keysOf(d, before[n])))].sort() : null;
        return {...(fields.length ? {fields} : {fields: ['other']}), ...(keys ? {keys} : {})};
      };
      const note = (f, reason) => { last = {token: f.token, reason, step: attempted + 1,
        ...(['fields_replaced', 'fields_relabeled', 'field_hidden'].includes(reason) ? involved(reason) : {})}; };
      // dlz: `paced` (strict fills in a frame): after each focus the closure pauses and hands back the field's
      // centre; the supervisor checks the cross-origin owner chain from the parents (unreachable from here)
      // and only then calls resume(), which re-runs every local check before the setter. Unpaced, the same
      // steps run in one go.
      // dlz (re-pin): a field whose check fails on a re-render (the field set replaced or re-described, the field
      // detached or not shown) is not written; the commit pauses with a re-pin request instead. The supervisor
      // re-proves the frame, reads rebind(), classifies the fields with the inspection's classifier and answers
      // the pin of every token (-1: not shown yet, written after the card number) or null (stop). Every check
      // then runs again on the new pins before any setter; at most MAX_REPINS per document.
      const steps = function* () {
        for (;;) {
          while (queue.length) {
            const f = queue[0];
            let reason = f.index === -1 ? 'deferred' : null;
            if (!reason) {
              const el = elements[f.index];
              reason = why() || (live(el) ? null : 'detached');
              if (!reason) {
                el.focus();
                if (late())  // focus handlers run page code; never write past the window
                  return expired();
                if (paced === true) {
                  const r = el.getBoundingClientRect();
                  yield {pending:true,filled_fields:attempted,x:r.left + r.width / 2,y:r.top + r.height / 2};
                }
                // dlz: focus handlers can hide, swap, relabel or navigate: the whole pinned set is re-checked
                // right before this setter, not only before the loop.
                reason = why() || (!live(el) ? 'detached' : !shown(el) ? 'hidden' :
                                   strict === true && !hits(el) ? 'hit_test' : null);
              }
            }
            if (reason) {
              note(f, reason);
              if (!REPINNABLE.includes(reason) || repins >= MAX_REPINS || late()) return changed();
              repins += 1;
              const pins = yield {repin:true,filled_fields:attempted,check:last,
                                  written:plan.filter(x=>x.done).map(x=>x.token)};
              if (!adopt(pins, plan)) return changed();
              if (f.index === -1) {
                if (reason === 'deferred') return changed();  // still not shown with the number in: stop
                later.push(queue.shift());
              }
              continue;
            }
            if (late())  // the checks above run page-visible code too (layout, style): the window again, last
              return expired();
            const el = elements[f.index];
            if (isSelect(el) && !oneOption(el,f.value)) {  // a re-pinned select is matched again, never guessed
              note(f, 'options_changed');
              return changed();
            }
            queue.shift();
            attempted += 1;
            f.done = true;
            if (isSelect(el)) {
              selectSetter.call(el,f.value);
              el.dispatchEvent(new Event('input',{bubbles:true}));
            } else {
              setter.call(el,f.value);
              el.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText'}));
            }
            el.dispatchEvent(new Event('change',{bubbles:true}));
            el.dispatchEvent(new FocusEvent('blur'));
            el.blur();
            if (f.token === 'cc-number') queue.push(...later.splice(0));
          }
          if (!later.length) break;
          // dlz: deferred fields wait for a card number in another document: the supervisor resumes this commit
          // after that document, and each deferred field is then re-pinned before it is written.
          yield {deferred:true,filled_fields:attempted};
          queue.push(...later.splice(0));
        }
        const held = plan.filter(f=>f.index !== -1 && live(elements[f.index]) && same(elements[f.index].value,f.value)).length;
        return held === plan.length && location.href === href ? {success:true,filled_fields:held} :
          {success:false,error_type:'payment_validation_failed',filled_fields:held,partially_filled:true};
      };
      paused = steps();
      return advance();
    },
    resume: answer => advance(answer)
  };
})()"""
