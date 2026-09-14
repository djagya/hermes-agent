"""Config integration — /etc/hermes is a seed; a present user leaf wins."""
import textwrap

import pytest
import yaml


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    import hermes_cli.config as cfg
    from hermes_cli import config_effective, managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()
    return home, managed


def _write(path, body):
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    import hermes_cli.config as cfg
    from hermes_cli import config_effective, managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


def test_user_write_approval_beats_managed_seed(homes):
    """Contradiction: managed skills.write_approval false, user true → effective true."""
    from hermes_cli.config import load_config, cfg_get
    from hermes_cli.config_effective import load_user_config_effective

    home, managed = homes
    _write(home / "config.yaml", "skills:\n  write_approval: true\n")
    _write(managed / "config.yaml", "skills:\n  write_approval: false\n")
    assert cfg_get(load_config(), "skills", "write_approval") is True
    assert load_user_config_effective(home / "config.yaml")["skills"]["write_approval"] is True


def test_user_scalar_beats_managed_seed(homes):
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "model:\n  default: user/model\n")
    _write(managed / "config.yaml", "model:\n  default: managed/model\n")
    assert cfg_get(load_config(), "model", "default") == "user/model"


def test_omitted_leaf_takes_managed_seed(homes):
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "display:\n  show_reasoning: true\n")
    _write(managed / "config.yaml", "model:\n  default: managed/model\n")
    assert cfg_get(load_config(), "model", "default") == "managed/model"
    assert cfg_get(load_config(), "display", "show_reasoning") is True


def test_nested_user_leaf_beats_managed_sibling_seed(homes):
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "display:\n  skin: user_skin\n")
    _write(managed / "config.yaml", "display:\n  skin: charizard\n  show_reasoning: true\n")
    assert cfg_get(load_config(), "display", "skin") == "user_skin"
    assert cfg_get(load_config(), "display", "show_reasoning") is True


def test_user_list_beats_managed_list(homes):
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "toolsets:\n  enabled: [a, b, c]\n")
    _write(managed / "config.yaml", "toolsets:\n  enabled: [x]\n")
    assert cfg_get(load_config(), "toolsets", "enabled") == ["a", "b", "c"]


def test_user_envref_leaf_is_present_and_wins(homes, monkeypatch):
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    monkeypatch.setenv("EVIL", "user/override")
    _write(home / "config.yaml", "model:\n  default: ${EVIL}\n")
    _write(managed / "config.yaml", "model:\n  default: managed/locked\n")
    assert cfg_get(load_config(), "model", "default") == "user/override"


def test_yaml_null_against_managed_dict_is_absent(homes):
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "skills:\n")
    # Explicit YAML null for a mapping key.
    _write(home / "config.yaml", "skills: null\n")
    _write(managed / "config.yaml", "skills:\n  write_approval: false\n")
    assert cfg_get(load_config(), "skills", "write_approval") is False


def test_managed_nested_dict_default_flattens_when_user_omits(homes):
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "display:\n  show_reasoning: true\n")
    _write(
        managed / "config.yaml",
        "model:\n  default:\n    provider: nous\n    model: managed/nested\n",
    )
    cfg = load_config()
    assert cfg_get(cfg, "model", "default") == "managed/nested"
    assert cfg_get(cfg, "model", "provider") == "nous"


def test_managed_bare_string_model_seeds_when_user_omits(homes):
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "display:\n  show_reasoning: true\n")
    _write(managed / "config.yaml", "model: managed/bare\n")
    cfg = load_config()
    assert cfg_get(cfg, "model", "default") == "managed/bare"


def test_profile_homes_do_not_share_user_leaves(tmp_path, monkeypatch):
    from hermes_cli.config import load_config, cfg_get
    import hermes_cli.config as cfg
    from hermes_cli import config_effective, managed_scope

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(
        "skills:\n  write_approval: false\n", encoding="utf-8"
    )
    a = tmp_path / "profile-a"
    b = tmp_path / "profile-b"
    a.mkdir()
    b.mkdir()
    (a / "config.yaml").write_text("skills:\n  write_approval: true\n", encoding="utf-8")
    (b / "config.yaml").write_text("display:\n  show_reasoning: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))

    def _load(home):
        monkeypatch.setenv("HERMES_HOME", str(home))
        cfg._LOAD_CONFIG_CACHE.clear()
        cfg._RAW_CONFIG_CACHE.clear()
        config_effective._EFFECTIVE_CACHE.clear()
        config_effective._LAST_GOOD_USER_RAW.clear()
        managed_scope.invalidate_managed_cache()
        return load_config()

    assert cfg_get(_load(a), "skills", "write_approval") is True
    assert cfg_get(_load(b), "skills", "write_approval") is False


def test_migrate_and_save_keep_explicit_user_leaf(homes):
    from hermes_cli.config import migrate_config, load_config, cfg_get, read_raw_config
    import hermes_cli.config as cfg

    home, managed = homes
    _write(
        home / "config.yaml",
        "_config_version: 42\nskills:\n  write_approval: true\n",
    )
    _write(managed / "config.yaml", "skills:\n  write_approval: false\n")
    migrate_config(interactive=False, quiet=True)
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    raw = read_raw_config()
    assert ((raw.get("skills") or {}).get("write_approval")) is True
    assert cfg_get(load_config(), "skills", "write_approval") is True
    on_disk = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert (on_disk.get("skills") or {}).get("write_approval") is True
