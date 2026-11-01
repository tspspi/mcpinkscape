from __future__ import annotations

import uuid

import pytest

from mcpinkscape.batches import validate_operations
from mcpinkscape.config.schema import AccessConfig, BridgeConfig
from mcpinkscape.errors import InvalidDrawingValueError
from mcpinkscape.service import InkscapeService


TARGET = {"instance_id": str(uuid.uuid4()), "document_id": str(uuid.uuid4())}
VALID = {"method": "object.move", "params": {"object_ids": ["rect"], "dx": 2, "dy": 3}}


@pytest.mark.parametrize("operations", [[], [VALID] * 101,
    [{"method": "application.shutdown", "params": {}}],
    [{**VALID, "target": TARGET}],
    [{"method": "object.move", "params": {**VALID["params"], "expected_revision": 2}}],
    [{"method": "object.delete", "params": {"object_ids": ["rect", "rect"]}}],
    [{"method": "object.scale", "params": {"object_ids": ["rect"], "scale_x": 0, "scale_y": 1}}],
    [{"method": "object.set_style", "params": {"object_ids": ["rect"], "style": {"fill": "url(file:///private.svg)"}}}],
    [{"method": "text.set", "params": {"object_id": "text", "text": "nul\0text"}}],
    [{"method": "shape.create", "params": {"kind": "rectangle", "object_id": "r", "values": {"x": 0, "y": 0, "width": float("nan"), "height": 4}}}],
])
def test_invalid_batch_rejected_before_native_connection(tmp_path, monkeypatch, operations):
    service = InkscapeService(AccessConfig(document_root=str(tmp_path)), BridgeConfig(executable="missing-inkscape", mode="native_uds"))
    monkeypatch.setattr(service.native_bridge, "request", lambda *a, **k: pytest.fail("invalid batch reached the bridge"))
    try:
        with pytest.raises(InvalidDrawingValueError):
            service.live_apply_batch(operations, expected_revision=0, **TARGET)
    finally:
        service.close()


def test_batch_normalizes_typed_operations_and_preserves_single_target(tmp_path, monkeypatch):
    service = InkscapeService(AccessConfig(document_root=str(tmp_path)), BridgeConfig(executable="missing-inkscape", mode="native_uds"))
    captured = []
    monkeypatch.setattr(service.native_bridge, "status", lambda: {"methods": ["document.batch"], "document_targeting": True})
    def request(method, params, revision=None, **kwargs):
        captured.append((method, params, revision, kwargs))
        return {**TARGET, "revision": 1, "affected_ids": ["rect"]}
    monkeypatch.setattr(service.native_bridge, "request", request)
    try:
        result = service.live_apply_batch([VALID], expected_revision=0, **TARGET)
        assert result["affected_ids"] == ["rect"]
        assert captured == [("document.batch", {"operations": [VALID]}, 0, {"target": TARGET})]
        with pytest.raises(InvalidDrawingValueError):
            service.live_apply_batch([VALID], None, None, 0)
        with pytest.raises(InvalidDrawingValueError):
            service.live_apply_batch([VALID], expected_revision=True, **TARGET)
        with pytest.raises(InvalidDrawingValueError):
            service.live_apply_batch([VALID], expected_revision=2**64, **TARGET)
        assert len(captured) == 1
    finally:
        service.close()


def test_gradient_aliases_are_canonical_and_payload_is_bounded():
    operations = [{"method": "gradient.create", "params": {"kind": "linear", "gradient_id": "g", "stops": [
        {"offset": 0, "color": "#000000"}, {"offset": 1, "colour": "#ffffff"}]}}]
    assert validate_operations(operations)[0]["params"]["stops"][0]["colour"] == "#000000"
    with pytest.raises(InvalidDrawingValueError, match="256 KiB"):
        validate_operations([{"method": "text.set", "params": {"object_id": "t", "text": "x"*32000}}]*10)


def test_batch_tool_schema_requires_one_target_and_revision(tmp_path):
    import asyncio
    from fastmcp import Client
    from mcpinkscape.app.server import build_server
    from mcpinkscape.config.schema import Config

    async def check():
        server, state = build_server(Config(stdio=AccessConfig(document_root=str(tmp_path)),
            bridge=BridgeConfig(mode="native_uds", executable="missing-inkscape")))
        try:
            async with Client(server) as client:
                tool = next(item for item in await client.list_tools() if item.name == "live_apply_batch")
                schema = tool.input_schema
                assert set(schema["required"]) == {"operations", "instance_id", "document_id", "expected_revision"}
                operations = schema["properties"]["operations"]
                assert operations["minItems"] == 1 and operations["maxItems"] == 100
                # FastMCP emits inline JSON Schema oneOf branches, removing
                # Pydantic's OpenAPI discriminator metadata on the MCP wire.
                branches = operations["items"]["oneOf"]
                methods = set()
                for branch in branches:
                    assert branch["additionalProperties"] is False
                    assert set(branch["required"]) == {"method", "params"}
                    method = branch["properties"]["method"]
                    methods.update(method.get("enum", [method.get("const")]))
                    assert branch["properties"]["params"]["additionalProperties"] is False
                assert {"shape.create", "text.create", "object.move", "object.delete", "layer.create"} <= methods
                assert "application.shutdown" not in methods and "document.batch" not in methods
        finally:
            state.close()
    asyncio.run(check())
