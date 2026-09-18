"""Bounded helper provenance for persistent-kernel cells (:mod:`tools.approval`).

In gateway smart mode the guardian sees only the CURRENT cell. A call to a helper
defined in an earlier cell reaches it as an opaque name, so a benign
``ThreadPoolExecutor(3).map(scan, public_urls)`` is judged without the one fact that
matters: what ``scan`` does. This module restores exactly that fact, without
evaluating anything:

- Host-side registry, keyed by approval session. The host already sees every cell's
  source (``execute_code``/``execute_in_session_kernel``), so definitions are parsed
  with :mod:`ast` — the cell is never run to learn what it defines, and nothing is
  introspected in the child interpreter.
- ``record_cell``: one prior cell -> top-level ``def``/``class``/assignments. Old
  definitions of a name are RETAINED (marked superseded) rather than dropped:
  shadowing must not let a dangerous old helper look clean because a benign one
  later reused its name.
- ``classify_name``: conservative membership test. ``UNDEFINED`` (never defined in
  the session) vs ``SUPERSEDED`` (defined more than once / possibly re-bound) vs
  ``MUTATED`` (re-assigned in the current cell before use) vs ``KNOWN``. Alias and
  attribute writes (``x = obj.attr = f``) mark BOTH names, conservatively.
- ``describe_name``: bounded, redacted definition/effect provenance for the guardian
  prompt. Comments and docstrings are STRIPPED (untrusted text stays out of the
  trusted system prompt channel); effect markers (open/write/subprocess/requests/
  socket/eval/exec...) are enumerated and capped; the body is size-capped with an
  explicit truncation marker; the whole description is force-redacted.

Trust boundary: cell source is UNTRUSTED data. Only its structural role (a def
exists, its body mentions these calls) is reported — as provenance for a human or
guardian decision, never as authorization. Nothing here approves anything; the
verdict stays with the existing gate.
"""

from __future__ import annotations

import ast
import logging
import threading
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Bounded state: a long-lived gateway session cannot grow this unbounded.
_MAX_SESSIONS = 64
_MAX_DEFS_PER_SESSION = 64
_MAX_DEF_SOURCE_CHARS = 700
_MAX_BODY_CHARS = 400
_MAX_MARKER_CHARS = 200

# Effects that decide what a helper can DO. Name-based, deliberately coarse — this is
# provenance for a reviewer, not a sandbox. Matched as call function names or dotted
# roots anywhere in the def's body.
_EFFECT_CALLS: Dict[str, str] = {
    "open": "file open", "exec": "dynamic code exec", "eval": "dynamic code eval",
    "compile": "dynamic code compile", "input": "interactive input",
    "subprocess": "subprocess spawn", "os.system": "shell execution",
    "os.popen": "shell execution", "shutil.rmtree": "recursive delete",
    "os.remove": "file delete", "os.unlink": "file delete",
    "requests": "HTTP client", "urllib.request": "HTTP client", "httpx": "HTTP client",
    "socket": "raw network", "http.client": "HTTP client",
}
# Attribute/method reads that themselves signal network egress or code loading.
_EFFECT_ATTRS: Dict[str, str] = {
    "urlopen": "HTTP fetch", "urllib.request.urlopen": "HTTP fetch",
    "getenv": "env read", "environ.get": "env read",
}


class _DefRecord:
    __slots__ = ("name", "kind", "source", "args", "markers", "cell_index", "superseded")

    def __init__(self, name: str, kind: str, source: str, args: str,
                 markers: List[str], cell_index: int) -> None:
        self.name = name
        self.kind = kind
        self.source = source
        self.args = args
        self.markers = markers
        self.cell_index = cell_index
        self.superseded = False


class _SessionDefs:
    __slots__ = ("defs", "cell_count", "order")

    def __init__(self) -> None:
        self.defs: Dict[str, List[_DefRecord]] = {}
        self.cell_count = 0
        self.order: List[str] = []


_sessions: Dict[str, _SessionDefs] = {}
_lock = threading.Lock()


def forget_session(session_key: str) -> None:
    """Drop all recorded definitions for *session_key* (called when its kernel dies)."""
    with _lock:
        _sessions.pop(session_key, None)


def _get_session(session_key: str) -> _SessionDefs:
    state = _sessions.get(session_key)
    if state is None:
        if len(_sessions) >= _MAX_SESSIONS:
            _sessions.pop(next(iter(_sessions)), None)
        state = _sessions[session_key] = _SessionDefs()
    return state


