from __future__ import annotations

import asyncio
import json
import socket
import threading
import uuid

from fastmcp import Client
import pytest

from mcpinkscape.app.server import build_server
from mcpinkscape.config.schema import AccessConfig, BridgeConfig, Config
from mcpinkscape.errors import InvalidDrawingValueError, LiveActionUnsupportedError, StaleDocumentTargetError
from mcpinkscape.native_bridge import NativeBridgeClient
from mcpinkscape.service import InkscapeService


TARGET = {"instance_id": str(uuid.uuid4()), "document_id": str(uuid.uuid4())}


@pytest.mark.parametrize("stale", [False, True])
def test_target_envelope_and_stale_error_mapping(tmp_path, stale):
    path = tmp_path / "target.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(1)
    listener.settimeout(5)
    received = []

    def serve():
        connection, _ = listener.accept()
        with connection, connection.makefile("rb") as reader:
            request = json.loads(reader.readline())
            received.append(request)
            response = {"type": "response", "id": request["id"], "ok": not stale}
            if stale:
                response["error"] = {"code": "stale_document_target", "message": "document closed"}
            else:
                response["result"] = {**TARGET, "objects": []}
            connection.sendall((json.dumps(response) + "\n").encode())

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    client = NativeBridgeClient(path)
    try:
        if stale:
            with pytest.raises(StaleDocumentTargetError, match="document closed"):
                client.request("object.list", {}, 0, target=TARGET)
        else:
            assert client.request("object.list", {}, 0, target=TARGET)["document_id"] == TARGET["document_id"]
        assert received[0]["target"] == TARGET
        assert received[0]["expected_revision"] == 0
        assert client._socket is None
    finally:
        client.close()
        worker.join(timeout=5)
        listener.close()
    assert not worker.is_alive()


def service(tmp_path, mode="native_uds"):
    return InkscapeService(AccessConfig(document_root=str(tmp_path / "library")),
        BridgeConfig(mode=mode, uds=str(tmp_path / "absent.sock"), executable="missing-inkscape"))


@pytest.mark.parametrize("arguments", [{"instance_id": TARGET["instance_id"]},
    {"document_id": TARGET["document_id"]}, {"instance_id": "active", "document_id": "active"}])
def test_partial_or_non_session_targets_are_rejected(tmp_path, arguments):
    instance = service(tmp_path)
    try:
        with pytest.raises(InvalidDrawingValueError):
            instance.live_status(**arguments)
    finally:
        instance.close()


def test_bound_request_never_falls_back_to_cli(tmp_path, monkeypatch):
    instance = service(tmp_path, "active_window")
    monkeypatch.setattr(instance.cli, "active_list_selection", lambda: pytest.fail("CLI fallback was invoked"))
    try:
        with pytest.raises(LiveActionUnsupportedError, match="targeting requires"):
            instance.live_list_selection(**TARGET)
    finally:
        instance.close()


@pytest.mark.parametrize("supported,wrong_receipt", [(False, False), (True, False), (True, True)])
def test_native_target_capability_forwarding_and_receipt(tmp_path, monkeypatch, supported, wrong_receipt):
    instance = service(tmp_path)
    calls = []
    monkeypatch.setattr(instance.native_bridge, "status", lambda: {
        "methods": ["object.list"], "document_targeting": supported})

    def request(method, params, expected_revision=None, **kwargs):
        calls.append(kwargs)
        return {**TARGET, "document_id": str(uuid.uuid4()) if wrong_receipt else TARGET["document_id"], "objects": []}

    monkeypatch.setattr(instance.native_bridge, "request", request)
    try:
        if not supported:
            with pytest.raises(LiveActionUnsupportedError, match="does not support"):
                instance.live_list_objects(**TARGET)
            assert not calls
        elif wrong_receipt:
            with pytest.raises(StaleDocumentTargetError, match="response does not identify"):
                instance.live_list_objects(**TARGET)
        else:
            assert instance.live_list_objects(**TARGET)["document_id"] == TARGET["document_id"]
            assert calls == [{"target": TARGET}]
    finally:
        instance.close()


def test_mcp_live_schemas_expose_target_pair(tmp_path):
    async def check():
        server, state = build_server(Config(stdio=AccessConfig(document_root=str(tmp_path / "library")),
            bridge=BridgeConfig(mode="native_uds", executable="missing-inkscape", uds=str(tmp_path / "absent.sock"))))
        try:
            async with Client(server) as client:
                for tool in await client.list_tools():
                    if tool.name.startswith("live_") and tool.name != "live_list_documents":
                        assert {"instance_id", "document_id"} <= set(tool.input_schema["properties"]), tool.name
        finally:
            state.close()
    asyncio.run(check())
