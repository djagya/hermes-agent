"""UX capture helpers that run INSIDE a ``browser_exec`` call.

``tools.browser_capture`` prepends a loader to any ``browser_exec`` code that
names ``ux_capture``; the loader imports this file by path and binds
``ux_capture`` to the harness globals (``cdp``, ``js``, ``drain_events``).
Stdlib only, plus Pillow for diffs. Nothing here imports the Hermes ``tools``
package: the harness subprocess runs on a PYTHONPATH that only holds the venv.

Every pixel/DOM capture first re-checks the payment capture guard for the slot
the call was granted (the gateway already refused the call before dispatch;
this is the second check, from the same mark file, fail closed). Files land in
``$HERMES_UX_CAPTURE_ROOT/<run>/`` with an ``index.json`` and ``index.md``;
results carry paths, sizes and sha256, never image bytes.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as _dt
import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

ROOT_ENV = "HERMES_UX_CAPTURE_ROOT"
GUARD_ENV = "HERMES_UX_CAPTURE_GUARD"
AXE_PATH_ENV = "HERMES_AXE_CORE_PATH"
TASK_ENV = "HERMES_UX_CAPTURE_TASK"
SLOT_ENV = "HERMES_BROWSER_SLOT_ID"
GENERATION_ENV = "HERMES_BROWSER_GENERATION"
MANAGED_ENV = "HERMES_BROWSER_LEASE_ID"

# axe-core 4.14.0 (MPL-2.0), installed by the image's npm toolbox step.
AXE_DEFAULT_PATH = "/usr/local/lib/node_modules/axe-core/axe.min.js"
AXE_VERSION = "4.14.0"
AXE_SHA256 = "20c09fe157a8a34a30e241aaa1fcdade657734f08ab379ecfbeb7d45cc46e878"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# Chromium refuses a capture taller than 16384 device pixels.
MAX_CAPTURE_DEVICE_PX = 16000
SHOT_TIMEOUT_S = 60.0
# Requests to the harness daemon are one JSON line read with a 64 KiB limit.
JS_CHUNK_CHARS = 24000
IPC_LINE_BUDGET = 60000

VIEWPORTS: dict[str, dict[str, Any]] = {
    "phone": {"width": 390, "height": 844, "dsf": 3, "mobile": True},
    "tablet": {"width": 820, "height": 1180, "dsf": 2, "mobile": True},
    "desktop": {"width": 1440, "height": 900, "dsf": 1, "mobile": False},
}

# Throughput in bytes/s, latency in ms. Approximations of the DevTools /
# Lighthouse presets; record the preset name with the result, not "real 3G".
NETWORK_PRESETS: dict[str, dict[str, Any]] = {
    "slow-3g": {"latency": 2000, "down_kbps": 400, "up_kbps": 400},
    "3g": {"latency": 563, "down_kbps": 1440, "up_kbps": 675},
    "slow-4g": {"latency": 150, "down_kbps": 1600, "up_kbps": 750},  # Lighthouse mobile
    "fast-4g": {"latency": 40, "down_kbps": 9000, "up_kbps": 1500},
    "offline": {"offline": True},
}

RECORD_MAX_SECONDS = 300
RECORD_MAX_MB = 200

# Thresholds from web.dev (good <= first, poor > second).
VITALS_THRESHOLDS = {"lcp_ms": (2500, 4000), "cls": (0.1, 0.25), "inp_ms": (200, 500),
                     "fcp_ms": (1800, 3000), "ttfb_ms": (800, 1800)}

HEURISTICS = (
    ("status", "Visibility of system status: loading, progress, saved/failed states are shown"),
    ("match", "Match with the real world: words, order and icons the user already knows"),
    ("control", "User control: undo, back, cancel and escape routes work"),
    ("consistency", "Consistency: same component, same behavior and look across pages"),
    ("errors", "Error prevention: risky actions confirm, inputs constrain before submit"),
    ("recognition", "Recognition over recall: options visible, labels persist, no hidden state"),
    ("efficiency", "Efficiency: shortcuts, sensible defaults, few steps for the main task"),
    ("minimal", "Minimal design: hierarchy, spacing, one primary action per view"),
    ("recovery", "Error recovery: messages say what happened and how to fix it"),
    ("help", "Help: empty states and first-run guidance where needed"),
    ("a11y", "Accessibility: contrast, focus order, target size >= 24px, labels, reduced motion"),
    ("responsive", "Responsive: no horizontal scroll, readable text, thumb-reachable actions on phone"),
)


class CaptureRefused(RuntimeError):
    """The payment capture guard refuses capture on this slot."""

    error_type = "payment_screenshot_blocked"


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _slug(text: str, limit: int = 48) -> str:
    return (re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "")).strip("-._") or "item")[:limit]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _private_write(path: Path, data: bytes) -> None:
    """Create ``path`` mode 0600 from the first byte (frames are written straight, not renamed)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


@contextlib.contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    """Exclusive advisory lock on ``lock_path`` (POSIX fcntl; a no-op where fcntl is missing)."""
    try:
        import fcntl
    except ImportError:  # Windows: one writer per run is the documented contract there
        yield
        return
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# Attribute values in axe node markup can carry what the user typed (value=), app state (data-*),
# tokens in URLs (href/src) or private text (aria-label, title). Keep only structural ones.
_KEEP_ATTRS = {"id", "class", "role", "type"}
_ATTR_RE = re.compile(r"""(\s)([^\s=<>"'/]+)(\s*=\s*)("[^"]*"|'[^']*'|[^\s"'>]+)""")
_SELECTOR_ATTR_RE = re.compile(r"""\[([^\]=\s~|^$*]+)([~|^$*]?=)("[^"]*"|'[^']*'|[^\]]*)\]""")


def scrub_markup(text: str) -> str:
    """Drop every attribute value except id/class/role/type from an HTML snippet."""
    def keep(m: re.Match) -> str:
        name = m.group(2).lower()
        return m.group(0) if name in _KEEP_ATTRS else f'{m.group(1)}{m.group(2)}="…"'
    return _ATTR_RE.sub(keep, str(text or ""))


def scrub_selector(text: Any) -> Any:
    if isinstance(text, list):
        return [scrub_selector(t) for t in text]
    return _SELECTOR_ATTR_RE.sub(lambda m: m.group(0) if m.group(1).lower() in _KEEP_ATTRS
                                 else f'[{m.group(1)}{m.group(2)}"…"]', str(text))


def scrub_axe(data: dict[str, Any]) -> dict[str, Any]:
    for group in ("violations", "incomplete"):
        for rule in data.get(group) or []:
            for node in rule.get("targets") or []:
                node["html"] = scrub_markup(node.get("html", ""))
                node["summary"] = scrub_markup(node.get("summary", ""))
                node["target"] = scrub_selector(node.get("target", []))
    return data