def record_cell(session_key: str, code: str) -> None:
    """Record the top-level definitions of one executed cell (AST-only; never raises).

    Called by the host AFTER a cell settles (or as part of the approval scan — the
    analysis is static either way). Prior definitions of a name are retained and
    marked superseded, so shadowing cannot launder an old definition.
    """
    if not session_key or not code or not code.strip():
        return
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return  # unparsable fragment: record nothing, stay conservative
    with _lock:
        state = _get_session(session_key)
        state.cell_count += 1
        index = state.cell_count
        names_in_cell: List[str] = []
        for node in tree.body:
            records: List[_DefRecord] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(node, ast.ClassDef) else "def"
                args = _format_args(node)
                markers = _scan_effects(node)
                source = _format_source(node, code)
                records.append(_DefRecord(node.name, kind, source, args, markers, index))
                names_in_cell.append(node.name)
            elif isinstance(node, ast.Assign) or isinstance(node, ast.AnnAssign):
                targets = (node.targets if isinstance(node, ast.Assign) else [node.target])
                for target in targets:
                    for name in _collect_target_names(target):
                        # Alias/attribute writes mark BOTH the target and the value's
                        # name: `handler = scan` re-binds `handler` wherever `scan` is.
                        value_name = _value_root_name(node.value)
                        for bound in filter(None, {name, value_name}):
                            records.append(_DefRecord(
                                bound, "assign", "", "", _scan_effects(node), index))
                            names_in_cell.append(bound)
            for record in records:
                bucket = state.defs.setdefault(record.name, [])
                for old in bucket:
                    old.superseded = True
                bucket.append(record)
                if len(bucket) > _MAX_DEFS_PER_SESSION:
                    del bucket[0]
                if record.name not in state.order:
                    state.order.append(record.name)
                    if len(state.order) > _MAX_DEFS_PER_SESSION:
                        forgotten = state.order.pop(0)
                        state.defs.pop(forgotten, None)
    return


def _format_args(node) -> str:
    try:
        return ", ".join(a.arg for a in node.args.args) if hasattr(node, "args") else ""
    except Exception:
        return ""


def _format_source(node, code: str) -> str:
    """Cleaned definition body: comments and docstrings stripped (untrusted prose must not
    reach the trusted prompt channel), whitespace-collapsed, bounded with an explicit
    marker. Computed ONCE at record time from the parseable source segment."""
    try:
        text = ast.get_source_segment(code, node) or ""
    except Exception:
        return ""
    text = _strip_comments_and_docstrings(text)
    text = " ".join(text.split())
    if len(text) > _MAX_DEF_SOURCE_CHARS:
        text = text[:_MAX_DEF_SOURCE_CHARS] + " …[truncated]"
    return text


