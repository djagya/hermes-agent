"""dlz: owner-authenticated callback intake for a native ``claude setup-token`` run.

A containerized Hermes can start ``claude setup-token`` but the owner's browser cannot reach the
CLI's dynamically allocated ``http://localhost:<port>/callback``. This broker owns that native CLI
run and lets the authenticated owner hand the provider's callback URL back to it, on the dashboard
or (one-time callback material, owner policy 2026-11-09) pasted in chat:

    model tool start -> broker spawns the CLI on a private PTY (its PKCE verifier never leaves it)
    -> the CLI-issued authorize URL is parsed and allowlisted, never rewritten
    -> the owner opens the dashboard handoff page (dashboard auth), approves at the provider, and
       pastes the callback URL there -> dashboard -> gateway control socket (same-uid peer) ->
       :meth:`Broker.submit` validates it against the recorded redirect + state, claims the one
       delivery and sends it to the recorded loopback listener only
       (dlz: or Danil pastes it in chat and the starting session's model tool hands it to
       :meth:`Broker.submit_from_session`, which runs the same validation and one-shot delivery)
    -> the CLI does its own token exchange and prints the token on the PTY
    -> the broker extracts it privately, stores it in the concealed sink, compares a private readback
    -> the model reads status and opaque handles only.

Nothing secret (callback, code, state, authorize URL, token, PTY bytes, stderr) is logged, raised or
returned to the model. The broker never installs the token as the runtime credential: storing a
pending credential and proving it can make a model request are separate, operator-owned steps, so
the previous credential is untouched by every path here.

Trust boundary: same-uid code in this container can read process memory and the dashboard token; as
in ``agent.vault_backends.unlock`` this stops the model-tool and chat channels, not local code.
Flows live in memory only: a gateway restart forgets every flow, so no stale intake can deliver into
a later run.
"""

from __future__ import annotations

import contextlib
import hmac
import http.client
import logging
import os
import re
import secrets
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

logger = logging.getLogger(__name__)

PROVIDER = "claude"
PURPOSE = "setup-token"
CONTROL_VERB = "claude-setup-token"
DEFAULT_TIMEOUT_S = 10 * 60
# What `claude setup-token` (2.1.x) emits: https://claude.com/cai/oauth/authorize?...
_AUTHORIZE_HOSTS = frozenset({"claude.com", "claude.ai"})
_AUTHORIZE_PATHS = frozenset({"/cai/oauth/authorize", "/oauth/authorize"})
# Recorded redirect host -> literal loopback addresses we dial (never a DNS lookup).
_LOOPBACK = {"localhost": ("127.0.0.1", "::1"), "127.0.0.1": ("127.0.0.1",), "::1": ("::1",)}
_TOKEN_RE = re.compile(rb"sk-ant-oat01-[A-Za-z0-9_-]{40,512}")
_URL_RE = re.compile(rb"https://[!-~]+")
_ANSI_RE = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
# RFC 3986 query characters with well-formed percent escapes only.
_QUERY_RE = re.compile(r"^(?:[A-Za-z0-9\-._~!$&'()*+,;=:@/?]|%[0-9A-Fa-f]{2})*$")
_VALUE_RE = re.compile(r"^[\x21-\x7e]{1,2048}$")
_MAX_CALLBACK_CHARS = 4096
_MAX_OUTPUT_BYTES = 1 << 20
# A CLI still running this long after printing a complete token is stopped and the token kept.
_TOKEN_LINGER_S = 10.0

# Flow states the model and the owner page may see.
STARTING, AWAITING, DELIVERING, DELIVERED, DELIVERY_UNKNOWN = (
    "starting", "awaiting_callback", "delivering", "delivered", "delivery_unknown")
STORED, FAILED, CANCELLED, EXPIRED = "stored_pending", "failed", "cancelled", "expired"
_TERMINAL = frozenset({STORED, FAILED, CANCELLED, EXPIRED})


