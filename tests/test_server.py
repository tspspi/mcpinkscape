from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from mcpinkscape.app.server import build_remote_app, build_server
from mcpinkscape.config.api_keys import derive_argon2id_hash, ensure_kdf_defaults
from mcpinkscape.config.schema import APIKeyConfig, AccessConfig, BridgeConfig, Config, RemoteServerConfig, TransportConfig
from mcpinkscape.service import NATIVE_TOOL_METHODS


def test_server_registers_rc1_tools(tmp_path):
    async def check_tools():
        server, state = build_server(
            Config(
                stdio = AccessConfig(document_root = str(tmp_path / "stdio")),
                bridge = BridgeConfig(mode = "offline", executable = "missing-inkscape"),
            )
        )
        try:
            for name in ("create_document", "create_rectangle", "set_fill", "render_snapshot", "export_document", "live_set_style"):
                assert await server.get_tool(name) is not None
        finally:
            state.close()

    asyncio.run(check_tools())


def test_server_discovers_all_and_only_advertised_native_only_tools(tmp_path):
    async def check_tools():
        server, state = build_server(
            Config(
                stdio = AccessConfig(document_root = str(tmp_path / "stdio")),
                bridge = BridgeConfig(mode = "offline", executable = "missing-inkscape"),
            )
        )
        try:
            # The offline service must not promise native-only operations.
            assert await server.get_tool("live_create_gradient") is None

            # Simulate a completed hello negotiation; discovery must use every
            # method in the shared service map, rather than a hand-picked subset.
            state.stdio_service._native_status = {"methods": list(NATIVE_TOOL_METHODS.values())}
            for tool_name in NATIVE_TOOL_METHODS:
                assert await server.get_tool(tool_name) is not None
        finally:
            state.close()

    asyncio.run(check_tools())


def test_remote_http_requires_key_and_serves_mcp_tools(tmp_path):
    kdf = ensure_kdf_defaults({"memory_cost": 8192})
    kdf["hash"] = derive_argon2id_hash("wire-key", kdf)
    config = Config(
        stdio = AccessConfig(document_root = str(tmp_path / "stdio")),
        bridge = BridgeConfig(mode = "offline", executable = "missing-inkscape"),
        remote_server = RemoteServerConfig(transport = TransportConfig(host = "127.0.0.1", port = 0)),
        api_keys = [APIKeyConfig(id = "test-key", kdf = kdf, document_root = str(tmp_path / "key"))],
    )
    server, state = build_server(config)
    app = build_remote_app(server, state)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    try:
        with TestClient(app) as client:
            assert client.get("/status").status_code == 200
            assert client.post("/mcp", json = initialize).status_code == 401
            headers = {"Authorization": "Bearer wire-key", "Accept": "application/json, text/event-stream"}
            response = client.post("/mcp", headers = headers, json = initialize)
            assert response.status_code == 200
            assert "mcp-session-id" in response.headers
            headers["mcp-session-id"] = response.headers["mcp-session-id"]
            listed = client.post(
                "/mcp",
                headers = headers,
                json = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert listed.status_code == 200
            assert "create_rectangle" in listed.text
            assert "live_set_style" in listed.text
    finally:
        state.close()


def test_remote_http_hides_tools_disallowed_by_key_policy(tmp_path):
    kdf = ensure_kdf_defaults({"memory_cost": 8192})
    kdf["hash"] = derive_argon2id_hash("readonly-key", kdf)
    config = Config(
        stdio = AccessConfig(document_root = str(tmp_path / "stdio")),
        bridge = BridgeConfig(mode = "offline", executable = "missing-inkscape"),
        remote_server = RemoteServerConfig(transport = TransportConfig(host = "127.0.0.1", port = 0)),
        api_keys = [APIKeyConfig(
            id = "readonly", kdf = kdf, document_root = str(tmp_path / "key"),
            allow_document_write = False, allow_snapshots = False, allow_live_control = False,
        )],
    )
    server, state = build_server(config)
    app = build_remote_app(server, state)
    initialize = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    }
    try:
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer readonly-key", "Accept": "application/json, text/event-stream"}
            response = client.post("/mcp", headers = headers, json = initialize)
            assert response.status_code == 200
            headers["mcp-session-id"] = response.headers["mcp-session-id"]
            listed = client.post("/mcp", headers = headers, json = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            assert listed.status_code == 200
            assert "server_status" in listed.text
            assert "create_document" not in listed.text
            assert "render_snapshot" not in listed.text
            assert "live_set_style" not in listed.text
    finally:
        state.close()
