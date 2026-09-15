"""sera-toolbox wrap must never world-chmod the caller's cwd."""

from pathlib import Path

WRAP = Path(__file__).resolve().parents[2] / "docker" / "sera-toolbox" / "wrap"


def test_wrap_world_chmod_is_root_only_and_skips_cwd():
    text = WRAP.read_text(encoding="utf-8")
    assert 'chmod a+rwx "$out_dir"' in text
    start = text.index('nproc_lim="${SERA_SANDBOX_NPROC:-$nproc_default}"')
    block = text[start : start + 800]
    assert '[ "$(id -u)" -eq 0 ]' in block
    assert 'mktemp -d /tmp/sera-out.XXXXXX' in block
    assert 'Never world-chmod the caller' in block or "Never world-chmod the caller's cwd" in block
    # Unconditional cwd chmod is the vault-0777 hole.
    assert "chmod a+rwx \"$out_dir\" 2>/dev/null || true\nif [ -n \"${lo_profile:-}\" ]" not in text
