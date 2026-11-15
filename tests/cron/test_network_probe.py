"""cron.network_probe transport: overall deadline, error classes, cross-process cache."""
from __future__ import annotations

import errno
import socket
import threading
import time

import pytest

from cron import network_probe

pytestmark = pytest.mark.real_network_probe  # keep the real ``_reachable``; tests fake the socket


def _fake_connection(monkeypatch, behaviour):
    monkeypatch.setattr(network_probe.socket, "create_connection", behaviour)


def _raiser(exc):
    def _connect(*args, **kwargs):
        raise exc
    return _connect


@pytest.mark.parametrize("exc", [
    ConnectionRefusedError(), ConnectionResetError(), ConnectionAbortedError(), TimeoutError(), socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
    OSError(errno.EHOSTUNREACH, "no route"), OSError(errno.ENETUNREACH, "net"),
    OSError(errno.ENETDOWN, "down"),
])
def test_connectivity_errors_are_unreachable(monkeypatch, exc):
    _fake_connection(monkeypatch, _raiser(exc))
    assert network_probe._reachable("h", 443) is False
    assert network_probe.box_is_offline() is True


@pytest.mark.parametrize("exc", [
    OSError(errno.EMFILE, "Too many open files"), PermissionError(errno.EACCES, "denied"),
    RuntimeError("boom"),
])
def test_local_failures_are_not_an_outage(monkeypatch, exc):
    _fake_connection(monkeypatch, _raiser(exc))
    assert network_probe._reachable("h", 443) is True
    assert network_probe.box_is_offline() is False


def test_blocked_resolver_returns_online_within_the_deadline_and_never_blocks_a_second_caller(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(network_probe, "PROBE_DEADLINE_SECONDS", 0.3)
    _fake_connection(monkeypatch, lambda *a, **k: release.wait(30) and None)
    try:
        started = time.monotonic()
        assert network_probe.box_is_offline() is False
        assert time.monotonic() - started < 2.0
        network_probe.reset_cache()
        second = []
        worker = threading.Thread(target=lambda: second.append(network_probe.box_is_offline()), daemon=True)
        started = time.monotonic()
        worker.start()
        assert network_probe.box_is_offline() is False
        worker.join(2.0)
        assert second == [False] and time.monotonic() - started < 2.0
    finally:
        release.set()


def test_one_stalled_target_does_not_stop_the_other_from_answering(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(network_probe, "PROBE_DEADLINE_SECONDS", 2.0)

    def _connect(address, timeout=None):
        if address[0] == network_probe.PROBE_TARGETS[0][0]:
            release.wait(30)
            return
        raise ConnectionRefusedError()  # the other target says unreachable

    _fake_connection(monkeypatch, _connect)
    try:
        started = time.monotonic()
        assert network_probe.box_is_offline() is False, "an unfinished target is not proof of an outage"
        assert time.monotonic() - started < 3.0
    finally:
        release.set()


def test_two_processes_share_one_probe_result(monkeypatch):
    probes = []
    monkeypatch.setattr(network_probe, "_probe_offline", lambda: probes.append(1) or True)
    assert network_probe.box_is_offline() is True
    network_probe._cached = None  # a second worker process: fresh module state, same HERMES_HOME
    assert network_probe.box_is_offline() is True
    assert probes == [1]


def test_stale_shared_result_is_probed_again(monkeypatch):
    probes = []
    monkeypatch.setattr(network_probe, "_probe_offline", lambda: probes.append(1) or True)
    network_probe.box_is_offline()
    path = network_probe._cache_path()
    path.write_text('{"checked_at": %s, "online": true}' % (time.time() - 120), encoding="utf-8")
    network_probe._cached = None
    assert network_probe.box_is_offline() is True
    assert probes == [1, 1]


def test_unwritable_home_falls_back_to_the_process_cache(monkeypatch):
    probes = []
    monkeypatch.setattr(network_probe, "_probe_offline", lambda: probes.append(1) or True)
    monkeypatch.setattr(network_probe, "_cache_path", lambda: (_ for _ in ()).throw(PermissionError("ro")))
    assert network_probe.box_is_offline() is True
    assert network_probe.box_is_offline() is True
    assert probes == [1]


def _write_cache(text):
    path = network_probe._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    network_probe._cached = None


@pytest.mark.parametrize("online", ["null", "0", '""', "[]", '"false"', "1"])
def test_shared_result_needs_a_json_bool_online(monkeypatch, online):
    probes = []
    monkeypatch.setattr(network_probe, "_probe_offline", lambda: probes.append(1) or False)
    _write_cache(f'{{"checked_at": {time.time()}, "online": {online}}}')
    assert network_probe.box_is_offline() is False
    assert probes == [1], "a non-bool online value must not be trusted as an offline answer"


@pytest.mark.parametrize("stamp", ["NaN", "Infinity", "null", '"now"', "true", "%s" % (time.time() + 3600)])
def test_shared_result_needs_a_finite_past_checked_at(monkeypatch, stamp):
    probes = []
    monkeypatch.setattr(network_probe, "_probe_offline", lambda: probes.append(1) or False)
    _write_cache(f'{{"checked_at": {stamp}, "online": false}}')
    assert network_probe.box_is_offline() is False
    assert probes == [1]


def test_oversized_shared_file_is_ignored(monkeypatch):
    probes = []
    monkeypatch.setattr(network_probe, "_probe_offline", lambda: probes.append(1) or False)
    _write_cache(f'{{"checked_at": {time.time()}, "online": false, "pad": "{"x" * 8192}"}}')
    assert network_probe.box_is_offline() is False
    assert probes == [1]


def test_fifo_at_the_cache_path_does_not_hang(monkeypatch):
    import os

    probes = []
    monkeypatch.setattr(network_probe, "_probe_offline", lambda: probes.append(1) or False)
    path = network_probe._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)
    network_probe._cached = None
    started = time.monotonic()
    assert network_probe.box_is_offline() is False
    assert time.monotonic() - started < 3.0
    assert probes == [1]


def test_shared_result_keeps_its_original_checked_at(monkeypatch):
    probes = []
    monkeypatch.setattr(network_probe, "_probe_offline", lambda: probes.append(1) or False)
    clock = {"mono": 1000.0, "wall": 5000.0}
    monkeypatch.setattr(network_probe.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(network_probe.time, "time", lambda: clock["wall"])
    _write_cache('{"checked_at": %s, "online": false}' % (clock["wall"] - 59))
    assert network_probe.box_is_offline() is True  # 59 s old offline answer: shared
    assert probes == []
    clock["mono"] += 1.5
    clock["wall"] += 1.5  # 60.5 s after the original probe
    assert network_probe.box_is_offline() is False, "expires at checked_at + 60 s, not now + 60 s"
    assert probes == [1]