class IntakeRejected(Exception):
    """A refused submission. ``code`` is a fixed identifier; nothing from the request is kept."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class CredentialSink(Protocol):
    """The approved concealed destination for an issued token."""

    def store_pending(self, flow_id: str, secret: str) -> str: ...  # -> opaque handle
    def readback_matches(self, handle: str, secret: str) -> bool: ...


class FilePendingSink:
    """Pending token in a mode-0600 file under a mode-0700 directory of the profile home. It never
    touches the runtime credential; promotion is a separate operator step."""

    def __init__(self, root: Optional[Path] = None):
        if root is None:
            from hermes_constants import get_hermes_home
            root = get_hermes_home() / "auth" / "claude-setup-token"
        self.root = Path(root)

    def _path(self, handle: str) -> Path:
        if not re.fullmatch(r"pending-[A-Za-z0-9_-]{8,64}", handle):
            raise ValueError("bad sink handle")
        return self.root / f"{handle}.token"

    def store_pending(self, flow_id: str, secret: str) -> str:
        from utils import atomic_write_bytes
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        handle = f"pending-{flow_id}"
        atomic_write_bytes(self._path(handle), secret.encode("ascii"), mode=0o600)
        return handle

    def readback_matches(self, handle: str, secret: str) -> bool:
        try:
            stored = self._path(handle).read_bytes()
        except (OSError, ValueError):
            return False
        return hmac.compare_digest(stored, secret.encode("ascii"))


@dataclass(frozen=True)
class NativeRequest:
    """What the CLI issued: kept verbatim for the owner, parsed for validation."""
    authorize_url: str = field(repr=False)
    redirect_host: str
    redirect_port: int
    redirect_path: str
    state: str = field(repr=False)


def parse_authorize_url(url: str) -> NativeRequest:
    """Validate a CLI-issued authorize URL: HTTPS, allowlisted provider host/path, PKCE S256, one of
    each parameter, and a loopback redirect. Raises ``ValueError`` (fixed text) otherwise."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError("authorize url malformed") from None
    if (parts.scheme != "https" or parts.username is not None or parts.password is not None
            or (parts.hostname or "") not in _AUTHORIZE_HOSTS or port not in (None, 443)
            or parts.path not in _AUTHORIZE_PATHS or parts.fragment or "#" in url):
        raise ValueError("authorize url not allowlisted")
    q = _strict_query(parts.query)
    if (q.get("response_type") != "code" or q.get("code_challenge_method") != "S256"
            or not q.get("code_challenge") or not q.get("client_id") or not q.get("state")
            or "user:inference" not in (q.get("scope") or "").split()):
        raise ValueError("authorize url is not a PKCE setup-token request")
    if not _VALUE_RE.match(q["state"]):
        raise ValueError("authorize url state malformed")
    host, rport, rpath = _parse_loopback_redirect(q.get("redirect_uri") or "")
    return NativeRequest(url, host, rport, rpath, q["state"])


def _parse_loopback_redirect(uri: str) -> tuple[str, int, str]:
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError:
        raise ValueError("redirect malformed") from None
    host = parts.hostname or ""
    if (parts.scheme != "http" or parts.username is not None or parts.password is not None
            or host not in _LOOPBACK or port is None or not 1024 <= port <= 65535
            or not parts.path.startswith("/") or parts.query or parts.fragment or "#" in uri):
        raise ValueError("redirect is not a loopback callback")
    return host, port, parts.path


def _strict_query(query: str) -> dict[str, str]:
    """One value per key, well-formed escapes only; raises ``ValueError``."""
    if not _QUERY_RE.match(query or ""):
        raise ValueError("query malformed")
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query),
                          errors="strict", max_num_fields=32)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("query malformed") from None
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("query has duplicated fields")
    return dict(pairs)


