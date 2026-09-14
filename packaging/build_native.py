#!/usr/bin/env python3
"""Build, stage, verify and install the matched native application.

Only the standard library is required. Platform wrappers supply the toolchain.
Source archives include vendored submodules and are verified before extraction.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "packaging/sources.json").read_text())
MARKER = "mcpinkscape-build.json"
APPLICATION = "mcpinkscape-native"


@contextmanager
def file_lock(path):
    """OS-owned lock; an interrupted build does not leave a stale lock behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError(f"Another process is using this build or installation: {path}") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def run(arguments, **kwargs):
    print("+", " ".join(map(str, arguments)), flush=True)
    return subprocess.run(list(map(str, arguments)), check=True, **kwargs)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch_source(work):
    spec = LOCK["inkscape"]
    archive = work / "downloads" / f"inkscape-{spec['version']}.tar.xz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        temporary = archive.with_suffix(".part")
        print(f"Downloading {spec['url']}", flush=True)
        with urllib.request.urlopen(spec["url"], timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        if digest(temporary) != spec["sha256"]:
            temporary.unlink()
            raise RuntimeError("Inkscape archive checksum mismatch")
        temporary.replace(archive)
    if digest(archive) != spec["sha256"]:
        raise RuntimeError(f"Cached archive checksum mismatch: {archive}")
    sources = work / "sources"
    sources.mkdir(exist_ok=True)
    source = sources / spec["directory"]
    marker = source / ".mcp-source-sha256"
    if source.exists():
        if not marker.exists() or marker.read_text().strip() != spec["sha256"]:
            raise RuntimeError(f"Unmanaged or incomplete source tree: {source}")
    else:
        with tempfile.TemporaryDirectory(dir=sources, prefix="extract-") as temporary:
            with tarfile.open(archive) as bundle:
                # data_filter rejects escaping links, absolute paths and devices.
                bundle.extractall(temporary, filter="data")
            extracted = Path(temporary) / spec["directory"]
            if not (extracted / "CMakeLists.txt").is_file():
                raise RuntimeError("Archive does not contain the locked source directory")
            (extracted / ".mcp-source-sha256").write_text(spec["sha256"] + "\n")
            extracted.rename(source)
    return source


def native_hashes():
    return {str(path.relative_to(ROOT)): digest(path)
            for path in sorted((ROOT / "native").rglob("*"))
            if path.is_file() and path.suffix in {".cpp", ".h", ".cmake", ".txt", ".inx", ".md"}}


def prepare_overlay(work):
    """Copy changed inputs with local timestamps, preserving incremental builds.

    A release archive or a checkout copied from another host may have older
    timestamps than existing object files. Compare content before invoking Ninja.
    """
    overlay = work / "native-overlay"
    marker = work / "native-inputs.json"
    previous = json.loads(marker.read_text()) if marker.exists() else {}
    current = native_hashes()
    for name in previous.keys() - current.keys():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "native":
            raise RuntimeError("Invalid cached native input path")
        old = overlay / relative.relative_to("native")
        if old.is_file():
            old.unlink()
    for name, checksum in current.items():
        target = overlay / Path(name).relative_to("native")
        if not target.is_file() or digest(target) != checksum:
            target.parent.mkdir(parents=True, exist_ok=True)
            # copyfile intentionally does not preserve the source timestamp.
            shutil.copyfile(ROOT / name, target)
    marker.write_text(json.dumps(current, indent=2) + "\n")
    return overlay


def owned(directory):
    try:
        return not directory.is_symlink() and json.loads((directory / MARKER).read_text()).get("application") == APPLICATION
    except (OSError, ValueError, AttributeError):
        return False


def desktop_entry(prefix):
    # Desktop Exec has its own quoting rules; it is not a shell command.
    executable = str(prefix / "bin/inkscape").replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        executable = executable.replace(character, "\\" + character)
    return ('[Desktop Entry]\nType=Application\nName=Inkscape (mcpInkscape)\n'
            f'Exec="{executable}" %F\n'
            f'Icon={prefix / "share/icons/hicolor/scalable/apps/org.inkscape.Inkscape.svg"}\n'
            'Terminal=false\nCategories=Graphics;VectorGraphics;\nMimeType=image/svg+xml;\n'
            'X-mcpInkscape-Managed=true\n')


def register_desktop(prefix):
    if not sys.platform.startswith("linux"):
        return
    destination = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications/mcpinkscape.desktop"
    if destination.exists() and "X-mcpInkscape-Managed=true" not in destination.read_text():
        raise RuntimeError(f"Refusing to overwrite unmanaged desktop entry: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".desktop.tmp")
    temporary.write_text(desktop_entry(prefix))
    temporary.replace(destination)


def uninstall(prefix):
    backup = prefix.with_name(prefix.name + ".previous")
    for directory in (prefix, backup):
        if directory.exists() and not owned(directory):
            raise RuntimeError(f"Refusing to remove unmanaged directory: {directory}")
    desktop = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications/mcpinkscape.desktop"
    if sys.platform.startswith("linux") and desktop.is_file() and desktop.read_text() == desktop_entry(prefix):
        desktop.unlink()
    for directory in (prefix, backup):
        if directory.exists():
            shutil.rmtree(directory)
    print(f"Removed managed installation: {prefix}")


def verify(stage):
    executable = stage / "bin" / ("inkscape.exe" if os.name == "nt" else "inkscape")
    if not executable.is_file():
        raise RuntimeError(f"Installed executable missing: {executable}")
    env = os.environ.copy()
    for name in ("INKSCAPE_DATADIR", "INKSCAPE_PROFILE_DIR", "LD_LIBRARY_PATH", "LD_PRELOAD",
                 "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY", "AT_SPI_BUS_ADDRESS"):
        env.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="mcpinkscape-check-") as temporary:
        env["INKSCAPE_PROFILE_DIR"] = temporary
        result = run([executable, "--version"], env=env, capture_output=True, text=True, timeout=30)
        if LOCK["inkscape"]["version"] not in result.stdout:
            raise RuntimeError(f"Unexpected installed Inkscape version: {result.stdout}")
        catalog = run([executable, "--action-list"], env=env, capture_output=True, text=True, timeout=30)
        # Inkscape 1.4.4 only registers extension application actions in GUI
        # mode. The headless pass loads modules but does not list those actions.
        if "Unable to load extension libmcpinkscape_bridge" in catalog.stderr:
            raise RuntimeError(f"Installed bridge could not load: {catalog.stderr}")
        for action in ("start", "stop", "status"):
            descriptor = stage / "share/inkscape/extensions" / f"mcpinkscape-bridge-{action}.inx"
            root = ET.parse(descriptor).getroot()
            identity = root.find("{http://www.inkscape.org/namespace/inkscape/extension}id")
            if identity is None or identity.text != f"org.mcpinkscape.bridge.{action}":
                raise RuntimeError(f"Installed bridge descriptor invalid: {descriptor}")
        fixture = Path(temporary) / "fixture.svg"
        fixture.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32">'
                           '<rect width="32" height="32" fill="blue"/></svg>')
        output = Path(temporary) / "fixture.png"
        run([executable, fixture, "--export-type=png", f"--export-filename={output}"], env=env, timeout=60)
        if not output.is_file() or output.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            raise RuntimeError("Installed application failed PNG export")
    return {"version": result.stdout.strip(), "descriptors": ["start", "stop", "status"], "png_export": True}


