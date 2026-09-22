"""Deterministic safe-operation automatic path for ``execute_code`` (HER-193).

The PR #23 guardian (smart approval) still escalates ordinary safe work — a
sum over a parsed list, a ``read_file`` → transform → ``report.txt`` flow — to
an auxiliary-LLM call and often a human prompt. This module defines a NARROW,
STATICALLY CHECKED subset of cells that skip that gate entirely:

- ``"pure"`` — closed computation: no imports, no dynamic-code or attribute
  machinery, no I/O. Every loaded name is either bound in THIS cell or is a
  whitelisted builtin that no earlier kernel cell could have re-bound.
- ``"bounded"`` — the pure rules plus the existing sandbox tools
  (``read_file``/``search_files``/``web_search``/``web_extract`` — each keeps
  its own policy; no new HTTP surface) and at most two writes, each a literal
  ``report<...>.txt`` filename opened for writing inside a ``with`` block.

Trust boundary: the classification is a function of the AST plus a
host-side namespace cross-check (:mod:`tools.approval_provenance`), never of
names, comments, or provenance text. Anything not provably inside the subset
returns ``None`` and the caller keeps the existing guardian gate — unknown
arbitrary Python stays gated by design.

Operator off-switch: ``approvals.safe_path: false`` in config.yaml.
"""

from __future__ import annotations

import ast
import logging

logger = logging.getLogger("tools.approval")

# Classification outcomes for :func:`classify_cell`.
PURE = "pure"
BOUNDED = "bounded"

# Builtin names a pure cell may load. Deliberately small and value-only: no
# type-mutating or namespace-touching builtins (no getattr/setattr/delattr,
# no globals/locals/vars, no eval/exec/compile/__import__/open/input).
_SAFE_BUILTINS = frozenset({
    "abs", "all", "any", "bool", "chr", "dict", "divmod", "enumerate",
    "filter", "float", "format", "frozenset", "hash", "hex", "int",
    "isinstance", "len", "list", "max", "min", "next", "oct", "ord", "pow",
    "print", "range", "repr", "reversed", "round", "set", "slice", "sorted",
    "str", "sum", "tuple", "zip",
    "True", "False", "None",
})

# Names a cell may NOT bind (would shadow the whitelist or dunder machinery).
_FORBIDDEN_TARGETS = frozenset(
    name for name in _SAFE_BUILTINS if name not in ("True", "False", "None")
) | {n for n in (
    "__builtins__", "__name__", "__file__", "__doc__", "__spec__",
    "__loader__", "__package__", "__debug__",
)}

# Builtin calls that make a cell unclassifiable even without an import.
_FORBIDDEN_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr",
    "globals", "locals", "vars", "input", "breakpoint", "open", "help",
    "memoryview", "bytearray", "bytes", "object", "super", "type",
    "classmethod", "staticmethod", "property", "iter", "callable", "id",
    "exit", "quit", "dir", "aiter", "anext", "bin", "bytes", "complex",
    "setattr", "delattr",
})

# Sandbox tools the bounded flow may call. These are the EXISTING constrained
# surfaces (each tool keeps its own policy/redaction); this module adds no
# network or read authority of its own.
_SANDBOX_TOOL_CALLS = frozenset({"read_file", "search_files", "web_search", "web_extract"})

# Read-only helper names also importable from the ``hermes_tools`` stub module.
# Deliberately EXCLUDES ``terminal``, ``write_file``, ``patch``, ``retry`` and
# every stateful/mutating helper.
_SANDBOX_HELPER_IMPORTS = frozenset({"json_parse", "shell_quote"})

# The only import a safe cell may make: the kernel's stub-tool module, and only
# read-only names from it.
_ALLOWED_IMPORT_MODULE = "hermes_tools"
_ALLOWED_IMPORTS = _SANDBOX_TOOL_CALLS | _SANDBOX_HELPER_IMPORTS

# Comprehension-scoped names are bound implicitly (comprehension target, the
# implicit function name, loop variables); they are tracked in _bound_names
# like any other binding, so no special-casing is needed here.

# Value-only expression shapes generic_visit may walk: containers, operators,
# comparisons, comprehensions, formatting. Every visited child is classified by
# its own visitor, so hostile elements are still caught. Statement shapes and
# anything with import/def/class/global semantics are NOT in this set.
_VALUE_ONLY_NODES = (
    ast.Tuple, ast.List, ast.Set, ast.Dict,
    ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.IfExp, ast.Starred,
    ast.Load, ast.Store, ast.Del,  # expression contexts
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.LShift, ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd, ast.MatMult,
    ast.USub, ast.UAdd, ast.Not, ast.Invert,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
    ast.In, ast.NotIn,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
    ast.comprehension, ast.Subscript, ast.Lambda,
    ast.JoinedStr, ast.FormattedValue, ast.Slice, ast.keyword, ast.Expr,
)

