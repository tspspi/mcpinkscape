#!/usr/bin/env python3
"""Capture port inputs before configure and reject drift before staging."""
import argparse
import hashlib
import json
from pathlib import Path
import platform


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inputs(args):
    native = args.port / "native"
    bridge = json.loads((native / "manifest.json").read_text())
    for name, expected in bridge["sha256"].items():
        if digest(native / name) != expected:
            raise RuntimeError(f"Stale or modified native payload: {name}")
    parent_files = [args.parent / "Makefile", args.distinfo]
    parent_files += sorted(path for path in args.patch_dir.rglob("*") if path.is_file())
    parent_files += args.extra_patch
    return {
        "bridge": bridge,
        "port_makefile_sha256": digest(args.port / "Makefile"),
        "parent_inputs_sha256": {str(path): digest(path) for path in parent_files},
        "distinfo": args.distinfo.read_text(),
        "selected_options": sorted(args.options.split()),
        "python": platform.python_version(),
        "system": platform.platform(),
        "architecture": platform.machine(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=Path, required=True)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--distinfo", type=Path, required=True)
    parser.add_argument("--patch-dir", type=Path, required=True)
    parser.add_argument("--extra-patch", type=Path, action="append", default=[])
    parser.add_argument("--options", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    current = inputs(args)
    if args.verify or args.build:
        previous = json.loads(args.output.read_text()) if args.output.is_file() else {}
        compared = current.copy()
        if args.build:
            # Native-only updates may rebuild against the configured core.
            previous.pop("bridge", None)
            compared.pop("bridge", None)
        if previous != compared:
            parser.error("Port inputs changed since configure; rebuild from a clean port work directory")
    if not args.verify:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(current, indent=2) + "\n")


if __name__ == "__main__":
    main()
