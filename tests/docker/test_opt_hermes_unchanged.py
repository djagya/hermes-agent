"""Offline boot must not mutate /opt/hermes (no venv/npm rebuild)."""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

from tests.docker.conftest import docker_exec_sh, wait_for_container_ready

# Boot must not fetch. Strings are log-only (not Dockerfile comments).
FORBIDDEN_BOOT_LOG = (
    "uv sync",
    "npm install",
    "npm ci",
    "pip install",
    "apt-get ",
    "playwright install",
)


def test_opt_hermes_unchanged_on_offline_boot(
    built_image: str, container_name: str,
) -> None:
    """Plan 5c: cold start with --network none leaves /opt/hermes identical.

    Also proves config/skills stage2 finished and the baked Playwright
    tree is already present (no download).
    """
    subprocess.run(
        [
            "docker", "run", "-d", "--name", container_name,
            "--network", "none",
            built_image, "sleep", "infinity",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    wait_for_container_ready(container_name, deadline_s=90)

    diff = subprocess.run(
        ["docker", "diff", container_name],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    hermes_hits = [
        line for line in diff.stdout.splitlines()
        if "/opt/hermes" in line
    ]
    assert hermes_hits == [], (
        "/opt/hermes changed during offline boot:\n" + "\n".join(hermes_hits)
    )

    logs = subprocess.run(
        ["docker", "logs", container_name],
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = logs.stdout + logs.stderr
    for needle in FORBIDDEN_BOOT_LOG:
        assert needle not in combined, (
            f"offline boot log contained {needle!r}:\n{combined[-4000:]}"
        )

    r = docker_exec_sh(
        container_name,
        "test -x /opt/hermes/.venv/bin/hermes && "
        "test -d /opt/hermes/.playwright && "
        "echo BOOT_OK",
        timeout=10,
    )
    assert "BOOT_OK" in r.stdout, (
        f"baked hermes/playwright missing after offline boot: "
        f"{r.stdout} {r.stderr}"
    )


def test_offline_gateway_run_starts_without_downloads(
    built_image: str, container_name: str,
) -> None:
    """Plan 5c: ``gateway run`` with --network none reaches a supervised slot.

    Adapters may fail offline. The image must still start the gateway
    process without apt/pip/uv/npm/Playwright fetches.
    """
    subprocess.run(
        [
            "docker", "run", "-d", "--name", container_name,
            "--network", "none",
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
        raise AssertionError(
            "offline gateway-default never came up:\n" + state
        )

    logs = subprocess.run(
        ["docker", "logs", container_name],
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = logs.stdout + logs.stderr
    for needle in FORBIDDEN_BOOT_LOG:
        assert needle not in combined, (
            f"offline gateway log contained {needle!r}:\n{combined[-4000:]}"
        )

    browser = docker_exec_sh(
        container_name,
        "test -s /run/s6/container_environment/AGENT_BROWSER_EXECUTABLE_PATH "
        "&& echo BROWSER_OK",
        timeout=10,
    )
    assert "BROWSER_OK" in browser.stdout, (
        f"browser path missing after offline gateway boot: "
        f"{browser.stdout} {browser.stderr}"
    )


def test_offline_boot_with_data_mount_leaves_opt_hermes(
    built_image: str, container_name: str,
) -> None:
    """Plan 5c: --network none + explicit /opt/data mount, no /opt/hermes drift.

    Complements the no-volume case: stage2 must seed a fresh home and
    migrate without apt/pip/uv/npm/Playwright downloads.
    """
    host = Path(tempfile.mkdtemp(prefix="hermes-rehearsal-"))
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--name", container_name,
                "--network", "none",
                "-e", "HERMES_REQUIRE_DATA_MOUNT=1",
                "-v", f"{host}:/opt/data",
                built_image, "sleep", "infinity",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        wait_for_container_ready(container_name, deadline_s=120)

        diff = subprocess.run(
            ["docker", "diff", container_name],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        hermes_hits = [
            line for line in diff.stdout.splitlines()
            if "/opt/hermes" in line
        ]
        assert hermes_hits == [], (
            "/opt/hermes changed during offline mounted boot:\n"
            + "\n".join(hermes_hits)
        )

        logs = subprocess.run(
            ["docker", "logs", container_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        combined = logs.stdout + logs.stderr
        for needle in FORBIDDEN_BOOT_LOG:
            assert needle not in combined, (
                f"offline mounted boot log contained {needle!r}:\n"
                f"{combined[-4000:]}"
            )

        seeded = docker_exec_sh(
            container_name,
            "test -f /opt/data/config.yaml && "
            "test -d /opt/data/hotfixes && "
            "test -d /opt/data/state && "
            "echo HOME_OK",
            timeout=10,
        )
        assert "HOME_OK" in seeded.stdout, (
            f"fresh home not seeded: {seeded.stdout} {seeded.stderr}"
        )
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