_MAX_CODE_CHARS = 20_000  # refuse to reason about enormous payloads; gate them


class _CellChecker(ast.NodeVisitor):
    """Walk one cell and decide whether it stays inside the safe subset.

    Fails closed: any node shape not explicitly understood rejects the cell.
    """

    def __init__(self) -> None:
        self.bound: set[str] = set()
        self.used_builtins: list[str] = []
        self.sandbox_calls = 0
        self.sandbox_tools_imported = False
        self.write_opens = 0
        self.read_opens = 0
        self._reject: str | None = None
        # write-opening `with` blocks: the body must only use .write()/.close()
        self._write_handles: set[str] = set()
        # Names whose in-cell binding is a string literal safe to embed in a
        # report filename (no separators, no traversal).
        self.safe_filename_names: set[str] = set()

    def reject(self, reason: str) -> None:
        if self._reject is None:
            self._reject = reason

    @property
    def rejected(self) -> str | None:
        return self._reject

    # ---- statement shapes -------------------------------------------------

    def visit_Module(self, node: ast.Module) -> None:
        for child in node.body:
            self.visit(child)

    def visit_Expr(self, node: ast.Expr) -> None:
        self.visit(node.value)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind(target, value=node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self._bind(node.target, value=node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        # target must already be bound; record it reads as a load too
        self.visit(node.target)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        for child in node.body + node.orelse:
            self.visit(child)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._bind(node.target)
        for child in node.body + node.orelse:
            self.visit(child)

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        for child in node.body + node.orelse:
            self.visit(child)

    def visit_With(self, node: ast.With) -> None:
        handles: list[str] = []
        for item in node.items:
            if isinstance(item.context_expr, ast.Call):
                self._visit_open_call(item.context_expr, handles)
            else:
                self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind(item.optional_vars)
                if isinstance(item.optional_vars, ast.Name):
                    handles.append(item.optional_vars.id)
        self._write_handles.update(handles)
        for child in node.body:
            self.visit(child)
        for handle in handles:
            self._write_handles.discard(handle)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.reject("function definition")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.reject("async function definition")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.reject("class definition")

    def visit_Import(self, node: ast.Import) -> None:
        self.reject("import")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # The ONLY import a safe cell may make: read-only names from the
        # kernel's stub-tool module (`from hermes_tools import read_file, ...`).
        if (node.module == _ALLOWED_IMPORT_MODULE and node.level == 0
                and node.names and all(
                    alias.name in _ALLOWED_IMPORTS and not alias.asname
                    for alias in node.names)):
            for alias in node.names:
                self.bound.add(alias.name)
                if alias.name in _SANDBOX_TOOL_CALLS:
                    self.sandbox_tools_imported = True
            return
        self.reject("import")

    def visit_Global(self, node: ast.Global) -> None:
        self.reject("global statement")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.reject("nonlocal statement")

    def visit_Try(self, node: ast.Try) -> None:
        self.reject("try statement")

    def visit_Raise(self, node: ast.Raise) -> None:
        self.reject("raise statement")

    def visit_Assert(self, node: ast.Assert) -> None:
        self.visit(node.test)
        if node.msg is not None:
            self.visit(node.msg)

    def visit_Delete(self, node: ast.Delete) -> None:
        self.reject("del statement")

    def visit_Match(self, node: ast.Match) -> None:  # pragma: no cover (3.10+)
        self.reject("match statement")

    def visit_comprehension(self, node: ast.comprehension) -> None:
        """Comprehension generators: the iterable is evaluated; the target BINDS
        names scoped to the comprehension (like a for loop)."""
        self.visit(node.iter)
        self._bind(node.target)
        for cond in node.ifs:
            self.visit(cond)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comp(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comp(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        # key/value are visited AFTER the generators bind their targets.
        self._visit_comp(node, extras=[node.key, node.value])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comp(node)

    def _visit_comp(self, node, extras: list | None = None) -> None:
        """Comprehensions: generators bind targets BEFORE the element/key/value
        expressions are visited (ast.iter_child_nodes yields the element first,
        which would see the comprehension-scoped target names as unbound)."""
        for gen in getattr(node, "generators", ()):
            self.visit(gen)
        if extras:
            for extra in extras:
                self.visit(extra)
        if hasattr(node, "elt"):
            self.visit(node.elt)  # type: ignore[attr-defined]

    def generic_visit(self, node: ast.AST) -> None:
        # Value-only expression shapes: walk children (each child is then
        # classified by its own visitor, so hostile elements are caught).
        # Everything else fails closed.
        if isinstance(node, _VALUE_ONLY_NODES):
            for child in ast.iter_child_nodes(node):
                self.visit(child)
            return
        self.reject(f"unsupported construct: {type(node).__name__}")

    # ---- expressions ------------------------------------------------------

    def visit_Constant(self, node: ast.Constant) -> None:
        pass  # literal values are always safe

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            if node.id in _FORBIDDEN_TARGETS:
                self.reject(f"load of reserved name {node.id!r}")
            elif node.id not in self.bound:
                if node.id in _SAFE_BUILTINS:
                    self.used_builtins.append(node.id)
                else:
                    # Loaded but neither bound here nor whitelisted: an earlier
                    # cell's helper/module/poisoned builtin. Fail closed.
                    self.reject(f"name {node.id!r} not bound in this cell")
        # Store/Del contexts arrive via _bind / visit_Delete.

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            self.reject(f"dunder/underscore attribute access .{node.attr}")
        self.visit(node.value)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in _FORBIDDEN_CALLS:
                self.reject(f"call to {func.id!r}")
            elif func.id in _SANDBOX_TOOL_CALLS:
                if func.id not in self.bound:
                    self.reject(f"sandbox tool {func.id!r} not bound in this cell")
                self.sandbox_calls += 1
            elif func.id in _SAFE_BUILTINS:
                # Whitelisted builtin call: record for the poisoning cross-check.
                self.used_builtins.append(func.id)
            elif func.id not in self.bound:
                # NOTE: `open(...)` outside a `with` item never reaches here as
                # an allowance: `open` is in _FORBIDDEN_CALLS, so a bare
                # open()/read-mode open() is rejected before this branch.
                self.reject(f"call to unknown name {func.id!r}")
        elif isinstance(func, ast.Attribute):
            base = func.value
            # write-handle method calls: h.write(...), h.close()
            if isinstance(base, ast.Name) and base.id in self._write_handles:
                if func.attr not in ("write", "close"):
                    self.reject(f"write-handle method .{func.attr}()")
                for arg in node.args + [kw.value for kw in node.keywords]:
                    self.visit(arg)
                return
            # str/list/dict method calls on bound values are fine; the
            # underscore rule in visit_Attribute already rejects dunders.
            self.visit(base)
        else:
            self.reject("call on complex expression")
            return
        for arg in node.args:
            self.visit(arg)
        for kw in node.keywords:
            self.visit(kw.value)

    def _visit_open_call(self, node: ast.Call, handles: list[str]) -> None:
        """A ``with open(...) as h`` item. Only write-mode report filenames.

        ``open`` is in _FORBIDDEN_CALLS, so this is the ONLY path that accepts
        an open() call — with exactly one argument (a safe report filename),
        no mode/keywords, and inside a ``with`` block.
        """
        if not (isinstance(node.func, ast.Name) and node.func.id == "open"):
            self.visit(node)
            return
        if len(node.args) != 1 or node.keywords:
            self.reject("open() with mode argument or keywords")
            return
        if not _is_report_filename_expr(node.args[0], self):
            self.reject("open() target is not a literal report*.txt filename")
            return
        self.write_opens += 1
        if self.write_opens > 2:
            self.reject("more than two file writes in one cell")

    def _bind(self, target: ast.AST, value=None) -> None:
        if isinstance(target, ast.Name):
            if target.id in _FORBIDDEN_TARGETS:
                self.reject(f"assignment to reserved name {target.id!r}")
            else:
                self.bound.add(target.id)
                # Track names bound to a literal that is itself a safe report
                # filename component (used by f-string filename rules).
                if (value is not None and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and _is_safe_report_component(value.value)):
                    self.safe_filename_names.add(target.id)
                elif target.id in self.safe_filename_names:
                    self.safe_filename_names.discard(target.id)
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind(element)
            return
        if isinstance(target, ast.Starred):
            self._bind(target.value)
            return
        if isinstance(target, ast.Subscript):
            # d[k] = v mutates an existing container; allow only on bound names.
            self.visit(target)
            return
        self.reject(f"unsupported assignment target: {type(target).__name__}")


def _is_report_filename_expr(node: ast.AST, checker: "_CellChecker") -> bool:
    """Literal report filename or a composition of literals plus cell-bound
    names whose CURRENT binding is a safe string literal.

    Accepted: ``"report.txt"``, ``f"report_{tag}.txt"`` where ``tag`` was bound
    to a literal like ``"2026"`` in this cell — as long as the expression
    contains no path separators, no ``..``, and no dynamic calls other than
    ``str(...)`` over such names. A name bound to computed data (tool output,
    concatenations) is NOT accepted: its runtime value is unknown.
    """
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, str):
            return False
        return _is_safe_report_name(node.value)
    if isinstance(node, ast.Name):
        # Only names whose in-cell binding is a known-safe literal qualify.
        return node.id in checker.safe_filename_names
    if isinstance(node, ast.Call):
        # str(...) wrap of a safe expression
        return (isinstance(node.func, ast.Name) and node.func.id == "str"
                and len(node.args) == 1 and not node.keywords
                and _is_report_filename_expr(node.args[0], checker))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        # "report_%s.txt" % part
        return (_is_report_filename_expr(node.left, checker)
                and _is_report_filename_expr(node.right, checker))
    if isinstance(node, ast.JoinedStr):
        # f-strings: every interpolated value must be a safe expression or
        # a constant literal part.
        return all(_format_part_is_safe(value, checker) for value in node.values)
    if isinstance(node, ast.FormattedValue):
        return _is_report_filename_expr(node.value, checker)
    return False


def _format_part_is_safe(value: ast.AST, checker: "_CellChecker") -> bool:
    """One f-string part: literal text or a ``name``/``str(name)`` interpolation
    where ``name``'s in-cell binding is a known-safe string literal."""
    if isinstance(value, ast.Constant):
        return True  # literal text between interpolations
    if isinstance(value, ast.FormattedValue):
        inner = value.value
        if isinstance(inner, ast.Name):
            return inner.id in checker.safe_filename_names
        if isinstance(inner, ast.Call):
            return (isinstance(inner.func, ast.Name) and inner.func.id == "str"
                    and len(inner.args) == 1 and isinstance(inner.args[0], ast.Name)
                    and inner.args[0].id in checker.safe_filename_names)
    return False


def _is_safe_report_name(name: str) -> bool:
    """Literal filename check: report*.txt, no separators or traversal."""
    if not isinstance(name, str) or not name:
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    if name.startswith("."):
        return False
    return name.startswith("report") and name.endswith(".txt")


def _is_safe_report_component(name: str) -> bool:
    """Literal fragment check for f-string filename interpolation: no path
    separators, no traversal, no control characters — ``"2026"``, ``"q3"``."""
    if not isinstance(name, str) or not name:
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return all(ch.isalnum() or ch in "-_ " for ch in name)


def _check_kernel_poisoning(session_key: str, used_builtins: list[str]) -> bool:
    """Cross-check every whitelisted builtin the cell loads against the
    host-side provenance registry: if ANY earlier cell in this session ever
    assigned it (poisoned ``len = f``, ``print = evil``), fail closed."""
    if not session_key or not used_builtins:
        return True
    try:
        from tools import approval_provenance as prov
    except Exception:
        # Registry unavailable: only safe when nothing needs checking.
        return not used_builtins
    for name in set(used_builtins):
        if prov.classify_name(session_key, name) != prov.UNKNOWN:
            # Known, superseded, or mutated — all mean an earlier cell bound it.
            return False
    return True


def safe_path_enabled() -> bool:
    """``approvals.safe_path`` (default true). Read at call time so a config
    change takes effect without a restart; a malformed value disables."""
    try:
        from tools import approval_context as _ctx
        return bool(_ctx._get_approval_config().get("safe_path", True))
    except Exception:
        return False


def classify_cell(code: str, session_key: str = "") -> str | None:
    """Classify one ``execute_code`` cell against the safe subset.

    Returns ``"pure"``, ``"bounded"``, or ``None`` (not provably safe — the
    caller keeps its existing approval gate). Never raises; any internal
    failure returns ``None`` (fail closed).
    """
    if not isinstance(code, str) or not code.strip():
        return None
    if len(code) > _MAX_CODE_CHARS:
        return None
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return None

    # Reject stray top-level expression shapes the visitor fails closed on via
    # generic_visit; run it.
    checker = _CellChecker()
    checker.visit(tree)
    if checker.rejected is not None:
        logger.debug("safe-path: rejected (%s)", checker.rejected)
        return None
    if not _check_kernel_poisoning(session_key, checker.used_builtins):
        logger.debug("safe-path: rejected (builtin re-bound in an earlier cell)")
        return None
    return BOUNDED if (checker.write_opens or checker.sandbox_calls) else PURE
