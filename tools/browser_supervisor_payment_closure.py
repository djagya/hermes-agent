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
  // dlz (re-pin, Codex r1 P1-2): every control the inspection judged exposed, with its inspection index: its role
  // (the supervisor's classification at inspection, card or not) binds that element for the whole commit.
  const inspected = entries.slice();
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
  // dlz: the pinned state holds: the decision of every fill without re-pin (and of `valid`, read by the supervisor),
  // unchanged since before re-pinning existed; why() only names what failed, after the decision.
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
  // dlz (re-pin, Codex r1 P1-1): a re-pinned field, after its focus scrolled it into view: its centre is inside this
  // document's viewport and what a click there would hit (the field or a descendant of it), never an overlay.
  const exposedHere = el => {
    const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
    const w = globalThis.innerWidth, h = globalThis.innerHeight;
    if (!(typeof w === 'number' && typeof h === 'number' && x >= 0 && y >= 0 && x < w && y < h)) return false;
    const root = el.getRootNode ? el.getRootNode() : doc;
    const at = (root && typeof root.elementFromPoint === 'function' ? root : doc).elementFromPoint(x, y);
    return !!at && (at === el || (typeof el.contains === 'function' && el.contains(at)));
  };
  let used = false, written = null, paused = null, candidate = null, repinning = false;
  // dlz (fill-auto): the description a label-only change was read as (single use; see relabeled / accept).
  let relabel = null;
  // dlz (fill-auto r3 G3): the approval this document's roles were recorded for (`remember`), and per element, in this
  // isolated world for the life of the document, the card token whose value it got the events of a write for (one
  // card read per approval: a token names its value). Tokens only, never a value.
  let approvalId = null;
  const notified = () => {
    if (!approvalId) return null;
    let reg = globalThis.__hermesPaymentNotified;
    if (!reg || reg.approval !== approvalId) globalThis.__hermesPaymentNotified = reg = {approval: approvalId, map: new WeakMap()};
    return reg.map;
  };
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
  // dlz (fill-auto): the pinned elements are the same ones and, row by row, only their `label` text differs from
  // the pinned description (validation text, a floating label, a placeholder hint). Type, autocomplete, name and id,
  // form, length limit and a select's options are all unchanged.
  const labelOnly = () => {
    const now = describe(), before = JSON.parse(signature);
    return now.length === before.length && now.every((d, n) =>
      keysOf(d, before[n]).every(k => k === 'label') && keysOf(before[n], d).every(k => k === 'label'));
  };
  // dlz: what a failed check saw, for the stop record: reason code, field token, write step, and for a changed
  // description the descriptor KEYS that changed; `fields` names the pinned tokens involved ('other': none).
  // `plan`: the fill entries (token and current pin). Fixed codes only, never a value, label or name.
  const involved = (reason, plan) => {
    const pinnedToken = n => (plan.find(f => f.index === n) || {token: 'other'}).token;
    const now = reason === 'fields_relabeled' ? describe() : null, before = now && JSON.parse(signature);
    const hit = reason === 'fields_replaced' ? plan.filter(f => f.index !== -1 && !allInputs().includes(elements[f.index]))
      : reason === 'field_hidden' ? entries.filter(x => !visible(x.e)).map(x => ({token: pinnedToken(x.index)}))
      : now ? now.map((d, n) => keysOf(d, before[n]).length ? {token: pinnedToken(n)} : null).filter(Boolean) : [];
    const fields = [...new Set(hit.map(f => f.token))].sort();
    const keys = now ? [...new Set(now.flatMap((d, n) => keysOf(d, before[n])))].sort() : null;
    return {...(fields.length ? {fields} : {fields: ['other']}), ...(keys ? {keys} : {})};
  };
  const record = (plan, token, reason, step) => ({token, reason, step,
    ...(['fields_replaced', 'fields_relabeled', 'field_hidden'].includes(reason) ? involved(reason, plan) : {})});
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
    // every field not written yet is re-pinned: before its setter it must be fully exposed (exposedHere) and, from the
    // supervisor, its owner chain hit at its centre (a pause after its focus), whatever `strict` is.
    plan.forEach((f, k) => { f.index = indexes[k]; if (!f.done) f.repinned = true;
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
    // dlz (fill-auto): a label-only change of the pinned fields, for the supervisor to classify again with the
    // inspection's classifier: this document's exposed fields as described now, or null when the pinned state
    // differs in any other way (then nothing here applies and the change stops the fill or is re-inspected).
    relabeled: () => {
      relabel = null;
      if (why() !== 'fields_relabeled' || !labelOnly()) return null;
      const d = describe();
      relabel = JSON.stringify(d);
      return bounded(d.filter(x=>selected.has(x.index)), false);
    },
    // The supervisor found every field's role unchanged in that reading: it becomes the pinned description, only if
    // the same elements are still described exactly as it was read. Single use; anything else answers false.
    accept: () => {
      const c = relabel;
      relabel = null;
      if (!c || document !== doc || location.href !== href || !sameSet(allInputs()) ||
          JSON.stringify(describe()) !== c) return false;
      signature = c;
      return true;
    },
    // dlz (fill-auto): the role (card token, or '' for any other exposed control) each exposed element was given
    // by an inspection of this approval, kept per element in this isolated world for the life of the document. A
    // later inspection of the same document (a re-inspection after a re-render) must give every element it still
    // exposes the role it had: `moved` when one differs (nothing is recorded then). `known`: the record of this
    // approval already existed here. Names of roles only.
    remember: (approval, roles) => {
      const KEY = '__hermesPaymentRoles';
      if (typeof approval !== 'string' || !approval || !roles || typeof roles !== 'object') return null;
      let reg = globalThis[KEY];
      const known = !!reg && reg.approval === approval;
      if (!known) reg = {approval, map: new WeakMap()};
      const role = x => String(Object.prototype.hasOwnProperty.call(roles, x.index) ? roles[x.index] : '');
      if (entries.some(x => reg.map.has(x.e) && reg.map.get(x.e) !== role(x))) return {known, moved: true};
      for (const x of entries) reg.map.set(x.e, role(x));
      globalThis[KEY] = reg;
      approvalId = approval;
      return {known, moved: false};
    },
    // dlz (re-pin): only while a commit waits on a re-pin answer, in the same document, URL and same-origin
    // chain: this document's exposed fields now (the inspection's own description and limits), per field token the
    // new index of the element pinned for it so far (-1: gone), and per control the inspection judged exposed its
    // [inspection index, new index] (-1: gone), for the role check. The answer is single-use.
    rebind: () => {
      candidate = null;
      if (!repinning || !paused || !written || why() === 'navigated' || !chain()) return null;
      const now = exposedNow();
      const fields = bounded(now.entries.map(x=>describeOne(x.e,x.index)), now.unsure);
      if (!Array.isArray(fields)) return fields;
      candidate = {els: now.els, sig: JSON.stringify(now.els.map(describeOne)), exposed: now.entries.map(x=>x.index)};
      const kept = {};
      for (const f of written) kept[f.token] = f.index === -1 ? -1 : now.els.indexOf(elements[f.index]);
      return {fields, kept, inspected: inspected.map(x => [x.index, now.els.indexOf(x.e)])};
    },
    // After every document was written and the page had time to settle: does each written field still
    // hold its value? States and lengths only; a value never leaves this world. Answers once.
    retained: () => {
      const w = written;
      written = null;
      if (!w) return null;
      const page = document === doc && location.href === href;
      return w.filter(f => f.done !== false).map(f => {
        const el = f.index === -1 ? null : elements[f.index];
        // dlz: a field the provider hid in favour of a fresh input is not the one the user sees.
        if (!page || !el || !live(el) || !shown(el)) return {token:f.token,state:'replaced',length:0};
        return {token:f.token,state:same(el.value,f.value)?'held':el.value?'changed':'empty',length:el.value.length};
      });
    },
    // dlz: `repin` (config vault.payment_repin, reviewed destinations; a literal true only): the commit may pause
    // for a re-pin or a deferral. Without it this is the commit as it was before re-pinning existed: any change of
    // the pinned state stops it (`plain` below), and the only addition is the stop record.
    // dlz (fill-auto): `auto` ('fill' or 'repair'; config vault.payment_fill_auto, on by default): the commit of a
    // fill that may be continued on a fresh inspection. It writes like `plain`, with three differences: a label-only
    // change pauses for the supervisor's re-classification instead of stopping (`relabel`); a field marked `kept`
    // (an earlier inspection of this approval may already have written its token) that still holds its value is not written
    // again; 'repair' writes the card number first and the CVC last. Every other change stops it exactly as `plain`
    // stops, with the stop record; the supervisor then decides, by a full new inspection, whether the fill goes on.
    commit: (fills, deadline, strict, paced, repin, auto) => {
      const first = () => String((Array.isArray(fills) && fills[0] && fills[0].token) || 'other');
      const automatic = auto === 'fill' || auto === 'repair';
      if (automatic) {
        if (used) return {success:false,error_type:'payment_fields_changed',filled_fields:0};
        used = true;
        const start = why();
        if (start && !(start === 'fields_relabeled' && labelOnly())) {
          let check = null;
          try {
            if (Array.isArray(fills)) check = record(fills.map(f=>({index:f.index,token:String(f.token||'')})), first(), start, 1);
          } catch (_) {}
          return {success:false,error_type:'payment_fields_changed',filled_fields:0,...(check ? {check} : {})};
        }
      } else if (repin !== true) {
        if (used) return {success:false,error_type:'payment_fields_changed',filled_fields:0};
        if (!valid()) {
          // the decision is taken; the record only names it (which pinned fields, which descriptor keys)
          let check = null;
          try {
            const start = why();
            if (start) check = record(fills.map(f=>({index:f.index,token:String(f.token||'')})), first(), start, 1);
          } catch (_) {}
          return {success:false,error_type:'payment_fields_changed',filled_fields:0,...(check ? {check} : {})};
        }
        used = true;
      } else {
        if (used) return {success:false,error_type:'payment_fields_changed',filled_fields:0};
        used = repinning = true;
        // dlz: a document that changed since the inspection (or since an earlier document's writes) stops here,
        // unless the change is a re-render: then the first field's checks ask for a re-pin before any write.
        const start = why();
        if (start && !REPINNABLE.includes(start))
          return {success:false,error_type:'payment_fields_changed',filled_fields:0,
                  check:{token:first(),reason:start,step:1}};
      }
      // dlz: `deadline` is ms after `born` on this document's monotonic clock (never Date.now(): a wall-clock
      // step must not reopen the window); checked here and again before every setter. Not a number: late.
      const late = () => !(typeof deadline === 'number' && performance.now() - born < deadline &&
                           Date.now() - bornWall < deadline);
      if (late()) return {success:false,error_type:'payment_consent_expired',filled_fields:0};
      if (!Array.isArray(fills) || !fills.length ||
          fills.some(f=>!selected.has(f.index) || typeof f.value !== 'string' || !f.value) ||
          new Set(fills.map(f=>f.index)).size !== fills.length ||
          (repinning && new Set(fills.map(f=>String(f.token||''))).size !== fills.length))  // pins are per token
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
      // dlz: the plan, one entry per field: its token, value and CURRENT pin (a re-pin moves it; -1: deferred
      // until the card number is in), and, re-pinning, whether it was written (`done`).
      const plan = fills.map(f=>({index:f.index,token:String(f.token||''),value:f.value,...(repinning ? {done:false} : {}),
                                  ...(automatic && f.kept === true ? {kept:true} : {}),
                                  ...(automatic && f.adopted === true ? {adopted:true} : {})}));
      written = plan;
      const queue = plan.slice().sort((a,b)=>(a.token==='cc-number')-(b.token==='cc-number'));
      const later = [];
      let attempted = 0, repins = 0, last = null;
      const changed = () => ({success:false,error_type:'payment_fields_changed',filled_fields:attempted,
                              partially_filled:attempted>0,...(last ? {check:last} : {})});
      const expired = () => ({success:false,error_type:'payment_consent_expired',filled_fields:attempted,
                              partially_filled:attempted>0});
      const note = (f, reason) => { last = record(plan, f.token, reason, attempted + 1); };
      // dlz: `paced` (strict fills in a frame): after each focus the closure pauses and hands back the field's
      // centre; the supervisor checks the cross-origin owner chain from the parents (unreachable from here)
      // and only then calls resume(), which re-runs every local check before the setter. Unpaced, the same
      // steps run in one go.
      // dlz: the stop record of a plain commit, named only after the decision to stop was taken (it never changes
      // that decision; a reason that cannot be named leaves the record out).
      const stopped = (f, el, focused) => {
        try {
          const reason = why() || (!live(el) ? 'detached' : !focused ? null : !shown(el) ? 'hidden' :
                                   strict === true && !hits(el) ? 'hit_test' : null);
          if (reason) note(f, reason);
        } catch (_) { last = null; }
        return changed();
      };
      // dlz: the commit without re-pin: Page handlers run between these writes, so each target is re-checked
      // first, and success is judged on the inputs still in the document: a re-render that replaced a node must
      // not let its detached copy report a value the live form does not hold.
      const plain = function* () {
        for (const f of ordered) {
          const el = elements[f.index];
          if (!valid() || !live(el))
            return stopped(f, el, false);
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
            return stopped(f, el, true);
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
                if (paced === true || f.repinned) {
                  const r = el.getBoundingClientRect();
                  yield {pending:true,filled_fields:attempted,x:r.left + r.width / 2,y:r.top + r.height / 2};
                }
                // dlz: focus handlers can hide, swap, relabel or navigate: the whole pinned set is re-checked
                // right before this setter, not only before the loop.
                reason = why() || (!live(el) ? 'detached' : !shown(el) ? 'hidden' :
                                   strict === true && !hits(el) ? 'hit_test' :
                                   f.repinned && !exposedHere(el) ? 'hit_test' : null);
              }
            }
            if (reason) {
              note(f, reason);
              if (!REPINNABLE.includes(reason) || repins >= MAX_REPINS || late()) return changed();
              repins += 1;
              const pins = yield {repin:true,filled_fields:attempted,check:last,
                                  written:plan.filter(x=>x.done).map(x=>x.token)};
              if (!adopt(pins, plan)) return changed();
              // dlz (Opus r1 P2-2): every field not shown yet waits for the card number, wherever it stands in the
              // queue; one that already came back after the number and is still not shown stops the commit.
              const waiting = queue.filter(x => x.index === -1);
              if (waiting.some(x => x.requeued)) return changed();
              for (const x of waiting) queue.splice(queue.indexOf(x), 1);
              later.push(...waiting);
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
            if (f.token === 'cc-number') queue.push(...later.splice(0).map(x => Object.assign(x, {requeued: true})));
          }
          if (!later.length) break;
          // dlz: deferred fields wait for a card number in another document: the supervisor resumes this commit
          // after that document, and each deferred field is then re-pinned before it is written.
          yield {deferred:true,filled_fields:attempted};
          queue.push(...later.splice(0).map(x => Object.assign(x, {requeued: true})));
        }
        const held = plan.filter(f=>f.index !== -1 && live(elements[f.index]) && same(elements[f.index].value,f.value)).length;
        return held === plan.length && location.href === href ? {success:true,filled_fields:held} :
          {success:false,error_type:'payment_validation_failed',filled_fields:held,partially_filled:true};
      };
      // dlz (fill-auto): see `commit`. The same checks as `plain` before the focus and before the setter; `settled`
      // answers them, pausing while the only change is a label. dlz (SBB/Datatrans, 11-13): there every write and
      // every focus re-labels some other field (validation text), so the bound grows with the plan and only ends a
      // page that never stops re-labelling: four per field and eight more. It is no re-inspection budget.
      const MAX_RELABELS = 4 * plan.length + 8;
      let relabels = 0;
      const RANK = {'cc-number': 0, 'cc-exp': 1, 'cc-exp-month': 1, 'cc-exp-year': 2, 'cc-name': 3, 'postal-code': 4, 'cc-csc': 5};
      // dlz (fill-auto, r1 F4, r3 G2): a field of a reading adopted through a re-inspection (`f.adopted`, set on every
      // fill of such a reading, the first generation after a re-inspection before the card read too) is one the
      // inspection shown to the owner never saw: before its setter it must be fully exposed (its centre inside the
      // viewport and hit there by the field or a descendant), on a reviewed destination too, as a re-pinned field
      // must (`exposedHere`); the supervisor adds the owner-chain hit test from the parents (paced).
      // dlz (fill-auto, r1 F3): an accepted relabel is an asynchronous pause after the paced check passed: a focused
      // field that is paced goes through that check again (`pending`) before the checks above run once more.
      const settled = function* (f, el, focused) {
        for (;;) {
          const reason = why() || (!live(el) ? 'detached' : !focused ? null : !shown(el) ? 'hidden' :
                                   strict === true && !hits(el) ? 'hit_test' :
                                   f.adopted === true && !exposedHere(el) ? 'hit_test' : null);
          if (reason !== 'fields_relabeled' || relabels >= MAX_RELABELS || !labelOnly() || late()) return reason;
          relabels += 1;
          const accepted = yield {relabel:true,filled_fields:attempted};
          if (accepted !== true) return reason;
          if (focused && paced === true) {
            const r = el.getBoundingClientRect();
            yield {pending:true,filled_fields:attempted,x:r.left + r.width / 2,y:r.top + r.height / 2};
          }
        }
      };
      const automaticSteps = function* () {
        const queue = auto === 'repair'
          ? plan.slice().sort((a,b)=>(RANK[a.token] ?? 4) - (RANK[b.token] ?? 4)) : plan.slice().sort((a,b)=>(a.token==='cc-number')-(b.token==='cc-number'));
        for (const f of queue) {
          const el = elements[f.index];
          let reason = yield* settled(f, el, false);
          if (reason) { note(f, reason); return changed(); }
          if (f.kept === true && shown(el) && same(el.value,f.value)) {  // written before and still held
            // dlz (fill-auto, r1 F14): no write, but the page's own state may not hold it (a controlled field that was
            // re-mounted with the value): the events of a write, with the value it already has, selects included.
            // r3 G3: only for an element that never got them for this token. Firing them again where they already
            // ran re-runs the page's handlers (a card number `input` that clears the CVC a moment later): the clear
            // the repair is there to fix.
            if (late())
              return expired();
            const seen = notified();
            if (!seen || seen.get(el) !== f.token) {
              el.dispatchEvent(isSelect(el) ? new Event('input',{bubbles:true})
                                            : new InputEvent('input',{bubbles:true,inputType:'insertText'}));
              el.dispatchEvent(new Event('change',{bubbles:true}));
              if (seen) seen.set(el, f.token);
            }
            attempted += 1;
            continue;
          }
          el.focus();
          if (late())  // focus handlers run page code; never write past the window
            return expired();
          if (paced === true) {
            const r = el.getBoundingClientRect();
            yield {pending:true,filled_fields:attempted,x:r.left + r.width / 2,y:r.top + r.height / 2};
          }
          reason = yield* settled(f, el, true);
          if (!reason && isSelect(el) && !oneOption(el,f.value)) reason = 'options_changed';
          if (reason) { note(f, reason); return changed(); }
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
          const seen = notified();
          if (seen) seen.set(el, f.token);
          el.dispatchEvent(new FocusEvent('blur'));
          el.blur();
        }
        // Whether every field holds its value is judged once the page settled (retained()): a provider that clears
        // a field right after its write gets one repair pass there. A moved document is never a success.
        return location.href === href ? {success:true,filled_fields:plan.length} :
          {success:false,error_type:'payment_validation_failed',filled_fields:attempted,partially_filled:true};
      };
      paused = automatic ? automaticSteps() : repinning ? steps() : plain();
      return advance();
    },
    resume: answer => advance(answer)
  };
})()"""
