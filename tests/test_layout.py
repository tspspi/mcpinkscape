"""Layout admission and MCP contract; geometry is qualified in the private GUI."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from mcpinkscape.batches import validate_operations
from mcpinkscape.config.schema import AccessConfig, BridgeConfig, Config
from mcpinkscape.errors import InvalidDrawingValueError
from mcpinkscape.service import InkscapeService

TARGET = {"instance_id": str(uuid.uuid4()), "document_id": str(uuid.uuid4())}


@pytest.mark.parametrize("method,params", [
    ("object.align", {"axis": "z", "alignment": "start"}),
    ("object.align", {"axis": "x", "alignment": "start", "reference": "object"}),
    ("object.align", {"axis": "x", "alignment": "start", "reference_id": "ref"}),
    ("object.align", {"axis": "x", "alignment": "start", "reference": "object", "reference_id": "a"}),
    ("object.distribute", {"axis": "x", "gap": -1}),
    ("object.distribute", {"axis": "x", "gap": float("inf")}),
    ("object.distribute", {"axis": "x", "spacing": "centers", "gap": 2}),
    ("object.distribute", {"axis": "x", "unit": "mm"}),
    ("object.distribute", {"axis": "x", "gap": 2, "unit": "percent"}),
    ("object.distribute", {"axis": "x"}),  # only two targets
])
def test_layout_rejects_ambiguous_or_invalid_options(method, params):
    with pytest.raises(InvalidDrawingValueError):
        validate_operations([{"method": method, "params": {"object_ids": ["a", "b"], **params}}])


def test_layout_routes_one_explicit_target_and_revision(tmp_path, monkeypatch):
    service = InkscapeService(AccessConfig(document_root=str(tmp_path)), BridgeConfig(mode="native_uds", executable="missing-inkscape"))
    captured = []
    monkeypatch.setattr(service.native_bridge, "status", lambda: {"document_targeting": True, "methods": ["object.align", "object.distribute"]})
    def request(method, params, revision=None, **kwargs):
        captured.append((method, params, revision, kwargs))
        return {**TARGET, "revision": 8, "changed": True, "operation_results": [{"moves": [{"object_id": "a", "dx": 5, "dy": 0}]}]}
    monkeypatch.setattr(service.native_bridge, "request", request)
    try:
        result = service.live_align_objects(["a"], "x", "end", expected_revision=7, reference="page", **TARGET)
        assert result["moves"][0]["dx"] == 5 and result["revision"] == 8
        service.live_distribute_objects(["a", "b"], "y", expected_revision=7, gap=8, unit="mm", **TARGET)
        assert captured[0] == ("object.align", {"object_ids": ["a"], "axis": "x", "alignment": "end", "reference": "page", "bounds": "geometric"}, 7, {"target": TARGET})
        assert captured[1][1]["gap"] == 8 and captured[1][1]["unit"] == "mm"
        for revision in (True, -1, 2**64):
            with pytest.raises(InvalidDrawingValueError):
                service.live_align_objects(["a"], "x", "start", expected_revision=revision, **TARGET)
        with pytest.raises(InvalidDrawingValueError):
            service.live_align_objects(["a", "a"], "x", "start", expected_revision=7, **TARGET)
        assert len(captured) == 2
    finally:
        service.close()


def test_layout_tools_and_batch_operations_are_discoverable(tmp_path):
    from fastmcp import Client
    from mcpinkscape.app.server import build_server
    async def check():
        server, state = build_server(Config(stdio=AccessConfig(document_root=str(tmp_path)), bridge=BridgeConfig(mode="native_uds", executable="missing-inkscape")))
        try:
            async with Client(server) as client:
                tools = {tool.name: tool for tool in await client.list_tools()}
                for name in ("live_align_objects", "live_distribute_objects"):
                    schema = tools[name].input_schema
                    assert {"instance_id", "document_id", "expected_revision", "object_ids", "axis"} <= set(schema["required"])
                    assert schema["properties"]["bounds"]["enum"] == ["geometric", "visual"]
                methods = [branch["properties"]["method"].get("const") for branch in tools["live_apply_batch"].input_schema["properties"]["operations"]["items"]["oneOf"]]
                assert "object.align" in methods and "object.distribute" in methods
        finally:
            state.close()
    asyncio.run(check())
