"""Deterministic safe-operation path for ``execute_code`` (HER-193).

A narrow, statically checked class of cells skips the whole-script approval gate
(guardian LLM and human prompt). Everything outside the class keeps the existing gate.

Soundness rests on two things:

1. **The cell grammar** (:func:`classify_cell`), an allowlist visitor that fails closed.
   A safe cell cannot import anything except named read/research stubs from the kernel's
   generated ``hermes_tools`` module. It cannot define functions, lambdas or classes, touch
   any underscore name or attribute, call a builtin outside a small value whitelist, call a
   method outside a fixed method-name allowlist (no ``format``/``format_map``), store
   attributes, delete, raise, or bind any builtin, tool or underscore name. The grammar
   admits three effects: sandbox read tools (``read_file``/``search_files``, each with its
   own policy); sandbox web tools (``web_search``/``web_extract``) with literal-only
   arguments and never in the same cell as a read tool; and exclusive-create writes of a
   literal plain file name in the kernel's working directory.

2. **Clean lineage.** A persistent kernel keeps state between cells, so a safe-looking
   ``len(x)`` is only safe if no earlier cell could have rebound ``len``, patched a module,
   or planted an object with hostile dunder methods. We do not try to reconstruct the
   namespace. Any cell outside the grammar taints its approval session when the guard sees
   it, before it can run. The kernel also taints itself, under its cell lock, when it runs
   a non-grammar cell. A cell is auto-approved only while the session is untainted, and the
   kernel refuses a safe-path admission if its own namespace is tainted. A clean-lineage
   namespace therefore holds only builtin-typed data, closed file handles and the real tool
   stubs, and there is nothing to poison.

Operator off-switch: ``approvals.safe_path: false`` in config.yaml.
"""

from __future__ import annotations

import ast
import builtins
import re
import threading
from typing import Optional

MAX_CODE_CHARS = 20_000
MAX_NODES = 4_000
MAX_INT_LITERAL = 10_000_000
MAX_WRITES = 16

# Builtins a safe cell may load. Value-only: nothing that reaches namespaces, attributes,
# code objects, I/O (``open`` is handled by the dedicated write grammar) or dunder hooks.
SAFE_BUILTINS = frozenset({
    "abs", "all", "any", "bin", "bool", "chr", "dict", "divmod", "enumerate", "filter",
    "float", "frozenset", "hex", "int", "isinstance", "len", "list", "map", "max", "min",
    "oct", "ord", "print", "range", "repr", "reversed", "round", "set", "sorted", "str",
    "sum", "tuple", "zip",
})

# Every builtin name known to the host interpreter plus the site-injected ones. A load
# of any of these outside SAFE_BUILTINS rejects; binding any of them rejects.
_ALL_BUILTINS = frozenset(dir(builtins)) | {"exit", "quit", "help", "copyright", "credits", "license"}

READ_TOOLS = frozenset({"read_file", "search_files"})
WEB_TOOLS = frozenset({"web_search", "web_extract"})
HELPERS = frozenset({"json_parse", "shell_quote"})
_IMPORTABLE = READ_TOOLS | WEB_TOOLS | HELPERS
_RESERVED = _ALL_BUILTINS | _IMPORTABLE | {"hermes_tools", "open"}

# Methods of str/list/dict/set/tuple/int/float that neither run user code nor reach
# attributes (so no ``format``/``format_map``, which resolve ``{0.__class__}``).
SAFE_METHODS = frozenset({
    # str
    "capitalize", "casefold", "center", "count", "endswith", "expandtabs", "find",
    "index", "isalnum", "isalpha", "isdecimal", "isdigit", "islower", "isnumeric",
    "isspace", "istitle", "isupper", "join", "ljust", "lower", "lstrip", "partition",
    "removeprefix", "removesuffix", "replace", "rfind", "rindex", "rjust", "rpartition",
    "rsplit", "rstrip", "split", "splitlines", "startswith", "strip", "swapcase", "title",
    "upper", "zfill",
    # list / dict / set
    "append", "clear", "copy", "extend", "insert", "pop", "remove", "reverse", "sort",
    "get", "items", "keys", "values", "setdefault", "update", "popitem",
    "add", "discard", "difference", "intersection", "union", "symmetric_difference",
    "issubset", "issuperset", "isdisjoint",
    # numbers
    "is_integer", "bit_length",
})

