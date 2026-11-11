"""Vault-value redaction registry (profile-scoped, bounded), re-exported by :mod:`agent.redact`.

Exact secret values that transited a server-side vault fill (browser_vault). Generic credential-shaped
regexes cannot catch an arbitrary user password, so the fill path registers the exact bytes and every
browser_* tool result (including browser_cdp Runtime.evaluate passthrough) is scrubbed against them before
it can reach the model. Memory only: never persisted or logged. Keyed by profile home so a multiplex
gateway never scrubs profile B's output with profile A's passwords (which would also confirm to B that the
bytes exist), and bounded per profile: a fill-heavy session evicts its oldest entries rather than growing
forever.

dlz: a value made only of digits is never replaced where it matches. A placeholder on the one run that
equals it would answer "is it 737?" for a model that prints candidates (a CVC has 1000), so while a
pure-digit value of length L is registered, EVERY digit run of exactly L digits is masked digit for digit
(L >= 12, a card number: every run of L or more, spaced or dashed groups counted together), and the output
is the same whatever the value is. A value the model itself supplied (``known_to_model``: a one-time code
it typed) has nothing to reveal and is replaced exactly. Card values live in payment windows
(:func:`register_card_window`), dropped once the payment lock release is confirmed, so the length masking
lasts only for the payment.
"""

import re
import threading

_VAULT_REDACTION_MAX_PER_PROFILE = 64
_VAULT_REDACTION_VALUES: dict = {}  # profile home → ordered {value: known_to_model}
_CARD_WINDOWS: dict = {}  # profile home → ordered {window key: (digit lengths, holder words)}
_MAX_CARD_WINDOWS = 16
_VAULT_REDACTION_LOCK = threading.Lock()
PLACEHOLDER = "«redacted-vault-secret»"
CARD_PLACEHOLDER = "«redacted-card»"
_LONG = 12  # shortest card number: a pure-digit value this long also masks longer runs and spaced groups
_ALL_DIGITS = re.compile(r"\d+")
_GROUPED = re.compile(r"\d+(?:[ -]\d+)+")


def _vault_scope() -> str:
    from hermes_constants import get_hermes_home
    return str(get_hermes_home())


def register_vault_redaction_value(value, *, known_to_model: bool = False) -> None:
    """Register an exact vault secret value for model-facing redaction.

    Called by the vault fill path BEFORE the injection happens, so no later browser tool result
    can echo the value back into model context. Also registers the form a text input normalizes
    it to (CR/LF stripped), since that is what the page holds. A value registered once without
    ``known_to_model`` stays oracle-free.
    """
    if not isinstance(value, str) or not value:
        return
    normalized = value.replace("\r", "").replace("\n", "")
    with _VAULT_REDACTION_LOCK:
        bucket = _VAULT_REDACTION_VALUES.setdefault(_vault_scope(), {})
        for v in (value, normalized):
            if v:
                known = bucket.pop(v, True) and known_to_model  # re-registering refreshes recency
                bucket[v] = known
        while len(bucket) > _VAULT_REDACTION_MAX_PER_PROFILE:
            del bucket[next(iter(bucket))]


def clear_vault_redaction_values() -> None:
    """Drop the current profile's registered values and payment windows (profile teardown / explicit lock)."""
    with _VAULT_REDACTION_LOCK:
        _VAULT_REDACTION_VALUES.pop(_vault_scope(), None)
        _CARD_WINDOWS.pop(_vault_scope(), None)


def register_card_window(window: str, digits=(), words=()) -> None:
    """Open (or extend) the payment window ``window`` of this profile: the card's pure-digit values
    (number, CVC; only their lengths are kept) and its holder (scrubbed as a whole word, any case)."""
    lengths = frozenset(len(d) for d in digits if isinstance(d, str) and _ALL_DIGITS.fullmatch(d))
    names = frozenset(w.strip() for w in words if isinstance(w, str) and w.strip())
    with _VAULT_REDACTION_LOCK:
        windows = _CARD_WINDOWS.setdefault(_vault_scope(), {})
        old = windows.pop(str(window or ""), (frozenset(), frozenset()))
        windows[str(window or "")] = (old[0] | lengths, old[1] | names)
        while len(windows) > _MAX_CARD_WINDOWS:
            del windows[next(iter(windows))]


def release_card_window(window: str) -> bool:
    """Close this profile's payment window ``window`` (the lock release was confirmed). Whether one was open."""
    with _VAULT_REDACTION_LOCK:
        return (_CARD_WINDOWS.get(_vault_scope()) or {}).pop(str(window or ""), None) is not None


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


def redact_registered_vault_values(text: str, *, nondigit_only: bool = False) -> str:
    """Scrub of the profile's registered vault values: values with a non-digit character (and pure-digit
    values the model supplied) exactly, pure-digit ones by length (module docstring). ``nondigit_only``
    skips everything digit-only (the dialog pre-mask pass, which masks digits itself)."""
    if not isinstance(text, str) or not text:
        return text
    scope = _vault_scope()
    with _VAULT_REDACTION_LOCK:
        bucket = dict(_VAULT_REDACTION_VALUES.get(scope) or {})
        windows = list((_CARD_WINDOWS.get(scope) or {}).values())
    lengths = {len(v) for v, known in bucket.items() if not known and _ALL_DIGITS.fullmatch(v)}
    for digit_lengths, _names in windows:
        lengths |= digit_lengths
    for value in sorted(bucket, key=len, reverse=True):  # longest first: a substring never shadows its superstring
        if value in text and not (_ALL_DIGITS.fullmatch(value) and (nondigit_only or not bucket[value])):
            text = text.replace(value, PLACEHOLDER)
    for name in sorted({n for _d, names in windows for n in names}, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", CARD_PLACEHOLDER, text, flags=re.IGNORECASE)
    if lengths and not nondigit_only:
        text = _mask_runs(text, {n for n in lengths if n < _LONG}, min((n for n in lengths if n >= _LONG), default=0))
    return text
