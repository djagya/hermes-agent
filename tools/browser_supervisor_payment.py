"""Document-pinned payment operations on the existing supervisor socket.

No whole-checkout transaction is claimed. Card controls sit in one document, or are split
across documents on the merchant and at most ONE reviewed PSP origin with every card field
exactly once (Datatrans Secure Fields, Stripe split elements); secrets never go through CLI
eval or a model tool argument.
"""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from agent.vault_login_classifier import LoginControl, classify_checkout_control
from agent.vault_payment_policy import (
    PAYMENT_PSP_ORIGINS,
    UNSUPPORTED_PROVIDERS,
    PaymentDestinationError,
    PaymentPolicyError,
    coverage_hint,
    https_origin,
    provider_for_origin,
)


_MAX_CARD_DOCUMENTS = 4
# dlz (design v2 B3): global traversal bounds, both paths: frames in the whole tree, nesting depth below the top
# document, and out-of-process iframe targets expanded into this page's tree.
_MAX_FRAMES = 64
_MAX_FRAME_DEPTH = 8
_MAX_OOPIF_TARGETS = 32
# dlz (Codex B r1 #9): iframe targets browser-wide that the ownership search may consider, and DOM.getFrameOwner
# probes per tree walk: both bounded before traversal, so unrelated or unresolvable targets cannot stall a checkout.
_MAX_IFRAME_TARGETS = 128
_MAX_OWNER_PROBES = 512
# dlz (v1 §3.3, v2 B1-B4): the unreviewed-destination discovery. Frames on unreviewed origins that pass admission
# and exposure and get the inspect closure; card documents on the ONE unreviewed origin (the PAYONE shape: number,
# CVC, month, year, holder); nesting below the top for an unreviewed frame; pinned card-free admissible frames.
_MAX_UNREVIEWED_FRAMES = 8
_MAX_UNREVIEWED_DOCUMENTS = 5
_MAX_UNREVIEWED_DEPTH = 3
_MAX_ABSENCE_WITNESSES = 8
_MAX_DESCRIBE_BYTES = 256 * 1024
_DISCOVERY_LIMITS = {"inputs": 300, "shadow": 16, "text": 200, "bytes": _MAX_DESCRIBE_BYTES}
# dlz (Codex B r1 #5): the exact shape of one discovery descriptor (describeOne in _INSPECT_TEMPLATE).
_DESCRIPTOR_KEYS = frozenset({"index", "autocomplete", "name", "label", "type", "formIndex", "maxLength"})
_SELECT_TYPES = ("select", "select-multiple")

PAGE_TOO_LARGE = ("This checkout page has more frames, frame nesting or form fields than Hermes inspects for a card "
                  "fill, so nothing was read or written. Tell the user; the card has to be entered by hand here.")
FIELDS_UNSUPPORTED = ("No complete card form (number, CVC and expiry, each exactly once) was found on this checkout, "
                      "including on unreviewed payment forms. Nothing was read or written. Tell the user the card "
                      "has to be entered by hand here; never type card values yourself.")
FIELDS_AMBIGUOUS = ("The card fields on this checkout are ambiguous (a field appears more than once, the fields are "
                    "spread over more than one payment form, there are too many payment frames, or a card form is "
                    "not fully visible), so Hermes will not guess. Nothing was read or written. Tell the user; do "
                    "not retry.")


PAGE_UNVERIFIABLE = ("Hermes could not verify part of this checkout (a frame or form it checks answered in an "
                     "unexpected shape, did not answer, or uses a clipping it cannot judge), so nothing was read or "
                     "written. Tell the user; the card has to be entered by hand here. Do not retry.")


def _page_too_large() -> PaymentDestinationError:
    return PaymentDestinationError("payment_page_too_large", PAGE_TOO_LARGE)


def _unverifiable() -> PaymentDestinationError:
    """dlz (Codex B r1 #4/#5/#7): unknown, malformed or unjudgeable page data never counts as a safe negative."""
    return PaymentDestinationError("payment_page_unverifiable", PAGE_UNVERIFIABLE)
# Refusals that mean "no complete reviewed card document": these get coverage_hint's reason.
_COVERAGE_REFUSALS = frozenset({
    "Missing, ambiguous or split payment documents",
    "Incomplete or ambiguous card fields",
    "Missing card expiry fields",
})
# Bounded wait between the last document's write and the all-documents retention check: long enough
# for a provider's deferred re-render, validation or brand detection to land, short enough to stay
# well inside commit_payment's 20 s budget. Never a retry window. dlz: 2 s, not 1 s: a reset that
# lands between 1 and 2 s after the last write was still reported as success.
_SETTLE_SECONDS = 2.0
# dlz: commit_payment's whole budget; no write lands in a renderer after it (see _renderer_deadline_ms).
_COMMIT_BUDGET_SECONDS = 20.0
_RETENTION_STATES = frozenset({"held", "empty", "changed", "replaced"})
# Card fields a saved card may lack: left empty for the user instead of refusing the whole fill.
OPTIONAL_CARD_TOKENS = frozenset({"cc-name", "postal-code"})


@dataclass
class PaymentDocument:
    """One document holding card controls, pinned by frame, session and isolated-world object."""

    frame_id: str
    frame_session: str
    object_id: str
    origin: str
    controls: tuple
    # dlz: host monotonic time just after this document's closure was created (its renderer clock anchor).
    clock_anchor: float | None = None


@dataclass
class PaymentInspection:
    operation_id: str
    page_session: str
    page_id: str
    merchant_origin: str
    documents: tuple
    tree: tuple
    expires_at: float
    consumed: bool = False
    owner: Any = field(default=None, repr=False)
    child_sessions: dict = field(default_factory=dict, repr=False)
    # dlz: parent frame id -> (session, [(frame id, pinned owner <iframe> handle)]) for every frame from a card
    # document up to the page, all handles of one parent in one isolated world (one call checks them all).
    frame_owners: dict = field(default_factory=dict, repr=False)
    # dlz: the one release pass (asyncio task) shared by commit and discard.
    release_task: Any = field(default=None, repr=False, compare=False)
    # dlz (Track B): receiving origins outside the reviewed set, in document order; empty = a reviewed fill, and
    # none of the fields below changes how it is checked.
    unreviewed_origins: tuple = ()
    # The reviewed hosted origins the inspection admitted (re-admission of new frames, B4).
    allowed_frames: tuple = ()
    # (session, object id) of each pinned card-free admissible document: it must stay card-free (B4).
    absence: tuple = field(default=(), repr=False)
    # dlz (Codex B r1 #2): their frame ids. With the card chains these are the frames bound by pinned objects; any
    # other admissible frame on an approved origin, new or present but hidden at inspection, must stay unexposed.
    absence_frames: tuple = ()
    # dlz (Codex B r1, switch/write race): a callable answering whether the destination policy still holds (set by
    # fill_payment for an unreviewed fill); re-read between documents and on every paced resume.
    policy_check: Any = field(default=None, repr=False, compare=False)
    # (session, object id) of each pinned amount witness (B5), and the page-check result shown to the owner.
    witnesses: tuple = field(default=(), repr=False)
    page_match: tuple = ("not_checked", "")

    @property
    def controls(self) -> tuple:
        return tuple(c for d in self.documents for c in d.controls)

    @property
    def target_origins(self) -> tuple:
        """Every origin that receives card fields, once each, in document order."""
        return tuple(dict.fromkeys(d.origin for d in self.documents))

    @property
    def frame_origin(self) -> str:
        """The hosted PSP origin when one receives fields, else the merchant."""
        hosted = [o for o in self.target_origins if o != self.merchant_origin]
        return hosted[0] if hosted else self.merchant_origin

    @property
    def frame_session(self) -> str:
        """The card document's session (the first one of a split checkout)."""
        return self.documents[0].frame_session

    def prompt(self) -> dict:
        n = len(self.documents)
        return {
            "operation_id": self.operation_id,
            "merchant_origin": self.merchant_origin,
            "target_origin": ", ".join(self.target_origins),
            "target_origins": list(self.target_origins),
            "fields": sorted(c.token for c in self.controls),
            "checks": [
                "HTTPS origins",
                "exact document and field references",
                f"card fields split across {n} documents, each field once" if n > 1 else "single card document",
            ],
            "merchant_reputation": "not established; verify the seller independently",
        }


def _card_documents(candidates: list, merchant: str, unreviewed: frozenset = frozenset()) -> tuple:
    """The documents to fill: one, or a split checkout where every card field appears exactly once
    across documents and the fields reach at most the merchant plus ONE hosted PSP origin. Anything
    else is ambiguous and refused. ``candidates`` holds ``(frame_id, session, object_id, origin,
    controls)`` for each allowed document that has card controls. ``unreviewed``: hosted origins the
    discovery admitted; one of them is bounded by ``_MAX_UNREVIEWED_DOCUMENTS``."""
    hosted = {c[3] for c in candidates} - {merchant}
    if len(hosted) > 1:
        raise PaymentPolicyError("Card fields split across payment providers")
    origin = next(iter(hosted)) if hosted else None
    provider = provider_for_origin(origin) if origin else None
    limit = (_MAX_UNREVIEWED_DOCUMENTS if origin in unreviewed
             else provider.max_documents if provider else _MAX_CARD_DOCUMENTS)
    if not candidates or len(candidates) > limit:
        raise PaymentPolicyError("Missing, ambiguous or split payment documents")
    tokens = [control.token for c in candidates for control in c[4]]
    if len(tokens) != len(set(tokens)) or not {"cc-number", "cc-csc"} <= set(tokens):
        raise PaymentPolicyError("Incomplete or ambiguous card fields")
    if not ("cc-exp" in tokens or {"cc-exp-month", "cc-exp-year"} <= set(tokens)):
        raise PaymentPolicyError("Missing card expiry fields")
    if "cc-exp" in tokens and ("cc-exp-month" in tokens or "cc-exp-year" in tokens):
        raise PaymentPolicyError("Ambiguous card expiry fields")
    return tuple(PaymentDocument(*c) for c in candidates)