_PRINT_KEYWORDS = frozenset({"sep", "end", "flush"})
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
                   ast.BitAnd, ast.BitOr, ast.BitXor, ast.RShift)
_ALLOWED_UNARY = (ast.UAdd, ast.USub, ast.Not, ast.Invert)
_ALLOWED_CMPOPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
                   ast.In, ast.NotIn)
_ALLOWED_BOOLOPS = (ast.And, ast.Or)

# Artifacts: a plain, literal file name with a data/report extension. Context-file stems
# that agents auto-load as instructions are excluded, so an artifact is never an
# instruction channel into later sessions.
_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.(?:txt|md|csv|tsv|json)")
_CONTEXT_STEMS = frozenset({"agents", "claude", "gemini", "soul", "hermes", "skill",
                            "memory", "user", "readme"})
_ENCODINGS = frozenset({"utf-8", "utf8"})


class _Reject(Exception):
    pass


class _Checker:
    """Fail-closed allowlist walk over one cell."""

    def __init__(self) -> None:
        self.effects: set[str] = set()
        self.writes = 0
        self.handles: set[str] = set()   # handles of the enclosing exclusive-create ``with``

    # -- helpers ---------------------------------------------------------------------
    @staticmethod
    def fail(reason: str) -> None:
        raise _Reject(reason)

    def bind(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            if target.id.startswith("_") or target.id in _RESERVED:
                self.fail(f"binds reserved name {target.id!r}")
            if target.id in self.handles:
                self.fail("rebinds a file handle")
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self.bind(elt)
        elif isinstance(target, ast.Subscript):
            # ``d[k] = v`` on builtin containers only (clean lineage holds no other kind).
            container = target.value
            if not isinstance(container, ast.Name):
                raise _Reject("subscript store on a non-name")
            self.load_name(container)
            self.expr(target.slice)
        else:
            self.fail(f"assignment target {type(target).__name__}")

    def load_name(self, node: ast.Name) -> None:
        name = node.id
        if name.startswith("_"):
            self.fail(f"underscore name {name!r}")
        if name in _IMPORTABLE or name == "hermes_tools":
            self.fail(f"tool {name!r} used outside a direct call")
        if name in self.handles:
            self.fail("file handle used outside handle.write(...)")
        if name in _ALL_BUILTINS and name not in SAFE_BUILTINS:
            self.fail(f"builtin {name!r} is outside the safe set")

    # -- statements ------------------------------------------------------------------
    def stmts(self, body: list, *, in_with: bool = False) -> None:
        for stmt in body:
            self.stmt(stmt, in_with=in_with)

    def stmt(self, node: ast.stmt, *, in_with: bool = False) -> None:
        if isinstance(node, ast.Expr):
            value = node.value
            if in_with and isinstance(value, ast.Call) and self._is_handle_write(value):
                self.writes += 1
                if self.writes > MAX_WRITES:
                    self.fail("too many writes")
                self.expr(value.args[0])
                return
            self.expr(value)
        elif isinstance(node, ast.Assign):
            self.expr(node.value)
            for target in node.targets:
                self.bind(target)
        elif isinstance(node, ast.AugAssign):
            if not isinstance(node.op, _ALLOWED_BINOPS):
                self.fail("operator")
            self.expr(node.value)
            if isinstance(node.target, ast.Name):
                self.bind(node.target)
                self.load_name(node.target)
            else:
                self.bind(node.target)
        elif isinstance(node, ast.If):
            self.expr(node.test)
            self.stmts(node.body, in_with=in_with)
            self.stmts(node.orelse, in_with=in_with)
        elif isinstance(node, ast.For):
            self.expr(node.iter)
            self.bind(node.target)
            # Writes stay at the top level of their ``with`` body: no unbounded write loops.
            self.stmts(node.body)
            self.stmts(node.orelse)
        elif isinstance(node, ast.With):
            if in_with or self.handles:
                self.fail("nested with")
            self.with_open(node)
        elif isinstance(node, ast.ImportFrom):
            self.import_from(node)
        elif isinstance(node, (ast.Pass, ast.Break, ast.Continue)):
            return
        else:
            self.fail(f"statement {type(node).__name__}")

    def import_from(self, node: ast.ImportFrom) -> None:
        if node.module != "hermes_tools" or node.level:
            self.fail("import outside hermes_tools")
        for alias in node.names:
            if alias.name not in _IMPORTABLE or (alias.asname and alias.asname != alias.name):
                self.fail(f"import of {alias.name!r}")

    def with_open(self, node: ast.With) -> None:
        if len(node.items) != 1:
            self.fail("with must open exactly one artifact")
        item = node.items[0]
        call, handle = item.context_expr, item.optional_vars
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "open" and isinstance(handle, ast.Name)):
            raise _Reject("with is only for open(<artifact>, 'x') as <name>")
        if len(call.args) != 2:
            raise _Reject("open needs a literal name and literal mode")
        name_node, mode_node = call.args
        if not (isinstance(name_node, ast.Constant) and isinstance(mode_node, ast.Constant)):
            raise _Reject("open needs a literal name and literal mode")
        name, mode = name_node.value, mode_node.value
        if mode != "x":
            self.fail("artifact mode must be exclusive-create 'x'")
        if not (isinstance(name, str) and _ARTIFACT_NAME.fullmatch(name)
                and name.rsplit(".", 1)[0].lower() not in _CONTEXT_STEMS):
            self.fail("artifact name is not a plain report/data file name")
        for kw in call.keywords:
            if kw.arg != "encoding" or not (isinstance(kw.value, ast.Constant)
                                            and str(kw.value.value).lower() in _ENCODINGS):
                self.fail("open keyword")
        self.bind(handle)
        self.effects.add("write")
        self.handles.add(handle.id)
        try:
            self.stmts(node.body, in_with=True)
        finally:
            self.handles.discard(handle.id)

    def _is_handle_write(self, node: ast.Call) -> bool:
        func = node.func
        return (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                and func.value.id in self.handles and func.attr == "write"
                and len(node.args) == 1 and not node.keywords
                and not isinstance(node.args[0], ast.Starred))

    # -- expressions -----------------------------------------------------------------
    def expr(self, node: Optional[ast.AST]) -> None:
        if node is None:
            return
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool) or value is None or isinstance(value, (str, float)):
                return
            if isinstance(value, int):
                if abs(value) > MAX_INT_LITERAL:
                    self.fail("integer literal too large")
                return
            self.fail(f"literal {type(value).__name__}")
        elif isinstance(node, ast.Name):
            if not isinstance(node.ctx, ast.Load):
                self.fail("name context")
            self.load_name(node)
        elif isinstance(node, ast.Call):
            self.call(node)
        elif isinstance(node, ast.Attribute):
            if not isinstance(node.ctx, ast.Load) or node.attr not in SAFE_METHODS:
                self.fail(f"attribute {node.attr!r}")
            self.expr(node.value)
        elif isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BINOPS):
                self.fail("operator")
            self.expr(node.left)
            self.expr(node.right)
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, _ALLOWED_UNARY):
                self.fail("operator")
            self.expr(node.operand)
        elif isinstance(node, ast.BoolOp):
            if not isinstance(node.op, _ALLOWED_BOOLOPS):
                self.fail("operator")
            for value in node.values:
                self.expr(value)
        elif isinstance(node, ast.Compare):
            if not all(isinstance(op, _ALLOWED_CMPOPS) for op in node.ops):
                self.fail("operator")
            self.expr(node.left)
            for comparator in node.comparators:
                self.expr(comparator)
        elif isinstance(node, ast.IfExp):
            self.expr(node.test)
            self.expr(node.body)
            self.expr(node.orelse)
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            if not isinstance(getattr(node, "ctx", ast.Load()), ast.Load):
                self.fail("container context")
            for elt in node.elts:
                self.expr(elt)
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if key is None:
                    self.fail("dict unpacking")
                self.expr(key)
            for value in node.values:
                self.expr(value)
        elif isinstance(node, ast.Starred):
            self.expr(node.value)
        elif isinstance(node, ast.Subscript):
            if not isinstance(node.ctx, ast.Load):
                self.fail("subscript context")
            self.expr(node.value)
            self.expr(node.slice)
        elif isinstance(node, ast.Slice):
            self.expr(node.lower)
            self.expr(node.upper)
            self.expr(node.step)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            self.comprehension(node.generators)
            self.expr(node.elt)
        elif isinstance(node, ast.DictComp):
            self.comprehension(node.generators)
            self.expr(node.key)
            self.expr(node.value)
        elif isinstance(node, ast.JoinedStr):
            for value in node.values:
                self.expr(value)
        elif isinstance(node, ast.FormattedValue):
            self.expr(node.value)
            self.expr(node.format_spec)
        else:
            self.fail(f"expression {type(node).__name__}")

    def comprehension(self, generators: list) -> None:
        for gen in generators:
            if gen.is_async:
                self.fail("async comprehension")
            self.expr(gen.iter)
            self.bind(gen.target)
            for cond in gen.ifs:
                self.expr(cond)

    def call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
            if name in WEB_TOOLS:
                self.effects.add("web")
                for value in list(node.args) + [kw.value for kw in node.keywords]:
                    if not _is_literal(value):
                        self.fail("web tool arguments must be literals")
                self._keywords(node)
                return
            if name in READ_TOOLS:
                self.effects.add("read")
            elif name in HELPERS:
                pass
            elif name in SAFE_BUILTINS:
                if name == "print" and any(kw.arg not in _PRINT_KEYWORDS for kw in node.keywords):
                    self.fail("print keyword")
            else:
                self.fail(f"call of {name!r}")
        elif isinstance(func, ast.Attribute):
            self.expr(func)
        else:
            self.fail("call of a computed callable")
        for arg in node.args:
            self.expr(arg)
        self._keywords(node)

    def _keywords(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg is None or kw.arg.startswith("_"):
                self.fail("keyword unpacking")
            self.expr(kw.value)


def _is_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, int, float, bool)) or node.value is None
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts)
    return False