def parse_callback(raw: str, native: NativeRequest) -> tuple[Optional[str], str]:
    """Owner-submitted callback URL -> ``(code, state)`` or ``(None, state)`` for a provider refusal.
    Must match the recorded redirect exactly and carry the recorded state. Raises ``IntakeRejected``."""
    if not isinstance(raw, str):
        raise IntakeRejected("callback_malformed")
    raw = raw.strip()
    if not raw or len(raw) > _MAX_CALLBACK_CHARS or any(c.isspace() or ord(c) < 0x21 or ord(c) > 0x7e for c in raw):
        raise IntakeRejected("callback_malformed")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise IntakeRejected("callback_malformed") from None
    if "#" in raw or parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise IntakeRejected("callback_malformed")
    if (parts.scheme != "http" or (parts.hostname or "") != native.redirect_host
            or port != native.redirect_port or unquote(parts.path) != parts.path
            or parts.path != native.redirect_path):
        raise IntakeRejected("callback_wrong_redirect")
    try:
        q = _strict_query(parts.query)
    except ValueError:
        raise IntakeRejected("callback_malformed") from None
    state = q.get("state") or ""
    if not state or not hmac.compare_digest(state.encode(), native.state.encode()):
        raise IntakeRejected("callback_wrong_state")
    if "error" in q:
        if set(q) - {"error", "error_description", "error_uri", "state"}:
            raise IntakeRejected("callback_malformed")
        return None, state
    if set(q) != {"code", "state"} or not _VALUE_RE.match(q["code"]):
        raise IntakeRejected("callback_malformed")
    return q["code"], state


@dataclass(eq=False)
class Flow:
    flow_id: str
    handoff_id: str
    profile: str
    session_id: str
    deadline: float
    sink: CredentialSink = field(repr=False)
    provider: str = PROVIDER
    purpose: str = PURPOSE
    owner: Optional[str] = None  # the dashboard principal allowed to view/submit, fixed at start
    state: str = STARTING
    reason: Optional[str] = None
    native: Optional[NativeRequest] = field(default=None, repr=False)
    proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    pgid: Optional[int] = None
    master_fd: Optional[int] = field(default=None, repr=False)
    output: bytearray = field(default_factory=bytearray, repr=False)
    overflow: bool = False
    token_seen_at: Optional[float] = None
    stopped_after_token: bool = False
    token_issued: bool = False
    sink_handle: Optional[str] = None
    readback_ok: bool = False
    url_ready: threading.Event = field(default_factory=threading.Event, repr=False)
    finished: threading.Event = field(default_factory=threading.Event, repr=False)

    def model_view(self) -> dict:
        """Status and opaque handles only."""
        return {"flow": self.flow_id, "state": self.state, "reason": self.reason,
                "expires_in_s": max(0, int(self.deadline - time.time())) if self.state not in _TERMINAL else 0,
                "token_issued": self.token_issued, "stored_pending": self.sink_handle is not None,
                "readback_ok": self.readback_ok, "sink_handle": self.sink_handle,
                # Installing the token as the runtime credential and proving a model request with it
                # are separate operator steps; nothing here changes the current credential.
                "installed": False, "model_request_verified": None}


