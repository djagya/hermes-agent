#!/usr/bin/env python3
"""Single-worker spool daemon for isolated local Hermes tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import tarfile
import threading
import time

SPOOL = Path(os.environ.get("HERMES_TEST_SPOOL", "/spool"))
REQUESTS = SPOOL / "requests"
RESULTS = SPOOL / "results"
WORK = Path(os.environ.get("HERMES_TEST_WORK", "/work"))
LOCK_SHA = Path(
    os.environ.get("HERMES_TEST_LOCK_FILE", "/opt/test-env/uv.lock.sha256")
).read_text().strip()
PYTHON = "/opt/test-env/.venv/bin/python"
STOP = threading.Event()
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
TARGET_RE = re.compile(r"tests/[A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_.\[\]-]+)*")


class Refusal(RuntimeError):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def assert_isolation() -> dict:
    forbidden = (
        Path("/run/service/gateway-default"),
        Path("/opt/data/gateway_state.json"),
        Path("/var/run/docker.sock"),
    )
    exposed = [str(path) for path in forbidden if path.exists()]
    pid1 = Path("/proc/1/comm").read_text().strip()
    if exposed or pid1 == "s6-svscan":
        raise Refusal(f"production boundary exposed: paths={exposed}, pid1={pid1}")
    return {"paths_absent": True, "pid1": pid1, "network_expected": "none"}


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n")
    os.replace(tmp, path)


def heartbeat_loop(attestation: dict) -> None:
    while not STOP.is_set():
        write_json_atomic(
            SPOOL / "runner-status.json",
            {
                "version": 1,
                "heartbeat_epoch": time.time(),
                "isolation_ok": True,
                "attestation": attestation,
                "lock_sha256": LOCK_SHA,
            },
        )
        STOP.wait(5)


def safe_extract(archive: Path, destination: Path) -> None:
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise Refusal("compressed source archive exceeds 256 MiB")
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        if len(members) > 20_000:
            raise Refusal("source archive has too many members")
        extracted_bytes = 0
        for member in members:
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or ".." in pure.parts:
                raise Refusal(f"unsafe archive path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise Refusal(f"archive member type refused: {member.name}")
            extracted_bytes += member.size
            if extracted_bytes > MAX_EXTRACTED_BYTES:
                raise Refusal("source archive expands beyond 512 MiB")
        tf.extractall(destination, members=members)


def validate_target(source: Path, target: str) -> None:
    if not TARGET_RE.fullmatch(target) or ".." in target:
        raise Refusal("invalid narrow target")
    file_part = target.split("::", 1)[0]
    if not (source / file_part).is_file():
        raise Refusal(f"target file does not exist: {file_part}")


def base_env(home: Path, source: Path) -> dict[str, str]:
    home.mkdir(parents=True)
    return {
        "PATH": "/opt/test-env/.venv/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "HERMES_HOME": str(home / ".hermes"),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_PYTHON": PYTHON,
        "HERMES_TEST_WORKERS": "2",
        "PYTHONPATH": str(source),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "OPENROUTER_API_KEY": "",
        "OPENAI_API_KEY": "",
        "NOUS_API_KEY": "",
    }


def prune_results() -> None:
    cutoff = time.time() - 7 * 86400
    for path in RESULTS.iterdir():
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
        except OSError:
            pass


def process_request(ready: Path, attestation: dict) -> None:
    request_id = ready.name.removesuffix(".ready")
    running = ready.with_name(f"{request_id}.running")
    os.replace(ready, running)
    result_tmp = RESULTS / f".{request_id}.building"
    result_final = RESULTS / request_id
    work_root = WORK / request_id
    started = time.monotonic()
    exit_code = 2
    isolation = "refused"
    error = ""
    result_tmp.mkdir()
    log_path = result_tmp / "output.log"

    try:
        request = json.loads((running / "request.json").read_text())
        if request.get("id") != request_id or request.get("version") != 1:
            raise Refusal("request identity/version mismatch")
        if request.get("lock_sha256") != LOCK_SHA:
            raise Refusal("request uv.lock differs from runner image")
        archive = running / "source.tar.gz"
        if sha256(archive) != request.get("source_archive_sha256"):
            raise Refusal("source archive checksum mismatch")

        source = work_root / "repo"
        source.mkdir(parents=True)
        safe_extract(archive, source)
        mode = request.get("mode")
        target = request.get("target", "")
        timeout = int(request.get("timeout_seconds", 0))
        if not 1 <= timeout <= 10800:
            raise Refusal("invalid timeout")
        if mode == "narrow":
            validate_target(source, target)
            command = [PYTHON, "-m", "pytest", target, "-q", "--tb=short"]
        elif mode == "full" and not target:
            command = ["bash", "scripts/run_tests.sh"]
        else:
            raise Refusal("invalid mode/target")

        env = base_env(work_root / "home", source)
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            log.write(json.dumps({"mode": mode, "target": target, "isolation": attestation}) + "\n")
            log.flush()
            proc = subprocess.Popen(
                command,
                cwd=source,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )
            try:
                exit_code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                log.write(f"\nREFUSED: timeout after {timeout}s\n")
                exit_code = 124
        isolation = "local-sidecar"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"REFUSED: {error}\n")
    finally:
        duration = round(time.monotonic() - started, 3)
        write_json_atomic(
            result_tmp / "result.json",
            {
                "version": 1,
                "id": request_id,
                "exit_code": exit_code,
                "duration_seconds": duration,
                "isolation": isolation,
                "error": error,
            },
        )
        os.replace(result_tmp, result_final)
        shutil.rmtree(work_root, ignore_errors=True)
        shutil.rmtree(running, ignore_errors=True)


def main() -> int:
    REQUESTS.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    attestation = assert_isolation()
    heartbeat = threading.Thread(target=heartbeat_loop, args=(attestation,), daemon=True)
    heartbeat.start()
    try:
        while not STOP.is_set():
            prune_results()
            ready = next(iter(sorted(REQUESTS.glob("*.ready"))), None)
            if ready is None:
                STOP.wait(0.5)
                continue
            process_request(ready, attestation)
    finally:
        STOP.set()
        heartbeat.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