def install(stage, destination):
    # Never merge into an unrelated prefix or delete files belonging to another app.
    if destination.exists() and not owned(destination):
        raise RuntimeError(f"Refusing to replace unmanaged directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(destination.name + ".previous")
    if backup.exists() and not owned(backup):
        raise RuntimeError(f"Refusing to replace unmanaged backup directory: {backup}")
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".mcp-install-") as temporary:
        incoming = Path(temporary) / "application"
        previous = Path(temporary) / "previous"
        shutil.copytree(stage, incoming, symlinks=True)
        if destination.exists():
            destination.rename(previous)
        try:
            incoming.rename(destination)
            verify(destination)
            register_desktop(destination)
        except BaseException:
            if destination.exists():
                destination.rename(Path(temporary) / "failed")
            if previous.exists():
                previous.rename(destination)
            raise
        if previous.exists():
            if backup.exists():
                shutil.rmtree(backup)
            previous.rename(backup)
    print(f"Installed: {destination}\nLaunch: {destination / 'bin' / 'inkscape'}")
    if backup.exists():
        print(f"Previous installation retained for rollback: {backup}")


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("fetch", "build", "verify", "install", "uninstall", "package"), default="install")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build/native")
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local/opt/mcpinkscape")
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 2, 4))
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    work = args.work_dir.expanduser().resolve()
    prefix = args.prefix.expanduser().resolve()
    if any(ord(character) < 32 for character in str(work) + str(prefix)):
        parser.error("paths containing control characters are unsupported")
    if work == prefix or work in prefix.parents or prefix in work.parents:
        parser.error("work directory and installation prefix must not contain one another")
    if prefix == ROOT or prefix in ROOT.parents:
        parser.error("installation prefix must not contain the source checkout")
    return args, work, prefix


def main():
    args, work, prefix = parse_arguments()
    with file_lock(work / ".build.lock"):
        if args.action in ("install", "uninstall"):
            with file_lock(prefix.parent / ("." + prefix.name + ".install.lock")):
                execute(args, work, prefix)
        else:
            execute(args, work, prefix)