def _explained_card_documents(candidates: list, merchant: str, tree: tuple, allowed: tuple) -> tuple:
    """``_card_documents``, with a coverage refusal naming the provider-level cause it can see in the
    frame tree (an unsupported PSP, a reviewed one not enabled, a non-card-field page)."""
    try:
        return _card_documents(candidates, merchant)
    except PaymentPolicyError as exc:
        hint = coverage_hint(tree, merchant, allowed) if str(exc) in _COVERAGE_REFUSALS else ""
        if hint:
            explained = PaymentPolicyError(f"{exc} ({hint})")
            # dlz (Codex B r1 #8): the same cause without page-controlled paths, for typed results.
            explained.coverage = f"{exc} ({coverage_hint(tree, merchant, allowed, paths=False)})"
            raise explained from None
        raise


def _frame_admitted(record: tuple, by_id: dict, merchant: str, allowed: tuple) -> bool:
    """Whether a frame ``(id, loader, url, origin, parent)`` may hold card fields: the merchant or a
    reviewed provider origin, on that provider's card-field path, under an ancestry of the same. False
    = not a candidate (ads, a provider's wallet/login page); raises on a reviewed frame placed under an
    unapproved ancestor."""
    def on_card_path(r: tuple) -> bool:
        provider = provider_for_origin(r[3]) if r[3] != merchant else None
        return provider is None or provider.admits(r[2])

    def may_contain(r: tuple) -> bool:
        provider = provider_for_origin(r[3]) if r[3] != merchant else None
        return provider is None or provider.admits_ancestor(r[2])

    # Ignore unrelated ads, but never inspect/inject into an arbitrary PSP.
    if record[3] not in (merchant, *allowed) or not on_card_path(record):
        return False
    https_origin(record[3])
    ancestor = record[4]
    while ancestor:
        above = by_id.get(ancestor)
        if above is None or above[3] not in (merchant, *allowed) or not may_contain(above):
            raise PaymentPolicyError("Unapproved payment frame ancestry")  # e.g. inside a wallet page
        ancestor = above[4]
    return True


def _unreviewed_admitted(record: tuple, by_id: dict, merchant: str, allowed: tuple) -> bool:
    """dlz (v1 §3.2, v2 B1): whether a frame ``(id, loader, url, origin, parent)`` on an origin outside the reviewed
    set is a STRUCTURAL candidate for the discovery inspection. Never raises: an ad frame never breaks a checkout.
    - not the top document, not the merchant, not an allowed origin;
    - no registry row (enabled or not: a reviewed provider that is off stays refused, D5) and not a reviewed
      unsupported product;
    - a canonical HTTPS origin, with a document URL on it (no about:blank, srcdoc, data: or blob:);
    - every ancestor up to the top is the merchant or this same origin (on an https URL of it), at most
      ``_MAX_UNREVIEWED_DEPTH`` levels below the top: a third origin anywhere in the chain (a wrapper) is refused."""
    _fid, _loader, url, origin, parent = record
    if not parent or origin == merchant or origin in allowed:
        return False
    if provider_for_origin(origin) is not None or origin in UNSUPPORTED_PROVIDERS:
        return False
    try:
        https_origin(origin)
    except PaymentPolicyError:
        return False
    if not isinstance(url, str) or not url.startswith(origin + "/"):
        return False
    depth, ancestor = 1, parent
    while True:
        above = by_id.get(ancestor)
        if above is None:
            return False
        if not above[4]:
            return above[3] == merchant
        if above[3] == origin:
            if not str(above[2]).startswith(origin + "/"):
                return False
        elif above[3] != merchant:
            return False
        depth += 1
        if depth > _MAX_UNREVIEWED_DEPTH:
            return False
        ancestor = above[4]


def _receiving(candidates: list, merchant: str, allowed: tuple) -> list:
    """Each origin holding card controls, in document order: ``reviewed`` is "merchant", True or False; field tokens
    only (never a label, name, option or URL)."""
    out: dict = {}
    for c in candidates:
        origin = c[3]
        entry = out.setdefault(origin, {"origin": origin, "reviewed": "merchant" if origin == merchant
                                        else origin in allowed, "fields": set()})
        entry["fields"].update(control.token for control in c[4])
    return [dict(e, fields=sorted(e["fields"])) for e in out.values()]


def _discovery_documents(candidates: list, merchant: str, allowed: tuple, inspected_frames: int) -> tuple:
    """``_card_documents`` for the discovery inspection, with its refusals typed: nothing complete is
    ``payment_fields_unsupported`` (a count only: ad origins are never implied as destinations); duplicates, two
    hosted origins, too many documents or mixed expiry shapes are ``payment_fields_ambiguous`` (origins and tokens).
    The exact-once rules are ``_card_documents``' own, not weakened."""
    unreviewed = frozenset(c[3] for c in candidates if c[3] != merchant and c[3] not in allowed)
    try:
        return _card_documents(candidates, merchant, unreviewed)
    except PaymentPolicyError as exc:
        tokens = [control.token for c in candidates for control in c[4]]
        incomplete = str(exc) in ("Incomplete or ambiguous card fields", "Missing card expiry fields")
        if not candidates or (incomplete and len(tokens) == len(set(tokens))):
            raise PaymentDestinationError("payment_fields_unsupported", FIELDS_UNSUPPORTED,
                                          {"unreviewed_frames_inspected": inspected_frames}) from None
        raise PaymentDestinationError("payment_fields_ambiguous", FIELDS_AMBIGUOUS,
                                      {"receiving_origins": _receiving(candidates, merchant, allowed)}) from None


def _bounded_description(raw: Any) -> list:
    """dlz (B3): the discovery closure's description, or ``payment_page_too_large`` when it overflowed its element
    or shadow-depth limit, exceeds the payload limit or has more than ``inputs`` rows. dlz (Codex B r1 #5): every
    row must be a complete descriptor of the exact shape (``_valid_descriptor``) with strictly increasing indexes;
    anything else, and the closure's ``unknown`` marker (a field whose exposure it cannot judge), is
    ``payment_page_unverifiable``: a malformed reading never becomes a card-free document."""
    if isinstance(raw, dict) and raw == {"overflow": True}:
        raise _page_too_large()
    if not isinstance(raw, list):
        raise _unverifiable()
    if len(raw) > _DISCOVERY_LIMITS["inputs"] or len(json.dumps(raw)) > _MAX_DESCRIBE_BYTES:
        raise _page_too_large()
    previous = -1
    for row in raw:
        if not _valid_descriptor(row, previous):
            raise _unverifiable()
        previous = row["index"]
    return raw


def _valid_descriptor(row: Any, previous: int) -> bool:
    """One ``describeOne`` row: exactly its keys (``options`` only on a select), exact types, the text limit, and an
    index above ``previous`` and inside the element limit."""
    if not isinstance(row, dict) or not isinstance(row.get("type"), str) or len(row["type"]) > 32:
        return False
    select = row["type"] in _SELECT_TYPES
    if set(row) != (_DESCRIPTOR_KEYS | {"options"} if select else _DESCRIPTOR_KEYS):
        return False
    index, form, length = row["index"], row["formIndex"], row["maxLength"]
    text = _DISCOVERY_LIMITS["text"]
    return (type(index) is int and previous < index < _DISCOVERY_LIMITS["inputs"]
            and all(type(row[k]) is str and len(row[k]) <= text for k in ("autocomplete", "name", "label"))
            and (form is None or (type(form) is int and form >= -1))
            and (length is None or (type(length) is int and length > 0))
            and (not select or _valid_options(row["options"])))


def _valid_options(options: Any) -> bool:
    return isinstance(options, list) and len(options) <= 400 and all(
        isinstance(o, list) and len(o) == 3 and type(o[0]) is str and type(o[1]) is str and len(o[1]) <= 64
        and type(o[2]) is bool for o in options)


def _take_probe(owner_nodes: dict, key: tuple) -> bool:
    """dlz (Codex B r1 #9): whether ``(target, session)`` may be asked (False: it already answered that it does not
    own the target); a new key spends one probe of ``_MAX_OWNER_PROBES`` and is held at None until it answers."""
    if key in owner_nodes:
        return owner_nodes[key] is not None
    if len(owner_nodes) >= _MAX_OWNER_PROBES:
        raise _page_too_large()
    owner_nodes[key] = None
    return True


def _card_controls(raw: list) -> tuple:
    return tuple(c for r in raw if (c := classify_checkout_control(LoginControl.from_dict(r))) and c.token.startswith("cc-"))


def _relevant_frames(i: PaymentInspection) -> set:
    """The top document plus every card document and its whole ancestor chain (B4)."""
    by_id = {f[0]: f for f in i.tree}
    out = {i.tree[0][0]} if i.tree else set()
    for d in i.documents:
        fid, hops = d.frame_id, 0
        while fid in by_id and hops <= _MAX_FRAME_DEPTH:
            out.add(fid)
            fid, hops = by_id[fid][4], hops + 1
        out.add(fid)  # a chain that does not reach a known top stays relevant, and then fails the comparison
    return out


def _fills_by_document(i: PaymentInspection, fills: list) -> list:
    """Group fills by their ``document`` index (default 0); every card document needs its fields."""
    groups: list = [[] for _ in i.documents]
    for f in fills:
        k = f.get("document", 0) if isinstance(f, dict) else None
        if type(k) is not int or not 0 <= k < len(groups):
            raise PaymentPolicyError("Fill targets an unknown document")
        groups[k].append({key: v for key, v in f.items() if key != "document"})
    # A document may go unwritten only when everything in it is optional (a holder-only frame and a
    # saved card without a holder name); a document with a required card field always needs its fill.
    if not any(groups) or any(not g and any(c.token not in OPTIONAL_CARD_TOKENS for c in d.controls)
                              for d, g in zip(i.documents, groups)):
        raise PaymentPolicyError("Every card document needs its fields")
    return groups


