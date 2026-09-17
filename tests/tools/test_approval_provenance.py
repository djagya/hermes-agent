"""Kernel-helper provenance (tools/approval_provenance.py) and its wiring.

Contract: the registry is AST-only (cells are never evaluated to learn what
they define), bounded per session, conservative under redefinition/aliasing,
strips comments and docstrings (untrusted prose never reaches the trusted
prompt channel), redacts, and is forgotten with the kernel at clear_session.
No helper evaluation happens during classification; no network or kernel
process is involved — the fixtures are plain source strings.
"""

from __future__ import annotations

import tools.approval_provenance as prov
from tools import approval as A

S = "prov-test-session"


def _fresh(key: str) -> str:
    prov.forget_session(key)
    return key


SCAN_CELL = '''
import json, os, urllib.request

def scan(url):
    """Fetch a page into the demo cache and extract links."""
    with urllib.request.urlopen(url) as resp:
        html = resp.read()
    with open("cache.html", "wb") as fh:
        fh.write(html)
    return extract_links(html)
'''


class TestRecordingAndClassification:
    def test_defined_name_is_known(self):
        key = _fresh("t-known")
        prov.record_cell(key, SCAN_CELL)
        assert prov.classify_name(key, "scan") == "known"

    def test_undefined_name_is_unknown(self):
        key = _fresh("t-unknown")
        prov.record_cell(key, SCAN_CELL)
        assert prov.classify_name(key, "never_seen") == "unknown"

    def test_redefinition_marks_superseded(self):
        key = _fresh("t-super")
        prov.record_cell(key, SCAN_CELL)
        prov.record_cell(key, "def scan(url):\n    return []\n")
        assert prov.classify_name(key, "scan") == "superseded"

    def test_alias_marks_both_names(self):
        key = _fresh("t-alias")
        prov.record_cell(key, "def real(x):\n    return x\n")
        prov.record_cell(key, "handler = real\n")
        assert prov.classify_name(key, "handler") == "known"

    def test_unparsable_cell_records_nothing(self):
        key = _fresh("t-broken")
        prov.record_cell(key, "def broken(:\n    pass")
        assert prov.classify_name(key, "broken") == "unknown"

    def test_registry_is_bounded(self):
        key = _fresh("t-bounded")
        for i in range(200):
            prov.record_cell(key, f"def h{i}(x):\n    return x\n")
        assert len(prov._sessions[key].defs) <= prov._MAX_DEFS_PER_SESSION


class TestDescribeName:
    def test_provenance_includes_body_and_effects(self):
        key = _fresh("t-desc")
        prov.record_cell(key, SCAN_CELL)
        text = prov.describe_name(key, "scan", "print(scan('x'))") or ""
        assert "urlopen" in text
        assert "HTTP fetch" in text

    def test_docstrings_and_comments_stripped(self):
        key = _fresh("t-strip")
        prov.record_cell(key, SCAN_CELL)
        text = prov.describe_name(key, "scan", "print(scan('x'))") or ""
        assert "Fetch a page" not in text
        assert "#" not in text.split("effects:")[-1]

    def test_superseded_marker_present_after_redefinition(self):
        key = _fresh("t-super2")
        prov.record_cell(key, SCAN_CELL)
        prov.record_cell(key, "def scan(url):\n    return []\n")
        assert "SUPERSEDED" in (prov.describe_name(key, "scan", "print(scan('x'))") or "")

    def test_long_body_truncated_with_marker(self):
        key = _fresh("t-long")
        body = "\n    ".join(f"y{i} = x + {i}" for i in range(60))
        prov.record_cell(key, f"def long(x):\n    {body}\n    return y0\n")
        assert "…[truncated]" in (prov.describe_name(key, "long", "long(1)") or "")

    def test_quoted_hash_survives_stripping(self):
        key = _fresh("t-hash")
        prov.record_cell(key, "def q(s):\n    tag = '# not a comment'\n    return tag\n")
        assert "# not a comment" in (prov.describe_name(key, "q", "q(1)") or "")

    def test_unknown_name_describes_nothing(self):
        key = _fresh("t-nodesc")
        assert prov.describe_name(key, "ghost", "ghost()") is None


class TestProvenanceContext:
    def test_guard_context_lists_helpers_with_classification(self):
        key = _fresh("t-ctx")
        prov.record_cell(key, SCAN_CELL)
        text = A._undefined_name_provenance(
            "results = ThreadPoolExecutor(3).map(scan, urls)", key)
        assert "scan: known" in text
        assert "ThreadPoolExecutor" in text
        assert "statically parsed, never evaluated" in text

    def test_no_context_when_everything_is_defined(self):
        key = _fresh("t-ctx2")
        text = A._undefined_name_provenance("x = 1 + 2\nprint(x)", key)
        assert text == ""

    def test_rebound_name_flagged_against_current_cell(self):
        assert prov.names_rebound_in_current_cell("scan = lambda u: u\nprint(scan('x'))",
                                                   ["scan"]) == {"scan"}


class TestLifecycle:
    def test_clear_session_forgets_provenance(self):
        key = _fresh("t-clear")
        prov.record_cell(key, "def g():\n    return 2\n")
        A.clear_session(key)
        assert prov.classify_name(key, "g") == "unknown"

    def test_kernel_record_hook_exists_and_never_raises(self):
        from tools.code_kernel import _record_cell_for_provenance
        _record_cell_for_provenance("t-kernel", "def k():\n    return 1\n")
        _record_cell_for_provenance("", "def k2():\n    return 1\n")  # no owner: no-op
        _record_cell_for_provenance("t-kernel", "def broken(:\n")  # unparsable: no-op
        prov.forget_session("t-kernel")