def _linux_listener_in_group(port: int, pgid: int) -> Optional[bool]:
    """Linux: are all TCP listeners on *port* held by processes of *pgid*? None where /proc is absent."""
    if not sys.platform.startswith("linux") or not os.path.isdir("/proc/net"):
        return None
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            cols = line.split()
            if len(cols) > 9 and cols[3] == "0A" and int(cols[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(f"socket:[{cols[9]}]")
    if not inodes:
        return False
    owned = set()
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            if os.getpgid(int(pid_dir.name)) != pgid:
                continue
            for fd in (pid_dir / "fd").iterdir():
                link = os.readlink(fd)
                if link in inodes:
                    owned.add(link)
        except OSError:
            continue
    # Every listener on that port, IPv4 and IPv6 alike: a foreign socket on the other family would
    # otherwise receive the dial meant for the CLI.
    return owned == inodes


def _complete_token(output: bytearray) -> Optional[str]:
    """The one distinct token the CLI printed on a full line, else None."""
    text = _ANSI_RE.sub(b"", bytes(output))
    found = {m.group(0) for m in _TOKEN_RE.finditer(text) if text[m.end():m.end() + 1] in (b"\r", b"\n")}
    return found.pop().decode("ascii") if len(found) == 1 else None


class Broker:
    """In-memory flow table for one gateway process."""

    def __init__(self, *, argv: Optional[list[str]] = None, sink_factory: Optional[Callable[[], CredentialSink]] = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S, url_wait_s: float = 30.0,
                 listener_check: Callable[[int, int], Optional[bool]] = _linux_listener_in_group,
                 delivery_timeout_s: float = 15.0):
        self._argv = argv
        self._sink_factory = sink_factory or FilePendingSink
        self._timeout_s = timeout_s
        self._url_wait_s = url_wait_s
        self._listener_check = listener_check
        self._delivery_timeout_s = delivery_timeout_s
        self._lock = threading.RLock()
        self._flows: dict[str, Flow] = {}
        self._by_handoff: dict[str, Flow] = {}

    # -- model side -------------------------------------------------------------------------------

    def start(self, *, profile: str, session_id: str, owner: str) -> Flow:
        """Spawn the native CLI on a private PTY and wait for its authorize URL. One live flow per
        profile+session; a second start returns the live one."""
        with self._lock:
            now = time.time()
            for stale in [f for f in self._flows.values() if f.state in _TERMINAL and f.deadline < now - 3600]:
                self._flows.pop(stale.flow_id, None)
            for flow in self._flows.values():
                if flow.profile == profile and flow.session_id == session_id and flow.state not in _TERMINAL:
                    return flow
            if not owner:
                raise IntakeRejected("no_owner")
            flow = Flow(flow_id=secrets.token_urlsafe(18), handoff_id=secrets.token_urlsafe(24),
                        profile=profile, session_id=session_id, deadline=time.time() + self._timeout_s,
                        sink=self._sink_factory(), owner=owner)
            self._flows[flow.flow_id] = flow
            self._by_handoff[flow.handoff_id] = flow
            # Spawned under the lock: a concurrent start/cancel sees the flow only with its process.
            try:
                self._spawn(flow)
            except Exception:
                self._finish(flow, FAILED, "cli_start_failed")
                return flow
        if not flow.url_ready.wait(self._url_wait_s) and flow.state == STARTING:
            self._finish(flow, FAILED, "no_authorize_url", kill=True)
        return flow

    def _owned(self, flow_id: str, profile: str, session_id: str) -> Flow:
        with self._lock:
            flow = self._flows.get(flow_id) if isinstance(flow_id, str) else None
        if flow is None or flow.profile != profile or flow.session_id != session_id:
            raise IntakeRejected("not_found")
        return flow

    def status(self, flow_id: str, *, profile: str, session_id: str) -> Flow:
        flow = self._owned(flow_id, profile, session_id)
        self._expire_if_due(flow)
        return flow

    def cancel(self, flow_id: str, *, profile: str, session_id: str) -> Flow:
        flow = self._owned(flow_id, profile, session_id)
        with self._lock:
            if flow.state in (AWAITING, STARTING):
                self._finish(flow, CANCELLED, "cancelled", kill=True)
            elif flow.state in (DELIVERING, DELIVERED, DELIVERY_UNKNOWN):
                # The code may already be with the CLI: stop it, but say the outcome is unknown.
                self._finish(flow, CANCELLED, "cancelled_after_delivery", kill=True)
        return flow

    # -- owner side (dashboard -> control socket) -------------------------------------------------

    def _owner_flow(self, handoff_id: str, profile: str, principal: str) -> Flow:
        if not isinstance(handoff_id, str) or not isinstance(principal, str) or not principal:
            raise IntakeRejected("not_found")
        with self._lock:
            flow = self._by_handoff.get(handoff_id)
            if flow is None or flow.profile != profile:
                raise IntakeRejected("not_found")
            if not flow.owner or not hmac.compare_digest(flow.owner.encode(), principal.encode()):
                raise IntakeRejected("not_owner")
        self._expire_if_due(flow)
        return flow

    def view(self, handoff_id: str, *, profile: str, principal: str) -> dict:
        """What the owner's handoff page shows: the CLI-issued authorize URL while it is awaited."""
        flow = self._owner_flow(handoff_id, profile, principal)
        out = {"state": flow.state, "reason": flow.reason, "expires_in_s": flow.model_view()["expires_in_s"],
               "token_issued": flow.token_issued, "readback_ok": flow.readback_ok}
        if flow.state == AWAITING and flow.native is not None:
            out["authorize_url"] = flow.native.authorize_url
        return out

    def submit(self, handoff_id: str, callback: str, *, profile: str, principal: str) -> dict:
        """Validate the owner's callback and deliver it once to the recorded live CLI listener."""
        return self._submit_flow(self._owner_flow(handoff_id, profile, principal), callback)

    def submit_from_session(self, flow_id: str, callback: str, *, profile: str, session_id: str) -> dict:
        """dlz: the callback URL Danil pasted in chat (owner policy 2026-11-09: one-time OAuth callback
        material may travel via chat), handed in by the model tool of the session that started the flow.
        Same strict parse and one-shot delivery as :meth:`submit`; the answer is status only."""
        flow = self._owned(flow_id, profile, session_id)
        self._expire_if_due(flow)
        return self._submit_flow(flow, callback)

    def _submit_flow(self, flow: Flow, callback: str) -> dict:
        with self._lock:
            if flow.state != AWAITING or flow.native is None:
                raise IntakeRejected("not_awaiting" if flow.state not in _TERMINAL else flow.state)
            code, state = parse_callback(callback, flow.native)
            if code is None:
                self._finish(flow, FAILED, "provider_rejected", kill=True)
                return {"state": flow.state, "reason": flow.reason}
            if not self._process_live(flow):
                self._finish(flow, FAILED, "cli_gone")
                raise IntakeRejected("cli_gone")
            flow.state = DELIVERING  # the one claim: every other submit now sees not_awaiting
        outcome = self._deliver(flow, code, state)
        del code, state
        with self._lock:
            if flow.state == DELIVERING:
                if outcome == "delivered":
                    flow.state = DELIVERED
                elif outcome == "unknown":
                    flow.state, flow.reason = DELIVERY_UNKNOWN, "delivery_outcome_unknown"
                else:
                    self._finish(flow, FAILED, "not_delivered", kill=True)
        return {"state": flow.state, "reason": flow.reason}

    # -- internals --------------------------------------------------------------------------------

    def _process_live(self, flow: Flow) -> bool:
        """Recheck process identity right before delivery: we hold the un-reaped child, so its PID
        and process group cannot have been reused while ``poll()`` is None."""
        if time.time() >= flow.deadline or flow.proc is None or flow.proc.poll() is not None:
            return False
        if flow.pgid is None or flow.native is None:
            return False
        # Fail closed where the listener cannot be attributed to the CLI's process group (non-Linux).
        return self._listener_check(flow.native.redirect_port, flow.pgid) is True

    def _deliver(self, flow: Flow, code: str, state: str) -> str:
        """GET the recorded loopback callback. 'delivered' / 'unknown' (sent, no clear answer) /
        'not_sent'. Literal loopback addresses only, no redirects followed, no retry once sent."""
        native = flow.native
        assert native is not None
        target = native.redirect_path + "?" + urlencode([("code", code), ("state", state)])
        host_header = f"{'[::1]' if native.redirect_host == '::1' else native.redirect_host}:{native.redirect_port}"
        for addr in _LOOPBACK[native.redirect_host]:
            conn = http.client.HTTPConnection(addr, native.redirect_port, timeout=self._delivery_timeout_s)
            try:
                conn.connect()
            except OSError:
                conn.close()
                continue
            with self._lock:  # serialized with cancel/expiry: re-verify right before the bytes go out
                if flow.state != DELIVERING or not self._process_live(flow):
                    conn.close()
                    return "not_sent"
                try:
                    conn.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
                    conn.putheader("Host", host_header)
                    conn.putheader("Connection", "close")
                    conn.endheaders()
                except OSError:
                    conn.close()
                    return "unknown"
            try:
                resp = conn.getresponse()
                resp.read(4096)
                return "delivered" if resp.status < 400 else "unknown"
            except (OSError, http.client.HTTPException):
                return "unknown"
            finally:
                conn.close()
        return "not_sent"

    def _spawn(self, flow: Flow) -> None:
        import fcntl
        import pty
        import struct
        import termios

        argv = self._argv or [self._claude_binary(), "setup-token"]
        master, slave = pty.openpty()
        try:
            # Wide enough that neither the URL nor the token wraps.
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 4000, 0, 0))
            proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                                    start_new_session=True, env=self._child_env())
        except Exception:
            os.close(master)
            raise
        finally:
            os.close(slave)
        flow.proc, flow.pgid, flow.master_fd = proc, proc.pid, master
        threading.Thread(target=self._pump, args=(flow,), name="claude-setup-token-pty", daemon=True).start()

    @staticmethod
    def _claude_binary() -> str:
        import shutil
        found = shutil.which("claude")
        if not found:
            raise FileNotFoundError("claude")
        return found

    @staticmethod
    def _child_env() -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY")}
        env.setdefault("TERM", "dumb")
        return env

    def _pump(self, flow: Flow) -> None:
        """Read the PTY privately until EOF, then settle the flow from the CLI's exit."""
        master = flow.master_fd
        assert master is not None and flow.proc is not None
        try:
            while True:
                remaining = flow.deadline - time.time()
                if remaining <= 0:
                    self._finish(flow, EXPIRED, "expired", kill=True)
                    break
                ready, _, _ = select.select([master], [], [], min(remaining, 0.5))
                if not ready:
                    if flow.proc.poll() is not None:
                        break
                    self._stop_if_lingering(flow)
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                if len(flow.output) + len(chunk) > _MAX_OUTPUT_BYTES:
                    flow.overflow = True
                else:
                    flow.output.extend(chunk)
                if flow.state == STARTING:
                    self._try_parse_url(flow)
        finally:
            with contextlib.suppress(OSError):
                os.close(master)
            flow.master_fd = None
            try:
                rc = flow.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill(flow)
                rc = None
            self._settle(flow, rc)

    def _stop_if_lingering(self, flow: Flow) -> None:
        with self._lock:
            if flow.state not in (DELIVERING, DELIVERED, DELIVERY_UNKNOWN) or flow.stopped_after_token:
                return
            if flow.token_seen_at is None:
                if _complete_token(flow.output) is not None:
                    flow.token_seen_at = time.monotonic()
                return
            if time.monotonic() - flow.token_seen_at >= _TOKEN_LINGER_S:
                flow.stopped_after_token = True
                self._kill(flow)

    def _try_parse_url(self, flow: Flow) -> None:
        text = _ANSI_RE.sub(b"", bytes(flow.output))
        candidates = set()
        for m in _URL_RE.finditer(text):
            try:
                candidates.add(parse_authorize_url(m.group(0).decode("ascii")))
            except ValueError:
                continue
        with self._lock:
            if flow.state != STARTING:
                return
            if len(candidates) == 1:
                flow.native = candidates.pop()
                flow.state = AWAITING
                flow.url_ready.set()
            elif len(candidates) > 1:
                self._finish(flow, FAILED, "ambiguous_authorize_url", kill=True)

    def _settle(self, flow: Flow, rc: Optional[int]) -> None:
        """After the CLI exits: extract the token privately, store it, compare a private readback.
        Holds the lock throughout so cancel/expiry either precede it (nothing stored) or see it done."""
        with self._lock:
            try:
                self._settle_locked(flow, rc)
            finally:
                flow.output.clear()
                flow.finished.set()

    def _settle_locked(self, flow: Flow, rc: Optional[int]) -> None:
        if flow.state in _TERMINAL:
            flow.output.clear()
            flow.finished.set()
            return
        # DELIVERING too: a CLI can finish its exchange before our GET reads the answer.
        delivered = flow.state in (DELIVERING, DELIVERED, DELIVERY_UNKNOWN)
        token = None
        if delivered and (rc == 0 or flow.stopped_after_token) and not flow.overflow:
            token = _complete_token(flow.output)
        flow.output.clear()
        if token is None:
            self._finish(flow, FAILED, "no_complete_token" if delivered else "cli_exited")
            return
        flow.token_issued = True
        try:
            handle = flow.sink.store_pending(flow.flow_id, token)
        except Exception:
            self._finish(flow, FAILED, "sink_failed")
            return
        flow.sink_handle = handle
        try:
            flow.readback_ok = bool(flow.sink.readback_matches(handle, token))
        except Exception:
            flow.readback_ok = False
        del token
        self._finish(flow, STORED if flow.readback_ok else FAILED, None if flow.readback_ok else "readback_mismatch")

    def _expire_if_due(self, flow: Flow) -> None:
        with self._lock:
            if flow.state not in _TERMINAL and time.time() >= flow.deadline:
                self._finish(flow, EXPIRED, "expired", kill=True)

    def _finish(self, flow: Flow, state: str, reason: Optional[str], *, kill: bool = False) -> None:
        with self._lock:
            if flow.state in _TERMINAL:
                return
            flow.state, flow.reason = state, reason
            flow.native = None  # no later submit or view can use the recorded request
            self._by_handoff.pop(flow.handoff_id, None)
            if kill:
                self._kill(flow)
            if flow.proc is None:  # otherwise the PTY pump sets it once the flow is settled
                flow.finished.set()

    @staticmethod
    def _kill(flow: Flow) -> None:
        """Signal only this flow's own process group, and only while its leader is un-reaped."""
        proc = flow.proc
        if proc is None or flow.pgid is None or proc.poll() is not None:
            return
        try:
            os.killpg(flow.pgid, signal.SIGTERM)
        except OSError:
            pass