def _value(reply: dict) -> Any:
    payload = reply.get("result", {})
    if payload.get("exceptionDetails"):
        raise PaymentPolicyError("Payment document unavailable")
    return payload.get("result", {}).get("value")


class PaymentSupervisionMixin:
    def inspect_payment(
        self,
        merchant_origin: str = "",
        *,
        bound_origins: tuple = (),
        frame_origins: tuple = PAYMENT_PSP_ORIGINS,
        unreviewed: bool = False,
        cart: tuple | None = None,
    ) -> PaymentInspection:
        """``unreviewed``: the discovery inspection (v1 §3, v2 B1-B5) that also admits unreviewed hosted frames by
        structure. ``cart``: ``(cents, currency)`` of the stated total, for the page check (B5)."""
        from tools.browser_supervisor import _schedule

        return _schedule(
            self._inspect_payment(merchant_origin, bound_origins, frame_origins, unreviewed, cart),
            self._loop,
            timeout=25,
        )

    async def _payment_tree(
        self, sid: str, child_sessions: dict | None = None
    ) -> tuple:
        child_sessions = child_sessions if child_sessions is not None else {}
        response = await self._cdp("Page.getFrameTree", session_id=sid)
        frames = []
        depth: dict = {}
        expanded = 0

        def walk(node, parent=""):
            frame = node["frame"]
            # dlz (B3): bounded while walking, not after: frames in the whole tree and depth below the top.
            level = depth.get(parent, 0) + 1 if parent else 0
            if level > _MAX_FRAME_DEPTH or len(frames) >= _MAX_FRAMES:
                raise _page_too_large()
            depth[frame["id"]] = level
            frames.append((
                frame["id"],
                frame.get("loaderId", ""),
                frame.get("url", ""),
                frame.get("securityOrigin", ""),
                parent,
            ))
            for child in node.get("childFrames", []):
                walk(child, frame["id"])

        walk(response["result"]["frameTree"])
        # Page.getFrameTree is renderer-scoped: OOPIF documents must be
        # read from their own sessions, then anchored with DOM.getFrameOwner.
        targets = (await self._cdp("Target.getTargets"))["result"]["targetInfos"]
        pending = [t for t in targets if t.get("type") == "iframe"]
        if len(pending) > _MAX_IFRAME_TARGETS:
            raise _page_too_large()
        # dlz (Codex B r1 #9): one DOM.getFrameOwner per (target, session), at most _MAX_OWNER_PROBES per walk; a
        # session that does not own the target is not asked again for its other frames or in a later round.
        owner_nodes: dict = {}
        while pending:
            progress = False
            for target in list(pending):
                fid = target["targetId"]
                parent = None
                # Discover ownership from the current DOM, including frames that
                # existed before this supervisor attached. Event history is not proof.
                for record in frames:
                    parent_sid = child_sessions.get(record[0], sid)
                    key = (fid, parent_sid)
                    if not _take_probe(owner_nodes, key):
                        continue
                    obj = None
                    try:
                        if owner_nodes[key] is None:
                            owner_nodes[key] = (await self._cdp(
                                "DOM.getFrameOwner", {"frameId": fid}, session_id=parent_sid))["result"]
                        owner = owner_nodes[key]
                        context = (
                            await self._cdp(
                                "Page.createIsolatedWorld",
                                {
                                    "frameId": record[0],
                                    "worldName": "hermes-payment-ancestry",
                                },
                                session_id=parent_sid,
                            )
                        )["result"]["executionContextId"]
                        obj = (
                            await self._cdp(
                                "DOM.resolveNode",
                                {
                                    "backendNodeId": owner["backendNodeId"],
                                    "executionContextId": context,
                                },
                                session_id=parent_sid,
                            )
                        )["result"]["object"]["objectId"]
                        belongs = _value(
                            await self._cdp(
                                "Runtime.callFunctionOn",
                                {
                                    "objectId": obj,
                                    "functionDeclaration": "function(){return this.ownerDocument===document && this.isConnected}",
                                    "returnByValue": True,
                                },
                                session_id=parent_sid,
                            )
                        )
                        if belongs is True:
                            parent = record[0]
                            break
                    except Exception:
                        continue
                    finally:
                        if obj:
                            await self._release_payment_object(parent_sid, obj)
                if not parent:
                    continue
                expanded += 1
                if expanded > _MAX_OOPIF_TARGETS:
                    raise _page_too_large()
                parent_sid = child_sessions.get(parent, sid)
                fsid = child_sessions.get(fid)
                if not fsid:
                    fsid = (
                        await self._cdp(
                            "Target.attachToTarget", {"targetId": fid, "flatten": True}
                        )
                    )["result"]["sessionId"]
                    child_sessions[fid] = fsid
                    await self._enable_page_domains(fsid, timeout=5)
                child = (await self._cdp("Page.getFrameTree", session_id=fsid))[
                    "result"
                ]["frameTree"]
                # Replace the parent's placeholder with the child's authoritative loader/origin.
                frames[:] = [f for f in frames if f[0] != fid]
                known = {f[0] for f in frames}
                walk(child, parent)
                # Same-origin frames inside this OOPIF (PayPal's guest card form inside /smart/buttons)
                # are in its process: they are reached through its session, never the page's.
                for f in frames:
                    if f[0] not in known:
                        child_sessions.setdefault(f[0], fsid)
                pending.remove(target)
                progress = True
            if not progress:
                break
        return (frames[0], *sorted(frames[1:]))

    async def _inspect_payment(
        self, origin: str, bound: tuple, allowed_frames: tuple, unreviewed: bool = False,
        cart: tuple | None = None,
    ) -> PaymentInspection:
        if origin:
            https_origin(origin)
        with self._state_lock:
            sid = self._page_session_id if self._active else None
        if not sid:
            raise PaymentPolicyError("Supervised browser required")
        if origin:
            sid = await self._merchant_tab_session(origin, sid)
        page = (await self._cdp("Target.getTargetInfo", session_id=sid))["result"][
            "targetInfo"
        ]
        child_sessions = {}
        operation = secrets.token_hex(16)
        # dlz: what this inspection pins; released on any refusal below.
        # dlz (Codex B r3 #4): "pending" holds each closure from the moment it exists until it is classified.
        found = {"candidates": [], "anchors": {}, "absence": [], "witnesses": [], "owners": {}, "pending": []}
        try:
            tree = await self._payment_tree(sid, child_sessions)
            merchant = https_origin(tree[0][3])
            if (origin and merchant != origin) or (bound and merchant not in bound):
                raise PaymentPolicyError("Merchant origin mismatch")
            on_provider = provider_for_origin(merchant)
            if on_provider is not None and not on_provider.admits(tree[0][2]):
                # The tab itself is a provider's non-card page (PayPal wallet or login), not a checkout.
                raise PaymentPolicyError("Merchant origin mismatch")
            unreviewed_frames = await self._inspect_frames(tree, merchant, allowed_frames, unreviewed, sid,
                                                           child_sessions, operation, found)
            candidates, absence = found["candidates"], found["absence"]
            if unreviewed:
                documents = _discovery_documents(candidates, merchant, allowed_frames, unreviewed_frames)
                # dlz (B4): card-free documents on the merchant or a receiving origin stay pinned as absence
                # witnesses; those on other origins (ads, widgets) are released: they may churn freely.
                receiving = {merchant, *(d.origin for d in documents)}
                for fsid, obj, _origin, _fid in [a for a in absence if a[2] not in receiving]:
                    await self._release_payment_object(fsid, obj)
                absence[:] = [a for a in absence if a[2] in receiving]
                if len(absence) > _MAX_ABSENCE_WITNESSES:
                    raise _page_too_large()
            else:
                documents = _explained_card_documents(candidates, merchant, tree, allowed_frames)
            for d in documents:
                d.clock_anchor = found["anchors"].get((d.frame_session, d.object_id))
            if await self._payment_tree(sid, child_sessions) != tree:
                raise PaymentPolicyError("Payment page changed during inspection")
            found["owners"] = await self._pin_frame_owners(documents, tree, sid, child_sessions, found["owners"])
            hosted = tuple(dict.fromkeys(d.origin for d in documents if d.origin != merchant))
            inspection = PaymentInspection(
                operation,
                sid,
                page["targetId"],
                merchant,
                documents,
                tree,
                time.monotonic() + 120,
                owner=self,
                child_sessions=child_sessions,
                frame_owners=found["owners"],
                unreviewed_origins=tuple(o for o in hosted if o not in allowed_frames) if unreviewed else (),
                allowed_frames=tuple(allowed_frames),
                absence=tuple((fsid, obj) for fsid, obj, _origin, _fid in absence),
                absence_frames=tuple(fid for _fsid, _obj, _origin, fid in absence),
            )
            if inspection.unreviewed_origins and \
                    await self._shown_documents(inspection) != {d.frame_id for d in documents}:
                # v1 §3.2: a card form the user cannot see, by the strict test over the whole owner chain.
                raise PaymentDestinationError("payment_fields_ambiguous", FIELDS_AMBIGUOUS,
                                              {"receiving_origins": _receiving(candidates, merchant, allowed_frames)})
            if cart is not None:
                await self._bind_amount_witnesses(inspection, cart, found["witnesses"])
            return inspection
        except BaseException:  # health: allow BLE001 -- released, then re-raised unchanged (cancellation too)
            # dlz (Codex B r3 #4): one release pass of everything pinned so far, as its own task: a cancellation
            # (the caller's timeout) cannot cut it short, and is re-raised once the pass is under way.
            cleanup = asyncio.ensure_future(self._release_found(found, child_sessions))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                if not cleanup.done():
                    raise  # dlz (Codex B r4 #4): the caller cancelled; the pass finishes behind the shield
                # The pass itself was cancelled (after attempting every release): the original exception stands.
            raise

    async def _release_found(self, found: dict, child_sessions: dict) -> None:
        """dlz (Codex B r4 #4): every release is attempted, a cancelled one included; a cancellation of this pass is
        re-raised only after the last."""
        objects = ([(fsid, obj) for _, fsid, obj, _, _ in found["candidates"]] + found["pending"]
                   + [a[:2] for a in found["absence"]] + found["witnesses"]
                   + [(psid, obj) for psid, items in found["owners"].values() for _fid, obj in items])
        await self._release_all(objects, child_sessions)

    async def _release_all(self, objects: list, sessions: dict) -> None:
        """Release each ``(session, object)`` and detach each session once, every one attempted even when an earlier
        one is cancelled; a cancellation is re-raised only after the last (Codex B r4 #4, r5 #3)."""
        cancelled = False
        for sid, obj in objects:
            try:
                await self._release_payment_object(sid, obj)
            except asyncio.CancelledError:
                cancelled = True
        for sid in dict.fromkeys(sessions.values()):
            try:
                await self._release_payment_sessions({"s": sid})
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _merchant_tab_session(self, origin: str, sid: str) -> str:
        """The page session on the one tab at ``origin``, attaching to it (and making it the supervised page) when
        the supervisor is on another tab."""
        from agent.vault_store import normalize_origin

        targets = (await self._cdp("Target.getTargets"))["result"]["targetInfos"]
        pages = [
            t
            for t in targets
            if t["type"] == "page"
            and t.get("url", "").startswith("https://")
            and normalize_origin(t["url"]) == origin
        ]
        if len(pages) != 1:
            raise PaymentPolicyError("Merchant tab missing or ambiguous")
        current = (await self._cdp("Target.getTargetInfo", session_id=sid))[
            "result"
        ]["targetInfo"]
        if current["targetId"] == pages[0]["targetId"]:
            return sid
        old_sid = sid
        sid = (
            await self._cdp(
                "Target.attachToTarget",
                {"targetId": pages[0]["targetId"], "flatten": True},
            )
        )["result"]["sessionId"]
        try:
            await self._enable_page_domains(sid, timeout=5)
            await self._install_dialog_bridge(sid)
        except Exception:
            await self._release_payment_sessions({"new": sid})
            raise
        with self._state_lock:
            self._page_session_id = sid
        await self._release_payment_sessions({"old": old_sid})
        return sid

    async def _frame_inspectable(self, record: tuple, by_id: dict, merchant: str, allowed_frames: tuple,
                                 unreviewed: bool, child_sessions: dict, sid: str, exposed: dict) -> str:
        """Whether a frame gets the inspect closure: "reviewed" (admitted by the reviewed rules), "unreviewed"
        (discovery: an exposed unreviewed hosted frame) or "" (skipped)."""
        fid, parent = record[0], record[4]
        if not _frame_admitted(record, by_id, merchant, allowed_frames):
            if not (unreviewed and _unreviewed_admitted(record, by_id, merchant, allowed_frames)):
                return ""
            # dlz (B2): only an exposed frame counts against the bound, so hidden copies cannot exhaust it.
            return "unreviewed" if await self._chain_exposed(fid, by_id, child_sessions, sid, exposed) else ""
        if unreviewed:
            # dlz (B2): discovery judges every owner <iframe> up to the page with the strict exposure test
            # before the frame can hold a candidate: a clipped, covered or transparent copy is no candidate.
            if parent and not await self._chain_exposed(fid, by_id, child_sessions, sid, exposed):
                return ""
        # A PSP field frame the page keeps hidden (Shopify's Maestro-only issue date and
        # number) looks visible from inside its own document; judge its iframe element.
        elif parent and not await self._frame_rendered(fid, parent, child_sessions.get(parent, sid)):
            return ""
        return "reviewed"

    async def _inspect_frames(self, tree: tuple, merchant: str, allowed_frames: tuple, unreviewed: bool, sid: str,
                              child_sessions: dict, operation: str, found: dict) -> int:
        """Pin the inspect closure in every inspectable frame: ``found["candidates"]`` gets each document with card
        controls, ``found["absence"]`` (discovery) each card-free one. Returns the unreviewed frames inspected."""
        by_id = {f[0]: f for f in tree}
        exposed: dict = {}
        unreviewed_frames = 0
        script = _INSPECT_DISCOVERY if unreviewed else _INSPECT
        for record in tree:
            fid, frame_origin = record[0], record[3]
            kind = await self._frame_inspectable(record, by_id, merchant, allowed_frames, unreviewed,
                                                 child_sessions, sid, exposed)
            if not kind:
                continue
            if kind == "unreviewed":
                unreviewed_frames += 1
                if unreviewed_frames > _MAX_UNREVIEWED_FRAMES:
                    raise PaymentDestinationError("payment_fields_ambiguous", FIELDS_AMBIGUOUS,
                                                  {"unreviewed_frames": f"more than {_MAX_UNREVIEWED_FRAMES}"})
            fsid = child_sessions.get(fid, sid)
            context = (
                await self._cdp(
                    "Page.createIsolatedWorld",
                    {"frameId": fid, "worldName": "hermes-payment-" + operation},
                    session_id=fsid,
                )
            )["result"]["executionContextId"]
            reply = await self._cdp(
                "Runtime.evaluate",
                {
                    "expression": script,
                    "contextId": context,
                    "returnByValue": False,
                },
                session_id=fsid,
            )
            anchor = time.monotonic()  # after the closure read its clock: see _renderer_deadline_ms
            obj = reply.get("result", {}).get("result", {}).get("objectId")
            if not obj:
                raise PaymentPolicyError("Payment document unavailable")
            # dlz (Codex B r2 #7): object ids are session-scoped; two frame sessions can hand out the same one.
            found["anchors"][(fsid, obj)] = anchor
            # dlz (Codex B r2 #6, r3 #4): the outer cleanup releases it from here on, on any exception or cancellation.
            found["pending"].append((fsid, obj))
            raw = _value(
                await self._cdp(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": obj,
                        "functionDeclaration": "function(){return this.describe()}",
                        "returnByValue": True,
                    },
                    session_id=fsid,
                )
            )
            controls = _card_controls(_bounded_description(raw) if unreviewed else raw)
            if controls or unreviewed:
                found["pending"].remove((fsid, obj))  # no await between: it is always in exactly one list
                if controls:
                    found["candidates"].append((fid, fsid, obj, frame_origin, controls))
                else:
                    # dlz (B4): a card-free document; it may become an absence witness.
                    found["absence"].append((fsid, obj, frame_origin, fid))
            else:
                await self._release_payment_object(fsid, obj)
                found["pending"].remove((fsid, obj))
        return unreviewed_frames

    async def _bind_amount_witnesses(self, inspection: PaymentInspection, cart: tuple, witnesses: list) -> None:
        """B5: pin an amount witness in the top document and each unreviewed receiving document (a transcription
        check only) and record the page-check result shown to the owner."""
        places = [("merchant page", inspection.tree[0][0], inspection.page_session)] + [
            ("payment form", d.frame_id, d.frame_session) for d in inspection.documents
            if d.origin in inspection.unreviewed_origins]
        pins = []
        for place, wfid, wsid in places:
            pins.append((place, await self._amount_witness(wfid, wsid, inspection.operation_id, cart, witnesses)))
        inspection.witnesses = tuple(witnesses)
        inspection.page_match = _page_match(pins, cart[0])

    async def _pin_frame_owners(self, documents: tuple, tree: tuple, page_sid: str, child_sessions: dict,
                                groups: dict | None = None) -> dict:
        """dlz: pin the owner <iframe> of every frame between a card document and the page, grouped by parent
        document (one isolated world each), into ``groups`` as each is pinned (the caller's cleanup sees them on a
        cancellation: Codex B r3 #4). Unresolvable: the inspection is refused."""
        by_id = {f[0]: f for f in tree}
        frames: list = []
        for d in documents:
            fid = d.frame_id
            while fid in by_id and by_id[fid][4] and fid not in frames:
                frames.append(fid)
                fid = by_id[fid][4]
        groups = {} if groups is None else groups
        contexts: dict = {}
        try:
            for fid in frames:
                parent = by_id[fid][4]
                psid = child_sessions.get(parent, page_sid)
                if parent not in contexts:
                    contexts[parent] = (await self._cdp("Page.createIsolatedWorld", {
                        "frameId": parent, "worldName": "hermes-payment-owners"}, session_id=psid))["result"]["executionContextId"]
                owner = (await self._cdp("DOM.getFrameOwner", {"frameId": fid}, session_id=psid))["result"]
                obj = (await self._cdp("DOM.resolveNode", {"backendNodeId": owner["backendNodeId"],
                                                           "executionContextId": contexts[parent]},
                                       session_id=psid))["result"]["object"]["objectId"]
                groups.setdefault(parent, (psid, []))[1].append((fid, obj))
        except Exception:  # health: allow BLE001 -- no pinned owner, no verifiable card frame; fail closed
            await self._release_frame_owners(groups)
            groups.clear()
            raise PaymentPolicyError("Payment document unavailable") from None
        return groups

    async def _release_frame_owners(self, groups: dict) -> None:
        for psid, items in groups.values():
            for _fid, obj in items:
                await self._release_payment_object(psid, obj)

    async def _shown_documents(self, i: PaymentInspection) -> set:
        """dlz: frame ids of the card documents the user still sees: every pinned owner <iframe> from the
        document up to the page is connected, laid out, visible and not transparent (ancestors included).
        One call per parent document; any failure counts as not shown."""
        shown: dict = {}
        # dlz (B2): an unreviewed inspection judges its owners with the strict exposure test.
        check = _OWNERS_EXPOSED if i.unreviewed_origins else _OWNERS_SHOWN
        for psid, items in i.frame_owners.values():
            try:
                answer = _value(await self._cdp("Runtime.callFunctionOn", {
                    "objectId": items[0][1], "functionDeclaration": check,
                    "arguments": [{"objectId": obj} for _fid, obj in items], "returnByValue": True,
                }, session_id=psid))
            except Exception:  # health: allow BLE001 -- an unreadable owner is not a frame the user sees
                answer = None
            ok = isinstance(answer, list) and len(answer) == len(items)
            for k, (fid, _obj) in enumerate(items):
                shown[fid] = ok and answer[k] is True
        by_id = {f[0]: f for f in i.tree}
        out = set()
        for d in i.documents:
            fid, chain_ok = d.frame_id, d.frame_id in by_id
            while chain_ok and by_id[fid][4]:
                chain_ok = shown.get(fid) is True and by_id[fid][4] in by_id
                fid = by_id[fid][4]
            if chain_ok:
                out.add(d.frame_id)
        return out

    async def _frame_rendered(self, fid: str, parent: str, parent_sid: str) -> bool:
        """Whether the iframe element hosting ``fid`` is laid out and visible in its parent document."""
        return await self._owner_check(fid, parent, parent_sid, _OWNER_RENDERED)

    async def _chain_exposed(self, fid: str, by_id: dict, child_sessions: dict, page_sid: str, cache: dict) -> bool:
        """dlz (B2): every owner <iframe> from ``fid`` up to the page passes ``_EXPOSED`` in its parent document
        (``cache`` keeps each owner's answer for one pass). A chain that does not reach the top is not exposed; an
        owner whose exposure cannot be read or judged raises ``payment_page_unverifiable`` (Codex B r1 #4)."""
        hops = 0
        while fid in by_id and by_id[fid][4]:
            parent = by_id[fid][4]
            hops += 1
            if parent not in by_id or hops > _MAX_FRAME_DEPTH:
                return False
            if fid not in cache:
                cache[fid] = await self._owner_check(fid, parent, child_sessions.get(parent, page_sid),
                                                     "function(){return (" + _EXPOSED + ")(this)}", strict=True)
            if cache[fid] is not True:
                return False
            fid = parent
        return fid in by_id

    async def _amount_witness(self, fid: str, sid: str, operation: str, cart: tuple, pinned: list) -> dict:
        """dlz (B5): pin an ``_AMOUNT_WITNESS`` in this document's isolated world (added to ``pinned`` as soon as it
        exists, for the inspection's cleanup: Codex B r3 #4) and return its pin. Only numbers and a state word leave
        the world; the inputs are digits and three capitals, validated before they get here."""
        cents, currency = cart
        if type(cents) is not int or not 0 <= cents < 10**9 or not (isinstance(currency, str) and len(currency) == 3
                                                                   and currency.isascii() and currency.isupper()):
            raise PaymentPolicyError("Invalid stated cart")
        context = (await self._cdp("Page.createIsolatedWorld", {"frameId": fid, "worldName": "hermes-payment-" + operation},
                                   session_id=sid))["result"]["executionContextId"]
        reply = await self._cdp("Runtime.evaluate", {
            "expression": "(" + _AMOUNT_WITNESS + ")(" + str(cents) + "," + json.dumps(currency) + ")",
            "contextId": context, "returnByValue": False}, session_id=sid)
        obj = reply.get("result", {}).get("result", {}).get("objectId")
        if not obj:
            raise PaymentPolicyError("Payment document unavailable")
        pinned.append((sid, obj))
        pin = _value(await self._cdp("Runtime.callFunctionOn", {
            "objectId": obj, "functionDeclaration": "function(){return this.pin()}", "returnByValue": True},
            session_id=sid))
        if not _valid_pin(pin):
            raise PaymentPolicyError("Payment document unavailable")
        return pin

    async def _owner_check(self, fid: str, parent: str, parent_sid: str, function: str, strict: bool = False) -> bool:
        """Evaluate ``function`` on the iframe element hosting ``fid``, in an isolated world of its parent; only
        a literal true counts. dlz: ``strict`` (discovery): only a literal false is a negative; no owner, a failed
        call or any other answer raises ``payment_page_unverifiable`` instead of reading as hidden."""
        obj = None
        try:
            owner = (await self._cdp("DOM.getFrameOwner", {"frameId": fid}, session_id=parent_sid))["result"]
            context = (await self._cdp(
                "Page.createIsolatedWorld", {"frameId": parent, "worldName": "hermes-payment-ancestry"},
                session_id=parent_sid))["result"]["executionContextId"]
            obj = (await self._cdp(
                "DOM.resolveNode", {"backendNodeId": owner["backendNodeId"], "executionContextId": context},
                session_id=parent_sid))["result"]["object"]["objectId"]
            answer = _value(await self._cdp("Runtime.callFunctionOn", {
                "objectId": obj,
                "functionDeclaration": function,
                "returnByValue": True,
            }, session_id=parent_sid))
        except Exception as exc:  # health: allow BLE001 -- strict: any failure is unknown; cancellation propagates
            if strict:
                # dlz (Codex B r2 #5): a CDP error, transport failure or timeout is as unknown as a malformed answer.
                raise _unverifiable() from None
            if not isinstance(exc, (KeyError, TypeError, PaymentPolicyError)):
                raise
            # No resolvable owner element: not a frame the user can see or type into.
            return False
        finally:
            if obj:
                await self._release_payment_object(parent_sid, obj)
        if strict and type(answer) is not bool:
            raise _unverifiable()
        return answer is True

    async def _release_payment_sessions(self, sessions: dict) -> None:
        # In-process child frames share their OOPIF's session: detach each session once.
        for sid in dict.fromkeys(sessions.values()):
            try:
                await self._cdp("Target.detachFromTarget", {"sessionId": sid})
            except Exception:
                # A frame gone from the browser has no live session to detach.
                continue

    async def _release_payment_object(self, sid: str, obj: str) -> None:
        try:
            await self._cdp("Runtime.releaseObject", {"objectId": obj}, session_id=sid)
        except Exception:
            # Navigation/disconnect already destroys the isolated world's object.
            return

    def payment_is_current(self, inspected: PaymentInspection) -> bool:
        from tools.browser_supervisor import _schedule

        return _schedule(self._payment_is_current(inspected), self._loop, timeout=15)

    async def _payment_is_current(self, i: PaymentInspection, during_fill: bool = False) -> bool:
        """The approved page is still the inspected one. dlz (Codex B r1 #1): ``during_fill`` (an unreviewed fill,
        between documents and after each focus) runs the same topology, absence and exposure bindings on the
        consumed inspection. dlz (Codex B r2 #1): and every card document's own ``valid()``, those already written
        included: page code run by a write or a focus can reveal or inject a card field in a document whose commit
        has finished, and no later check of that document would see it."""
        with self._state_lock:
            active = self._active and self._page_session_id == i.page_session
        if (
            not active
            or i.owner is not self
            or (i.consumed and not during_fill)
            or time.monotonic() >= i.expires_at
        ):
            return False
        try:
            current = await self._payment_tree(i.page_session, i.child_sessions)
            if i.unreviewed_origins:
                # dlz (B4): the relevant chains, no new admissible frame; frames on other origins may churn.
                if not await self._relevant_unchanged(i, current):
                    return False
            elif current != i.tree:
                return False
            for d in i.documents:
                valid = _value(
                    await self._cdp(
                        "Runtime.callFunctionOn",
                        {
                            "objectId": d.object_id,
                            "functionDeclaration": "function(){return this.valid()}",
                            "returnByValue": True,
                        },
                        session_id=d.frame_session,
                    )
                )
                if valid is not True:
                    return False
            if i.unreviewed_origins:
                if not await self._absence_holds(i):
                    return False
                if await self._shown_documents(i) != {d.frame_id for d in i.documents}:
                    return False
            return True
        except Exception:  # health: allow BLE001 -- any CDP failure means the approved page is not current; fail closed
            return False

    async def _relevant_unchanged(self, i: PaymentInspection, current: tuple) -> bool:
        """dlz (B4): (1) the top frame and every card document's whole chain have their inspected records; (2) no
        frame on the merchant or an approved origin is admissible and exposed unless it is bound by a pinned object
        (a card chain or an absence witness, unchanged): a new or changed frame, and (Codex B r1 #2) one that was
        present but hidden at inspection and is revealed now, withdraw the approval; (3, in ``_absence_holds``)
        every pinned card-free document stays card-free. Frames on other origins (ads, widgets) may appear,
        navigate or disappear."""
        if not current or not i.tree or current[0] != i.tree[0]:
            return False
        old = {f[0]: f for f in i.tree}
        now = {f[0]: f for f in current}
        if any(now.get(fid) != old.get(fid) for fid in _relevant_frames(i)):
            return False
        approved = {i.merchant_origin, *i.target_origins}
        bound = _relevant_frames(i) | set(i.absence_frames)
        cache: dict = {}
        for record in current[1:]:
            if (record[0] in bound and old.get(record[0]) == record) or record[3] not in approved:
                continue
            try:
                admissible = (_frame_admitted(record, now, i.merchant_origin, i.allowed_frames)
                              or _unreviewed_admitted(record, now, i.merchant_origin, i.allowed_frames))
            except PaymentPolicyError:
                admissible = True  # an approved-origin frame under an unapproved ancestry is not provably harmless
            if admissible and await self._chain_exposed(record[0], now, i.child_sessions, i.page_session, cache):
                return False
        return True

    async def _absence_holds(self, i: PaymentInspection) -> bool:
        """dlz (B4): each pinned card-free admissible document still classifies to zero exposed card controls; its
        reading is validated like the inspection's (Codex B r1 #5): a malformed or unknown one raises."""
        for sid, obj in i.absence:
            raw = _value(await self._cdp("Runtime.callFunctionOn", {
                "objectId": obj, "functionDeclaration": "function(){return this.scan()}", "returnByValue": True},
                session_id=sid))
            if _card_controls(_bounded_description(raw)):
                return False
        return True

    def payment_cart_current(self, inspected: PaymentInspection) -> bool:
        from tools.browser_supervisor import _schedule

        return _schedule(self._cart_current(inspected), self._loop, timeout=10)

    async def _cart_current(self, i: PaymentInspection) -> bool:
        """dlz (B5): every amount witness still reads the same tuple in the same document at the same URL."""
        try:
            for sid, obj in i.witnesses:
                answer = _value(await self._cdp("Runtime.callFunctionOn", {
                    "objectId": obj, "functionDeclaration": "function(){return this.valid()}", "returnByValue": True},
                    session_id=sid))
                if answer is not True:
                    return False
            return True
        except Exception:  # health: allow BLE001 -- an unreadable witness is not the page the owner approved
            return False

    def commit_payment(self, inspected: PaymentInspection, fills: list[dict], strict: bool = False) -> dict:
        """Write the fills. ``strict`` (unreviewed destinations) adds a hit test at each field's centre
        before its setter; reviewed provider shapes keep ``False`` so they cannot regress."""
        import concurrent.futures

        from agent.async_utils import safe_schedule_threadsafe
        from tools.browser_supervisor import _LoopUnavailable

        until = time.monotonic() + _COMMIT_BUDGET_SECONDS
        future = safe_schedule_threadsafe(self._commit_payment(inspected, fills, until, strict), self._loop)
        if future is None:
            raise _LoopUnavailable("Browser supervisor loop unavailable")
        try:
            return future.result(timeout=_COMMIT_BUDGET_SECONDS)
        except concurrent.futures.TimeoutError:
            # dlz: stop the commit where it is; a write already in flight is refused by the renderer past
            # `until`, and what landed is unknown. Consumed (never a retry), so the caller skips its discard:
            # the release pass is awaited here instead (bounded), also when the commit never started.
            future.cancel()
            inspected.consumed = True
            try:
                self.discard_payment(inspected)
            except Exception:  # health: allow BLE001 -- a stalled loop or gone browser; the release task keeps going
                pass
            return {"success": False, "error_type": "payment_transport_uncertain", "partially_filled": True,
                    "write_uncertain": True}

    async def _commit_payment(self, i: PaymentInspection, fills: list[dict], until: float | None = None,
                              strict: bool = False) -> dict:
        if i.consumed:
            return {"success": False, "error_type": "payment_consent_consumed"}
        try:
            return await self._commit_payment_once(i, fills, until, strict)
        finally:
            # dlz: one cleanup for the whole lifecycle (freshness included) that a cancellation cannot cut short.
            await self._release_once(i)

    async def _commit_payment_once(self, i: PaymentInspection, fills: list[dict], until: float | None,
                                   strict: bool = False) -> dict:
        if time.monotonic() >= i.expires_at:
            # dlz: the inspection window closed (late approval, slow card lookup): nothing is written, and
            # the result says so instead of reading as a changed page.
            i.consumed = True
            return {"success": False, "error_type": "payment_consent_expired"}
        if not await self._payment_is_current(i):
            i.consumed = True
            return {"success": False, "error_type": "payment_page_changed"}
        if i.witnesses and not await self._cart_current(i):
            i.consumed = True
            return {"success": False, "error_type": "payment_cart_changed"}
        if not _policy_holds(i):  # dlz: re-read after the asynchronous freshness pass
            i.consumed = True
            return {"success": False, "error_type": "payment_destination_disabled"}
        if time.monotonic() >= i.expires_at:  # dlz: the freshness pass itself can outlast the window
            i.consumed = True
            return {"success": False, "error_type": "payment_consent_expired"}
        # Consume before ingress; transport uncertainty is never a retry authorization.
        i.consumed = True
        # dlz: the renderer's write deadline: the consent window or the commit budget, whichever ends first.
        stop = i.expires_at if until is None else min(i.expires_at, until)
        filled = 0
        try:
            # Every document was re-validated just above. A split checkout is written one
            # document at a time: no cross-document transaction exists, so a later failure
            # reports what an earlier document already received instead of retrying.
            written = []
            order = _commit_order(i, _fills_by_document(i, fills))
            for document, document_fills in order:
                if time.monotonic() >= i.expires_at:
                    # dlz: no write after the window closes; earlier documents' values are reported, not
                    # completed.
                    return _partial({"success": False, "error_type": "payment_consent_expired"}, filled)
                # dlz: an earlier document's page handlers ran after its writes: before the next document, every
                # approved chain is checked again against the pinned state (bounded: no OOPIF rediscovery), and
                # each document written so far must still be the one that received its fields.
                if written and not await self._payment_scope_current(i, [d for d, _f in written] + [document]):
                    return _partial({"success": False, "error_type": "payment_page_changed"}, filled)
                # dlz (Codex B r1 #1): an unreviewed fill also re-binds the cart, the absence witnesses, the candidate
                # topology and the policy before each later document, on the consumed inspection.
                verdict = await self._fill_scope_verdict(i) if written and i.unreviewed_origins else None
                if verdict:
                    return _partial({"success": False, "error_type": verdict}, filled)
                # dlz: strict fills in a frame pause after each focus for the owner-chain check from the parents
                # (a cross-origin parent's blur handler is out of the frame's reach); an unreviewed fill pauses in
                # every document, for the same bindings after the focus handlers ran.
                paced = (lambda point, d=document: self._paced_check(i, d, point)) \
                    if strict and (_has_parent(i, document) or i.unreviewed_origins) else None
                result = await self._commit_payment_document(
                    document, document_fills, deadline_ms=_renderer_deadline_ms(document, stop), strict=strict,
                    paced=paced)
                if not result.get("success"):
                    return _partial(result, filled)
                filled += result.get("filled_fields", 0)
                written.append((document, document_fills))
            # A document's own check runs right after its writes. Providers reset controlled inputs
            # later, and a later document (brand detection on the number, a blur) can clear an
            # earlier one, so success is only reported once EVERY document still holds its values.
            await asyncio.sleep(_SETTLE_SECONDS)
            # dlz: every frame up to the page must be shown before AND after the reads: a frame hidden while
            # the answers were collected is not the one the user sees, so its fields count as replaced.
            before = await self._shown_documents(i)
            answers = [(d, f, await self._payment_retention(i, d)) for d, f in written]
            after = await self._shown_documents(i)
            result = _retention_result([(f, a if d.frame_id in before and d.frame_id in after else None)
                                        for d, f, a in answers])
            if await self._top_navigated(i):
                result["navigated_after_fill"] = True
            return result
        except PaymentPolicyError:
            return _partial({"success": False, "error_type": "payment_fields_invalid"}, filled)
        except Exception:  # health: allow BLE001 -- CDP/transport text can carry values; reported as a fixed type, never retried
            return _partial({"success": False, "error_type": "payment_transport_uncertain"}, filled)

    async def _payment_scope_current(self, i: PaymentInspection, receiving: list) -> bool:
        """dlz: between the documents of a split fill, bounded and against the pinned state (no OOPIF
        rediscovery), over EVERY approved chain:
        - each ``receiving`` document (those written so far and the next) still has its inspected id, loader,
          URL and origin in its own session (one ``Page.getFrameTree`` per session);
        - every parent document on any card chain is still at its inspected URL and origin, and all its
          pinned owner <iframe>s are connected and shown (one call per parent, in the parent's pinned world:
          a new parent document destroys that world and the call fails).
        Frames off the card chains (ads) are ignored. Any failure to answer counts as changed."""
        by_id = {f[0]: f for f in i.tree}
        framed: set = set()
        for d in i.documents:
            fid, hops = d.frame_id, 0
            while fid in by_id and by_id[fid][4] and hops < 16:
                framed.add(fid)
                fid, hops = by_id[fid][4], hops + 1
            if fid not in by_id or by_id[fid][4]:
                return False
        pinned = {fid for _sid, items in i.frame_owners.values() for fid, _obj in items}
        if not framed <= pinned or any(p not in by_id for p in i.frame_owners):
            return False
        leaves = list(dict.fromkeys(d.frame_id for d in receiving))
        try:
            seen: dict = {}
            for sid in dict.fromkeys(i.child_sessions.get(fid, i.page_session) for fid in leaves):
                frames: dict = {}

                def walk(node):
                    frame = node["frame"]
                    frames[frame["id"]] = (frame.get("loaderId", ""), frame.get("url", ""),
                                           frame.get("securityOrigin", ""))
                    for child in node.get("childFrames", []):
                        walk(child)

                walk((await self._cdp("Page.getFrameTree", session_id=sid))["result"]["frameTree"])
                seen[sid] = frames
            for fid in leaves:
                sid = i.child_sessions.get(fid, i.page_session)
                if seen[sid].get(fid) != tuple(by_id[fid][1:4]):
                    return False
            scope = _OWNERS_SCOPE_EXPOSED if i.unreviewed_origins else _OWNERS_SCOPE
            for parent, (psid, items) in i.frame_owners.items():
                answer = _value(await self._cdp("Runtime.callFunctionOn", {
                    "objectId": items[0][1], "functionDeclaration": scope,
                    "arguments": [{"value": by_id[parent][2]}, {"value": by_id[parent][3]}]
                                 + [{"objectId": obj} for _fid, obj in items],
                    "returnByValue": True,
                }, session_id=psid))
                if not (isinstance(answer, list) and len(answer) == len(items) and all(a is True for a in answer)):
                    return False
            return True
        except Exception:  # health: allow BLE001 -- an unreadable page is not the approved page; fail closed
            return False

    async def _fill_scope_verdict(self, i: PaymentInspection) -> str | None:
        """dlz (Codex B r1 #1): during an unreviewed fill, the error type that stops it, or None: the window, the
        topology/absence/exposure bindings and every receiving document's ``valid()`` (Codex B r2 #1), the cart
        witnesses, and last (after the asynchronous checks) the destination policy."""
        if time.monotonic() >= i.expires_at:
            return "payment_consent_expired"
        if not await self._payment_is_current(i, during_fill=True):
            return "payment_page_changed"
        if i.witnesses and not await self._cart_current(i):
            return "payment_cart_changed"
        return None if _policy_holds(i) else "payment_destination_disabled"

    async def _paced_check(self, i: PaymentInspection, d: PaymentDocument, point) -> bool | str:
        """After a strict fill's focus, before the setter: True, or the error type that stops the document."""
        if _has_parent(i, d) and not await self._owner_chain_current(i, d, point):
            return "payment_page_changed"
        return (await self._fill_scope_verdict(i) or True) if i.unreviewed_origins else True

    async def _owner_chain_current(self, i: PaymentInspection, d: PaymentDocument, point) -> bool:
        """dlz: strict fills, after the focus and before each setter (Codex A2 r3 #1): from the card document up
        to the page, each parent document is still at its inspected URL and origin, the pinned owner <iframe>
        is connected, shown and not transparent, and the focused field's centre, mapped into the parent, hits
        that <iframe> (nothing of the merchant's covers it). One call per parent; unprovable = False."""
        by_id = {f[0]: f for f in i.tree}
        if not (isinstance(point, (list, tuple)) and len(point) == 2
                and all(type(v) in (int, float) and math.isfinite(v) for v in point)):
            return False
        x, y = point
        fid, hops = d.frame_id, 0
        try:
            while fid in by_id and by_id[fid][4]:
                parent = by_id[fid][4]
                psid, items = i.frame_owners.get(parent, (None, ()))
                owner = next((obj for f, obj in items if f == fid), None)
                hops += 1
                if parent not in by_id or owner is None or hops > 16:
                    return False
                answer = _value(await self._cdp("Runtime.callFunctionOn", {
                    "objectId": owner, "functionDeclaration": _OWNER_HIT,
                    "arguments": [{"value": by_id[parent][2]}, {"value": by_id[parent][3]},
                                  {"value": x}, {"value": y}],
                    "returnByValue": True,
                }, session_id=psid))
                if not (isinstance(answer, list) and len(answer) == 2
                        and all(type(v) in (int, float) and math.isfinite(v) for v in answer)):
                    return False
                x, y = answer
                fid = parent
        except Exception:  # health: allow BLE001 -- an owner chain that cannot answer is not provably the approved one
            return False
        return fid in by_id and not by_id[fid][4]

    async def _top_navigated(self, i: PaymentInspection) -> bool:
        """dlz: whether the top document's URL differs from the inspected one after the fill (True only when
        read and different; an unreadable page is reported through retention instead)."""
        try:
            tree = await self._cdp("Page.getFrameTree", session_id=i.page_session)
            url = tree["result"]["frameTree"]["frame"]["url"]
        except Exception:  # health: allow BLE001 -- retention already reports a page that cannot answer
            return False
        return bool(i.tree) and isinstance(url, str) and url != i.tree[0][2]

    async def _commit_payment_document(self, d: PaymentDocument, fills: list[dict], deadline_ms: float | None = None,
                                       strict: bool = False, paced=None) -> dict:
        """One document's commit. ``paced`` (strict fills in a frame, and every document of an unreviewed fill): an
        async ``check(point)`` run after each focus, before that field's setter; anything but True (False or an error
        type from ``_PACED_STOPS``) stops the document with nothing more written.
        dlz (Codex B r2 #2): the commit call carries the values, and each resume() runs a setter, so from the first
        call on a lost, failed or malformed reply means a write may have landed: the result then says so
        (``write_uncertain``, ``partially_filled``) with the last acknowledged count of this document's writes,
        never as if nothing was written."""
        acknowledged = 0

        def uncertain(error_type: str) -> dict:
            return {"success": False, "error_type": error_type, "filled_fields": acknowledged,
                    "partially_filled": True, "write_uncertain": True}

        try:
            result = _value(
                await self._cdp(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": d.object_id,
                        "functionDeclaration": "function(fills,deadline,strict,paced){"
                                               "return this.commit(fills,deadline,strict,paced)}",
                        "arguments": [{"value": fills}, {"value": deadline_ms}, {"value": strict is True},
                                      {"value": paced is not None}],
                        "returnByValue": True,
                    },
                    session_id=d.frame_session,
                )
            )
            for _ in range(len(fills) + 1):
                if paced is None or not isinstance(result, dict) or result.get("pending") is not True:
                    break
                attempted = result.get("filled_fields")
                # dlz (Codex B r3 #2): the focus point is checked here for every document, the top one included
                # (its paced check reads no point): a finite number each, never a bool or a missing value.
                if set(result) != {"pending", "filled_fields", "x", "y"} or type(attempted) is not int \
                        or not acknowledged <= attempted < len(fills) \
                        or not all(type(result[k]) in (int, float) and math.isfinite(result[k]) for k in ("x", "y")):
                    return uncertain("payment_result_invalid")
                acknowledged = attempted
                verdict = await paced((result["x"], result["y"]))
                if verdict is not True:
                    # dlz: the focused field is never written; earlier fields of this document are reported.
                    return {"success": False,
                            "error_type": verdict if verdict in _PACED_STOPS else "payment_page_changed",
                            "filled_fields": attempted, "partially_filled": attempted > 0}
                result = _value(await self._cdp("Runtime.callFunctionOn", {
                    "objectId": d.object_id, "functionDeclaration": "function(){return this.resume()}",
                    "returnByValue": True,
                }, session_id=d.frame_session))
        except Exception:  # health: allow BLE001 -- CDP/transport text can carry values; typed, never retried
            return uncertain("payment_transport_uncertain")
        if not _valid_document_result(result, len(fills)) or result["filled_fields"] < acknowledged:
            return uncertain("payment_result_invalid")  # dlz (Codex B r3 #1): a count never goes backwards
        if result["success"] is not True and (acknowledged or result.get("filled_fields", 0)):
            # An acknowledged write is never reported away by a later answer, nor by its own partial flag.
            result = {**result, "filled_fields": max(result.get("filled_fields", 0), acknowledged),
                      "partially_filled": True}
        return result

    async def _payment_retention(self, i: PaymentInspection, d: PaymentDocument) -> list | None:
        """Per written field ``{token, state, length}`` from the document's commit closure, or None when
        the document cannot answer (navigated, replaced, transport failure): its fields are then unknown
        and reported as replaced. Never retried, never a value. Frame visibility: ``_shown_documents``."""
        try:
            states = _value(await self._cdp("Runtime.callFunctionOn", {
                "objectId": d.object_id,
                "functionDeclaration": "function(){return this.retained()}",
                "returnByValue": True,
            }, session_id=d.frame_session))
        except Exception:  # health: allow BLE001 -- a gone document is a failed retention check, never a value
            return None
        if not isinstance(states, list):
            return None
        return states

    async def _release_payment_documents(self, i: PaymentInspection) -> None:
        objects = [(psid, obj) for psid, items in i.frame_owners.values() for _fid, obj in items]
        i.frame_owners = {}
        objects += [(d.frame_session, d.object_id) for d in i.documents] + list(i.absence) + list(i.witnesses)
        await self._release_all(objects, i.child_sessions)

    async def _release_once(self, i: PaymentInspection) -> None:
        """dlz: release every pinned object and child session once per inspection. The pass runs as its own
        task (each CDP call bounded by ``_cdp``'s timeout), so cancelling a commit or a discard that waits on it
        never cuts it short; a later discard awaits the same pass instead of starting another."""
        if i.release_task is None:
            i.release_task = asyncio.ensure_future(self._release_payment_documents(i))
        try:
            await asyncio.shield(i.release_task)
        except asyncio.CancelledError:
            if not i.release_task.done():
                raise  # the caller was cancelled; the pass goes on behind the shield
            # dlz (Codex B r5 #3): the pass itself ended cancelled after attempting every release; nothing is left
            # to wait for, and a commit's result is never replaced by that cancellation.

    def discard_payment(self, i: PaymentInspection) -> None:
        from tools.browser_supervisor import _schedule

        i.consumed = True
        _schedule(self._release_once(i), self._loop, timeout=12)