def execute(args, work, prefix):
    if args.action == "uninstall":
        uninstall(prefix)
        return
    work.mkdir(parents=True, exist_ok=True)
    stage = work / "stage"
    if args.action == "verify":
        print(json.dumps(verify(stage), indent=2))
        return
    source = fetch_source(work)
    if args.action == "fetch":
        print(source)
        return
    build = work / "cmake"
    overlay = prepare_overlay(work)
    # Describe and distribute the snapshot actually compiled, even if the
    # developer edits the checkout while a long Inkscape build is running.
    input_hashes = json.loads((work / "native-inputs.json").read_text())
    flags = ["-DCMAKE_BUILD_TYPE=Release", f"-DCMAKE_INSTALL_PREFIX={prefix}",
             "-DBUILD_SHARED_LIBS=ON", "-DBUILD_TESTING=OFF", "-DWITH_NLS=ON",
             "-DWITH_IMAGE_MAGICK=OFF", "-DWITH_GRAPHICS_MAGICK=OFF",
             f"-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES={overlay / 'cmake-overlay.cmake'}",
             f"-DMCPINKSCAPE_OVERLAY_DIR={overlay}"]
    if os.name == "nt":
        flags += ["-DCMAKE_WINDOWS_EXPORT_ALL_SYMBOLS=ON"]
    run(["cmake", "-S", source, "-B", build, "-G", "Ninja", *flags])
    run(["cmake", "--build", build, "--parallel", args.jobs])
    for name, checksum in input_hashes.items():
        if digest(overlay / Path(name).relative_to("native")) != checksum:
            raise RuntimeError("Native build inputs changed during compilation; retry the build")
    if stage.exists():
        if not owned(stage):
            raise RuntimeError(f"Incomplete stage at {stage}; inspect and remove it before retrying")
        shutil.rmtree(stage)
    stage.mkdir()
    # Mark ownership before installation so failed staging can be retried safely.
    (stage / MARKER).write_text(json.dumps({"application": APPLICATION, "state": "staging"}) + "\n")
    run(["cmake", "--install", build, "--prefix", stage])
    evidence = verify(stage)
    metadata = {"application": APPLICATION, "inkscape": LOCK["inkscape"], "native_sha256": input_hashes,
                "platform": platform.platform(), "architecture": platform.machine(),
                "configure_flags": flags, "verification": evidence}
    for command, key in ((["cmake", "--version"], "cmake"), (["c++", "--version"], "compiler")):
        metadata[key] = run(command, capture_output=True, text=True).stdout
    if shutil.which("dpkg-query"):
        metadata["dependencies"] = run(["dpkg-query", "-W"], capture_output=True, text=True).stdout
    elif shutil.which("pacman"):
        metadata["dependencies"] = run(["pacman", "-Q"], capture_output=True, text=True).stdout
    (stage / MARKER).write_text(json.dumps(metadata, indent=2) + "\n")
    if args.action == "install":
        install(stage, prefix)
    elif args.action == "package":
        # Do not publish a package merely because command-line rendering works.
        run([sys.executable, ROOT / "packaging/verify_live.py",
             stage / "bin" / ("inkscape.exe" if os.name == "nt" else "inkscape"),
             "--output", work / "live-verification.json"])
        metadata["live_verification"] = json.loads((work / "live-verification.json").read_text())
        (stage / MARKER).write_text(json.dumps(metadata, indent=2) + "\n")
        artifacts = work / "artifacts"
        artifacts.mkdir(exist_ok=True)
        filename = artifacts / f"inkscape-mcpinkscape-{LOCK['inkscape']['version']}-{sys.platform}-{platform.machine()}"
        archive = Path(shutil.make_archive(str(filename), "zip" if os.name == "nt" else "gztar", root_dir=work, base_dir="stage"))
        archive.with_name(archive.name + ".sha256").write_text(f"{digest(archive)}  {archive.name}\n")
        sources = artifacts / (filename.name + "-sources.tar.gz")
        with tarfile.open(sources, "w:gz") as bundle:
            bundle.add(work / "downloads" / f"inkscape-{LOCK['inkscape']['version']}.tar.xz", arcname="upstream/inkscape.tar.xz")
            bundle.add(overlay, arcname="mcpinkscape/native")
            for name in ("packaging", "build.sh", "build.ps1", "LICENSE.md", "INSTALL.md"):
                bundle.add(ROOT / name, arcname="mcpinkscape/" + name,
                           filter=lambda member: None if "__pycache__" in member.name else member)
        sources.with_name(sources.name + ".sha256").write_text(f"{digest(sources)}  {sources.name}\n")
        print(f"Packaged: {archive}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        sys.exit(1)
