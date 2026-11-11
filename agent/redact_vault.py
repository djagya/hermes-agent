"""Vault-value redaction registry (profile-scoped, bounded), re-exported by :mod:`agent.redact`.

Exact secret values that transited a server-side vault fill (browser_vault). Generic credential-shaped
regexes cannot catch an arbitrary user password, so the fill path registers the exact bytes and every
browser_* tool result (including browser_cdp Runtime.evaluate passthrough) is scrubbed against them before
it can reach the model. Memory only: never persisted or logged. Keyed by profile home so a multiplex
gateway never scrubs profile B's output with profile A's passwords (which would also confirm to B that the
bytes exist), and bounded per profile: a fill-heavy session evicts its oldest entries rather than growing
forever.

dlz: the scrub depends on who reads the text (``audience``, :func:`redact_registered_vault_values`):

* ``model`` (the default: a tool result the model reads that it did not write) and ``browser`` (a browser
  tool result): a value made only of digits is never replaced where it matches. A placeholder on the one
  run that equals it would answer "is it 4711?" for a model that prints candidates, so while a pure-digit
  value of length L masks, EVERY digit run of exactly L digits is masked digit for digit (L >= 12, a card
  number: every run of L or more, spaced or dashed groups counted together), and the output is the same
  whatever the value is. A pure-digit login secret masks for ``LOGIN_MASK_S`` after its registration (a
  one-time code for its TTL), then leaves the length classes. A pure-digit value the model supplied itself
  is never registered (it knows it; masking it would answer "does my guess equal the PIN?").
* ``log`` (log files, not model-visible): every non-digit value is replaced exactly; a pure-digit value is
  length-masked during its window and then not scrubbed at all (an exact scrub of a PIN would let logs answer
  "is it 4711?"); the card windows still mask by length.
* ``authored`` (persisted text the model wrote: tool-call arguments): only values with a non-digit
  character, so the JSON stays valid and a reload marks nothing.

Values with a non-digit character are replaced exactly for every audience. Card values live in payment
windows (:func:`register_card_window`, lengths and holder only, never the values), fenced by a per-window
generation: a claim or an open bumps it, and a release that observed an older generation changes nothing.
A confirmed lock release keeps the window for browser results only (the page may still show card text);
the window goes when the slot's mark is cleared.
"""

import json
import re
import threading
import time

_VAULT_REDACTION_MAX_PER_PROFILE = 64
_VAULT_REDACTION_VALUES: dict = {}  # profile home → ordered {value: _Entry}
_CARD_WINDOWS: dict = {}  # profile home → ordered {window key: [digit lengths, holder words, released]}
_CARD_GENERATIONS: dict = {}  # profile home → {window key: generation}
_MAX_CARD_WINDOWS = 16
_MAX_GENERATIONS = 256
_VAULT_REDACTION_LOCK = threading.Lock()
PLACEHOLDER = "«redacted-vault-secret»"
CARD_PLACEHOLDER = "«redacted-card»"
LOGIN_MASK_S = 900.0  # a pure-digit login secret masks digit runs of its length for 15 minutes
AUDIENCES = ("model", "browser", "log", "authored")
_LONG = 12  # shortest card number: a pure-digit value this long also masks longer runs and spaced groups
_ALL_DIGITS = re.compile(r"\d+")
_GROUPED = re.compile(r"\d+(?:[ -]\d+)+|\d{2,6}(?:[ .-]\d{2,6}){2,}")  # spaced, dashed or dotted (3+ short groups) card groups


class _Entry:
    """One registered value: permanent and expiring registrations kept apart (a TTL never shortens a
    permanent one), and how long a pure-digit value masks by length."""

    __slots__ = ("known", "mask_until", "permanent", "until")

    def __init__(self):
        self.permanent, self.until, self.mask_until, self.known = False, 0.0, 0.0, True

    def alive(self, now: float) -> bool:
        return self.permanent or self.until > now


def _vault_scope() -> str:
    from hermes_constants import get_hermes_home
    return str(get_hermes_home())


