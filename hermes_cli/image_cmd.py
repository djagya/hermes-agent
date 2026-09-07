"""``hermes image info|doctor`` — thin wrappers over the baked toolbox bins."""

from __future__ import annotations

import shutil
import subprocess
import sys


def cmd_image(args) -> None:
    """Dispatch to hermes-image-info / hermes-image-doctor on PATH."""
    sub = getattr(args, "image_command", None)
    if sub == "info":
        binary = "hermes-image-info"
        extra = ["--json"] if getattr(args, "json", False) else []
    elif sub == "doctor":
        binary = "hermes-image-doctor"
        if getattr(args, "prune_dry_run", False):
            extra = ["--prune-dry-run"]
        elif getattr(args, "prune", False):
            extra = ["--prune"]
        elif getattr(args, "full", False):
            extra = ["--full"]
        else:
            extra = ["--check"]
    else:
        print("usage: hermes image info|doctor", file=sys.stderr)
        sys.exit(2)

    path = shutil.which(binary)
    if not path:
        print(f"missing {binary} (image toolbox)", file=sys.stderr)
        sys.exit(2)
    raise SystemExit(subprocess.call([path, *extra]))
