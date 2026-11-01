from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from mcpinkscape.config.schema import AccessConfig, BridgeConfig
from mcpinkscape.errors import LiveBackendUnavailableError, RevisionConflictError
from mcpinkscape.native_bridge import NativeBridgeClient
from mcpinkscape.service import InkscapeService, NATIVE_TOOL_METHODS


@pytest.mark.parametrize("mode,required", [("auto", False), ("auto", True), ("native_uds", True)])
@pytest.mark.parametrize("first_call", ["server_status", "live_status", "live_create_shape", "live_list_selection"])
def test_service_connects_when_bridge_starts_later(tmp_path, mode, required, first_call):
    path = tmp_path / "late.sock"
    service = InkscapeService(
        AccessConfig(document_root = str(tmp_path / "library")),
        BridgeConfig(mode = mode, required = required, uds = str(path), executable = "missing-inkscape"),
    )
    assert service.server_status()["native_bridge"]["available"] is False
    assert service.native_supports_tool("live_create_shape") is True
    assert service.create_document("offline")["document_id"]
    with pytest.raises(LiveBackendUnavailableError):
        service.live_create_shape("rectangle", {"x": 0, "y": 0, "width": 3, "height": 4})

    # Start a real socket listener only after service construction and a failed call.
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(1)
    listener.settimeout(0.2)
    received = []
    stopped = threading.Event()

    def server():
        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection, connection.makefile("rb") as reader:
                request = json.loads(reader.readline())
                received.append(request["method"])
                result = {"revision": 12, "object_ids": []}
                if request["method"] == "bridge.hello":
                    result = {"methods": ["document.revision", "shape.create", "selection.get"]}
                connection.sendall((json.dumps({"type": "response", "id": request["id"], "ok": True, "result": result}) + "\n").encode())

    thread = threading.Thread(target = server, daemon = True)
    thread.start()
    try:
        if first_call == "live_create_shape":
            result = service.live_create_shape("rectangle", {"x": 0, "y": 0, "width": 3, "height": 4})
        else:
            result = getattr(service, first_call)()
        if first_call == "server_status":
            assert result["native_bridge"]["available"] is True
        else:
            assert result["backend"] == "native_uds"
        assert received[:2] == ["bridge.hello", "document.revision"]
    finally:
        service.close()
        stopped.set()
        thread.join(timeout = 5)
        listener.close()
    assert not thread.is_alive()


def test_native_bridge_reconnects_after_eof_without_replaying_request(tmp_path):
    path = tmp_path / "restart.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(1)
    listener.settimeout(5)
    received = []

    def server():
        for attempt in range(2):
            connection, _ = listener.accept()
            with connection, connection.makefile("rb") as reader:
                request = json.loads(reader.readline())
                received.append(request["method"])
                if attempt == 1:
                    connection.sendall((json.dumps({"type": "response", "id": request["id"], "ok": True, "result": {"bridge": "ready"}}) + "\n").encode())

    thread = threading.Thread(target = server, daemon = True)
    thread.start()
    client = NativeBridgeClient(path)
    try:
        with pytest.raises(LiveBackendUnavailableError, match = "closed the connection"):
            client.request("shape.create", {})
        assert received == ["shape.create"]
        assert client.status()["bridge"] == "ready"
        assert received == ["shape.create", "bridge.hello"]
    finally:
        client.close()
        thread.join(timeout = 5)
        listener.close()
    assert not thread.is_alive()


def test_native_bridge_json_request_response(tmp_path):
    path = tmp_path / "bridge.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(1)

    def server():
        connection, _ = listener.accept()
        with connection:
            payload = json.loads(connection.makefile("rb").readline().decode("utf-8"))
            response = {
                "type": "response",
                "id": payload["id"],
                "ok": True,
                "result": {"bridge": "ready", "protocol_version": 1},
            }
            connection.sendall((json.dumps(response) + "\n").encode("utf-8"))

    thread = threading.Thread(target = server)
    thread.start()
    try:
        with NativeBridgeClient(path) as client:
            assert client.status()["bridge"] == "ready"
    finally:
        thread.join(timeout = 5)
        listener.close()


def test_windows_bridge_uses_literal_ipv4_loopback_only():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def server():
        connection, _ = listener.accept()
        with connection:
            payload = json.loads(connection.makefile("rb").readline().decode("utf-8"))
            connection.sendall((json.dumps({"type": "response", "id": payload["id"], "ok": True, "result": {"bridge": "ready"}}) + "\n").encode("utf-8"))

    thread = threading.Thread(target = server)
    thread.start()
    try:
        with NativeBridgeClient("unused.sock", tcp_port = port, platform_name = "nt") as client:
            assert client.status()["bridge"] == "ready"
    finally:
        thread.join(timeout = 5)
        listener.close()


def test_windows_bridge_default_port_matches_extension_contract():
    assert BridgeConfig().tcp_host == "127.0.0.1"
    assert BridgeConfig().tcp_port == 61779


