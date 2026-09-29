#!/usr/bin/env python3
"""Inventory the already-loaded image filesystem without importing Hermes code."""

import hashlib
import json
import os
from pathlib import Path


DIRECTORIES = (
    "/opt", "/usr", "/opt/hermes", "/opt/hermes/tools",
    "/opt/hermes/.venv", "/opt/hermes/node_modules", "/usr/local",
)
LARGE_FILE_BYTES = 10 * 1024 * 1024


def inventory():
    files = []
    for top in ("/opt", "/usr"):
        for directory, dirs, names in os.walk(top, followlinks=False):
            dirs.sort()
            names.sort()
            for name in names:
                path = Path(directory) / name
                if path.is_symlink():
                    continue
                stat = path.stat()
                if not path.is_file():
                    continue
                files.append((str(path), stat.st_size, stat.st_blocks * 512))

    report = {"schema": 1, "units": "bytes", "directories": {}}
    for directory in DIRECTORIES:
        members = [entry for entry in files if entry[0].startswith(directory + "/")]
        children = {}
        for path, logical, allocated in members:
            child = path[len(directory) + 1:].split("/", 1)[0]
            counts = children.setdefault(child, [0, 0])
            counts[0] += logical
            counts[1] += allocated
        report["directories"][directory] = {
            "logical_bytes": sum(item[1] for item in members),
            "allocated_bytes": sum(item[2] for item in members),
            "top_children": [
                {"name": name, "logical_bytes": value[0], "allocated_bytes": value[1]}
                for name, value in sorted(children.items(), key=lambda item: (-item[1][0], item[0]))[
                    :None if directory == "/opt/hermes/tools" else 30
                ]
            ],
        }

    site_packages = sorted({
        path.split("/site-packages/", 1)[0] + "/site-packages"
        for path, _, _ in files
        if path.startswith("/opt/hermes/.venv/") and "/site-packages/" in path
    })
    for directory in site_packages:
        members = [entry for entry in files if entry[0].startswith(directory + "/")]
        packages = {}
        for path, logical, allocated in members:
            package = path[len(directory) + 1:].split("/", 1)[0]
            counts = packages.setdefault(package, [0, 0])
            counts[0] += logical
            counts[1] += allocated
        report["directories"][directory] = {
            "logical_bytes": sum(entry[1] for entry in members),
            "allocated_bytes": sum(entry[2] for entry in members),
            "top_children": [
                {"name": name, "logical_bytes": size[0], "allocated_bytes": size[1]}
                for name, size in sorted(packages.items(), key=lambda item: (-item[1][0], item[0]))[:50]
            ],
        }

    # Hash only same-size large files; a matching size alone is not duplication.
    by_size = {}
    for path, logical, _ in files:
        if logical >= LARGE_FILE_BYTES:
            by_size.setdefault(logical, []).append(path)
    duplicates = []
    for size, paths in sorted(by_size.items(), reverse=True):
        if len(paths) < 2:
            continue
        by_hash = {}
        for path in paths:
            digest = hashlib.sha256()
            with open(path, "rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            by_hash.setdefault(digest.hexdigest(), []).append(path)
        for digest, matches in sorted(by_hash.items()):
            if len(matches) > 1:
                duplicates.append({
                    "file_bytes": size, "potential_savings_bytes": size * (len(matches) - 1),
                    "sha256": digest, "paths": sorted(matches),
                })
    report["duplicate_large_files"] = sorted(
        duplicates, key=lambda item: (-item["potential_savings_bytes"], item["paths"])
    )[:50]
    report["large_file_threshold_bytes"] = LARGE_FILE_BYTES
    report["largest_files"] = [
        {"path": path, "logical_bytes": logical, "allocated_bytes": allocated}
        for path, logical, allocated in sorted(files, key=lambda item: (-item[1], item[0]))[:50]
    ]
    return report


if __name__ == "__main__":
    print(json.dumps(inventory(), sort_keys=True, indent=2))