def png_size(data: bytes) -> tuple[int, int]:
    """(width, height) from a PNG's IHDR; ValueError when the bytes are not a PNG."""
    if len(data) < 24 or not data.startswith(PNG_MAGIC) or data[12:16] != b"IHDR":
        raise ValueError("not a PNG (bad signature or IHDR)")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def guard_sentinel(guard: str) -> Path:
    """``$HERMES_HOME/payment-capture-guard.sentinel`` beside ``$HERMES_HOME/browser/<marks>.json``."""
    return Path(guard).parent.parent / "payment-capture-guard.sentinel"


def capture_blocked(env: dict[str, str] | None = None) -> bool:
    """Same rule as ``tools.browser_payment_privacy.capture_blocked_for_surface``.

    A missing guard path, an unreadable mark file, a profile-wide mark, or any
    mark on this slot (any generation) blocks. Outside managed mode any mark
    blocks, as the profile-wide check does.
    """
    env = os.environ if env is None else env
    guard = str(env.get(GUARD_ENV) or "")
    if not guard:
        return True
    try:
        raw = Path(guard).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        # No mark file is a fresh install, unless the first mark's sentinel says marks were lost.
        return guard_sentinel(guard).exists()
    except OSError:
        return True
    try:
        marks = json.loads(raw).get("marks")
    except (ValueError, AttributeError):
        return True
    if not isinstance(marks, list) or not all(isinstance(m, dict) for m in marks):
        return True
    slot = str(env.get(SLOT_ENV) or "").strip()
    generation = str(env.get(GENERATION_ENV) or "").strip()
    if not slot or not generation:
        return bool(marks)
    return any(m.get("scope") != "slot" or m.get("slot_id") in (None, "", slot) for m in marks)


def parse_viewport(spec: Any) -> dict[str, Any]:
    """'phone' | 'tablet' | 'desktop' | 'WxH' | 'WxH@2' | 'WxH@3m' | {width, height, dsf, mobile}."""
    if isinstance(spec, dict):
        vp = {"dsf": 1, "mobile": False, **spec}
        vp.setdefault("name", f"{vp['width']}x{vp['height']}")
    elif str(spec) in VIEWPORTS:
        vp = {"name": str(spec), **VIEWPORTS[str(spec)]}
    else:
        m = re.fullmatch(r"(\d{2,5})x(\d{2,5})(?:@(\d(?:\.\d+)?))?(m?)", str(spec).strip())
        if not m:
            raise ValueError(f"unknown viewport {spec!r}: use phone/tablet/desktop or WxH[@dsf][m]")
        vp = {"name": str(spec), "width": int(m[1]), "height": int(m[2]),
              "dsf": float(m[3] or 1), "mobile": bool(m[4])}
    vp["width"], vp["height"] = int(vp["width"]), int(vp["height"])
    if not (100 <= vp["width"] <= 4000 and 100 <= vp["height"] <= 4000 and 0.5 <= float(vp["dsf"]) <= 4):
        raise ValueError(f"viewport out of range: {vp}")
    return vp


def _rating(metric: str, value: float | None) -> str | None:
    if value is None:
        return None
    good, poor = VITALS_THRESHOLDS[metric]
    return "good" if value <= good else ("poor" if value > poor else "needs-improvement")


_VITALS_SCRIPT = r"""
(async (wait) => {
  const out = {lcp_ms: null, cls: 0, inp_ms: null, fcp_ms: null, ttfb_ms: null, interactions: 0};
  const lcp = [], shifts = [], events = [];
  const obs = [];
  const watch = (type, sink, extra) => {
    try { const o = new PerformanceObserver(l => sink.push(...l.getEntries()));
          o.observe({type, buffered: true, ...(extra || {})}); obs.push(o); } catch (e) {}
  };
  watch('largest-contentful-paint', lcp);
  watch('layout-shift', shifts);
  watch('event', events, {durationThreshold: 16});
  await new Promise(r => setTimeout(r, wait));
  obs.forEach(o => { try { o.takeRecords && o.takeRecords(); o.disconnect(); } catch (e) {} });
  if (lcp.length) out.lcp_ms = lcp[lcp.length - 1].startTime;
  let win = 0, best = 0, first = 0, last = 0;
  for (const s of shifts) {
    if (s.hadRecentInput) continue;
    if (win && s.startTime - last < 1000 && s.startTime - first < 5000) { win += s.value; }
    else { win = s.value; first = s.startTime; }
    last = s.startTime; best = Math.max(best, win);
  }
  out.cls = best;
  const byId = new Map();
  for (const e of events) if (e.interactionId) byId.set(e.interactionId, Math.max(byId.get(e.interactionId) || 0, e.duration));
  const d = [...byId.values()].sort((a, b) => b - a);
  out.interactions = d.length;
  if (d.length) out.inp_ms = d[Math.min(d.length - 1, Math.floor(d.length / 50))];
  const paint = performance.getEntriesByName('first-contentful-paint')[0];
  if (paint) out.fcp_ms = paint.startTime;
  const nav = performance.getEntriesByType('navigation')[0];
  if (nav) { out.ttfb_ms = nav.responseStart; out.dom_content_loaded_ms = nav.domContentLoadedEventEnd;
             out.load_ms = nav.loadEventEnd; out.transfer_bytes = nav.transferSize; }
  out.resources = performance.getEntriesByType('resource').length;
  return JSON.stringify(out);
})(%d)
"""

_AXE_RUN_SCRIPT = r"""
(async (tags, selector) => {
  const ctx = selector ? document.querySelector(selector) : document;
  if (!ctx) return JSON.stringify({error: 'selector not found: ' + selector});
  const res = await axe.run(ctx, {runOnly: {type: 'tag', values: tags}, resultTypes: ['violations', 'incomplete']});
  const slim = r => ({id: r.id, impact: r.impact, help: r.help, helpUrl: r.helpUrl, nodes: r.nodes.length,
    targets: r.nodes.slice(0, 5).map(n => ({target: n.target, html: String(n.html || '').slice(0, 200),
      summary: String(n.failureSummary || '').slice(0, 300)}))});
  return JSON.stringify({version: axe.version, url: location.origin + location.pathname, violations: res.violations.map(slim),
    incomplete: res.incomplete.map(slim), passes: (res.passes || []).length});
})(%s, %s)
"""

_ELEMENT_RECT_SCRIPT = r"""
(() => { const el = document.querySelector(%s); if (!el) return null;
  el.scrollIntoView({block: 'center', inline: 'nearest'});
  const r = el.getBoundingClientRect();
  return JSON.stringify({x: r.left + scrollX, y: r.top + scrollY, width: r.width, height: r.height}); })()
"""

_SETTLE_SCRIPT = "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"
# origin + path only: query strings and fragments carry reset links, OAuth codes and session ids.
_PAGE_SCRIPT = "JSON.stringify({url: location.origin + location.pathname, title: document.title, scroll_width: document.documentElement.scrollWidth, client_width: document.documentElement.clientWidth})"