def test_native_only_tool_discovery_covers_every_service_route():
    """Do not advertise only a subset of an otherwise capable native bridge."""
    service = object.__new__(InkscapeService)
    service._native_status = {"methods": list(NATIVE_TOOL_METHODS.values())}

    assert set(NATIVE_TOOL_METHODS) == {
        "live_align_objects", "live_distribute_objects",
        "live_apply_batch", "live_get_document_info", "live_get_object", "live_list_layers", "live_create_layer", "live_set_current_layer",
        "live_list_documents",
        "live_poll_changes", "live_list_objects", "live_create_shape", "live_create_text",
        "live_set_text", "live_set_page_background", "live_create_gradient",
        "live_set_gradient_stops", "live_apply_gradient", "live_import_image",
        "live_delete_objects", "live_duplicate_objects", "live_group_objects",
        "live_ungroup_objects", "live_raise_objects", "live_lower_objects",
    }
    assert all(service.native_supports_tool(tool_name) for tool_name in NATIVE_TOOL_METHODS)

    service._native_status = {"methods": ["shape.create"]}
    assert service.native_supports_tool("live_create_shape") is True
    assert service.native_supports_tool("live_create_gradient") is False


def test_service_routes_native_live_operations_and_imports_snapshot(tmp_path):
    path = tmp_path / "bridge.sock"
    png = tmp_path / "bridge-snapshot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nbridge-test")
    png.chmod(0o600)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(1)
    received: list[dict] = []
    stopped = threading.Event()
    listener.settimeout(0.2)

    def server():
        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection, connection.makefile("rb") as reader:
                request = json.loads(reader.readline())
                received.append(request)
                method = request["method"]
                if method == "bridge.hello":
                    result = {
                        "bridge": "ready", "protocol_version": 1,
                        "methods": ["document.revision", "document.poll_changes", "shape.create", "object.set_style", "object.move", "snapshot.render", "snapshot.release"],
                    }
                elif method == "snapshot.render":
                    result = {"snapshot_id": "native-snapshot-1", "staging_path": str(png), "width_px": 1, "height_px": 1, "revision": 12}
                elif method == "document.poll_changes":
                    result = {"document_id": "active", "open": True, "revision": 12, "selection_generation": 3, "document_changed": True, "selection_changed": False, "object_ids": ["live-rect"]}
                else:
                    result = {"revision": 12, "changed_ids": ["live-rect"]}
                connection.sendall((json.dumps({"type": "response", "id": request["id"], "ok": True, "result": result}) + "\n").encode("utf-8"))

    thread = threading.Thread(target = server)
    thread.start()
    service = InkscapeService(
        AccessConfig(document_root = str(tmp_path / "library")),
        BridgeConfig(mode = "native_uds", uds = str(path), executable = "missing-inkscape"),
    )
    try:
        shape = service.live_create_shape(
            "rectangle", {"x": 1, "y": 2, "width": 3, "height": 4},
            object_id = "live-rect", expected_revision = 11,
        )
        assert shape["backend"] == "native_uds"
        assert received[-1]["method"] == "shape.create"
        assert received[-1]["expected_revision"] == 11
        service.live_set_style(["live-rect"], {"fill": "#123456"}, expected_revision = 12)
        assert received[-1]["method"] == "object.set_style"
        service.live_transform("move", ["live-rect"], [5, 6], expected_revision = 12)
        assert received[-1]["method"] == "object.move"
        assert received[-1]["params"] == {"object_ids": ["live-rect"], "dx": 5, "dy": 6}
        changes = service.live_poll_changes(11, 3)
        assert changes["revision"] == 12
        assert changes["document_changed"] is True
        assert received[-1]["method"] == "document.poll_changes"
        snapshot = service.live_render_snapshot()
        assert snapshot["backend"] == "native_uds"
        assert service.get_snapshot_base64(snapshot["snapshot_id"])["mime_type"] == "image/png"
        assert received[-1]["method"] == "snapshot.release"
    finally:
        service.close()
        stopped.set()
        thread.join(timeout = 5)
        listener.close()


def test_native_bridge_maps_revision_conflict_to_stable_error(tmp_path):
    path = tmp_path / "bridge-conflict.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(1)

    def server():
        connection, _ = listener.accept()
        with connection:
            payload = json.loads(connection.makefile("rb").readline().decode("utf-8"))
            response = {
                "type": "response", "id": payload["id"], "ok": False,
                "error": {"code": "revision_conflict", "message": "document changed", "current_revision": 7},
            }
            connection.sendall((json.dumps(response) + "\n").encode("utf-8"))

    thread = threading.Thread(target = server)
    thread.start()
    try:
        with NativeBridgeClient(path) as client:
            with pytest.raises(RevisionConflictError, match = "current revision is 7"):
                client.request("shape.create", {}, expected_revision = 6)
    finally:
        thread.join(timeout = 5)
        listener.close()


def test_native_bridge_sends_utf8_without_json_unicode_escapes(tmp_path):
    path = tmp_path / "bridge-utf8.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(1)
    received: list[bytes] = []

    def server():
        connection, _ = listener.accept()
        with connection:
            raw = connection.makefile("rb").readline()
            received.append(raw)
            request = json.loads(raw.decode("utf-8"))
            connection.sendall((json.dumps({"type": "response", "id": request["id"], "ok": True, "result": {}}) + "\n").encode("utf-8"))

    thread = threading.Thread(target = server)
    thread.start()
    try:
        with NativeBridgeClient(path) as client:
            client.request("text.create", {"text": "Grüße"})
        assert b"Gr\xc3\xbc\xc3\x9fe" in received[0]
        assert b"\\u" not in received[0]
    finally:
        thread.join(timeout = 5)
        listener.close()