def _collect_target_names(target: ast.AST) -> List[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: List[str] = []
        for element in target.elts:
            names.extend(_collect_target_names(element))
        return names
    if isinstance(target, ast.Starred):
        return _collect_target_names(target.value)
    if isinstance(target, ast.Attribute):
        # `x.y = scan` re-binds the attribute; conservatively mark the root name.
        root = target
        while isinstance(root, ast.Attribute):
            root = root.value
        return [root.id] if isinstance(root, ast.Name) else []
    if isinstance(target, ast.Subscript):
        return []
    return []


def _value_root_name(value: Optional[ast.AST]) -> Optional[str]:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        return value.func.id
    return None


def _scan_effects(node: ast.AST) -> List[str]:
    """Names of effect-bearing calls/dotted reads inside *node*'s body, bounded."""
    markers: List[str] = []
    seen = set()
    for sub in ast.walk(node):
        dotted = None
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                dotted = func.id
            elif isinstance(func, ast.Attribute):
                parts = []
                cursor = func
                while isinstance(cursor, ast.Attribute):
                    parts.append(cursor.attr)
                    cursor = cursor.value
                if isinstance(cursor, ast.Name):
                    parts.append(cursor.id)
                    dotted = ".".join(reversed(parts))
        elif isinstance(sub, ast.Attribute):
            parts = []
            cursor = sub
            while isinstance(cursor, ast.Attribute):
                parts.append(cursor.attr)
                cursor = cursor.value
            if isinstance(cursor, ast.Name):
                parts.append(cursor.id)
                dotted = ".".join(reversed(parts))
        if dotted:
            label = _EFFECT_CALLS.get(dotted) or _EFFECT_ATTRS.get(dotted.split(".")[-1]) \
                or _EFFECT_CALLS.get(dotted.split(".")[-1])
            if label and label not in seen:
                seen.add(label)
                markers.append(label)
            if len(markers) >= 6:
                break
    return markers


UNKNOWN = "unknown"
KNOWN = "known"
SUPERSEDED = "superseded"
MUTATED = "mutated"


def classify_name(session_key: str, name: str) -> str:
    """``unknown`` | ``known`` | ``superseded`` | ``mutated`` for one name in the session.

    ``mutated`` is detected against the CURRENT cell by
    :func:`names_rebound_in_current_cell`; this function alone reports session history.
    """
    with _lock:
        state = _sessions.get(session_key)
        if state is None or name not in state.defs:
            return UNKNOWN
        bucket = state.defs[name]
        if len(bucket) > 1 or bucket[-1].superseded:
            return SUPERSEDED
        return KNOWN


def names_rebound_in_current_cell(code: str, names: List[str]) -> set:
    """Names from *names* that *code* re-binds before any use — conservative:
    any top-level assignment to a name counts as a possible rebinding."""
    rebound: set = set()
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return rebound
    wanted = set(names)
    for node in tree.body:
        if isinstance(node, ast.Assign) or isinstance(node, ast.AnnAssign):
            targets = (node.targets if isinstance(node, ast.Assign) else [node.target])
            for target in targets:
                for bound in _collect_target_names(target):
                    if bound in wanted:
                        rebound.add(bound)
    return rebound


def _module_level_undefined(tree: ast.Module) -> List[str]:
    """Names read at module level before any assignment in the same cell, ordered.

    Conservative approximation of "used but not defined here": a top-level call to
    ``Name`` not previously bound in THIS cell. Builtins/imports from earlier cells
    are filtered by the caller. Bound the walk by construction (top-level only).
    """
    defined: set = set()
    ordered_uses: List[str] = []
    seen = set()

    def visit_reads(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                if child.id not in defined and child.id not in seen:
                    seen.add(child.id)
                    ordered_uses.append(child.id)
            visit_reads(child)

    for node in tree.body:
        visit_reads(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign) or isinstance(node, ast.AnnAssign):
            targets = (node.targets if isinstance(node, ast.Assign) else [node.target])
            for target in targets:
                defined.update(_collect_target_names(target))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                defined.add((alias.asname or alias.name).split(".")[0])
    return ordered_uses


def undefined_names_in_cell(code: str) -> List[str]:
    """Names the cell uses at top level without defining, in first-use order (AST-only)."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return []
    return _module_level_undefined(tree)


def describe_name(session_key: str, name: str, code: str) -> Optional[str]:
    """Bounded, redacted provenance text for *name*, or ``None`` when unknown.

    Rendered for the approval gate's observer payload / guardian context: source
    with comments and docstrings stripped, effect markers, definition kind, and an
    explicit superseded marker. Never evaluates the helper.
    """
    with _lock:
        state = _sessions.get(session_key)
        bucket = list(state.defs.get(name, ())) if state else []
    if not bucket:
        return None
    parts: List[str] = []
    for record in bucket[-2:]:  # bounded: latest two definitions at most
        head = f"{record.kind} {name}({record.args}) — defined in cell #{record.cell_index}"
        if record.superseded:
            head += " (SUPERSEDED by a later definition: do not trust this one blindly)"
        if record.markers:
            head += "; effects: " + ", ".join(record.markers)
        # record.source is already comment/docstring-stripped, whitespace-collapsed, and
        # size-bounded at record time — never re-parsed here.
        body = record.source
        if len(body) > _MAX_BODY_CHARS:
            body = body[:_MAX_BODY_CHARS] + " …[truncated]"
        parts.append(f"{head}\n  {body}" if body else head)
    if len(bucket) > 2:
        parts.append(f"(+{len(bucket) - 2} earlier definition(s) omitted)")
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text("\n".join(parts), force=True)[:_MAX_MARKER_CHARS + 900]
    except Exception:
        return "\n".join(parts)[:_MAX_MARKER_CHARS + 900]


def _strip_inline_comment(line: str) -> str:
    """Remove one ``# ...`` tail from a code line, quote-aware (``x = '# not a comment'``
    survives). Line numbers stay untouched, so docstring spans remain valid."""
    in_single = in_double = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and in_double and i + 1 < len(line):
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i].rstrip()
        i += 1
    return line


def _strip_comments_and_docstrings(source: str) -> str:
    """Comment/docstring-free source: untrusted prose must not enter the trusted prompt."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ""
    lines = source.splitlines()
    keep: List[str] = []
    docstring_spans: List[tuple] = []

    class _DocstringFinder(ast.NodeVisitor):
        def _visit_body(self, node) -> None:
            body = getattr(node, "body", None)
            if body and len(body) > 0 and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                span = (body[0].lineno, body[0].end_lineno)
                if None not in span:
                    docstring_spans.append(span)
            for child in ast.iter_child_nodes(node):
                self.visit(child)

        visit_FunctionDef = _visit_body
        visit_AsyncFunctionDef = _visit_body
        visit_ClassDef = _visit_body
        visit_Module = _visit_body

    _DocstringFinder().visit(tree)
    doc_lines = set()
    for start, end in docstring_spans:
        doc_lines.update(range(start, end + 1))
    for number, line in enumerate(lines, start=1):
        if number in doc_lines:
            continue
        stripped = _strip_inline_comment(line.rstrip())
        if not stripped:
            continue
        keep.append(stripped)
    return "\n".join(keep)