class Run:
    """One capture folder: ``<root>/<run>/`` with index.json, index.md and the files."""

    def __init__(self, cap: UXCapture, name: str):
        self.cap = cap
        self.name = _slug(name, 80)
        self.dir = _private_dir(cap.root / self.name)

    # ---- index -------------------------------------------------------
    @property
    def index_path(self) -> Path:
        return self.dir / "index.json"

    def index(self) -> dict[str, Any]:
        """The run index; a missing file is a new run, a malformed one is an error (never reset)."""
        try:
            raw = self.index_path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {"run": self.name, "created": _now(), "task": self.cap.env.get(TASK_ENV, ""), "entries": []}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"{self.index_path} is malformed ({exc}); move it aside to start over") from exc
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise ValueError(f"{self.index_path} is malformed (no entries list); move it aside to start over")
        return data

    def _lock(self) -> contextlib.AbstractContextManager[None]:
        return _locked(self.dir / ".index.lock")

    def _next_seq(self) -> int:
        """Reserve the next sequence number (file prefix and index seq), safe across concurrent calls."""
        counter = self.dir / ".seq"
        with self._lock():
            try:
                last = int(counter.read_text(encoding="utf-8-sig").strip() or 0)
            except (FileNotFoundError, ValueError):
                last = 0
            seq = max(last, len(self.index()["entries"])) + 1
            _atomic_write(counter, str(seq).encode("utf-8"))
        return seq

    def _add(self, entry: dict[str, Any], seq: int | None = None) -> dict[str, Any]:
        seq = seq or self._next_seq()
        with self._lock():
            data = self.index()
            entry = {"seq": seq, "at": _now(), **entry}
            data["entries"].append(entry)
            data["updated"] = _now()
            _atomic_write(self.index_path, json.dumps(data, indent=2).encode("utf-8"))
            _atomic_write(self.dir / "index.md", _index_markdown(data).encode("utf-8"))
        return entry

    def _file_entry(self, kind: str, path: Path, seq: int, **extra: Any) -> dict[str, Any]:
        path.chmod(0o600)  # every writer already creates 0600; keep the invariant explicit
        return self._add({"kind": kind, "file": path.name, "path": str(path),
                          "bytes": path.stat().st_size, "sha256": _sha256(path), **extra}, seq=seq)

    def note(self, text: str, **extra: Any) -> dict[str, Any]:
        """Add a free-text step note (shows in the storyboard timeline)."""
        return self._add({"kind": "note", "text": str(text), **extra})

    # ---- screenshots -------------------------------------------------
    def shot(self, name: str, *, full: bool = False, selector: str | None = None,
             viewport: Any = None, scheme: str | None = None, reduced_motion: bool = False,
             settle: float = 0.5, note: str = "") -> dict[str, Any]:
        """One PNG: the viewport, the full page (``full=True``) or one element (``selector``).

        ``viewport``/``scheme``/``reduced_motion`` apply for this capture only and are
        restored afterwards; without them the page's current emulation is used.
        """
        self.cap.check_guard()
        with self.cap.emulate(viewport=viewport, scheme=scheme, reduced_motion=reduced_motion, settle=settle) as vp:
            return self._shoot(name, full=full, selector=selector, vp=vp, scheme=scheme,
                               reduced_motion=reduced_motion, note=note)

    def matrix(self, name: str, *, viewports: Any = ("phone", "tablet", "desktop"),
               schemes: Any = ("light",), full: bool = False, reduced_motion: bool = False,
               settle: float = 0.8, note: str = "") -> list[dict[str, Any]]:
        """Capture every (viewport x scheme) pair; emulation is restored once at the end."""
        self.cap.check_guard()
        viewports = (viewports,) if isinstance(viewports, (str, dict)) else viewports
        schemes = (schemes,) if isinstance(schemes, str) else schemes
        out = []
        try:
            for spec in viewports:
                for scheme in schemes:
                    vp = self.cap.apply_emulation(viewport=spec, scheme=scheme, reduced_motion=reduced_motion)
                    self.cap.settle(settle)
                    out.append(self._shoot(name, full=full, selector=None, vp=vp, scheme=scheme,
                                           reduced_motion=reduced_motion, note=note))
        finally:
            self.cap.restore_emulation()
        return out

    def _shoot(self, name: str, *, full: bool, selector: str | None, vp: dict[str, Any] | None,
               scheme: str | None, reduced_motion: bool, note: str) -> dict[str, Any]:
        self.cap.check_guard()  # every shot, so a slot marked mid-matrix stops the matrix
        params: dict[str, Any] = {"format": "png"}
        truncated = False
        dsf = float((vp or {}).get("dsf") or 1)
        if selector:
            rect = self.cap.js(_ELEMENT_RECT_SCRIPT % json.dumps(selector))
            if not rect:
                raise ValueError(f"selector not found: {selector}")
            rect = json.loads(rect)
            if rect["width"] < 1 or rect["height"] < 1:
                raise ValueError(f"element has no box: {selector}")
            self.cap.settle(0.2)
            params.update(captureBeyondViewport=True, clip={**rect, "scale": 1})
        elif full:
            size = self.cap.cdp("Page.getLayoutMetrics").get("cssContentSize") or {}
            width, height = float(size.get("width") or 0), float(size.get("height") or 0)
            cap_height = MAX_CAPTURE_DEVICE_PX / dsf
            truncated = height > cap_height
            if width and height:
                params.update(captureBeyondViewport=True,
                              clip={"x": 0, "y": 0, "width": width, "height": min(height, cap_height), "scale": 1})
        result = self.cap.cdp("Page.captureScreenshot", _response_timeout=SHOT_TIMEOUT_S, **params)
        data = base64.b64decode(result.get("data") or "")
        width_px, height_px = png_size(data)
        tag = "-".join(p for p in ((vp or {}).get("name"), scheme, "rm" if reduced_motion else "") if p)
        seq = self._next_seq()
        path = self.dir / f"{seq:03d}-{_slug(name)}{'-' + _slug(tag) if tag else ''}.png"
        _atomic_write(path, data)
        page = self.cap.page_state()
        return self._file_entry(
            "screenshot", path, seq, name=name, mode="element" if selector else ("full" if full else "viewport"),
            selector=selector, viewport=vp, scheme=scheme or "page-default", reduced_motion=reduced_motion,
            width_px=width_px, height_px=height_px, truncated=truncated, note=note, **page)

    # ---- visual diff and baselines ------------------------------------
    def diff(self, name: str, before: Any, after: Any, *, tolerance: int = 16) -> dict[str, Any]:
        """Pixel diff of two PNGs (paths or entries): a diff image plus the changed-pixel ratio."""
        before_p, after_p = Path(_entry_path(before)), Path(_entry_path(after))
        seq = self._next_seq()
        path = self.dir / f"{seq:03d}-diff-{_slug(name)}.png"
        stats = diff_images(before_p, after_p, path, tolerance=tolerance)
        return self._file_entry("diff", path, seq, name=name, before=str(before_p), after=str(after_p), **stats)

    def baseline(self, key: str, entry: dict[str, Any], *, update: bool = False,
                 max_ratio: float = 0.001) -> dict[str, Any]:
        """Compare a screenshot entry with the stored baseline for ``key``.

        The first call (or ``update=True``) stores it. Baselines live outside runs, in
        ``<root>/baselines/<key>/<viewport>-<scheme>.png``, so later runs compare with them.
        """
        vp = (entry.get("viewport") or {}).get("name") or "page"
        variant = f"{_slug(vp)}-{_slug(entry.get('scheme') or 'page-default')}"
        base = self.cap.root / "baselines" / _slug(key, 80) / f"{variant}.png"
        src = Path(entry["path"])
        if update or not base.exists():
            _private_dir(base.parent.parent)
            _private_dir(base.parent)
            _atomic_write(base, src.read_bytes())  # mkstemp: 0600
            return self._add({"kind": "baseline", "name": key, "status": "stored", "baseline": str(base),
                              "sha256": _sha256(base), "source": str(src)})
        result = self.diff(f"{key}-{variant}-vs-baseline", base, src)
        status = "pass" if result["changed_ratio"] <= max_ratio and not result["size_changed"] else "changed"
        return self._add({"kind": "baseline", "name": key, "status": status, "baseline": str(base),
                          "diff": result["path"], "changed_ratio": result["changed_ratio"], "source": str(src)})

    # ---- review kit ----------------------------------------------------
    def audit_a11y(self, name: str = "a11y", *, tags: Any = ("wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"),
                   selector: str | None = None) -> dict[str, Any]:
        """Run axe-core on the current page; writes the slim result JSON and returns a summary."""
        self.cap.check_guard()
        self.cap.inject_axe()
        raw = self.cap.evaluate_isolated(_AXE_RUN_SCRIPT % (json.dumps(list(tags)), json.dumps(selector)),
                                         timeout=120.0)
        data = scrub_axe(json.loads(raw))
        if data.get("error"):
            raise ValueError(data["error"])
        seq = self._next_seq()
        path = self.dir / f"{seq:03d}-{_slug(name)}.json"
        _atomic_write(path, json.dumps(data, indent=2).encode("utf-8"))
        counts: dict[str, int] = {}
        for v in data["violations"]:
            counts[v.get("impact") or "unknown"] = counts.get(v.get("impact") or "unknown", 0) + 1
        top = [f"{v['impact']}: {v['id']} ({v['nodes']}) - {v['help']}" for v in data["violations"][:10]]
        return self._file_entry("a11y", path, seq, name=name, url=data.get("url"), axe_version=data.get("version"),
                                violations=len(data["violations"]), incomplete=len(data["incomplete"]),
                                by_impact=counts, top=top)

    def perf(self, name: str = "perf", *, reload: bool = False, observe_seconds: float = 3.0,
             network: str | None = None, cpu: float | None = None) -> dict[str, Any]:
        """CDP Performance.getMetrics plus LCP/CLS/INP/FCP/TTFB from PerformanceObserver.

        ``reload=True`` measures a fresh load (needed for LCP/FCP under ``network``/``cpu``
        throttling). INP needs real interactions during or before the observation window.
        """
        self.cap.check_guard()
        with self.cap.throttle(network=network, cpu=cpu):
            if reload:
                with contextlib.suppress(Exception):
                    self.cap.cdp("Page.bringToFront")  # hidden tabs never paint: no FCP/LCP
                self.cap.cdp("Page.reload", ignoreCache=True)
                self.cap.wait_for_load()
            self.cap.cdp("Performance.enable")
            try:
                vitals = json.loads(self.cap.js(_VITALS_SCRIPT % int(min(max(observe_seconds, 0.5), 20.0) * 1000)))
                metrics = {m["name"]: m["value"] for m in self.cap.cdp("Performance.getMetrics").get("metrics", [])}
            finally:
                with contextlib.suppress(Exception):
                    self.cap.cdp("Performance.disable")
        ratings = {k: _rating(k, vitals.get(k)) for k in VITALS_THRESHOLDS}
        keep = ("JSHeapUsedSize", "JSHeapTotalSize", "Nodes", "Documents", "LayoutCount", "RecalcStyleCount",
                "LayoutDuration", "RecalcStyleDuration", "ScriptDuration", "TaskDuration", "JSEventListeners")
        data = {"vitals": vitals, "ratings": ratings, "network": network, "cpu": cpu, "reload": reload,
                "metrics": {k: metrics[k] for k in keep if k in metrics}, **self.cap.page_state()}
        seq = self._next_seq()
        path = self.dir / f"{seq:03d}-{_slug(name)}.json"
        _atomic_write(path, json.dumps(data, indent=2).encode("utf-8"))
        return self._file_entry("perf", path, seq, name=name, vitals=vitals, ratings=ratings,
                                network=network, cpu=cpu, url=data.get("url"))

    # ---- video ---------------------------------------------------------
    def record(self, name: str, **kw: Any) -> Recording:
        """Context manager: ``with run.record("signup", max_seconds=30, gif=True): ...``.

        Or call ``rec = run.record(...); rec.start(); ...; rec.stop()`` in the SAME
        browser_exec call: the recorder is a thread of this call and ends with it.
        """
        return Recording(self, name, **kw)

    # ---- storyboard ----------------------------------------------------
    def storyboard(self, title: str | None = None, *, critique: dict[str, str] | None = None,
                   summary: str = "", embed: bool = False) -> dict[str, Any]:
        """Write storyboard.html and storyboard.md from the run index.

        ``critique`` maps heuristic keys (see ``ux_capture.HEURISTICS``) to findings.
        ``embed=True`` inlines images as data URLs so the HTML is one shareable file.
        """
        data = self.index()
        title = title or self.name
        html_text = _storyboard_html(self.dir, data, title, critique or {}, summary, embed)
        md_text = _storyboard_markdown(self.dir, data, title, critique or {}, summary)
        html_path, md_path = self.dir / "storyboard.html", self.dir / "storyboard.md"
        _atomic_write(html_path, html_text.encode("utf-8"))
        _atomic_write(md_path, md_text.encode("utf-8"))
        return {"html": str(html_path), "md": str(md_path), "bytes": html_path.stat().st_size,
                "sha256": _sha256(html_path), "entries": len(data["entries"])}


