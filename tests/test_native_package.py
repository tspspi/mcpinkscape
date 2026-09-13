from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from pathlib import Path


NATIVE_DIRECTORY = Path(__file__).parents[1] / "native"


def test_native_extension_descriptors_reference_the_shared_library():
    expected_ids = {
        "mcpinkscape-bridge-start.inx": "org.mcpinkscape.bridge.start",
        "mcpinkscape-bridge-stop.inx": "org.mcpinkscape.bridge.stop",
        "mcpinkscape-bridge-status.inx": "org.mcpinkscape.bridge.status",
    }
    for filename, extension_id in expected_ids.items():
        root = ElementTree.parse(NATIVE_DIRECTORY / filename).getroot()
        values = {element.tag.rsplit("}", 1)[-1]: (element.text or "").strip() for element in root}
        assert values["id"] == extension_id
        plugin = next(element for element in root if element.tag.rsplit("}", 1)[-1] == "plugin")
        assert plugin.get("name") == "libmcpinkscape_bridge"


def test_native_build_target_and_required_plugin_exports_are_present():
    cmake = (NATIVE_DIRECTORY / "CMakeLists.txt").read_text(encoding = "utf-8")
    source = (NATIVE_DIRECTORY / "src" / "mcpinkscape_bridge.cpp").read_text(encoding = "utf-8")
    assert "TARGET inkscape_base" in cmake
    assert "target_link_libraries(mcpinkscape_bridge PRIVATE inkscape_base)" in cmake
    assert "GetImplementation" in source
    assert "GetInkscapeVersion" in source
    assert "bridge.hello" in source
    assert "INADDR_LOOPBACK" in source
    assert "ws2_32" in cmake
    assert "MCPINKSCAPE_EFFECT_API_14" in source
    assert "MCPINKSCAPE_EFFECT_API_14" in cmake


def test_native_cmake_overlay_defers_external_subdirectory_addition():
    overlay = (NATIVE_DIRECTORY / "cmake-overlay.cmake").read_text(encoding = "utf-8")
    assert "cmake_language(DEFER" in overlay
    assert "MCPINKSCAPE_OVERLAY_DIR" in overlay
    assert "include" in overlay


def test_native_unix_listener_requires_same_user_peer_credentials():
    source = (NATIVE_DIRECTORY / "src" / "mcpinkscape_bridge.cpp").read_text(encoding = "utf-8")
    assert "getpeereid" in source
    assert "SO_PEERCRED" in source
    assert "peer_is_current_user" in source


def test_native_document_queries_cross_the_gui_main_loop_boundary():
    source = (NATIVE_DIRECTORY / "src" / "mcpinkscape_bridge.cpp").read_text(encoding = "utf-8")
    assert "g_main_context_invoke" in source
    assert "GuiExecutor" in source
    assert "document.revision" in source
    assert "object.list" in source
    assert "selection.get" in source
    assert "selection.set" in source
    assert "object.set_style" in source
    assert "document.set_background" in source
    assert "shape.create" in source
    assert "text.create" in source
    assert "text.set" in source
    assert "object.move" in source
    assert "object.rotate" in source
    assert "object.scale" in source
    assert "snapshot.render" in source
    assert "snapshot.release" in source
    assert "document.poll_changes" in source
    assert "connectModified" in source
    assert "connectChanged" in source
    assert "connectDestroy" in source
    assert "BridgeMutation" in source
    assert "sp_repr_save_file" in source
    assert "KERN_PROC_PATHNAME" in source
    assert "DBUS_SESSION_BUS_ADDRESS" in source
    assert "DocumentUndo::done" in source
    assert "revision_conflict" in source
    assert "SP_ACTIVE_DESKTOP" in source
    assert "wait_for" in source
    assert "gui_timeout" in source