def _renderer_deadline_ms(d: PaymentDocument, stop: float) -> float:
    """dlz: the write deadline in the document's own monotonic clock: ms after its closure was created
    (``born = performance.now()``). The anchor is taken after that creation returned, so the deadline lands
    at or before ``stop`` on the host however late the commit call runs, and a wall-clock step cannot move
    it (no margin needed). No anchor: 0, nothing is written."""
    if d.clock_anchor is None:
        return 0.0
    return max(0.0, (stop - d.clock_anchor) * 1000)


# dlz: each pinned owner <iframe> is still shown to the user (strict: transparent or hidden ancestors too).
_OWNER_VISIBLE = ("e=>{if(!e||!e.isConnected)return false;"
                  "const r=e.getBoundingClientRect(),s=getComputedStyle(e);return e.getClientRects().length>0&&"
                  "r.width>2&&r.height>2&&s.visibility!=='hidden'&&Number(s.opacity)>0&&"
                  "(typeof e.checkVisibility!=='function'||e.checkVisibility({opacityProperty:true,"
                  "visibilityProperty:true}))}")
_OWNERS_SHOWN = "function(...els){return els.map(" + _OWNER_VISIBLE + ")}"
# dlz: in the parent's pinned world: the parent document is still at its inspected URL (CDP frame URLs carry
# no fragment) and origin, and each owner <iframe> belongs to it and is shown. null = the parent changed.
_PARENT_AT = ("if(String(location.href).split('#')[0]!==href||location.origin!==origin)return null;")
_OWNERS_SCOPE = ("function(href,origin,...els){" + _PARENT_AT + "return els.map(e=>!!e&&e.ownerDocument===document&&("
                 + _OWNER_VISIBLE + ")(e))}")
