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
  const elements = allInputs();
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
  const judged = elements.map((e,index)=>({e,index,x:exposure(e)}));
  const unknown = judged.some(x => x.x !== true && x.x !== false);
  const entries = judged.filter(x=>x.x === true);
  const selected = new Set(entries.map(x=>x.index));
  // dlz (Codex B r1 #2): discovery binds the exposed set: a field hidden at inspection and shown now (a revealed
  // duplicate) or one whose exposure became unknown changes the document.
  const unexposed = () => !LIM || elements.every((e,n)=>selected.has(n) || exposure(e) === false);
  const signature = JSON.stringify(describe());
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
  const scope = () => scopeChain.every(c => { try { return c.pw.location.href === c.ph && ownerShown(c.fe); }
                                             catch (_) { return false; } }) &&
    (topHref === null || readTop() === topHref);
  const valid = () => document === doc && location.href === href && scope() &&
    JSON.stringify(describe()) === signature &&
    (now => now.length === elements.length && now.every((e,n)=>e === elements[n]))(allInputs()) &&
    entries.every(x=>visible(x.e)) && unexposed();
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
  let used = false, written = null, paused = null;
  // One step of the commit; a finished (or never started) commit answers changed, never a second run.
  const advance = () => {
    const step = paused ? paused.next() : {done: true, value: {success:false,error_type:'payment_fields_changed',filled_fields:0}};
    if (step.done) paused = null;
    return step.value;
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
    // After every document was written and the page had time to settle: does each written field still
    // hold its value? States and lengths only; a value never leaves this world. Answers once.
    retained: () => {
      const w = written;
      written = null;
      if (!w) return null;
      const page = document === doc && location.href === href;
      return w.map(f => {
        const el = elements[f.index];
        // dlz: a field the provider hid in favour of a fresh input is not the one the user sees.
        if (!page || !live(el) || !shown(el)) return {token:f.token,state:'replaced',length:0};
        return {token:f.token,state:same(el.value,f.value)?'held':el.value?'changed':'empty',length:el.value.length};
      });
    },
    commit: (fills, deadline, strict, paced) => {
      if (used || !valid()) return {success:false,error_type:'payment_fields_changed',filled_fields:0};
      used = true;
      // dlz: `deadline` is ms after `born` on this document's monotonic clock (never Date.now(): a wall-clock
      // step must not reopen the window); checked here and again before every setter. Not a number: late.
      const late = () => !(typeof deadline === 'number' && performance.now() - born < deadline &&
                           Date.now() - bornWall < deadline);
      if (late()) return {success:false,error_type:'payment_consent_expired',filled_fields:0};
      if (!Array.isArray(fills) || !fills.length ||
          fills.some(f=>!selected.has(f.index) || typeof f.value !== 'string' || !f.value) ||
          new Set(fills.map(f=>f.index)).size !== fills.length)
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
      const ordered = fills.slice().sort((a,b)=>(a.token==='cc-number')-(b.token==='cc-number'));
      // Page handlers run between these writes, so each target is re-checked first, and success is
      // judged on the inputs still in the document: a re-render that replaced a node must not let
      // its detached copy report a value the live form does not hold.
      written = fills.map(f=>({index:f.index,token:String(f.token||''),value:f.value}));
      let attempted = 0;
      const changed = () => ({success:false,error_type:'payment_fields_changed',filled_fields:attempted,
                              partially_filled:attempted>0});
      const expired = () => ({success:false,error_type:'payment_consent_expired',filled_fields:attempted,
                              partially_filled:attempted>0});
      // dlz: `paced` (strict fills in a frame): after each focus the closure pauses and hands back the field's
      // centre; the supervisor checks the cross-origin owner chain from the parents (unreachable from here)
      // and only then calls resume(), which re-runs every local check before the setter. Unpaced, the same
      // steps run in one go.
      const steps = function* () {
        for (const f of ordered) {
          const el = elements[f.index];
          if (!valid() || !live(el))
            return changed();
          el.focus();
          if (late())  // focus handlers run page code; never write past the window
            return expired();
          if (paced === true) {
            const r = el.getBoundingClientRect();
            yield {pending:true,filled_fields:attempted,x:r.left + r.width / 2,y:r.top + r.height / 2};
          }
          // dlz: focus handlers can hide, swap, relabel or navigate: the whole pinned set is re-checked
          // right before this setter, not only before the loop.
          if (!valid() || !live(el) || !shown(el) || (strict === true && !hits(el)))
            return changed();
          if (late())  // the checks above run page-visible code too (layout, style): the window again, last
            return expired();
          attempted += 1;
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
        }
        const held = fills.filter(f=>live(elements[f.index]) && same(elements[f.index].value,f.value)).length;
        return held === fills.length && location.href === href ? {success:true,filled_fields:held} :
          {success:false,error_type:'payment_validation_failed',filled_fields:held,partially_filled:true};
      };
      paused = steps();
      return advance();
    },
    resume: () => advance()
  };
})()"""
