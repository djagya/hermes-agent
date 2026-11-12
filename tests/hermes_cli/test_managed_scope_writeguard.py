"""Write-guard tests — managed config seeds are live-settable; managed env is not."""
import pytest


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
    (managed / "config.yaml").write_text(
        "model:\n  default: managed/model\n", encoding="utf-8"
    )
    managed_scope.invalidate_managed_cache()
    return home, managed


def test_config_set_managed_key_writes_user_yaml_and_wins(homes):
    from hermes_cli.config import set_config_value, load_config, cfg_get
    import hermes_cli.config as cfg

    home, _ = homes
    set_config_value("model.default", "user/override")
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    assert cfg_get(load_config(), "model", "default") == "user/override"
    assert "user/override" in (home / "config.yaml").read_text(encoding="utf-8")


def test_config_unset_managed_key_falls_back_to_seed(homes):
    from hermes_cli.config import (
        set_config_value,
        unset_config_value,
        load_config,
        cfg_get,
    )
    import hermes_cli.config as cfg

    set_config_value("model.default", "user/override")
    unset_config_value("model.default")
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    assert cfg_get(load_config(), "model", "default") == "managed/model"


def test_save_config_keeps_user_override_of_managed_leaf(homes):
    from hermes_cli.config import save_config, load_config, cfg_get, read_raw_config
    import hermes_cli.config as cfg

    save_config({"model": {"default": "user/saved"}})
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    assert cfg_get(load_config(), "model", "default") == "user/saved"
    assert read_raw_config().get("model", {}).get("default") == "user/saved"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("model:\n  default: managed/model\n", "model:\n  default: managed/next\n"),
        ("model: managed/model\n", "model: managed/next\n"),  # bare shorthand string
    ],
    ids=["mapping", "shorthand"],
)
def test_load_then_save_leaves_a_seed_a_seed(homes, first, second):
    """A bulk save of the loaded config must not copy the seed into the user file: the user leaf would then
    win over every later managed change."""
    from hermes_cli.config import save_config, load_config, read_raw_config
    import hermes_cli.config as cfg

    _home, managed = homes
    (managed / "config.yaml").write_text(first, encoding="utf-8")
    from hermes_cli import managed_scope
    managed_scope.invalidate_managed_cache()
    cfg._LOAD_CONFIG_CACHE.clear()
    assert load_config()["model"]["default"] == "managed/model"
    cfg_now = load_config()
    cfg_now.setdefault("display", {})["skin"] = "mono"
    save_config(cfg_now)
    cfg._RAW_CONFIG_CACHE.clear()
    assert "model" not in read_raw_config() and read_raw_config()["display"]["skin"] == "mono"
    (managed / "config.yaml").write_text(second, encoding="utf-8")
    managed_scope.invalidate_managed_cache()
    cfg._LOAD_CONFIG_CACHE.clear()
    assert load_config()["model"]["default"] == "managed/next"


@pytest.mark.parametrize(
    "user_yaml",
    [
        "model:\n  name: vendor/example\n",
        "model:\n  model: vendor/example\n",
    ],
    ids=["name-alias", "model-alias"],
)
def test_save_keeps_a_user_pin_written_under_a_model_alias(homes, user_yaml):
    """A user model id written under an alias (model.name / model.model) is canonicalized to
    model.default on load; an unrelated save must keep it as the user's pin, not drop it as a managed seed."""
    from hermes_cli import managed_scope
    from hermes_cli.config import save_config, load_config
    import hermes_cli.config as cfg

    home, managed = homes
    (managed / "config.yaml").write_text("model:\n  default: vendor/example\n", encoding="utf-8")
    (home / "config.yaml").write_text(user_yaml, encoding="utf-8")
    managed_scope.invalidate_managed_cache()
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    loaded = load_config()
    assert loaded["model"]["default"] == "vendor/example"
    loaded.setdefault("display", {})["skin"] = "mono"
    save_config(loaded)
    cfg._RAW_CONFIG_CACHE.clear()
    assert cfg.read_raw_config()["model"]["default"] == "vendor/example"
    (managed / "config.yaml").write_text("model:\n  default: vendor/other\n", encoding="utf-8")
    managed_scope.invalidate_managed_cache()
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    assert load_config()["model"]["default"] == "vendor/example"


def test_a_null_user_leaf_stays_unset_so_the_managed_seed_is_not_pinned(homes):
    """A YAML null leaf is unset on load (the managed seed fills it); a save must not write the
    seed into the user file, or later managed changes would no longer apply."""
    from hermes_cli import managed_scope
    from hermes_cli.config import save_config, load_config
    import hermes_cli.config as cfg

    home, managed = homes
    (managed / "config.yaml").write_text("display:\n  skin: managedskin\n", encoding="utf-8")
    (home / "config.yaml").write_text("display:\n  skin: null\n", encoding="utf-8")
    managed_scope.invalidate_managed_cache()
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    loaded = load_config()
    assert loaded["display"]["skin"] == "managedskin"
    loaded.setdefault("model", {})
    save_config(loaded)
    cfg._RAW_CONFIG_CACHE.clear()
    assert (cfg.read_raw_config().get("display") or {}).get("skin") != "managedskin"
    (managed / "config.yaml").write_text("display:\n  skin: nextskin\n", encoding="utf-8")
    managed_scope.invalidate_managed_cache()
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    assert load_config()["display"]["skin"] == "nextskin"


