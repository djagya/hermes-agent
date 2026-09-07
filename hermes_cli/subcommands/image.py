"""``hermes image`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_image_parser(subparsers, *, cmd_image: Callable) -> None:
    """Attach ``image info`` and ``image doctor``."""
    image_parser = subparsers.add_parser(
        "image",
        help="Image identity and read-only doctor (baked toolbox)",
        description=(
            "Wrappers for hermes-image-info / hermes-image-doctor. "
            "Does not print env or credentials."
        ),
    )
    image_sub = image_parser.add_subparsers(dest="image_command")
    info = image_sub.add_parser("info", help="Sanitized image identity")
    info.add_argument("--json", action="store_true", help="JSON output")
    info.set_defaults(func=cmd_image)

    doctor = image_sub.add_parser(
        "doctor",
        help="Read-only image/runtime checks (never prunes at boot)",
    )
    doctor.add_argument("--full", action="store_true", help="Include golden fixture checks")
    doctor.add_argument(
        "--prune-dry-run",
        action="store_true",
        help="List disposable cache candidates only",
    )
    doctor.add_argument(
        "--prune",
        action="store_true",
        help="Delete disposable cache; requires HERMES_CACHE_PRUNE=1",
    )
    doctor.set_defaults(func=cmd_image)
    image_parser.set_defaults(func=cmd_image)