# dlz: strict, per setter: as _OWNERS_SCOPE for this one owner, plus a hit test of the focused field's centre
# (x, y in the child's viewport) mapped through the <iframe>'s border and padding; the mapped point, or null.
_OWNER_HIT = ("function(href,origin,x,y){" + _PARENT_AT + "const e=this;"
              "if(e.ownerDocument!==document||!(" + _OWNER_VISIBLE + ")(e))return null;"
              "const r=e.getBoundingClientRect(),s=getComputedStyle(e);if(s.pointerEvents==='none')return null;"
              "const px=r.left+e.clientLeft+(parseFloat(s.paddingLeft)||0)+x,"
              "py=r.top+e.clientTop+(parseFloat(s.paddingTop)||0)+y;"
              "const n=e.getRootNode&&e.getRootNode(),root=n&&typeof n.elementFromPoint==='function'?n:document;"
              "return root.elementFromPoint(px,py)===e?[px,py]:null}")


# The reviewed path's owner check (unchanged): the <iframe> is laid out and not visibility:hidden.
_OWNER_RENDERED = ("function(){const r=this.getBoundingClientRect();return this.isConnected&&"
                   "this.getClientRects().length>0&&r.width>2&&r.height>2&&"
                   "getComputedStyle(this).visibility!=='hidden'}")
