"""Tests for ``hermes image info|doctor`` dispatch."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.image_cmd import cmd_image


@patch("hermes_cli.image_cmd.subprocess.call", return_value=0)
@patch("hermes_cli.image_cmd.shutil.which", return_value="/usr/local/bin/hermes-image-info")
def test_image_info_json(mock_which, mock_call):
    with pytest.raises(SystemExit) as exc:
        cmd_image(SimpleNamespace(image_command="info", json=True))
    assert exc.value.code == 0
    mock_call.assert_called_once_with(
        ["/usr/local/bin/hermes-image-info", "--json"]
    )


@patch("hermes_cli.image_cmd.subprocess.call", return_value=0)
@patch("hermes_cli.image_cmd.shutil.which", return_value="/usr/local/bin/hermes-image-doctor")
def test_image_doctor_check(mock_which, mock_call):
    with pytest.raises(SystemExit) as exc:
        cmd_image(
            SimpleNamespace(
                image_command="doctor",
                json=False,
                full=False,
                prune_dry_run=False,
                prune=False,
            )
        )
    assert exc.value.code == 0
    mock_call.assert_called_once_with(
        ["/usr/local/bin/hermes-image-doctor", "--check"]
    )


@patch("hermes_cli.image_cmd.shutil.which", return_value=None)
def test_image_missing_binary(mock_which):
    with pytest.raises(SystemExit) as exc:
        cmd_image(SimpleNamespace(image_command="info", json=False))
    assert exc.value.code == 2
