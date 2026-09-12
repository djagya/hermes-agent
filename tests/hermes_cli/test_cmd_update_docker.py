"""Tests for ``hermes update`` / ``--check`` inside the Docker container.

Background: ``.dockerignore`` excludes ``.git``, so the existing git-pull
update path can never succeed inside the published image.  Before this
fix, ``hermes update`` would fall through to ``"✗ Not a git repository.
Please reinstall: curl ... install.sh"`` — that script installs a *new*
host-side Hermes, not an update to the running container, so the message
was actively misleading.

These tests pin the new behaviour: when ``detect_install_method`` reports
``"docker"`` (stamped by ``docker/stage2-hook.sh``), both the apply path
(``cmd_update``) and the check path (``_cmd_update_check``) print the
``docker pull`` guidance from ``format_docker_update_message`` and exit
with status 1, without running ``git fetch`` / ``subprocess.run``.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.main import _cmd_update_check, cmd_update


# ---------- cmd_update (apply path) ----------


@patch("hermes_cli.image_provenance.read_image_provenance", return_value=None)
@patch("hermes_cli.config.is_managed", return_value=False)
@patch("hermes_cli.config.detect_install_method", return_value="docker")
@patch("subprocess.run")
def test_cmd_update_in_docker_prints_guidance_and_exits(
    mock_run, _mock_method, _mock_managed, _mock_provenance, capsys
):
    """``hermes update`` inside Docker → friendly message + exit 2, no git calls.

    Exit 2 = refused-by-contract (#91277 Phase 3), distinct from exit-1 errors.
    """
    with pytest.raises(SystemExit) as excinfo:
        cmd_update(SimpleNamespace(check=False))

    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    # Spot-check the key guidance — exhaustive wording is locked in by the
    # config-module test below to keep these CLI tests resilient to copy edits.
    assert "doesn't apply inside the Docker container" in out
    assert "docker pull nousresearch/hermes-agent:latest" in out

    # No git invocations — the early-return must beat every git command.
    git_calls = [c for c in mock_run.call_args_list if c.args and c.args[0] and "git" in str(c.args[0][0])]
    assert git_calls == [], f"expected no git calls, got: {git_calls}"




# ---------- _cmd_update_check (check path, direct entry) ----------


# ---------- Non-Docker installs unaffected ----------




# ---------- format_docker_update_message — content lock ----------


@patch("hermes_cli.image_provenance.read_image_provenance", return_value=None)
def test_format_docker_update_message_contents(_mock_provenance):
    """Lock in the high-value content of the Docker update message.

    These are the bits a user actually needs to act on; if any of them
    disappear in a copy edit, the message has lost its value.  Specific
    wording around them is free to evolve (we don't assert full text).
    """
    from hermes_cli.config import format_docker_update_message

    msg = format_docker_update_message()

    # Primary command — the entire reason this message exists.
    assert "docker pull nousresearch/hermes-agent:latest" in msg

    # The four key concepts the message must cover:
    assert "restart" in msg.lower(), "must explain that a restart is required"
    assert "--version" in msg, "must show how to verify the new version"
    assert ":latest" in msg, "must mention tag pinning caveat"
    assert "HERMES_HOME" in msg or "/opt/data" in msg, (
        "must address config persistence across upgrades"
    )

    # Acknowledges that forks exist (build-your-own-image escape hatch).
    assert "fork" in msg.lower() or "Dockerfile" in msg


def test_format_docker_update_message_fork_teaches_upgrade_script():
    """Baked djagya GHCR marker must not teach Hub :latest or compose up."""
    from hermes_cli.config import format_docker_update_message
    from hermes_cli.image_provenance import ImageProvenance

    fork = ImageProvenance(
        schema=1,
        deployment_kind="image",
        manager="docker",
        image="ghcr.io/djagya/hermes-agent",
        version="0.21.0",
        revision="a" * 40,
        marker_path="/etc/hermes/image-provenance.json",
    )
    with patch(
        "hermes_cli.image_provenance.read_image_provenance", return_value=fork
    ):
        msg = format_docker_update_message()
        from hermes_cli.config import recommended_update_command_for_method

        cmd = recommended_update_command_for_method("docker")

    assert "update-stack.sh --upgrade" in msg
    assert cmd == "./scripts/update-stack.sh --upgrade"
    assert "nousresearch/hermes-agent:latest" not in msg
    # "compose up" must never appear as a *runnable* remediation step
    # (indented command block, like the upstream message teaches). Warning
    # against it in prose — "Do not ``docker compose up`` — that wipes
    # /run/service" — is exactly the guidance this fork must give.
    assert not re.search(r"(?m)^\s*docker compose\b", msg), (
        "fork guidance must not present `docker compose` as a runnable step"
    )
    assert "/run/service" in msg