class Recording:
    """CDP screencast frames to MP4 (and optional GIF) through ffmpeg.

    One recording per capture at a time. Frames and ffmpeg output stay in a 0700
    scratch dir until they are 0600; the dir is removed on stop and on every error.
    The payment guard is re-read for every frame: a slot marked mid-recording drops
    all frames and ``stop()`` raises ``CaptureRefused``.
    """

    def __init__(self, run: Run, name: str, *, max_seconds: float = 60, max_mb: float = 100,
                 gif: bool = False, gif_width: int = 720, max_width: int = 1280, max_height: int = 1280,
                 quality: int = 70, fps: int = 25):
        self.run, self.name = run, name
        self.max_seconds = min(float(max_seconds), RECORD_MAX_SECONDS)
        self.max_bytes = int(min(float(max_mb), RECORD_MAX_MB) * 1024 * 1024)
        self.gif, self.gif_width, self.fps = gif, int(gif_width), int(fps)
        self.params = {"format": "jpeg", "quality": int(quality), "maxWidth": int(max_width),
                       "maxHeight": int(max_height), "everyNthFrame": 1}
        self.frames_dir = run.dir / f".frames-{_slug(name)}-{os.getpid()}"
        self.frames: list[tuple[Path, float]] = []
        self.bytes = 0
        self.stopped_by: str | None = None
        self.refused = False
        self.result: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._other: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self._restore: Callable[[], None] | None = None
        self._screencasting = False

    def __enter__(self) -> Recording:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> Recording:
        cap = self.run.cap
        cap.check_guard()
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is not on PATH; video capture is unavailable")
        if cap.active_recording is not None:
            raise RuntimeError(f"recording {cap.active_recording.name!r} is still running in this call; "
                               "stop it before starting another")
        cap.active_recording = self
        try:
            _private_dir(self.frames_dir)
            self._restore = cap.patch_drain(self._drain_for_caller)
            self._started = time.monotonic()
            # A background tab paints nothing, so it sends no frames.
            with contextlib.suppress(Exception):
                cap.cdp("Page.bringToFront")
            cap.cdp("Page.startScreencast", **self.params)
            self._screencasting = True
            # health: allow HX012 -- harness subprocess: no Hermes profile scope exists here to carry
            self._thread = threading.Thread(target=self._loop, name="ux-capture-screencast", daemon=True)
            self._thread.start()
        except BaseException:
            self._teardown()
            raise
        return self

    def _drop_frames(self) -> None:
        for path, _ in self.frames:
            path.unlink(missing_ok=True)
        self.frames.clear()

    def _take(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            if event.get("method") != "Page.screencastFrame":
                self._other.append(event)
                continue
            params = event.get("params") or {}
            with contextlib.suppress(Exception):
                self.run.cap.cdp("Page.screencastFrameAck", sessionId=params.get("sessionId"))
            if self._stop.is_set() or self.stopped_by:
                continue
            if capture_blocked(self.run.cap.env):
                self.refused, self.stopped_by = True, "payment_guard"
                self._drop_frames()
                continue
            data = base64.b64decode(params.get("data") or "")
            stamp = float((params.get("metadata") or {}).get("timestamp") or time.time())
            path = self.frames_dir / f"{len(self.frames):06d}.jpg"
            _private_write(path, data)
            self.frames.append((path, stamp))
            self.bytes += len(data)
            if self.bytes >= self.max_bytes:
                self.stopped_by = "max_mb"

    def _pump(self) -> None:
        with self._lock:
            self._take(self.run.cap.raw_drain())

    def _drain_for_caller(self) -> list[dict[str, Any]]:
        """Replacement for drain_events() while recording: frames stay ours, the rest is passed on."""
        with self._lock:
            self._take(self.run.cap.raw_drain())
            out, self._other = self._other, []
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            if time.monotonic() - self._started >= self.max_seconds and not self.stopped_by:
                self.stopped_by = "max_seconds"
            with contextlib.suppress(Exception):
                self._pump()
            if self.stopped_by in ("payment_guard", "max_seconds", "max_mb"):
                self._stop_screencast()
            self._stop.wait(0.05)

    def _stop_screencast(self) -> None:
        if self._screencasting:
            self._screencasting = False
            with contextlib.suppress(Exception):
                self.run.cap.cdp("Page.stopScreencast")

    def _teardown(self) -> None:
        """Stop the thread and screencast, restore drain_events, release the capture. Idempotent."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._stop_screencast()
        if self._restore:
            self._restore()
            self._restore = None
        if self.run.cap.active_recording is self:
            self.run.cap.active_recording = None
        if self.refused:
            self._drop_frames()

    def stop(self) -> dict[str, Any]:
        if self.result is not None:
            return self.result
        try:
            with contextlib.suppress(Exception):
                self._pump()
            self._teardown()
            if self.refused or capture_blocked(self.run.cap.env):
                self.refused = True
                self._drop_frames()
                raise CaptureRefused("payment_screenshot_blocked: the slot was payment-marked during the "
                                     "recording; all frames were discarded.")
            self.result = self._encode()
        finally:
            shutil.rmtree(self.frames_dir, ignore_errors=True)
        return self.result

    def _encode(self) -> dict[str, Any]:
        if not self.frames:
            raise RuntimeError("no screencast frames arrived (page idle, hidden tab, or screencast refused)")
        seq = self.run._next_seq()
        listing = self.frames_dir / "frames.txt"
        lines = []
        for i, (path, stamp) in enumerate(self.frames):
            nxt = self.frames[i + 1][1] if i + 1 < len(self.frames) else stamp + 0.5
            lines.append(f"file '{path.name}'\nduration {max(0.02, min(nxt - stamp, 5.0)):.3f}")
        lines.append(f"file '{self.frames[-1][0].name}'")  # concat demuxer drops the last duration
        _private_write(listing, ("\n".join(lines) + "\n").encode("utf-8"))
        # ffmpeg writes 0644 into the 0700 scratch dir; chmod, then move into the run dir.
        tmp_mp4 = self.frames_dir / "out.mp4"
        _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing),
                 "-vf", f"scale=trunc(iw/2)*2:trunc(ih/2)*2,fps={self.fps},format=yuv420p",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-movflags", "+faststart", str(tmp_mp4)])
        extra: dict[str, Any] = {}
        if self.gif:
            tmp_gif = self.frames_dir / "out.gif"
            _ffmpeg(["-i", str(tmp_mp4), "-vf", f"fps=8,scale={self.gif_width}:-1:flags=lanczos,split[a][b];"
                     "[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer", str(tmp_gif)])
            gif = _publish(tmp_gif, self.run.dir / f"{seq:03d}-{_slug(self.name)}.gif")
            extra["gif"] = {"path": str(gif), "bytes": gif.stat().st_size, "sha256": _sha256(gif)}
        mp4 = _publish(tmp_mp4, self.run.dir / f"{seq:03d}-{_slug(self.name)}.mp4")
        duration = self.frames[-1][1] - self.frames[0][1]
        return self.run._file_entry("video", mp4, seq, name=self.name, frames=len(self.frames),
                                    seconds=round(duration, 2), stopped_by=self.stopped_by or "stop",
                                    frame_bytes=self.bytes, **extra, **self.run.cap.page_state())


def _publish(tmp: Path, dest: Path) -> Path:
    tmp.chmod(0o600)
    os.replace(tmp, dest)
    return dest


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=300, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode('utf-8', 'replace')[-600:]}")


def _entry_path(item: Any) -> str:
    return str(item["path"]) if isinstance(item, dict) else str(item)


def diff_images(before: Path, after: Path, out: Path, *, tolerance: int = 16) -> dict[str, Any]:
    """Diff image (after, dimmed, changed pixels red) and the changed-pixel ratio.

    Different sizes compare over the union canvas: area only one image covers counts
    as changed.
    """
    from PIL import Image, ImageChops

    with Image.open(before) as a_img, Image.open(after) as b_img:
        a, b = a_img.convert("RGB"), b_img.convert("RGB")
    width, height = max(a.width, b.width), max(a.height, b.height)
    canvas_a, canvas_b = Image.new("RGB", (width, height), (255, 0, 255)), Image.new("RGB", (width, height), (0, 255, 0))
    canvas_a.paste(a, (0, 0))
    canvas_b.paste(b, (0, 0))
    delta = ImageChops.difference(canvas_a, canvas_b).convert("L")
    mask = delta.point(lambda v: 255 if v > tolerance else 0)
    changed = mask.histogram()[255]
    total = width * height
    overlay = Image.blend(canvas_b, Image.new("RGB", (width, height), (255, 255, 255)), 0.6)
    overlay.paste(Image.new("RGB", (width, height), (230, 0, 40)), (0, 0), mask)
    buf = io.BytesIO()
    overlay.save(buf, format="PNG")
    _atomic_write(out, buf.getvalue())  # mkstemp: 0600
    return {"changed_px": changed, "total_px": total, "changed_ratio": round(changed / total, 6) if total else 0.0,
            "changed_bbox": mask.getbbox(), "size_changed": a.size != b.size,
            "before_size": list(a.size), "after_size": list(b.size), "tolerance": tolerance}


class UXCapture:
    """Bound to one harness call's globals. ``ux_capture.run(name)`` opens a folder."""

    VIEWPORTS = VIEWPORTS
    NETWORK_PRESETS = NETWORK_PRESETS
    HEURISTICS = HEURISTICS
    CaptureRefused = CaptureRefused

    def __init__(self, g: dict[str, Any], env: dict[str, str] | None = None):
        self.g = g
        self.env = dict(os.environ if env is None else env)
        root = self.env.get(ROOT_ENV) or ""
        if not root:
            raise RuntimeError(f"{ROOT_ENV} is unset: ux_capture runs only inside browser_exec")
        self.root = Path(root)
        self._emulated = False
        self._world: tuple[str, str, int] | None = None
        self.active_recording: Recording | None = None

    # ---- plumbing ------------------------------------------------------
    def cdp(self, method: str, **params: Any) -> dict[str, Any]:
        return self.g["cdp"](method, **params) or {}

    def js(self, expression: str) -> Any:
        return self.g["js"](expression)

    def raw_drain(self) -> list[dict[str, Any]]:
        return list(self._orig_drain() or [])

    def _orig_drain(self) -> Any:
        fn = getattr(self, "_saved_drain", None) or self.g["drain_events"]
        return fn()

    def patch_drain(self, replacement: Callable[[], list[dict[str, Any]]]) -> Callable[[], None]:
        """Route drain_events() (ours and helpers.wait_for_network_idle's) through ``replacement``."""
        import sys

        helpers = sys.modules.get("browser_harness.helpers")
        self._saved_drain = getattr(helpers, "drain_events", None) or self.g["drain_events"]
        saved_global, saved_helper = self.g.get("drain_events"), getattr(helpers, "drain_events", None)
        self.g["drain_events"] = replacement
        if helpers is not None:
            helpers.drain_events = replacement

        def restore() -> None:
            self.g["drain_events"] = saved_global
            if helpers is not None and saved_helper is not None:
                helpers.drain_events = saved_helper
            self._saved_drain = None

        return restore

    def wait_for_load(self, timeout: float = 30.0) -> None:
        fn = self.g.get("wait_for_load")
        if fn:
            fn(timeout)

    def reset_slot(self) -> list[str]:
        """Clear emulation, throttling and screencast; drop dead recordings' frame dirs."""
        for method, params in RESET_CALLS:
            with contextlib.suppress(Exception):
                self.cdp(method, **params)
        self._emulated = False
        with contextlib.suppress(OSError):
            _private_dir(self.root)
            return remove_stale_frames(self.root)
        return []

    def check_guard(self) -> None:
        if capture_blocked(self.env):
            raise CaptureRefused("payment_screenshot_blocked: this slot held a card fill; capture refused. "
                                 "Use a research slot that never held the payment page.")

    def settle(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)
        with contextlib.suppress(Exception):
            self.js(_SETTLE_SCRIPT)

    def page_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.js(_PAGE_SCRIPT))
        except (RuntimeError, OSError, ValueError, TypeError):  # page state is decoration, never fatal
            return {}

    def run(self, name: str | None = None) -> Run:
        """Open (or reopen) a run folder; the default name is today's date plus the task."""
        if not name:
            name = f"{_dt.date.today().isoformat()}-{_slug(self.env.get(TASK_ENV) or 'task', 40)}"
        return Run(self, name)

    # ---- emulation -----------------------------------------------------
    def apply_emulation(self, *, viewport: Any = None, scheme: str | None = None,
                        reduced_motion: bool = False) -> dict[str, Any] | None:
        vp = parse_viewport(viewport) if viewport else None
        features = []
        if scheme:
            if scheme not in ("light", "dark"):
                raise ValueError("scheme must be 'light' or 'dark'")
            features.append({"name": "prefers-color-scheme", "value": scheme})
        if reduced_motion:
            features.append({"name": "prefers-reduced-motion", "value": "reduce"})
        # Set before the first call: a later failing call must still leave a restore to do.
        self._emulated = self._emulated or bool(vp or features)
        if vp:
            self.cdp("Emulation.setDeviceMetricsOverride", width=vp["width"], height=vp["height"],
                     deviceScaleFactor=vp["dsf"], mobile=bool(vp["mobile"]),
                     screenWidth=vp["width"], screenHeight=vp["height"])
            self.cdp("Emulation.setTouchEmulationEnabled", enabled=bool(vp["mobile"]), maxTouchPoints=5)
        if features:
            self.cdp("Emulation.setEmulatedMedia", features=features)
        return vp

    def restore_emulation(self) -> None:
        if not self._emulated:
            return
        for method, params in (("Emulation.clearDeviceMetricsOverride", {}),
                               ("Emulation.setTouchEmulationEnabled", {"enabled": False}),
                               ("Emulation.setEmulatedMedia", {"media": "", "features": []})):
            with contextlib.suppress(Exception):
                self.cdp(method, **params)
        self._emulated = False

    @contextlib.contextmanager
    def emulate(self, *, viewport: Any = None, scheme: str | None = None, reduced_motion: bool = False,
                settle: float = 0.5) -> Iterator[dict[str, Any] | None]:
        """Apply viewport / color scheme / reduced motion, restore on exit."""
        if not (viewport or scheme or reduced_motion):
            yield None
            return
        try:
            vp = self.apply_emulation(viewport=viewport, scheme=scheme, reduced_motion=reduced_motion)
            self.settle(settle)
            yield vp
        finally:
            self.restore_emulation()

    @contextlib.contextmanager
    def throttle(self, *, network: str | None = None, cpu: float | None = None) -> Iterator[None]:
        """Network preset (``NETWORK_PRESETS``) and/or CPU slowdown factor, restored on exit."""
        if network and network not in NETWORK_PRESETS:
            raise ValueError(f"unknown network preset {network!r}: {sorted(NETWORK_PRESETS)}")
        try:
            if network:
                p = NETWORK_PRESETS[network]
                self.cdp("Network.enable")
                self.cdp("Network.emulateNetworkConditions", offline=bool(p.get("offline")),
                         latency=p.get("latency", 0),
                         downloadThroughput=p["down_kbps"] * 125 if "down_kbps" in p else -1,
                         uploadThroughput=p["up_kbps"] * 125 if "up_kbps" in p else -1)
            if cpu:
                self.cdp("Emulation.setCPUThrottlingRate", rate=max(1.0, float(cpu)))
            yield
        finally:
            if network:
                with contextlib.suppress(Exception):
                    self.cdp("Network.emulateNetworkConditions", offline=False, latency=0,
                             downloadThroughput=-1, uploadThroughput=-1)
            if cpu:
                with contextlib.suppress(Exception):
                    self.cdp("Emulation.setCPUThrottlingRate", rate=1)

    # ---- isolated world + axe --------------------------------------------
    def _isolated_context(self) -> int:
        """Isolated world for the main frame's current document (keyed on frame id + loaderId)."""
        frame = self.cdp("Page.getFrameTree")["frameTree"]["frame"]
        key = (str(frame["id"]), str(frame.get("loaderId") or ""))
        if self._world and self._world[:2] == key:
            return self._world[2]
        ctx = self.cdp("Page.createIsolatedWorld", frameId=key[0], worldName="hermes-ux-capture",
                       grantUniveralAccess=True)["executionContextId"]
        self._world = (*key, ctx)
        return ctx

    def evaluate_isolated(self, expression: str, *, timeout: float = 30.0) -> Any:
        for attempt in (1, 2):
            try:
                res = self.cdp("Runtime.evaluate", expression=expression, contextId=self._isolated_context(),
                               returnByValue=True, awaitPromise=True, _response_timeout=timeout)
            except RuntimeError as exc:
                # The page navigated and our cached world went with the old document.
                if attempt == 1 and _STALE_CONTEXT_RE.search(str(exc)):
                    self._world = None
                    continue
                raise
            break
        if res.get("exceptionDetails"):
            detail = res["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text") or "error"
            raise RuntimeError(f"isolated evaluate failed: {str(text)[:400]}")
        return (res.get("result") or {}).get("value")

    def inject_axe(self) -> None:
        """Load the pinned axe-core into an isolated world (page globals untouched)."""
        path = Path(self.env.get(AXE_PATH_ENV) or AXE_DEFAULT_PATH)
        try:
            source = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"axe-core not installed at {path}: {exc}") from exc
        if hashlib.sha256(source).hexdigest() != (self.env.get(AXE_PATH_ENV + "_SHA256") or AXE_SHA256):
            raise RuntimeError(f"axe-core at {path} does not match the pinned sha256 (axe {AXE_VERSION})")
        if self.evaluate_isolated("typeof axe !== 'undefined' && typeof axe.run === 'function'"):
            return
        text = source.decode("utf-8")
        self.evaluate_isolated("globalThis.__hermesAxe = ''")
        for chunk in js_chunks(text):
            self.evaluate_isolated("globalThis.__hermesAxe += " + chunk)
        self.evaluate_isolated("(0, eval)(globalThis.__hermesAxe); delete globalThis.__hermesAxe; typeof axe")