# dlz (design v2 B2): what the user is actually shown, for a field or an owner <iframe> in its own document:
# 1. connected, laid out, larger than 2 px each way; 2. checkVisibility (opacity, visibility, content-visibility),
# own visibility visible and no legacy `clip`, and a non-zero opacity on it and every flat-tree ancestor;
# 3. its rectangle cut by every ancestor that clips by overflow (not the root or body, whose overflow is the
# viewport's) and by every clip-path inset() on it or an ancestor keeps 4x4 px; 4. that rectangle lies on the page
# (document coordinates inside the scroll size: a fixed-position offscreen copy fails); 5. when its centre is in the
# viewport, the hit test there returns the element itself (an overlay fails; hit testing honours every clip);
# a centre outside the viewport is left to the per-setter hit test.
# dlz (Codex B r1 #4/#7, B r2 #4): true, false or null. null = unknown: a clip-path other than a plain rectangular
# inset() (a rounded one included) or a legacy clip on an
# ancestor (it can only hide more, so a rectangle already too small stays false) with the centre outside the
# viewport, a tree deeper than 256 or any exception. Callers never read null as hidden.
_EXPOSED = r"""(e => {
  try {
    if (!e || !e.isConnected) return false;
    const doc = e.ownerDocument, win = doc && doc.defaultView;
    if (!win) return false;
    const r = e.getBoundingClientRect();
    if (!e.getClientRects().length || !(r.width > 2 && r.height > 2)) return false;
    if (typeof e.checkVisibility === 'function' && !e.checkVisibility({opacityProperty: true,
        visibilityProperty: true, contentVisibilityAuto: true})) return false;
    const own = win.getComputedStyle(e);
    if (own.visibility !== 'visible' || (own.clip && own.clip !== 'auto')) return false;
    // The rectangle a computed `inset(t r b l)` keeps of box a (px or % of the box), or null. dlz (Codex B r2 #4):
    // a rounded inset (`round`) cuts its corners off that rectangle: unknown, never computed as the rectangle.
    const inset = (path, a) => {
      const m = /^inset\(([^()]*)\)$/.exec(path);
      if (!m || /round/i.test(m[1])) return null;
      const parts = m[1].trim().split(/\s+/);
      if (!parts.length || !parts[0] || parts.length > 4) return null;
      const v = [];
      for (const p of parts) {
        const q = /^(-?\d+(?:\.\d+)?)(px|%)?$/.exec(p);
        if (!q || (!q[2] && Number(q[1]) !== 0)) return null;
        v.push(q);
      }
      const [t, rr, b, l] = [v[0], v[1] || v[0], v[2] || v[0], v[3] || v[1] || v[0]];
      const len = (q, size) => q[2] === '%' ? Number(q[1]) * size / 100 : Number(q[1]);
      return [a.left + len(l, a.width), a.top + len(t, a.height), a.right - len(rr, a.width),
              a.bottom - len(b, a.height)];
    };
    const top = doc.documentElement, body = doc.body;
    let x0 = r.left, y0 = r.top, x1 = r.right, y1 = r.bottom, n = e, k = 0, unknown = false;
    const cut = c => { x0 = Math.max(x0, c[0]); y0 = Math.max(y0, c[1]); x1 = Math.min(x1, c[2]); y1 = Math.min(y1, c[3]); };
    for (; n && k < 256; k++) {
      const st = n === e ? own : win.getComputedStyle(n);
      if (!(Number(st.opacity) > 0)) return false;
      const a = n.getBoundingClientRect();
      if (n !== e && n !== top && n !== body && (st.overflowX !== 'visible' || st.overflowY !== 'visible'))
        cut([a.left, a.top, a.right, a.bottom]);
      if (st.clipPath && st.clipPath !== 'none') {
        const c = inset(String(st.clipPath), a);
        if (c) cut(c); else unknown = true;
      }
      if (n !== e && st.clip && st.clip !== 'auto') unknown = true;
      n = n.assignedSlot || n.parentElement || (n.parentNode && n.parentNode.host) || null;
    }
    if (n) return null;
    if (!(x1 - x0 >= 4 && y1 - y0 >= 4)) return false;
    const sx = win.scrollX || 0, sy = win.scrollY || 0;
    const w = Math.max(top ? top.scrollWidth : 0, body ? body.scrollWidth : 0);
    const h = Math.max(top ? top.scrollHeight : 0, body ? body.scrollHeight : 0);
    if (x1 + sx <= 0 || y1 + sy <= 0 || x0 + sx >= w || y0 + sy >= h) return false;
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    if (cx >= 0 && cy >= 0 && cx < win.innerWidth && cy < win.innerHeight) {
      const root = e.getRootNode ? e.getRootNode() : doc;
      return (root && typeof root.elementFromPoint === 'function' ? root : doc).elementFromPoint(cx, cy) === e;
    }
    return unknown ? null : true;
  } catch (_) { return null; }
})"""
_OWNERS_EXPOSED = "function(...els){return els.map(" + _EXPOSED + ")}"
_OWNERS_SCOPE_EXPOSED = ("function(href,origin,...els){" + _PARENT_AT + "return els.map(e=>!!e&&e.ownerDocument===document&&("
                         + _EXPOSED + ")(e))}")

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


