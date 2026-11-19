"""xai-oauth route identity across both spellings: one mechanism, every site enumerated.

xai-oauth has two spellings of one route: stored state keeps the legacy API URL (rollback-safe) and the
runtime uses the subscription proxy. ``hermes_cli.route_identity`` owns the mapping:
``canonical_xai_route`` / ``route_key`` (read and compare) and ``persisted_xai_route`` (write).

1. ``test_every_site_is_registered``: an AST scan of the source tree finds every function that
   (A) calls a route-identity primitive, (B) writes to a persistence sink and mentions ``base_url``, or
   (C) compares a ``base_url`` while naming xai-oauth or a provider's registry default. Each must be a
   ``SITES`` row with a kind; a new site fails until it is classified.
2. ``test_site_kind_uses_the_mechanism``: compare/read/persist rows call the mechanism (static check).
3. The matrix tests run every site with a harness over
   {stored legacy, proxy, legacy with slash, custom ``*.x.ai``, foreign} x {active legacy, proxy}
   for ``xai-oauth`` and the API-key provider ``xai``.
4. Refresh failures (AuthError, timeout) during resolution never persist the proxy, never send an API
   key to the proxy and keep the stored spelling.
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import re
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
LEGACY = "https://api.x.ai/v1"
PROXY = "https://cli-chat-proxy.grok.com/v1"
CUSTOM = "https://us-east-1.api.x.ai/v1"
FOREIGN = "https://llm.example.net/v1"
STORED = {"legacy": LEGACY, "proxy": PROXY, "legacy-slash": LEGACY + "/", "custom": CUSTOM, "foreign": FOREIGN}
ACTIVE = {"legacy": LEGACY, "proxy": PROXY}
PROVIDERS = ("xai-oauth", "xai")
DEFAULT_SPELLINGS = ("legacy", "proxy", "legacy-slash")

# ── the site registry ──────────────────────────────────────────────────────────────────────────
# kind: mechanism | compare (route_key / canonical_xai_route) | pin (the context-pin route check, which
# is a compare site) | read (stored spelling -> live) | persist (persisted_xai_route or a persist
# chokepoint) | transport (the URL a client dials or a cache key: raw on purpose) | entry (a custom or
# named provider entry's own URL, never an xai-oauth route) | other (persists or checks another
# provider's or a user-entered URL; no xai-oauth runtime URL reaches it).
SITES: dict[str, tuple[str, str]] = {
    # mechanism
    "hermes_cli/route_identity.py::normalize_route_base_url": ("mechanism", "spelling-only normalization"),
    "hermes_cli/route_identity.py::canonical_xai_route": ("mechanism", "read side"),
    "hermes_cli/route_identity.py::persisted_xai_route": ("mechanism", "write side"),
    "hermes_cli/route_identity.py::route_key": ("mechanism", "compare key"),
    "hermes_cli/route_identity.py::should_clear_context_pin": ("pin", "-> _context_route_mismatch"),
    "hermes_cli/route_identity.py::should_clear_context_pin_async": ("pin", "-> should_clear_context_pin"),
    "utils.py::is_xai_inference_host": ("mechanism", "host class (r2), not route identity"),
    # compare
    "agent/agent_init.py::_context_route_mismatch": ("compare", "the context-pin route check"),
    "agent/agent_init.py::_provider_default_routes.add": ("compare", "default routes in route_key form"),
    "agent/agent_init.py::_scope_context_length_to_default_runtime": ("pin", "startup pin"),
    "agent/credential_pool.py::credential_pool_entry_serves_endpoint": ("compare", "pool entry vs session"),
    "gateway/run.py::_resolve_gateway_model_context._pin_still_applies": ("pin", "gateway display pin"),
    "gateway/run_inbound.py::GatewayInboundMixin._inbound_model_context_length": ("pin", "inbound pin"),
    "gateway/run_turn_hygiene.py::GatewayTurnHygieneMixin._hmwa_hygiene_settings": ("pin", "hygiene pin"),
    "gateway/run_turn_prepare.py::_resolve_session_override_runtime": ("compare", "managed override vs runtime"),
    "gateway/run_turn_prepare.py::_session_override_uses_managed_oauth": ("compare", "managed host set"),
    "gateway/session_mutation_model.py::_selection_guard_refusal": ("compare", "confirm-token binding"),
    "gateway/session_policy.py::model_route": ("compare", "endpoint-bound credential identity"),
    "gateway/session_route_identity.py::same_route": ("compare", "session route identity"),
    "gateway/session_worker_construct.py::route_runtime": ("compare", "launch URL vs frozen route"),
    "hermes_cli/model_switch.py::effective_context_pin": ("pin", "display pin"),
    "hermes_cli/model_switch.py::model_selection_config_updates": ("persist", "persisted_xai_route, and the pin"),
    "hermes_cli/model_switch.py::selection_route_changed": ("compare", "endpoint-bound credentials"),
    "hermes_cli/models.py::_configured_relay_base_url": ("compare", "own endpoint vs relay"),
    "hermes_cli/runtime_provider.py::is_foreign_provider_endpoint": ("compare", "legacy is xai-oauth's own"),
    # read
    "gateway/run_agent_cache.py::GatewayAgentCacheMixin._rehydrate_session_model_override": ("read", "persisted override"),
    "hermes_state_gateway.py::SessionGatewayMixin.session_gateway_runtime": ("read", "resume route reader"),
    "hermes_cli/auth_xai.py::_xai_oauth_runtime_base_url": ("read", "auth/pool rows"),
    "hermes_cli/auth_xai.py::_xai_oauth_inference_base_url": ("read", "-> _xai_oauth_runtime_base_url"),
    "agent/credential_pool.py::PooledCredential.runtime_base_url": ("read", "-> _xai_oauth_runtime_base_url"),
    "tools/xai_http.py::resolve_xai_http_credentials": ("read", "-> _xai_oauth_runtime_base_url"),
    "hermes_cli/runtime_provider.py::_pool_entry_mode_and_url": ("transport", "entry URL is runtime_base_url"),
    # persist
    "gateway/session.py::sanitize_model_override": ("persist", "session-store override"),
    "hermes_cli/route_identity.py::persisted_route_patch": ("persist", "session DB route patch"),
    "hermes_state_sessions.py::SessionSessionsMixin._write_model_config_patch": ("persist", "persisted_route_patch"),
    "hermes_state_sessions.py::SessionSessionsMixin.update_session_model": ("persist", "_write_model_config_patch"),
    "gateway/slash_commands_model.py::GatewayModelCommandsMixin._record_model_switch": ("persist", "update_session_model"),
    "hermes_cli/cli_model_switch_mixin.py::CLIModelSwitchMixin._persist_model_switch_to_session": (
        "persist", "patch_session_model_config"),
    "hermes_cli/auth.py::_update_config_for_provider": ("persist", "login config writer"),
    "hermes_cli/auth_xai.py::_xai_oauth_persisted_base_url": ("persist", "auth/pool rows"),
    "hermes_cli/auth_xai.py::_xai_oauth_device_code_login": ("persist", "_xai_oauth_persisted_base_url"),
    "agent/credential_pool.py::_seed_tokens_singleton": ("persist", "_xai_oauth_persisted_base_url"),
    "hermes_cli/auth_commands.py::<module>": ("persist", "_xai_oauth_persisted_base_url"),
    "hermes_cli/web_server_config.py::_apply_main_assignment_sync": ("persist", "_prepare_main_assignment"),
    "hermes_cli/web_routers/profiles.py::_write_profile_model": ("persist", "_apply_main_model_assignment"),
    # transport: raw URL on purpose (which host a client dials, cache keys, earned verdicts)
    "agent/agent_init.py::_active_route_url": ("transport", "query-string reattach"),
    "agent/agent_runtime_helpers.py::_route_may_be_custom": ("entry", "custom entries"),
    "agent/auxiliary_client.py::_is_actual_auxiliary_route": ("transport", "Actual daemon"),
    "agent/auxiliary_client.py::_recoverable_pool_provider": ("transport", "aux vs runtime origin"),
    "agent/auxiliary_client.py::_resolve_api_key_branch": ("other", "explicit aux URL"),
    "agent/auxiliary_client.py::_resolve_api_key_provider": ("other", "API-key providers"),
    "agent/auxiliary_client.py::_try_payment_fallback": ("transport", "health by label"),
    "agent/auxiliary_health.py::_unhealthy_cache_key": ("transport", "health cache key"),
    "agent/backend_identity.py::BackendIdentity.build": ("transport", "backend/cache identity"),
    "agent/client_lifecycle.py::ClientLifecycleMixin._swap_credential": ("transport", "client rebuild"),
    "agent/client_lifecycle.py::ClientLifecycleMixin._try_refresh_codex_client_credentials": (
        "transport", "client rebuild"),
    "agent/client_lifecycle.py::ClientLifecycleMixin._try_refresh_env_client_credentials": (
        "transport", "client rebuild"),
    "agent/client_lifecycle.py::ClientLifecycleMixin._resolve_env_credentials": ("other", "kimi/zai env"),
    "agent/codex_responses_adapter.py::_classify_responses_issuer": ("transport", "wire issuer class"),
    "agent/conversation_compression.py::_aux_inherits_main_route": ("transport", "aux vs main, both runtime"),
    "agent/image_routing.py::_probe_ollama": ("other", "ollama"),
    "agent/route_verdicts.py::reset_route_verdicts_if_moved": ("transport", "endpoint that earned verdicts"),
    "agent/route_verdicts.py::route_endpoint": ("transport", "endpoint that earned verdicts"),
    "agent/agent_init.py::_configured_default_base_url": ("entry", "feeds _context_route_mismatch"),
    "agent/agent_init.py::_custom_provider_configured_base_url": ("entry", "custom entries"),
    "agent/agent_init.py::_warn_invalid_custom_provider_context_length": ("entry", "custom entries"),
    "agent/credential_pool.py::CredentialPool._clear_terminal_nous_state": ("other", "nous"),
    "cron/scheduler_prompt.py::_has_base_url_override": ("other", "cron override flag"),
    "gateway/session_policy.py::entry_serves": ("entry", "named entries"),
    "gateway/session_policy_credentials.py::_url": ("entry", "named-entry digest"),
    "hermes_cli/anon_auth.py::_mint_locked": ("other", "anon portal"),
    "hermes_cli/anon_auth.py::_reconcile_and_provision": ("other", "anon portal"),
    "hermes_cli/auth.py::_reset_config_provider": ("other", "clears base_url"),
    "hermes_cli/auth.py::resolve_api_key_provider_credentials": ("other", "API-key providers"),
    "hermes_cli/auth_codex.py::_probe_codex_pool_entry_quota_restored": ("other", "codex"),
    "hermes_cli/auth_model_picker.py::_save_model_choice": ("other", "model id only"),
    "hermes_cli/auth_qwen.py::_mark_qwen_oauth_active": ("other", "qwen"),
    "hermes_cli/auth_zai_kimi.py::_resolve_zai_base_url": ("other", "zai"),
    "hermes_cli/cli_init_mixin.py::CLIInitMixin._init_model_and_provider": ("transport", "loopback check"),
    "hermes_cli/codex_models.py::codex_catalog_credential_identity": ("transport", "codex catalog cache"),
    "hermes_cli/config.py::set_config_value": ("other", "user-entered value"),
    "hermes_cli/config_providers.py::_entries_for_route": ("entry", "custom entries"),
    "hermes_cli/config_providers.py::_normalize_custom_provider_entry": ("entry", "custom entries"),
    "hermes_cli/credential_lifecycle.py::_scrub_config_yaml_mirrors": ("other", "secret scrub"),
    "hermes_cli/doctor_config.py::_drift_stale_root_keys": ("other", "doctor"),
    "hermes_cli/main_provider_setup.py::_remove_custom_provider": ("entry", "custom entries"),
    "hermes_cli/main_provider_setup.py::_reset_aux_to_auto": ("other", "aux"),
    "hermes_cli/main_provider_setup.py::_save_aux_choice": ("other", "aux, user-entered"),
    "hermes_cli/main_provider_setup.py::_save_custom_provider": ("entry", "custom entries"),
    "hermes_cli/model_setup_flows.py::_model_flow_api_key_provider": ("other", "API-key providers"),
    "hermes_cli/model_setup_flows.py::_model_flow_stepfun": ("other", "stepfun"),
    "hermes_cli/model_setup_flows.py::_nous_persist_selection": ("other", "nous"),
    "hermes_cli/model_setup_flows_custom.py::_model_flow_custom": ("entry", "custom"),
    "hermes_cli/model_setup_flows_custom.py::_model_flow_named_custom": ("entry", "custom"),
    "hermes_cli/model_switch_providers.py::_save_discovered_models_to_config": ("entry", "custom models"),
    "hermes_cli/models_pricing.py::pricing_cache_scope": ("transport", "pricing cache"),
    "hermes_cli/observability/shared_metrics_catalog.py::user_named_model_providers": ("other", "metrics"),
    "hermes_cli/providers.py::get_provider": ("other", "registry lookup"),
    "hermes_cli/proxy/server.py::_open_upstream": ("transport", "hermes proxy upstream"),
    "hermes_cli/runtime_provider_custom.py::_route_key": ("entry", "custom entries"),
    "hermes_cli/tools_config_providers.py::_configure_vision_backend": ("other", "vision, user-entered"),
    "hermes_cli/tools_config_providers.py::_configure_vision_provider_model": ("other", "vision"),
    "hermes_cli/web_routers/config_env.py::activate_custom_endpoint": ("entry", "custom"),
    "hermes_cli/web_server_config.py::_apply_aux_assignment_sync": ("other", "aux"),
    "tools/cronjob_job_args.py::_custom_stored_key_error": ("other", "custom stored key"),
    "tui_gateway/methods_profiles.py::_inherit_launch_model": ("other", "raw config copy"),
}

_PRIM = re.compile(r"\b(normalize_route_base_url|route_key|canonical_xai_route|persisted_xai_route|persisted_route_patch|"
                   r"should_clear_context_pin(?:_async)?|_context_route_mismatch|selection_route_changed|"
                   r"_xai_oauth_persisted_base_url|_xai_oauth_runtime_base_url)\(")
_SINK = re.compile(r"\b(save_config|atomic_config_replace|write_config_keys|set_config_value|save_auth_store|"
                   r"_save_auth_store|_store_provider_state|patch_session_model_config|update_session_model|"
                   r"set_model_override|_persist_routing_data)\(")
_NAMES_DEFAULT = re.compile(r"xai-oauth|xai_oauth|XAI_OAUTH|grok-oauth|inference_base_url|PROVIDER_REGISTRY|"
                            r"base_url_override")
_CMP = re.compile(r"base_url[^\n]*(==|!=| in )|(==|!=)[^\n]*base_url")
_SKIP = ("tests/", ".venv", "venv/", "node_modules", "website/", "ui-tui/", "evals/", ".claude/", "scripts/",
         "build/", "docs/", "skills/", "optional-skills/")


def _anchors(seg: str) -> frozenset:
    out = set()
    if _PRIM.search(seg):
        out.add("A")
    if _SINK.search(seg) and "base_url" in seg:
        out.add("B")
    if _NAMES_DEFAULT.search(seg) and _CMP.search(seg):
        out.add("C")
    return frozenset(out)


def _scan() -> dict[str, str]:
    """``path::Qual.name`` -> source segment of the innermost function holding each anchor."""
    found: dict[str, str] = {}
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(_SKIP) or "/tests/" in rel or "/." in "/" + rel:
            continue
        src = path.read_text(encoding="utf-8", errors="ignore")
        if not (_PRIM.search(src) or _SINK.search(src) or _NAMES_DEFAULT.search(src)):
            continue
        tree = ast.parse(src)
        owned: set[int] = set()

        def visit(node, prefix):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    qual = f"{prefix}{child.name}"
                    visit(child, qual + ".")
                    if isinstance(child, ast.ClassDef):
                        continue
                    seg = ast.get_source_segment(src, child) or ""
                    mine = _anchors(seg)
                    # Anchors a nested function already owns are attributed to it, not to the parent.
                    inner = [found[k] for k in found if k.startswith(f"{rel}::{qual}.")]
                    if mine and not any(_anchors(s) >= mine for s in inner):
                        found[f"{rel}::{qual}"] = seg
                        owned.update(range(child.lineno, (child.end_lineno or child.lineno) + 1))
                else:
                    visit(child, prefix)

        visit(tree, "")
        loose = "\n".join(line for i, line in enumerate(src.splitlines(), 1)
                          if i not in owned and not line.lstrip().startswith(("import ", "from ")))
        if "A" in _anchors(loose):
            found[f"{rel}::<module>"] = loose
    return found


@pytest.fixture(scope="module")
def scanned():
    return _scan()


def test_every_site_is_registered(scanned):
    missing = sorted(set(scanned) - set(SITES))
    stale = sorted(set(SITES) - set(scanned))
    assert not missing, f"unclassified route-identity sites (add a SITES row): {missing}"
    assert not stale, f"SITES rows that no longer exist: {stale}"


_USES = {
    "compare": re.compile(r"\b(route_key|canonical_xai_route)\("),
    "read": re.compile(r"\b(canonical_xai_route|_xai_oauth_runtime_base_url|runtime_base_url)\b"),
    "pin": re.compile(r"\b(should_clear_context_pin(?:_async)?|_context_route_mismatch)\("),
}


@pytest.mark.parametrize("site", sorted(k for k, v in SITES.items() if v[0] in ("compare", "read", "pin", "persist")))
def test_site_kind_uses_the_mechanism(scanned, site):
    kind, note = SITES[site]
    seg = scanned.get(site, "")
    if kind == "persist":
        token = note.split()[0].rstrip(",")
        assert "persisted_xai_route(" in seg or token in seg, f"{site}: persist site lost {token}"
    else:
        assert _USES[kind].search(seg), f"{site}: {kind} site does not use the mechanism"


def test_alias_set_matches_the_provider_registry():
    from hermes_cli.providers import _ALIAS_GROUPS
    from hermes_cli.route_identity import XAI_OAUTH_PROVIDER_IDS
    assert XAI_OAUTH_PROVIDER_IDS == {"xai-oauth", *_ALIAS_GROUPS["xai-oauth"]}


# ── expectations ───────────────────────────────────────────────────────────────────────────────

def _same(provider, stored, active):
    """One route? xai-oauth: both default spellings are one route; a custom or foreign host is its own.
    xai (API key): only an equal spelling."""
    if provider == "xai-oauth":
        return stored in DEFAULT_SPELLINGS
    return STORED[stored].rstrip("/") == ACTIVE[active]


MATRIX = [(p, s, a) for p in PROVIDERS for s in STORED for a in ACTIVE]


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("model:\n  default: grok-4\n")
    (hermes_home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    for var in ("HERMES_XAI_BASE_URL", "XAI_BASE_URL", "XAI_API_KEY", "XAI_STT_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    return hermes_home


def _result(provider, base_url):
    from hermes_cli.model_switch import ModelSwitchResult
    return ModelSwitchResult(success=True, new_model="grok-4", target_provider=provider, base_url=base_url,
                             api_mode="codex_responses")


def _h_route_mismatch(p, s, a, mp):
    from agent.agent_init import _context_route_mismatch
    from hermes_cli.route_identity import normalize_route_base_url as n
    raw = not _context_route_mismatch(s, a, p, p)
    assert raw == (not _context_route_mismatch(n(s), n(a), p, p, already_normalized=True))
    return raw


def _h_startup_pin(p, s, a, mp):
    from agent.agent_init import _scope_context_length_to_default_runtime
    agent = SimpleNamespace(model="grok-4", provider=p, base_url=a)
    return _scope_context_length_to_default_runtime(
        agent, {}, {"default": "grok-4", "provider": p, "base_url": s}, [], 131072, None) == 131072


def _h_should_clear(p, s, a, mp):
    from hermes_cli.route_identity import should_clear_context_pin, should_clear_context_pin_async
    sync = not should_clear_context_pin("grok-4", "grok-4", s, a, p, p)
    assert sync == (not asyncio.run(should_clear_context_pin_async("grok-4", "grok-4", s, a, p, p)))
    return sync


def _h_effective_pin(p, s, a, mp):
    from hermes_cli.model_switch import effective_context_pin
    return effective_context_pin(131072, "grok-4", p, a, configured_model="grok-4", configured_provider=p,
                                 configured_base_url=s) == 131072


def _h_selection_updates(p, s, a, mp):
    from hermes_cli.model_switch import model_selection_config_updates
    updates = model_selection_config_updates(_result(p, a), {
        "default": "grok-4", "provider": p, "base_url": s, "context_length": 131072, "key_env": "XAI_KEY_ENV"})
    assert ("context_length" in updates) == ("key_env" in updates)  # one route verdict for both
    return "context_length" not in updates


def _h_selection_route_changed(p, s, a, mp):
    from hermes_cli.model_switch import selection_route_changed
    return not selection_route_changed({"provider": p, "base_url": s}, _result(p, a))


def _h_same_route(p, s, a, mp):
    from gateway.session_route_identity import same_route
    return same_route({"provider": p, "base_url": s}, {"provider": p, "base_url": a}, {})


def _h_model_route(p, s, a, mp):
    from gateway.session_policy import model_route
    return model_route({"provider": p, "base_url": s}) == model_route({"provider": p, "base_url": a})


def _h_pool_entry(p, s, a, mp):
    from agent.credential_pool import credential_pool_entry_serves_endpoint
    return credential_pool_entry_serves_endpoint(SimpleNamespace(provider=p, base_url=s), a)


def _h_override_runtime(p, s, a, mp):
    import gateway.run
    from gateway.run_turn_prepare import _resolve_session_override_runtime
    mp.setattr(gateway.run, "_resolve_runtime_agent_kwargs_for_provider",
               lambda provider, target_model=None: {"provider": provider, "base_url": a, "api_key": "synthetic"})
    try:
        _resolve_session_override_runtime({"provider": p, "base_url": s, "model": "grok-4"}, managed_oauth=True)
    except RuntimeError:
        return False
    return True


def _h_route_runtime(p, s, a, mp):
    import hermes_cli.runtime_provider as rp
    import hermes_cli.runtime_provider_custom as rpc
    from contextlib import nullcontext
    from gateway.session_worker_construct import route_runtime
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return {"provider": p}
    mp.setattr(rp, "frozen_runtime_config", lambda frozen: nullcontext())
    mp.setattr(rpc, "_resolve_named_custom_runtime", fake)
    policy = SimpleNamespace(request_json=json.dumps({"base_url": a}))
    route_runtime(policy, {"model": {"provider": p, "base_url": s}}, "grok-4", None)
    return seen["explicit_base_url"] == a


def _h_relay(p, s, a, mp):
    from hermes_cli import models
    mp.setattr(models, "_get_model_config_dict", lambda: {"provider": p, "base_url": s})
    return models._configured_relay_base_url(p) == ""


def _relay_expect(p, s, a):  # the provider's own endpoint is the "active" side here
    return s in DEFAULT_SPELLINGS if p == "xai-oauth" else s in ("legacy", "legacy-slash")


def _h_foreign(p, s, a, mp):
    from hermes_cli.runtime_provider import is_foreign_provider_endpoint
    return not is_foreign_provider_endpoint(p, s)


def _foreign_expect(p, s, a):  # legacy is xai-oauth's own; the proxy is foreign to the API-key provider
    return not (p == "xai" and s == "proxy")


def _h_managed(p, s, a, mp):
    from gateway.run_turn_prepare import _session_override_uses_managed_oauth
    if a == LEGACY:  # active legacy = the documented env override names the API URL
        mp.setenv("HERMES_XAI_BASE_URL", LEGACY)
    return _session_override_uses_managed_oauth({"provider": p, "base_url": s})


def _managed_expect(p, s, a):
    return p == "xai-oauth" and s in DEFAULT_SPELLINGS


COMPARE = {
    "agent/agent_init.py::_context_route_mismatch": (_h_route_mismatch, _same),
    "agent/agent_init.py::_scope_context_length_to_default_runtime": (_h_startup_pin, _same),
    "hermes_cli/route_identity.py::should_clear_context_pin": (_h_should_clear, _same),
    "hermes_cli/model_switch.py::effective_context_pin": (_h_effective_pin, _same),
    "hermes_cli/model_switch.py::model_selection_config_updates": (_h_selection_updates, _same),
    "hermes_cli/model_switch.py::selection_route_changed": (_h_selection_route_changed, _same),
    "gateway/session_route_identity.py::same_route": (_h_same_route, _same),
    "gateway/session_policy.py::model_route": (_h_model_route, _same),
    "agent/credential_pool.py::credential_pool_entry_serves_endpoint": (_h_pool_entry, _same),
    "gateway/run_turn_prepare.py::_resolve_session_override_runtime": (_h_override_runtime, _same),
    "gateway/session_worker_construct.py::route_runtime": (_h_route_runtime, _same),
    "hermes_cli/models.py::_configured_relay_base_url": (_h_relay, _relay_expect),
    "hermes_cli/runtime_provider.py::is_foreign_provider_endpoint": (_h_foreign, _foreign_expect),
    "gateway/run_turn_prepare.py::_session_override_uses_managed_oauth": (_h_managed, _managed_expect),
}
# Compare sites without a matrix harness, and why (their static check above still holds).
STATIC_ONLY = {
    "agent/agent_init.py::_provider_default_routes.add": "test_default_routes_hold_both_spellings",
    "gateway/session_mutation_model.py::_selection_guard_refusal": "async authority; token over route_key",
    "gateway/run.py::_resolve_gateway_model_context._pin_still_applies": "closure; -> should_clear_context_pin (matrix above)",
    "gateway/run_inbound.py::GatewayInboundMixin._inbound_model_context_length": "async; -> should_clear_context_pin_async",
    "gateway/run_turn_hygiene.py::GatewayTurnHygieneMixin._hmwa_hygiene_settings": "async; -> should_clear_context_pin_async",
    "hermes_cli/route_identity.py::should_clear_context_pin_async": "covered by the sync harness",
}


def test_every_compare_site_has_a_matrix_or_a_reason():
    compare = {k for k, v in SITES.items() if v[0] in ("compare", "pin")}
    assert not compare - set(COMPARE) - set(STATIC_ONLY)
    assert set(COMPARE) | set(STATIC_ONLY) <= set(SITES)


@pytest.mark.parametrize("site", sorted(COMPARE))
@pytest.mark.parametrize(("provider", "stored", "active"), MATRIX)
def test_compare_site_matrix(home, monkeypatch, site, provider, stored, active):
    harness, expect = COMPARE[site]
    assert harness(provider, STORED[stored], ACTIVE[active], monkeypatch) == expect(provider, stored, active), (
        f"{site}: provider={provider} stored={stored} active={active}")


def test_default_routes_hold_both_spellings():
    from agent.agent_init import _provider_default_routes
    from hermes_cli.route_identity import route_key
    routes = _provider_default_routes("xai-oauth")
    assert route_key("xai-oauth", LEGACY) in routes and route_key("xai-oauth", PROXY) in routes
    assert PROXY in routes and LEGACY not in routes  # stored in the live spelling
    assert LEGACY in _provider_default_routes("xai") and PROXY not in _provider_default_routes("xai")


# ── persist and read sites ────────────────────────────────────────────────────────────────────

def _expect_stored(provider, url):
    return LEGACY if provider == "xai-oauth" and url.rstrip("/") == PROXY else url


def _p_selection(p, url, home):
    from hermes_cli.model_switch import model_selection_config_updates
    return model_selection_config_updates(_result(p, url), {})["base_url"]


def _p_sanitize(p, url, home):
    from gateway.session import sanitize_model_override
    return sanitize_model_override({"model": "grok-4", "provider": p, "base_url": url})["base_url"]


def _session_db(home):
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    db.create_session("s1", "cli", model="grok-4")
    return db


def _row(db):
    return json.loads(db.get_session("s1")["model_config"])


def _p_update_session_model(p, url, home):
    db = _session_db(home)
    db.update_session_model("s1", "grok-4", provider=p, base_url=url, api_mode="codex_responses")
    cfg = _row(db)
    assert cfg["base_url"] == cfg["gateway_runtime"]["base_url"]
    return cfg["base_url"]


def _p_patch_session(p, url, home):  # the CLI /model writer's shape
    db = _session_db(home)
    route = {"provider": p, "base_url": url, "api_mode": "codex_responses"}
    db.patch_session_model_config("s1", {"gateway_runtime": route, **route})
    cfg = _row(db)
    assert cfg["base_url"] == cfg["gateway_runtime"]["base_url"]
    return cfg["base_url"]


def _p_login_config(p, url, home):
    import hermes_yaml as yaml
    from hermes_cli.auth import _update_config_for_provider
    _update_config_for_provider(p, url)
    return yaml.safe_load((home / "config.yaml").read_text())["model"]["base_url"]


PERSIST = {
    "hermes_cli/model_switch.py::model_selection_config_updates": _p_selection,
    "gateway/session.py::sanitize_model_override": _p_sanitize,
    "hermes_state_sessions.py::SessionSessionsMixin.update_session_model": _p_update_session_model,
    "hermes_cli/route_identity.py::persisted_route_patch": _p_patch_session,
    "hermes_cli/auth.py::_update_config_for_provider": _p_login_config,
}


@pytest.mark.parametrize("site", sorted(PERSIST))
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("active", [PROXY, PROXY + "/", LEGACY, CUSTOM, FOREIGN])
def test_persist_site_matrix(home, site, provider, active):
    assert PERSIST[site](provider, active, home).rstrip("/") == _expect_stored(provider, active).rstrip("/")


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("stored", sorted(STORED))
def test_session_route_reads_back_live(home, provider, stored):
    from hermes_state import SessionDB
    row = {"model_config": {"gateway_runtime": {"provider": provider, "base_url": STORED[stored]}}}
    got = SessionDB.session_gateway_runtime(row)["base_url"]
    want = PROXY if provider == "xai-oauth" and stored in ("legacy", "legacy-slash") else STORED[stored]
    assert got == want
    # And a /model write followed by resume returns the route that was switched to.
    db = _session_db(home)
    db.update_session_model("s1", "grok-4", provider=provider, base_url=PROXY)
    assert SessionDB.session_gateway_runtime(db.get_session("s1"))["base_url"] == PROXY


@pytest.mark.parametrize("stored", sorted(STORED))
def test_auth_row_reads_back_live(stored):
    from hermes_cli.auth_xai import _xai_oauth_runtime_base_url
    want = {"custom": CUSTOM, "foreign": PROXY}.get(stored, PROXY)  # foreign: refused, default route
    assert _xai_oauth_runtime_base_url(STORED[stored]) == want


# ── resolution with refresh outcomes ──────────────────────────────────────────────────────────

class _Runner:
    """Just what ``_rehydrate_session_model_override`` reads."""

    def __init__(self, persisted):
        from gateway.run_agent_cache import GatewayAgentCacheMixin
        self._rehydrate = GatewayAgentCacheMixin._rehydrate_session_model_override.__get__(self)
        self.session_store = SimpleNamespace(get_model_override=lambda key: dict(persisted))
        self.state = SimpleNamespace(conversation=SimpleNamespace(model_override=None))

    def _session_model_override(self, key):
        return self.state.conversation.model_override

    def _session_state(self, key):
        return self.state


def _outcome(kind, provider, active):
    from hermes_cli.auth_constants import AuthError

    def resolve(_provider, target_model=None):
        if kind == "auth_error":
            raise AuthError("xAI token refresh failed (synthetic)", provider="xai-oauth", code="xai_refresh_failed")
        if kind == "timeout":
            raise TimeoutError("synthetic refresh timeout")
        return {"provider": provider, "base_url": active, "api_key": "synthetic", "api_mode": "codex_responses"}
    return resolve


@pytest.mark.parametrize("outcome", ["ok", "auth_error", "timeout"])
@pytest.mark.parametrize(("provider", "stored", "active"), MATRIX)
def test_rehydrated_override_under_refresh_outcomes(home, monkeypatch, outcome, provider, stored, active):
    """A persisted /model override, re-resolved at restart: whatever the refresh does, the override keeps
    the live spelling of its stored route, and a managed xai-oauth override still counts as managed."""
    import gateway.run
    from gateway.run_turn_prepare import _session_override_uses_managed_oauth
    monkeypatch.setattr(gateway.run, "_resolve_runtime_agent_kwargs_for_provider",
                        _outcome(outcome, provider, ACTIVE[active]))
    runner = _Runner({"model": "grok-4", "provider": provider, "base_url": STORED[stored]})
    runner._rehydrate("k")
    override = runner.state.conversation.model_override
    from hermes_cli.route_identity import canonical_xai_route
    if provider == "xai" and stored == "proxy":  # another provider's endpoint: dropped, re-resolved on success
        want = ACTIVE[active] if outcome == "ok" else None
    else:
        want = canonical_xai_route(provider, STORED[stored])
    assert override["base_url"] == want
    assert (override.get("api_key") == "synthetic") == (outcome == "ok")
    assert _session_override_uses_managed_oauth(override) == (provider == "xai-oauth" and stored in DEFAULT_SPELLINGS)


def _expiring_login(home):
    import base64
    import time

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    jwt = f"{b64({'alg': 'none'})}.{b64({'exp': int(time.time()) + 30})}.sig"  # inside the refresh skew
    from hermes_cli import auth_xai
    auth_xai._save_xai_oauth_tokens({"access_token": jwt, "refresh_token": "synthetic-refresh"},
                                    discovery={"token_endpoint": "https://auth.x.ai/oauth2/token"})


def _token_endpoint(monkeypatch, outcome):
    """Every refresh path (singleton and pool) ends in ``httpx.Client.post`` to the token endpoint."""
    import httpx

    def post(self, url, *a, **kw):
        request = httpx.Request("POST", str(url))
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic refresh timeout", request=request)
        if outcome == "rejected":
            return httpx.Response(400, json={"error": "invalid_grant"}, request=request)
        if outcome == "unavailable":
            return httpx.Response(503, text="synthetic outage", request=request)
        return httpx.Response(200, request=request, json={
            "access_token": "synthetic-fresh", "refresh_token": "synthetic-refresh-2", "expires_in": 21600,
            "token_type": "Bearer"})
    monkeypatch.setattr(httpx.Client, "post", post)


REFRESH_OUTCOMES = ["ok", "rejected", "unavailable", "timeout"]


@pytest.mark.parametrize("outcome", REFRESH_OUTCOMES)
@pytest.mark.parametrize("stored", sorted(STORED))
def test_global_switch_under_refresh_outcomes(home, monkeypatch, outcome, stored):
    """Login -> ``/model grok-4 --global`` with the refresh succeeding, rejected or timing out: the pin survives
    a same-route switch, a failed resolution writes nothing, and no stored row ever carries the proxy."""
    import httpx
    from hermes_cli.auth_constants import AuthError
    from hermes_cli.model_switch import ModelSwitchResult, persist_model_selection
    from hermes_cli.runtime_provider import resolve_runtime_provider
    (home / "config.yaml").write_text(
        f"model:\n  default: grok-4\n  provider: xai-oauth\n  base_url: {STORED[stored]}\n  context_length: 131072\n")
    _expiring_login(home)
    _token_endpoint(monkeypatch, outcome)
    before = (home / "config.yaml").read_text()
    try:
        rt = resolve_runtime_provider(requested="xai-oauth")
    except (AuthError, httpx.HTTPError):  # a refresh timeout surfaces as httpx's own error (fails closed)
        assert outcome != "ok"
        assert (home / "config.yaml").read_text() == before
    else:
        assert outcome == "ok" and rt["base_url"] == PROXY
        persist_model_selection(ModelSwitchResult(success=True, new_model="grok-4", target_provider="xai-oauth",
                                                  base_url=rt["base_url"], api_mode=rt["api_mode"]),
                                home / "config.yaml")
        import hermes_yaml as yaml
        model = yaml.safe_load((home / "config.yaml").read_text())["model"]
        assert model["base_url"] == LEGACY
        assert ("context_length" in model) == (stored in DEFAULT_SPELLINGS)
    for name in ("config.yaml", "auth.json"):
        assert "cli-chat-proxy" not in (home / name).read_text() or stored == "proxy"


@pytest.mark.parametrize("outcome", ["auth_error", "timeout"])
@pytest.mark.parametrize("override", [PROXY, LEGACY, CUSTOM, FOREIGN, ""])
def test_api_key_fallback_after_a_failed_refresh_never_reaches_the_proxy(home, monkeypatch, outcome, override):
    """x_search-style direct call: the pool refresh fails, the resolver falls back to XAI_API_KEY, and the key
    goes to a validated xAI API origin only (never the proxy, never a foreign host)."""
    import agent.credential_pool as cp
    from tools.xai_http import resolve_xai_http_credentials

    class Pool:
        def try_refresh_matching(self, hint):
            raise TimeoutError("synthetic") if outcome == "timeout" else RuntimeError("refresh rejected (synthetic)")

        def select(self):
            return None
    monkeypatch.setattr(cp, "load_pool", lambda provider: Pool())
    monkeypatch.setenv("XAI_API_KEY", "synthetic-api-key")
    if override:
        monkeypatch.setenv("XAI_BASE_URL", override)
    creds = resolve_xai_http_credentials(force_refresh=True, api_key_hint="old", subscription_route=True)
    assert creds["provider"] == "xai" and creds["api_key"] == "synthetic-api-key"
    assert creds["base_url"] == (CUSTOM if override == CUSTOM else LEGACY)