_STALE_CONTEXT_RE = re.compile(r"Cannot find (?:default )?(?:execution )?context|context with specified id"
                               r"|Execution context was destroyed", re.IGNORECASE)


def js_chunks(text: str, limit: int = IPC_LINE_BUDGET) -> Iterator[str]:
    """JSON string literals of ``text`` whose IPC request line (escaped twice) stays under ``limit``."""
    i = 0
    while i < len(text):
        size = JS_CHUNK_CHARS
        while True:
            literal = json.dumps(text[i:i + size])
            if len(json.dumps(literal)) < limit or size <= 256:
                break
            size //= 2
        yield literal
        i += size


# Every override ux_capture can leave on the tab. They live in the harness daemon's CDP session,
# which outlives a killed call, so they are cleared on every bind and after a failed call.
RESET_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("Page.stopScreencast", {}),
    ("Emulation.clearDeviceMetricsOverride", {}),
    ("Emulation.setTouchEmulationEnabled", {"enabled": False}),
    ("Emulation.setEmulatedMedia", {"media": "", "features": []}),
    ("Emulation.setCPUThrottlingRate", {"rate": 1}),
    ("Network.emulateNetworkConditions", {"offline": False, "latency": 0,
                                          "downloadThroughput": -1, "uploadThroughput": -1}),
)


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return False  # a dir named for this process predates it (pid reuse): nothing records yet
    try:
        os.kill(pid, 0)  # windows-footgun: ok — signal 0 is an existence probe, never delivered
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, owned by someone else
    return True