_PACED_STOPS = frozenset({"payment_page_changed", "payment_cart_changed", "payment_destination_disabled",
                          "payment_consent_expired"})


def _policy_holds(i: PaymentInspection) -> bool:
    """dlz: the destination policy captured for this unreviewed fill still holds (always, without a check)."""
    return i.policy_check is None or i.policy_check() is True


def _has_parent(i: PaymentInspection, d: PaymentDocument) -> bool:
    record = next((f for f in i.tree if f[0] == d.frame_id), None)
    return record is None or bool(record[4])  # an unknown frame is paced too (and then refused)


def _commit_order(i: PaymentInspection, groups: list) -> list:
    """Documents with something to write, the one holding the card number LAST: providers re-shape
    the other fields once they detect the brand (Mollie drops the CVC maxLength from 4 to 3), which a
    later write would correctly see as a changed document."""
    pairs = [(d, g) for d, g in zip(i.documents, groups) if g]
    return sorted(pairs, key=lambda p: any(c.token == "cc-number" for c in p[0].controls))


def _retention_result(documents: list) -> dict:
    """Success only when every written field of every document still holds its value after settling.
    ``documents`` pairs each document's fills with its ``retained()`` answer (None: unreadable). The
    result names tokens, states and lengths only."""
    states: dict = {}
    lengths: dict = {}
    held = total = 0
    for fills, answer in documents:
        answer = answer if isinstance(answer, list) and len(answer) == len(fills) else [None] * len(fills)
        for f, entry in zip(fills, answer):  # the closure answers in fill order
            token = str(f.get("token") or "")
            entry = entry if isinstance(entry, dict) and entry.get("token") == token else {}
            state = entry.get("state") if entry.get("state") in _RETENTION_STATES else "replaced"
            total += 1
            held += state == "held"
            states[token] = state
            if state != "held":
                length = entry.get("length")
                lengths[token] = length if type(length) is int and 0 <= length <= 256 else 0
    if total and held == total:
        return {"success": True, "filled_fields": held, "retention_verified": True}
    return {
        "success": False,
        "error_type": "payment_fields_not_retained",
        "filled_fields": held,
        "partially_filled": True,
        "field_retention": dict(sorted(states.items())),
        "field_lengths": dict(sorted(lengths.items())),
    }


