"""Run a vault fill, or an automatic one-time-code entry, inside the selected backend's ``fill_context``
(a plugin backend's lock/snapshot/manager session).

Order, for every backend: the item's metadata is read and the page origin checked against it
(``_bound_page_origin``) BEFORE the context is entered, so a page on the wrong origin never opens a
backend's manager session or lock. Inside the context a backend with its own ``fill_context`` has the
item re-read, so the origins checked and the secret resolved come from one snapshot.

A plugin backend's failure arrives as ``PluginBackendError`` (fixed text, see
``agent/vault_backends/plugin.py``) and becomes a typed refusal; built-in backends behave as before.
"""
import json
from functools import wraps


def plugin_errors_as_refusal(fn):
    """A ``PluginBackendError`` raised anywhere in ``fn`` (metadata read, context enter/exit, resolve)
    becomes the ``backend_unavailable`` refusal."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        from agent.vault_backends.plugin import PluginBackendError

        try:
            return fn(*args, **kwargs)
        except PluginBackendError as exc:
            return json.dumps({"success": False, "error_type": "backend_unavailable", "error": str(exc)})
    return wrapped


def saved_origins(meta) -> list[str]:
    """The exact origins a vault item may be filled on (several for manager items)."""
    return list(meta.allowed_origins) or ([str(meta.origin)] if meta.origin else [])


def _has_own_context(backend) -> bool:
    """True when the backend overrides ``LoginBackend.fill_context`` (a plugin lock/snapshot/session).
    dlz: a duck-typed backend with no ``fill_context`` at all freezes nothing, like the default."""
    from agent.vault_backends.base import LoginBackend

    own = getattr(type(backend), "fill_context", None)
    return own is not None and own is not LoginBackend.fill_context


def fill_context(backend):
    """``backend.fill_context()``, or a no-op context for a backend that has none (the default's behaviour)."""
    import contextlib

    return backend.fill_context() if hasattr(backend, "fill_context") else contextlib.nullcontext()


def snapshot_refusal(backend, handle, meta, page_origin):
    """Inside ``fill_context``: ``None`` when the item still binds ``page_origin``, else the refusal.
    Only a backend with its own context is re-read (the built-in managers' metadata read is a full
    listing, and their default context freezes nothing)."""
    if not _has_own_context(backend):
        return None
    fresh = backend.get_meta(handle)
    if fresh is not None and fresh.kind == meta.kind and page_origin in saved_origins(fresh):
        return None
    return {"success": False, "error_type": "origin_mismatch",
            "error": (f"Refused: the saved item changed before the fill and no longer binds {page_origin}. "
                      "Nothing was resolved or written.")}


def write_backend_code(backend, handle, meta, origin, write):
    """Mint the backend's one-time code for ``handle`` and ``write`` it, both inside ``fill_context``, as a
    password fill is. The caller has already checked ``origin`` against ``meta``. ``None`` when the backend
    has no code (or failed to give one): the caller asks the user."""
    from agent.vault_backends import BackendError
    from agent.vault_backends.plugin import PluginBackendError

    written = None
    try:
        with fill_context(backend):
            refusal = snapshot_refusal(backend, handle, meta, origin)
            if refusal is not None:
                return json.dumps(refusal)
            try:
                code = backend.resolve_otp(handle)
            except BackendError as exc:
                # dlz: a typed manager outage (rate budget, expired token, network) is reported as itself,
                # never as "save an authenticator key"; an untyped op failure still falls back to asking.
                if type(exc) is not BackendError:
                    return json.dumps(exc.result())
                code = None
            except Exception:  # health: allow BLE001 -- no automatic code; the user is asked instead
                code = None
            if code:
                written = write(code)
    except PluginBackendError:  # the plugin's context failed to open or close; a code already written stands
        pass
    return written