def remove_stale_frames(root: Path) -> list[str]:
    """Delete ``<root>/<run>/.frames-*-<pid>`` dirs whose recording process is gone."""
    removed = []
    for d in root.glob("*/.frames-*"):
        tail = d.name.rsplit("-", 1)[-1]
        if d.is_dir() and not (tail.isdigit() and _pid_alive(int(tail))):
            shutil.rmtree(d, ignore_errors=True)
            removed.append(str(d))
    return removed


def bind(g: dict[str, Any]) -> UXCapture:
    """Bind ``ux_capture`` for this call; the tab starts with no ux_capture override left on it."""
    cap = UXCapture(g)
    cap.reset_slot()
    return cap


# ---- index / storyboard rendering ------------------------------------------

def _entry_caption(e: dict[str, Any]) -> str:
    vp = e.get("viewport") or {}
    bits = [e.get("name") or e.get("kind")]
    if vp:
        bits.append(f"{vp.get('name')} {vp.get('width')}x{vp.get('height')}@{vp.get('dsf')}")
    if e.get("scheme") and e.get("scheme") != "page-default":
        bits.append(e["scheme"])
    if e.get("reduced_motion"):
        bits.append("reduced motion")
    return " · ".join(str(b) for b in bits if b)


def _pct(ratio: Any) -> str:
    return f"{float(ratio or 0) * 100:.3f}%"