def register_vault_redaction_value(value, *, known_to_model: bool = False, ttl_s: float | None = None,
                                   length_mask: bool = True) -> None:
    """Register an exact vault secret value for redaction.

    Called by the vault fill path BEFORE the injection happens, so no later browser tool result
    can echo the value back into model context. Also registers the form a text input normalizes
    it to (CR/LF stripped), since that is what the page holds. ``ttl_s`` (one-time codes) lets the value
    lapse after that many seconds; a permanent registration of the same bytes is kept regardless.
    A pure-digit value masks by length for ``LOGIN_MASK_S`` (or its TTL, if shorter); with
    ``length_mask=False`` (an identifier such as a numeric username) it is not scrubbed by any audience. A
    pure-digit ``known_to_model`` value (a code the model typed) is not registered at all.
    """
    if not isinstance(value, str) or not value:
        return
    normalized = value.replace("\r", "").replace("\n", "")
    if not normalized:
        return
    # a value that is digits once CR/LF and surrounding blanks are gone is a digit value: its raw forms are never
    # exact non-digit ones (a placeholder on "4711 " would answer "is it 4711?")
    forms = (normalized.strip(),) if _ALL_DIGITS.fullmatch(normalized.strip()) else (value, normalized)
    now = time.monotonic()
    with _VAULT_REDACTION_LOCK:
        bucket = _VAULT_REDACTION_VALUES.setdefault(_vault_scope(), {})
        for v in dict.fromkeys(forms):
            digits = bool(_ALL_DIGITS.fullmatch(v))
            if not v or (digits and known_to_model):
                continue
            entry = bucket.pop(v, None) or _Entry()  # re-registering refreshes recency
            entry.known = entry.known and known_to_model
            if ttl_s is None:
                entry.permanent = True
            else:
                entry.until = max(entry.until, now + max(0.0, float(ttl_s)))
            if digits and length_mask:
                life = LOGIN_MASK_S if ttl_s is None else min(LOGIN_MASK_S, max(0.0, float(ttl_s)))
                entry.mask_until = max(entry.mask_until, now + life)
            bucket[v] = entry
        while len(bucket) > _VAULT_REDACTION_MAX_PER_PROFILE:  # model-typed values go first, then the oldest
            del bucket[next((v for v, e in bucket.items() if e.known), next(iter(bucket)))]


def _live(scope: str) -> dict:
    """Under the lock: forget this profile's lapsed values; the live ones."""
    now = time.monotonic()
    bucket = _VAULT_REDACTION_VALUES.get(scope) or {}
    for v in [v for v, e in bucket.items() if not e.alive(now)]:
        del bucket[v]
    return dict(bucket)


def clear_vault_redaction_values() -> None:
    """Drop the current profile's registered values and payment windows (profile teardown / explicit lock)."""
    with _VAULT_REDACTION_LOCK:
        _VAULT_REDACTION_VALUES.pop(_vault_scope(), None)
        _CARD_WINDOWS.pop(_vault_scope(), None)


def _bump(scope: str, key: str) -> int:
    gens = _CARD_GENERATIONS.setdefault(scope, {})
    gens[key] = gens.pop(key, 0) + 1
    windows = _CARD_WINDOWS.get(scope) or {}
    while len(gens) > _MAX_GENERATIONS:  # forget the oldest counter with no window (it restarts above 0 anyway)
        stale = next((k for k in gens if k not in windows and k != key), None)
        if stale is None:
            break
        del gens[stale]
    return gens[key]


def bump_card_window(window: str) -> int:
    """A claim on ``window`` started: a release that observed an older generation must change nothing."""
    with _VAULT_REDACTION_LOCK:
        return _bump(_vault_scope(), str(window or ""))


def card_window_generations() -> dict:
    """This profile's window generations, observed before a status read, a prune or a clear."""
    with _VAULT_REDACTION_LOCK:
        return dict(_CARD_GENERATIONS.get(_vault_scope()) or {})


def card_window_keys() -> list:
    with _VAULT_REDACTION_LOCK:
        return list(_CARD_WINDOWS.get(_vault_scope()) or {})


def register_card_window(window: str, digits=(), words=()) -> int:
    """Open (or extend, reopening it for every output) the payment window ``window`` of this profile: the
    card's pure-digit values (number, CVC; only their lengths are kept) and its holder (scrubbed as a whole
    word, any case). Returns the window's new generation."""
    lengths = frozenset(len(d) for d in digits if isinstance(d, str) and _ALL_DIGITS.fullmatch(d))
    names = frozenset(w.strip() for w in words if isinstance(w, str) and w.strip())
    key = str(window or "")
    with _VAULT_REDACTION_LOCK:
        scope = _vault_scope()
        windows = _CARD_WINDOWS.setdefault(scope, {})
        old = windows.pop(key, (frozenset(), frozenset(), False))
        windows[key] = (old[0] | lengths, old[1] | names, False)
        while len(windows) > _MAX_CARD_WINDOWS:
            del windows[next(iter(windows))]
        return _bump(scope, key)


def release_card_window(window: str, observed: int | None = None, *, keep_for_browser: bool = False) -> bool:
    """Close this profile's payment window ``window``; with ``keep_for_browser`` (a confirmed lock release)
    it keeps masking browser tool results only. ``observed`` is the generation the caller saw before it
    decided: a newer claim or open since then refuses the release. Whether the release was accepted."""
    key = str(window or "")
    with _VAULT_REDACTION_LOCK:
        scope = _vault_scope()
        if observed is not None and (_CARD_GENERATIONS.get(scope) or {}).get(key, 0) != observed:
            return False
        windows = _CARD_WINDOWS.get(scope) or {}
        if key in windows and keep_for_browser:
            windows[key] = (windows[key][0], windows[key][1], True)
        else:
            windows.pop(key, None)
        return True


