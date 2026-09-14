"""apply_managed_overlay() — seed missing leaves; a present user leaf wins."""
import textwrap

import pytest


@pytest.fixture
def managed(tmp_path, monkeypatch):
    md = tmp_path / "managed"
    md.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(md))
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    return md


def _write(md, body):
    (md / "config.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()


def test_overlay_noop_without_scope(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "nope"))
    managed_scope.invalidate_managed_cache()
    src = {"display": {"skin": "user"}}
    assert managed_scope.apply_managed_overlay(src) == {"display": {"skin": "user"}}


def test_overlay_user_leaf_wins_and_preserves_siblings(managed):
    from hermes_cli import managed_scope

    _write(managed, "display:\n  skin: charizard\n")
    out = managed_scope.apply_managed_overlay(
        {"display": {"skin": "user", "show_reasoning": True}}
    )
    assert out["display"]["skin"] == "user"
    assert out["display"]["show_reasoning"] is True


def test_overlay_seeds_omitted_leaf(managed):
    from hermes_cli import managed_scope

    _write(managed, "display:\n  skin: charizard\n")
    out = managed_scope.apply_managed_overlay(
        {"display": {"show_reasoning": True}}
    )
    assert out["display"]["skin"] == "charizard"
    assert out["display"]["show_reasoning"] is True


def test_overlay_user_raw_keeps_schema_defaults_from_shadowing_seed(managed):
    from hermes_cli import managed_scope

    _write(managed, "display:\n  skin: charizard\n")
    defaults_plus_user = {"display": {"skin": "default", "show_reasoning": True}}
    out = managed_scope.apply_managed_overlay(
        defaults_plus_user, user_raw={"display": {"show_reasoning": True}}
    )
    assert out["display"]["skin"] == "charizard"
    assert out["display"]["show_reasoning"] is True


def test_overlay_scalar_null_takes_seed(managed):
    from hermes_cli import managed_scope

    _write(managed, "security:\n  tirith_enabled: true\n")
    out = managed_scope.apply_managed_overlay(
        {"security": {"tirith_enabled": None}},
        user_raw={"security": {"tirith_enabled": None}},
    )
    assert out["security"]["tirith_enabled"] is True


def test_overlay_bare_model_string_keeps_user_default(managed):
    from hermes_cli import managed_scope

    _write(managed, "model:\n  default: managed/model\n  provider: nous\n")
    out = managed_scope.apply_managed_overlay(
        {"model": {"default": "user/model", "provider": "nous"}},
        user_raw={"model": "user/model"},
    )
    assert out["model"]["default"] == "user/model"