def _facts_screenshot(e: dict[str, Any]) -> list[str]:
    out = [f"{e.get('mode')} {e.get('width_px')}x{e.get('height_px')}px{' (truncated)' if e.get('truncated') else ''}"]
    if e.get("scroll_width") and e.get("client_width") and e["scroll_width"] > e["client_width"]:
        out.append(f"horizontal overflow: scrollWidth {e['scroll_width']} > clientWidth {e['client_width']}")
    return out


def _facts_a11y(e: dict[str, Any]) -> list[str]:
    return [f"axe {e.get('axe_version')}: {e.get('violations')} violations {e.get('by_impact')}, "
            f"{e.get('incomplete')} need review", *(e.get("top") or [])]


def _facts_perf(e: dict[str, Any]) -> list[str]:
    v, r = e.get("vitals") or {}, e.get("ratings") or {}
    out = [", ".join(f"{k}={round(v[k], 3) if isinstance(v.get(k), float) else v.get(k)} ({r.get(k)})"
                     for k in VITALS_THRESHOLDS if k in v)]
    if e.get("network") or e.get("cpu"):
        out.append(f"throttled: network={e.get('network')} cpu={e.get('cpu')}")
    return out


_FACTS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "screenshot": _facts_screenshot,
    "a11y": _facts_a11y,
    "perf": _facts_perf,
    "diff": lambda e: [f"changed {_pct(e.get('changed_ratio'))} ({e.get('changed_px')} px)"
                       f"{', size changed' if e.get('size_changed') else ''}"],
    "baseline": lambda e: [f"baseline {e.get('status')}" + (f", changed {_pct(e.get('changed_ratio'))}"
                                                           if e.get("changed_ratio") is not None else "")],
    "video": lambda e: [f"{e.get('seconds')}s, {e.get('frames')} frames, stopped by {e.get('stopped_by')}"],
}


