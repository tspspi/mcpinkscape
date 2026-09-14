#!/usr/bin/env python3
"""Refresh the self-contained port payload; run by maintainers before release.

Users copy the resulting port directory and run make install. No checkout,
network download of a moving bridge branch, or other port's work tree is used.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PORT = ROOT / "freebsd/usr/ports/graphics/inkscape-mcpinkscape"
FILES = (
    "CMakeLists.txt", "cmake-overlay.cmake",
    "mcpinkscape-bridge-start.inx", "mcpinkscape-bridge-stop.inx",
    "mcpinkscape-bridge-status.inx", "src/bridge_json.h",
    "src/mcpinkscape_bridge.cpp", "LICENSE.md",
)


def copy_input(source: Path, target: Path) -> None:
    # Keep compiler readers on a stable inode and avoid rebuilding unchanged
    # input when maintainers refresh the distributed payload.
    contents = source.read_bytes()
    if target.is_file() and target.read_bytes() == contents:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".new")
    temporary.write_bytes(contents)
    temporary.replace(target)


def export() -> None:
    copy_input(ROOT / "LICENSE.md", PORT / "LICENSE.md")
    copy_input(ROOT / "packaging/freebsd_build_info.py", PORT / "files/freebsd_build_info.py")
    hashes = {}
    for name in FILES:
        source = ROOT / "native" / name
        target = PORT / "native" / name
        copy_input(source, target)
        hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
    ).strip()
    (PORT / "native/manifest.json").write_text(
        json.dumps({"base_revision": revision, "sha256": hashes}, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    export()
