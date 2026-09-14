from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("build_native", ROOT / "packaging/build_native.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


def test_exported_port_payload_matches_native_sources():
    payload = ROOT / "freebsd/usr/ports/graphics/inkscape-mcpinkscape/native"
    manifest = json.loads((payload / "manifest.json").read_text())
    for name, expected in manifest["sha256"].items():
        original = ROOT / "native" / name
        assert (payload / name).read_bytes() == original.read_bytes(), "Refresh packaging/export_freebsd_port.py"
        assert hashlib.sha256(original.read_bytes()).hexdigest() == expected
    assert (ROOT / "packaging/freebsd_build_info.py").read_bytes() == (
        payload.parent / "files/freebsd_build_info.py").read_bytes()


def test_port_provenance_rejects_patch_drift_and_tracks_native_rebuild(tmp_path):
    port = tmp_path / "port"
    native = port / "native"
    native.mkdir(parents=True)
    (port / "Makefile").write_text("local recipe")
    parent = tmp_path / "parent"
    patches = parent / "files"
    patches.mkdir(parents=True)
    (parent / "Makefile").write_text("upstream recipe")
    (parent / "distinfo").write_text("pinned source archive")
    patch = patches / "patch-example"
    patch.write_text("original patch")
    def native_version(contents):
        (native / "bridge.cpp").write_text(contents)
        (native / "manifest.json").write_text(json.dumps({"base_revision": "example", "sha256": {
            "bridge.cpp": hashlib.sha256(contents.encode()).hexdigest()}}))
    native_version("first")
    command = [sys.executable, str(ROOT / "packaging/freebsd_build_info.py"),
               "--port", str(port), "--parent", str(parent), "--patch-dir", str(patches),
               "--distinfo", str(parent / "distinfo"), "--output", str(tmp_path / "inputs.json")]
    def invoke(*flags):
        return subprocess.run(command + list(flags), capture_output=True, text=True)
    assert invoke().returncode == 0
    assert invoke("--verify").returncode == 0
    native_version("second")
    assert invoke("--verify").returncode != 0
    assert invoke("--build").returncode == 0
    assert invoke("--verify").returncode == 0
    patch.write_text("different patch")
    assert invoke("--build").returncode != 0
    assert invoke("--verify").returncode != 0


def test_corrupt_cached_archive_is_rejected(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "inkscape-1.4.4.tar.xz").write_bytes(b"truncated download")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        driver.fetch_source(tmp_path)
    assert not (tmp_path / "sources").exists()


def test_build_lock_rejects_concurrent_writer_and_releases_after_error(tmp_path):
    path = tmp_path / "build.lock"
    with pytest.raises(ValueError):
        with driver.file_lock(path):
            with pytest.raises(RuntimeError, match="Another process"):
                with driver.file_lock(path):
                    pytest.fail("A second writer acquired the build lock")
            raise ValueError("interrupted operation")
    with driver.file_lock(path):
        pass


def test_changed_overlay_content_rebuilds_even_with_old_source_timestamp(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    (repository / "native/src").mkdir(parents=True)
    source = repository / "native/src/bridge.cpp"
    source.write_text("old")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(driver, "ROOT", repository)
    overlay = driver.prepare_overlay(work)
    target = overlay / "src/bridge.cpp"
    unchanged_time = target.stat().st_mtime_ns
    driver.prepare_overlay(work)
    assert target.stat().st_mtime_ns == unchanged_time
    source.write_text("new")
    os.utime(source, (1, 1))
    driver.prepare_overlay(work)
    assert target.read_text() == "new"
    assert target.stat().st_mtime > 1


def test_archive_cannot_write_outside_source_directory(tmp_path, monkeypatch):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    archive = downloads / "inkscape-1.4.4.tar.xz"
    with tarfile.open(archive, "w:xz") as bundle:
        entry = tarfile.TarInfo("../../escaped")
        entry.size = 1
        bundle.addfile(entry, io.BytesIO(b"x"))
    monkeypatch.setitem(driver.LOCK, "inkscape", {
        **driver.LOCK["inkscape"], "sha256": driver.digest(archive),
    })
    with pytest.raises(tarfile.TarError):
        driver.fetch_source(tmp_path)
    assert not (tmp_path / "escaped").exists()


def test_install_refuses_unmanaged_destination(tmp_path):
    destination = tmp_path / "existing application"
    destination.mkdir()
    (destination / "user-file").write_text("keep")
    with pytest.raises(RuntimeError, match="unmanaged"):
        driver.install(tmp_path / "stage", destination)
    assert (destination / "user-file").read_text() == "keep"


def test_failed_installed_verification_restores_previous_app(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    destination = tmp_path / "installed"
    for directory, value in ((stage, "new"), (destination, "old")):
        directory.mkdir()
        (directory / driver.MARKER).write_text(json.dumps({"application": driver.APPLICATION}))
        (directory / "app").write_text(value)
    def fail(_):
        raise RuntimeError("installed runtime could not load")
    monkeypatch.setattr(driver, "verify", fail)
    with pytest.raises(RuntimeError, match="runtime could not load"):
        driver.install(stage, destination)
    assert (destination / "app").read_text() == "old"
    assert not destination.with_name("installed.previous").exists()


def test_repeated_upgrades_keep_one_backup_and_uninstall_preserves_neighbors(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "desktop"))
    monkeypatch.setattr(driver, "verify", lambda _: {})
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / driver.MARKER).write_text(json.dumps({"application": driver.APPLICATION}))
    destination = tmp_path / "installed"
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("keep")
    for version in ("one", "two", "three"):
        (stage / "version").write_text(version)
        driver.install(stage, destination)
    assert (destination / "version").read_text() == "three"
    assert (tmp_path / "installed.previous/version").read_text() == "two"
    driver.uninstall(destination)
    assert not destination.exists()
    assert not (tmp_path / "installed.previous").exists()
    assert unrelated.read_text() == "keep"


def test_uninstall_refuses_unmanaged_backup_before_removing_current(tmp_path):
    destination = tmp_path / "installed"
    destination.mkdir()
    (destination / driver.MARKER).write_text(json.dumps({"application": driver.APPLICATION}))
    backup = tmp_path / "installed.previous"
    backup.mkdir()
    (backup / "user-file").write_text("keep")
    with pytest.raises(RuntimeError, match="unmanaged"):
        driver.uninstall(destination)
    assert destination.exists()
    assert (backup / "user-file").read_text() == "keep"


def configure_fixture(tmp_path, *, version="1.4.4", kind="SHARED"):
    source = tmp_path / "source with spaces"
    source.mkdir()
    overlay = tmp_path / "overlay"
    shutil.copytree(ROOT / "native", overlay)
    (overlay / "src/mcpinkscape_bridge.cpp").write_text(
        'extern "C" int core_value();\nextern "C" int bridge_value() { return core_value(); }\n'
    )
    (source / "core.cpp").write_text('extern "C" int core_value() { return 42; }\n')
    (source / "CMakeLists.txt").write_text(f'''
cmake_minimum_required(VERSION 3.24)
project(install_fixture LANGUAGES CXX)
include(GNUInstallDirs)
set(INKSCAPE_VERSION "{version}")
set(INKSCAPE_SHARE_INSTALL "share/inkscape")
add_library(inkscape_base {kind} core.cpp)
set_target_properties(inkscape_base PROPERTIES OUTPUT_NAME mcp_fixture_core)
install(TARGETS inkscape_base LIBRARY DESTINATION "${{CMAKE_INSTALL_LIBDIR}}/inkscape")
''')
    build = tmp_path / "build"
    stage = tmp_path / "installed app"
    result = subprocess.run([
        "cmake", "-S", str(source), "-B", str(build),
        f"-DCMAKE_INSTALL_PREFIX={stage}", "-DCMAKE_INSTALL_LIBDIR=lib",
        f"-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES={overlay / 'cmake-overlay.cmake'}",
        f"-DMCPINKSCAPE_OVERLAY_DIR={overlay}",
    ], capture_output=True, text=True)
    return result, source, overlay, build, stage


@pytest.mark.skipif(not shutil.which("cmake") or os.name == "nt", reason="Unix CMake runtime fixture")
def test_installed_plugin_loads_matching_core_without_build_tree(tmp_path):
    result, source, overlay, build, stage = configure_fixture(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    subprocess.run(["cmake", "--build", str(build)], check=True, capture_output=True)
    subprocess.run(["cmake", "--install", str(build)], check=True, capture_output=True)
    shutil.rmtree(build)
    shutil.rmtree(source)
    shutil.rmtree(overlay)
    module = stage / "share/inkscape/extensions/libmcpinkscape_bridge.so"
    env = {key: value for key, value in os.environ.items() if key not in ("LD_LIBRARY_PATH", "LD_PRELOAD")}
    subprocess.run([sys.executable, "-c",
                    "import ctypes,sys; assert ctypes.CDLL(sys.argv[1]).bridge_value() == 42", str(module)],
                   check=True, env=env, capture_output=True)
    assert len(list(module.parent.glob("*.inx"))) == 3


@pytest.mark.parametrize("version,kind,message", [
    ("1.5", "SHARED", "Unsupported Inkscape"),
    ("1.4.4", "STATIC", "requires BUILD_SHARED_LIBS=ON"),
])
@pytest.mark.skipif(not shutil.which("cmake"), reason="CMake required")
def test_overlay_rejects_incompatible_core(tmp_path, version, kind, message):
    result, *_ = configure_fixture(tmp_path, version=version, kind=kind)
    assert result.returncode != 0
    assert message in result.stderr