def _entry_facts(e: dict[str, Any]) -> list[str]:
    facts = _FACTS.get(str(e.get("kind")), lambda _e: [])(e)
    return facts + ([str(e["url"])] if e.get("url") else [])


def _index_markdown(data: dict[str, Any]) -> str:
    lines = [f"# UX capture run `{data.get('run')}`", "", f"Created {data.get('created')}; updated {data.get('updated')}.",
             "", "| # | kind | what | file | bytes | sha256 |", "|---|---|---|---|---|---|"]
    for e in data["entries"]:
        what = _entry_caption(e) if e.get("kind") != "note" else str(e.get("text", ""))[:80]
        lines.append(f"| {e['seq']} | {e.get('kind')} | {what.replace('|', '/')} | {e.get('file', '')} | "
                     f"{e.get('bytes', '')} | {str(e.get('sha256', ''))[:12]} |")
    return "\n".join(lines) + "\n"


def _img_src(run_dir: Path, path: str, embed: bool) -> str:
    p = Path(path)
    if embed and p.suffix.lower() in (".png", ".gif", ".jpg", ".jpeg") and p.exists():
        mime = "image/gif" if p.suffix.lower() == ".gif" else ("image/png" if p.suffix.lower() == ".png" else "image/jpeg")
        return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode('ascii')}"
    return html.escape(_rel(run_dir, path))


def _rel(run_dir: Path, path: str) -> str:
    p = Path(path)
    try:
        return p.relative_to(run_dir).as_posix()
    except ValueError:
        return p.as_uri() if p.is_absolute() else p.as_posix()


def _critique_rows(critique: dict[str, str]) -> list[tuple[str, str, str]]:
    known = [(k, label, critique.get(k, "")) for k, label in HEURISTICS]
    extra = [(k, k, v) for k, v in critique.items() if k not in dict(HEURISTICS)]
    return known + extra


_STORY_CSS = """
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#57534e;--card:#fff;--line:#e7e5e4;--bad:#b91c1c;--ok:#15803d}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--fg:#f5f5f4;--muted:#a8a29e;--card:#292524;--line:#44403c;--bad:#f87171;--ok:#4ade80}}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:1200px;margin:0 auto}h1{font-size:24px;margin:0 0 4px}.muted{color:var(--muted)}
.step{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:16px 0}
.step h2{font-size:16px;margin:0 0 8px}.step img,.step video{max-width:100%;max-height:80vh;border:1px solid var(--line);border-radius:6px}
.step ul{margin:8px 0 0;padding-left:20px}.note{border-left:3px solid var(--muted)}
table{border-collapse:collapse;width:100%}td,th{border-top:1px solid var(--line);padding:6px;text-align:left;vertical-align:top}
"""


def _storyboard_html(run_dir: Path, data: dict[str, Any], title: str, critique: dict[str, str],
                     summary: str, embed: bool) -> str:
    esc = html.escape
    parts = ["<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
             f"<title>{esc(title)}</title><style>{_STORY_CSS}</style><main><h1>{esc(title)}</h1>",
             f"<p class=muted>Run {esc(str(data.get('run')))} · {len(data['entries'])} steps · generated {esc(_now())}</p>"]
    if summary:
        parts.append(f"<p>{esc(summary)}</p>")
    for e in data["entries"]:
        kind = e.get("kind")
        if kind == "note":
            parts.append(f"<section class='step note'><p>{esc(str(e.get('text', '')))}</p></section>")
            continue
        parts.append(f"<section class=step><h2>{e['seq']}. {esc(_entry_caption(e))}</h2>")
        if kind in ("screenshot", "diff"):
            parts.append(f"<img loading=lazy alt='{esc(_entry_caption(e))}' src='{_img_src(run_dir, e['path'], embed)}'>")
        elif kind == "baseline" and e.get("diff"):
            parts.append(f"<img loading=lazy alt='baseline diff' src='{_img_src(run_dir, e['diff'], embed)}'>")
        elif kind == "video":
            if (e.get("gif") or {}).get("path"):
                parts.append(f"<img loading=lazy alt='flow' src='{_img_src(run_dir, e['gif']['path'], embed)}'>")
            parts.append(f"<p><a href='{_img_src(run_dir, e['path'], False)}'>MP4 ({e.get('bytes')} bytes)</a></p>")
        facts = _entry_facts(e)
        if e.get("note"):
            facts.insert(0, str(e["note"]))
        if facts:
            parts.append("<ul>" + "".join(f"<li>{esc(str(f))}</li>" for f in facts) + "</ul>")
        parts.append("</section>")
    rows = _critique_rows(critique)
    if any(v for _, _, v in rows):
        parts.append("<section class=step><h2>Heuristic critique</h2><table><tr><th>Heuristic</th><th>Finding</th></tr>")
        parts += [f"<tr><td>{esc(label)}</td><td>{esc(v) or '<span class=muted>not reviewed</span>'}</td></tr>"
                  for _, label, v in rows]
        parts.append("</table></section>")
    parts.append("</main></html>")
    return "\n".join(parts)


def _storyboard_markdown(run_dir: Path, data: dict[str, Any], title: str, critique: dict[str, str], summary: str) -> str:
    lines = [f"# {title}", "", f"Run `{data.get('run')}`, {len(data['entries'])} steps.", ""]
    if summary:
        lines += [summary, ""]
    for e in data["entries"]:
        if e.get("kind") == "note":
            lines += [f"> {e.get('text', '')}", ""]
            continue
        lines.append(f"## {e['seq']}. {_entry_caption(e)}")
        target = e.get("diff") if e.get("kind") == "baseline" else (e.get("gif") or {}).get("path") or e.get("path")
        if target and str(target).lower().endswith((".png", ".gif")):
            lines.append(f"![{_entry_caption(e)}]({_rel(run_dir, str(target))})")
        elif e.get("file"):
            lines.append(f"File: `{e['file']}`")
        if e.get("note"):
            lines.append(f"Note: {e['note']}")
        lines += [f"- {f}" for f in _entry_facts(e)] + [""]
    rows = _critique_rows(critique)
    if any(v for _, _, v in rows):
        lines += ["## Heuristic critique", "", "| Heuristic | Finding |", "|---|---|"]
        lines += [f"| {label} | {(v or 'not reviewed').replace('|', '/')} |" for _, label, v in rows]
    return "\n".join(lines) + "\n"