def test_save_keeps_a_user_root_provider_over_a_managed_model_provider(homes):
    """A legacy root ``provider`` migrates under model.provider on load; it is the user's, not a seed."""
    from hermes_cli import managed_scope
    from hermes_cli.config import save_config, load_config
    import hermes_cli.config as cfg

    home, managed = homes
    (managed / "config.yaml").write_text("model:\n  provider: custom\n", encoding="utf-8")
    (home / "config.yaml").write_text("provider: custom\n", encoding="utf-8")
    managed_scope.invalidate_managed_cache()
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    loaded = load_config()
    assert loaded["model"]["provider"] == "custom"
    loaded.setdefault("display", {})["skin"] = "mono"
    save_config(loaded)
    cfg._RAW_CONFIG_CACHE.clear()
    raw = cfg.read_raw_config()
    assert raw.get("model", {}).get("provider") == "custom" or raw.get("provider") == "custom"
    (managed / "config.yaml").write_text("model:\n  provider: other\n", encoding="utf-8")
    managed_scope.invalidate_managed_cache()
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    assert load_config()["model"]["provider"] == "custom"


@pytest.fixture
def env_homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    (managed / ".env").write_text(
        "OPENAI_API_BASE=https://org.example/v1\n", encoding="utf-8"
    )
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    return home, managed


def test_save_env_value_managed_key_rejected(env_homes, capsys):
    from hermes_cli.config import save_env_value, get_env_path

    save_env_value("OPENAI_API_BASE", "https://user.example/v1")
    assert "managed" in capsys.readouterr().err.lower()
    env_path = get_env_path()
    body = env_path.read_text() if env_path.exists() else ""
    assert "user.example" not in body


# ── refused .env writes must fail, not half-apply ────────────────────────────

# Runtime-constructed fake credentials (never literal key-shaped strings).
ADMIN_KEY = "sk-ds-" + "a" * 24
OLD_KEY = "sk-ds-" + "b" * 24
NEW_KEY = "sk-ds-" + "c" * 24


@pytest.fixture
def pinned_env(tmp_path, monkeypatch):
    """The administrator pins DEEPSEEK_API_KEY and TELEGRAM_HOME_CHANNEL; the user's own stores
    still hold an older key in .env, a config.yaml mirror of it, its env-seeded pool entry, and a
    stale top-level config.yaml copy of the channel."""
    import json

    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    home, managed = tmp_path / "home", tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    (managed / ".env").write_text(
        f"DEEPSEEK_API_KEY={ADMIN_KEY}\nTELEGRAM_HOME_CHANNEL=111\n", encoding="utf-8")
    (home / ".env").write_text(
        f"DEEPSEEK_API_KEY={OLD_KEY}\nTELEGRAM_HOME_CHANNEL=222\n", encoding="utf-8")
    (home / "config.yaml").write_text(
        f"model:\n  provider: deepseek\n  default: deepseek-chat\n  api_key: {OLD_KEY}\n"
        "TELEGRAM_HOME_CHANNEL: '333'\n", encoding="utf-8")
    (home / "auth.json").write_text(json.dumps({"credential_pool": {"deepseek": [{
        "id": "e1", "label": "env", "auth_type": "api_key", "priority": 0,
        "source": "env:DEEPSEEK_API_KEY", "access_token": OLD_KEY}]}}), encoding="utf-8")
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    cfg.invalidate_env_cache()
    managed_scope.invalidate_managed_cache()
    return home, _user_stores(home)


def _user_stores(home):
    return {name: (home / name).read_text(encoding="utf-8") for name in (".env", "config.yaml", "auth.json")}


def _assert_user_stores_untouched(home, before):
    assert _user_stores(home) == before


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
@pytest.mark.parametrize("lock, refusal", [
    pytest.param("scope", "managed by your administrator", id="managed-scope"),
    pytest.param("install", "managed by nixos", id="package-managed"),
])
def test_api_env_write_of_locked_key_is_refused_and_writes_nothing(pinned_env, monkeypatch, method, lock, refusal):
    from fastapi.testclient import TestClient

    from hermes_cli.web_server import _SESSION_TOKEN, app

    if lock == "install":
        monkeypatch.setenv("HERMES_MANAGED", "nixos")
    home, before = pinned_env
    body = {"key": "DEEPSEEK_API_KEY", **({"value": NEW_KEY} if method == "PUT" else {})}
    resp = TestClient(app).request(method, "/api/env", json=body, headers={"X-Hermes-Session-Token": _SESSION_TOKEN})
    assert resp.status_code == 400
    assert refusal in resp.json()["detail"]
    _assert_user_stores_untouched(home, before)


_ANTHROPIC_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


@pytest.fixture
def anthropic_homes(tmp_path, monkeypatch):
    """A user home whose auth.json holds a manually added anthropic key, and an empty managed dir."""
    import json

    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    home, managed = tmp_path / "home", tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    for name in _ANTHROPIC_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    (home / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": [{
        "id": "m1", "label": "manual", "auth_type": "api_key", "priority": 0,
        "source": "manual", "access_token": OLD_KEY}]}}), encoding="utf-8")

    def reload():
        cfg.invalidate_env_cache()
        managed_scope.invalidate_managed_cache()

    reload()
    return home, managed, reload


def _disconnect(slug):
    from tui_gateway import server

    return server._methods["model.disconnect"](1, {"slug": slug})


def test_disconnect_on_a_package_managed_install_still_clears_the_auth_store(anthropic_homes, monkeypatch):
    """The .env lock refuses every key on such an install, but a key that holds nothing is nothing to refuse."""
    import json

    home, _managed, _reload = anthropic_homes
    monkeypatch.setenv("HERMES_MANAGED", "nixos")
    resp = _disconnect("anthropic")
    assert resp.get("result", {}).get("disconnected") is True, resp
    assert "anthropic" not in json.loads((home / "auth.json").read_text(encoding="utf-8"))["credential_pool"]