def classify_cell(code: str) -> Optional[str]:
    """Return the effect class of a safe cell ("pure", "read", "web", "write", or a
    ``+``-joined combination), or ``None`` when the cell is outside the safe grammar."""
    if not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE_CHARS:
        return None
    try:
        tree = ast.parse(code, mode="exec")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        return None
    checker = _Checker()
    try:
        checker.stmts(tree.body)
    except (_Reject, RecursionError):
        return None
    effects = checker.effects
    if "web" in effects and "read" in effects:
        return None  # private-data egress: reads and outbound fetches never share a cell
    return "+".join(sorted(effects)) or "pure"


# ---- clean-lineage session registry -----------------------------------------------
# No LRU eviction: forgetting a taint would fail open. Cleared only when the session's
# kernels are torn down (``tools.approval.clear_session``).
_lock = threading.Lock()
_tainted: set[str] = set()


def observe_cell(session_key: str, code: str) -> Optional[str]:
    """Classify *code* and taint *session_key* if it is outside the safe grammar.

    Called by the guard for every execute_code cell before any early return, so cells
    admitted by yolo, headless or container paths still count toward the lineage.
    """
    kind = classify_cell(code)
    if kind is None:
        taint_session(session_key)
    return kind


def taint_session(session_key: str) -> None:
    with _lock:
        _tainted.add(session_key or "")


def session_is_clean(session_key: str) -> bool:
    with _lock:
        return (session_key or "") not in _tainted


def forget_session(session_key: str) -> None:
    with _lock:
        _tainted.discard(session_key or "")


def safe_path_enabled() -> bool:
    """``approvals.safe_path`` (default on). Strings are parsed strictly: only an explicit
    truthy spelling keeps it on, so a quoted "false" disables it."""
    from tools import approval_context

    value = approval_context._get_approval_config().get("safe_path", True)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return value is True