_broker: Optional[Broker] = None
_broker_lock = threading.Lock()


def get_broker() -> Optional[Broker]:
    """The gateway's broker, or None outside a gateway that registered the intake verb."""
    return _broker


def install_broker(broker: Optional[Broker] = None) -> Broker:
    global _broker
    with _broker_lock:
        if broker is not None or _broker is None:
            _broker = broker or Broker()
        return _broker


def control_verb(params: dict, peer_subject: str) -> dict:
    """Gateway control-socket private verb: the dashboard forwards an authenticated owner's
    ``view``/``submit``. Answers fixed codes only and never raises (the socket echoes exceptions)."""
    broker = get_broker()
    try:
        if broker is None:
            return {"ok": False, "error": "unavailable"}
        action = params.get("action")
        if set(params) - {"action", "handoff", "profile", "principal", "callback"}:
            return {"ok": False, "error": "bad_request"}
        profile, principal = params.get("profile"), params.get("principal")
        if not isinstance(profile, str) or not isinstance(principal, str):
            return {"ok": False, "error": "bad_request"}
        if action == "view":
            return {"ok": True, **broker.view(params.get("handoff"), profile=profile, principal=principal)}
        if action == "submit":
            return {"ok": True, **broker.submit(params.get("handoff"), params.get("callback"),
                                                profile=profile, principal=principal)}
        return {"ok": False, "error": "bad_request"}
    except IntakeRejected as exc:
        return {"ok": False, "error": exc.code}
    except Exception:
        logger.warning("claude setup-token intake failed (details withheld)")
        return {"ok": False, "error": "internal"}


def register_control_verbs(server) -> None:
    """Install the broker and its private intake verb on the gateway control socket."""
    install_broker()
    server.private_handlers[CONTROL_VERB] = control_verb


def profile_key(home: Optional[Path] = None) -> str:
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    return str(Path(home).resolve())


__all__ = ["Broker", "FilePendingSink", "IntakeRejected", "control_verb", "get_broker", "install_broker",
           "parse_authorize_url", "parse_callback", "profile_key", "register_control_verbs"]