# The error types a document's commit closure answers with (_INSPECT_TEMPLATE's commit and steps).
_DOCUMENT_ERRORS = frozenset({"payment_fields_changed", "payment_consent_expired", "payment_fields_invalid",
                              "payment_expiry_option_unmatched", "payment_validation_failed"})


def _valid_document_result(result: Any, fills: int) -> bool:
    """dlz (Codex B r2 #2, r3 #1): one document's final commit answer, exact keys and types. A success is exactly
    ``{success, filled_fields}`` with every one of the ``fills`` written; a failure names its error type and counts
    at most ``fills``."""
    if not isinstance(result, dict) or type(result.get("success")) is not bool:
        return False
    count = result.get("filled_fields")
    if result["success"]:
        return set(result) == {"success", "filled_fields"} and type(count) is int and count == fills
    # dlz (Codex B r4 #1): one of the closure's own error types, and always an explicit count (0 before any write).
    return (not set(result) - {"success", "filled_fields", "error_type", "partially_filled"}
            and type(result.get("error_type")) is str and result["error_type"] in _DOCUMENT_ERRORS
            and type(count) is int and 0 <= count <= fills
            and type(result.get("partially_filled", False)) is bool)


def _partial(result: dict, filled_before: int) -> dict:
    """A failed document after earlier documents of a split checkout already received values."""
    if not filled_before:
        return result
    return {**result, "filled_fields": filled_before + result.get("filled_fields", 0), "partially_filled": True}

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
_INSPECT = _INSPECT_TEMPLATE.replace("__LIMITS__", "null").replace("__EXPOSED__", "null")
_INSPECT_DISCOVERY = (_INSPECT_TEMPLATE.replace("__LIMITS__", json.dumps(_DISCOVERY_LIMITS))
                      .replace("__EXPOSED__", _EXPOSED))