def _mask_runs(text: str, exact: set, at_least: int) -> str:
    def dots(m):
        return re.sub(r"\d", "•", m.group(0))

    def grouped(m):
        return dots(m) if sum(map(len, _ALL_DIGITS.findall(m.group(0)))) >= at_least else m.group(0)

    def run(m):
        n = len(m.group(0))
        return dots(m) if n in exact or 0 < at_least <= n else m.group(0)

    if at_least:
        text = _GROUPED.sub(grouped, text)
    return _ALL_DIGITS.sub(run, text)


def redact_registered_vault_values(text: str, *, nondigit_only: bool = False, audience: str = "model") -> str:
    """Scrub of the profile's registered vault values for ``audience`` (module docstring; an unknown one is
    ``model``). ``nondigit_only`` skips everything digit-only (the dialog pre-mask pass, which masks digits
    itself)."""
    if not isinstance(text, str) or not text:
        return text
    if audience == "none":  # a caller that scrubbed the vault values leaf by leaf and wants only the rest
        return text
    audience = audience if audience in AUDIENCES else "model"
    scope, now = _vault_scope(), time.monotonic()
    with _VAULT_REDACTION_LOCK:
        bucket = _live(scope)
        windows = [w for w in (_CARD_WINDOWS.get(scope) or {}).values()
                   if not w[2] or audience in ("browser", "log")]
    for value in sorted(bucket, key=len, reverse=True):  # longest first: a substring never shadows its superstring
        if not _ALL_DIGITS.fullmatch(value) and value in text:
            text = text.replace(value, PLACEHOLDER)
    for name in sorted({n for _d, names, _r in windows for n in names}, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", CARD_PLACEHOLDER, text, flags=re.IGNORECASE)
    if nondigit_only or audience == "authored":
        return text
    lengths = {n for digit_lengths, _names, _r in windows for n in digit_lengths}
    lengths |= {len(v) for v, e in bucket.items() if e.mask_until > now and _ALL_DIGITS.fullmatch(v)}
    if lengths:
        text = _mask_runs(text, {n for n in lengths if n < _LONG}, min((n for n in lengths if n >= _LONG), default=0))
    return text


_JSON_MAX_DEPTH = 200


def redact_number(value, audience: str = "model"):
    """A number leaf of parsed data (``value: 4711``): masked by length like text, and then returned as the
    mask string, never as the number."""
    masked = redact_registered_vault_values(str(value), audience=audience)
    return value if masked == str(value) else masked


def _scrub_json(value, audience: str, depth: int = 0):
    if depth >= _JSON_MAX_DEPTH:
        return "[redacted: nested too deep]"
    if isinstance(value, str):
        return redact_registered_vault_values(value, audience=audience)
    if isinstance(value, (int, float)) and not isinstance(value, bool):  # a number is data too: masked as a string
        return redact_number(value, audience)
    if isinstance(value, list):
        return [_scrub_json(v, audience, depth + 1) for v in value]
    if isinstance(value, dict):
        return {(_scrub_json(k, audience, depth + 1) if isinstance(k, str) else k): _scrub_json(v, audience, depth + 1)
                for k, v in value.items()}
    return value


class _DuplicateKey(ValueError):
    pass


def _unique_pairs(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _DuplicateKey("duplicate JSON key")
    return dict(pairs)


def redact_json_text(text: str, audience: str = "model", then=None) -> str:
    """Vault scrub of serialized JSON that stays valid JSON: string leaves and keys are scrubbed one by one
    (a registered ``true``, a trailing backslash or a masked-length number cannot break the syntax). Text that
    does not parse gets the plain text scrub. ``then`` (optional) is applied last to the serialized text, for
    a caller's own generic pass (use ``vault="none"`` there: the vault values are already done)."""
    if not isinstance(text, str) or not text:
        return text
    try:
        parsed = json.loads(text, object_pairs_hook=_unique_pairs)
    except _DuplicateKey:  # parsing would keep only the last value: re-serialize with every value scrubbed
        out = json.dumps(_scrub_json(json.loads(text), audience), ensure_ascii=False)
    except (ValueError, RecursionError):
        out = redact_registered_vault_values(text, audience=audience)
    else:
        scrubbed = _scrub_json(parsed, audience)
        out = text if scrubbed == parsed else json.dumps(scrubbed, ensure_ascii=False)
    return then(out) if then else out
