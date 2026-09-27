"""The sealed production image supplies every Python toolbox smoke import.

Run against the built image, never against the host's developer environment.
"""

import subprocess


def test_toolbox_python_imports(built_image: str) -> None:
    probe = """
import importlib
import importlib.metadata
import shutil
import tomllib
from pathlib import Path

project = tomllib.loads(Path('/opt/hermes/pyproject.toml').read_text())['project']
extra = project['optional-dependencies']['sera-toolbox']
# The smoke's import names differ from their distribution names. Assert the
# image really installed the declared extra, not just the test runner's deps.
required = {
    'PyMuPDF': 'fitz',
    'weasyprint': 'weasyprint',
    'python-docx': 'docx',
    'openpyxl': 'openpyxl',
    'yt-dlp': 'yt_dlp',
    'ruff': None,
}
for distribution, module in required.items():
    assert any(item.lower().startswith(distribution.lower() + '==') for item in extra), distribution
    assert importlib.metadata.version(distribution)
    if module:
        importlib.import_module(module)
assert shutil.which('ruff')
for module in ('ddgs', 'fal_client', 'faster_whisper', 'pillow_heif'):
    importlib.import_module(module)
"""
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "/opt/hermes/.venv/bin/python",
         built_image, "-c", probe],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
