"""Plan 5d: docker stop drains one in-flight child before SIGKILL."""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

from tests.docker.conftest import docker_exec_sh, wait_for_container_ready


def test_docker_stop_drains_in_flight_child(
    built_image: str, container_name: str,
) -> None:
    host = Path(tempfile.mkdtemp(prefix="hermes-shutdown-"))
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--name", container_name,
                "--network", "none",
                "-e", "HERMES_REQUIRE_DATA_MOUNT=1",
                "-v", f"{host}:/opt/data",
                built_image, "gateway", "run",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        wait_for_container_ready(container_name, deadline_s=120)

        end = time.monotonic() + 90
        state = ""
        while time.monotonic() < end:
            r = docker_exec_sh(
                container_name,
                "/command/s6-svstat /run/service/gateway-default",
                timeout=10,
            )
            state = r.stdout + r.stderr
            if r.returncode == 0 and "up" in r.stdout:
                break
            time.sleep(1)
        else:
            raise AssertionError("gateway-default never came up:\n" + state)

        started = docker_exec_sh(
            container_name,
            "sleep 15 >/tmp/in-flight.log 2>&1 & echo $!",
            timeout=10,
        )
        assert started.returncode == 0, started.stderr
        assert started.stdout.strip().isdigit(), started.stdout

        t0 = time.monotonic()
        stop = subprocess.run(
            ["docker", "stop", "-t", "90", container_name],
            capture_output=True,
            text=True,
            timeout=120,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        assert stop.returncode == 0, stop.stderr
        assert elapsed_ms <= 90_000, (
            f"docker stop took {elapsed_ms}ms; expected drain under 90s"
        )
        inspect = subprocess.run(
            [
                "docker", "inspect", "-f",
                "{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        status = inspect.stdout.strip()
        assert status.startswith("exited"), status
        assert " true" not in f" {status}", status
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{host}:/clean",
                "--entrypoint", "sh", built_image,
                "-c",
                "chown -R 0:0 /clean 2>/dev/null; "
                "rm -rf /clean/* /clean/.* 2>/dev/null; true",
            ],
            capture_output=True,
            timeout=20,
        )
