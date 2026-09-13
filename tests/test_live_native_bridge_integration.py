from __future__ import annotations

import base64
import os
from pathlib import Path
from uuid import uuid4

import pytest

from mcpinkscape.config.schema import AccessConfig, BridgeConfig
from mcpinkscape.errors import RevisionConflictError
from mcpinkscape.service import InkscapeService


_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
    "S1Z8fQAAAABJRU5ErkJggg=="
)


@pytest.mark.skipif(
    os.environ.get("MCPINKSCAPE_NATIVE_VERIFY") != "1",
    reason = "set MCPINKSCAPE_NATIVE_VERIFY=1 with a dedicated bridge socket",
)
def test_native_rc1_collaboration_surface(tmp_path: Path):
    """Exercise RC1 on the explicitly authorized, disposable GUI fixture."""
    socket_path = os.environ.get("MCPINKSCAPE_NATIVE_VERIFY_SOCKET")
    if not socket_path:
        pytest.skip("set MCPINKSCAPE_NATIVE_VERIFY_SOCKET to the dedicated bridge socket")
    image = tmp_path / "pixel.png"
    image.write_bytes(_TINY_PNG)
    prefix = f"mcp-native-verify-{uuid4().hex}"
    rectangle_id = f"{prefix}-rect"
    text_id = f"{prefix}-text"
    image_id = f"{prefix}-image"
    group_id = f"{prefix}-group"
    service = InkscapeService(
        AccessConfig(document_root = str(tmp_path)),
        BridgeConfig(mode = "native_uds", uds = socket_path, executable = "missing-inkscape"),
    )
    created_ids: list[str] = []
    try:
        assert service.live_status()["backend"] == "native_uds"
        initial_selection = service.live_list_selection().get("object_ids", [])
        revision = service.native_bridge.request("document.revision", {})["revision"]

        # This fixture is created solely for the native verification test, so its page
        # metadata is safely part of the RC1 surface exercised here.
        revision = service.live_set_page_background("#eef4ff", 0.15, revision)["revision"]

        rectangle = service.live_create_shape(
            "rectangle",
            {"x": 12, "y": 12, "width": 32, "height": 18, "rx": 3, "ry": 3},
            object_id = rectangle_id,
            style = {"fill": "#1565c0", "stroke": "#0d223f", "stroke_width": "1.2px"},
            expected_revision = revision,
        )
        revision = rectangle["revision"]
        created_ids.append(rectangle_id)

        # Cover every typed primitive kind, not merely the rounded-rectangle
        # special case used for appearance validation below.
        primitive_values = {
            "ellipse": {"cx": 60, "cy": 20, "rx": 5, "ry": 3},
            "circle": {"cx": 72, "cy": 20, "r": 3},
            "line": {"x1": 56, "y1": 28, "x2": 66, "y2": 34},
            "polyline": {"points": [[70, 28], [74, 34], [78, 29]]},
            "polygon": {"points": [[82, 28], [88, 34], [92, 28]]},
            "path": {"d": "M 56,40 C 61,34 66,46 72,40"},
        }
        for kind, values in primitive_values.items():
            object_id = f"{prefix}-{kind}"
            primitive = service.live_create_shape(
                kind, values, object_id = object_id,
                style = {"fill": "none", "stroke": "#334455", "stroke_width": 0.8},
                expected_revision = revision,
            )
            revision = primitive["revision"]
            created_ids.append(object_id)

        text = service.live_create_text("RC1", 16, 25, text_id, style = {"fill": "#ffffff"}, expected_revision = revision)
        revision = text["revision"]
        created_ids.append(text_id)
        revision = service.live_set_text(text_id, "RC1 bridge", revision)["revision"]
        styled = service.live_set_style(
            [rectangle_id],
            {
                "stroke": "#102030", "stroke_width": "1.5px", "stroke_opacity": 0.8,
                "stroke_dasharray": "2,1", "stroke_linecap": "round",
                "stroke_linejoin": "bevel", "stroke_miterlimit": 3,
            },
            expected_revision = revision,
        )
        revision = styled["revision"]

        gradient_id = f"{prefix}-gradient"
        gradient = service.live_create_gradient(
            "linear",
            [{"offset": 0, "colour": "#ff0000"}, {"offset": 1, "colour": "#0000ff", "opacity": 0.7}],
            {"x1": 12, "y1": 12, "x2": 44, "y2": 30}, gradient_id, revision,
        )
        revision = gradient["revision"]
        revision = service.live_set_gradient_stops(
            gradient_id,
            [{"offset": 0, "colour": "#ffcc00"}, {"offset": 1, "colour": "#0044cc", "opacity": 0.7}],
            revision,
        )["revision"]
        applied = service.live_apply_gradient([rectangle_id], gradient_id, "fill", revision)
        revision = applied["revision"]

        imported = service.live_import_image("pixel.png", 48, 12, 8, 8, image_id, revision)
        revision = imported["revision"]
        created_ids.append(image_id)
        revision = service.live_transform("move", [rectangle_id], [2, 1], revision)["revision"]
        revision = service.live_transform("rotate", [rectangle_id], [15], revision)["revision"]
        revision = service.live_transform("scale", [rectangle_id], [1.1, 0.9], revision)["revision"]

        duplicated = service.live_duplicate_objects([rectangle_id], revision)
        revision = duplicated["revision"]
        duplicate_id = duplicated["created_ids"][0]
        created_ids.append(duplicate_id)
        grouped = service.live_group_objects([rectangle_id, duplicate_id], group_id, revision)
        revision = grouped["revision"]
        ungrouped = service.live_ungroup_objects(group_id, revision)
        revision = ungrouped["revision"]
        revision = service.live_reorder_objects([rectangle_id], "top", revision)["revision"]
        revision = service.live_reorder_objects([rectangle_id], "bottom", revision)["revision"]

        root_ids = {item["id"] for item in service.live_list_objects(limit = 1000)["objects"]}
        assert set(created_ids).issubset(root_ids)
        service.live_select([rectangle_id], revision)
        if initial_selection:
            service.live_select(initial_selection, revision)
        else:
            service.live_select([], revision)
        changes = service.live_poll_changes(revision - 1, 0)
        assert changes["revision"] == revision
        assert changes["document_changed"] is True
        with pytest.raises(RevisionConflictError):
            service.live_set_style([rectangle_id], {"opacity": 0.9}, revision - 1)

        snapshot = service.live_render_snapshot("page")
        assert snapshot["backend"] == "native_uds"
        assert snapshot["revision"] == revision
        assert service.get_snapshot_base64(snapshot["snapshot_id"])["mime_type"] == "image/png"
        drawing_snapshot = service.live_render_snapshot("drawing")
        assert drawing_snapshot["backend"] == "native_uds"
        assert drawing_snapshot["revision"] == revision
        assert service.get_snapshot_base64(drawing_snapshot["snapshot_id"])["mime_type"] == "image/png"
        assert service.live_list_selection().get("object_ids", []) == initial_selection

        removed = service.live_delete_objects(created_ids, revision)
        assert removed["revision"] == revision + 1
    finally:
        # A failed assertion must not leave test-created objects in the
        # explicitly authorized fixture. Best-effort cleanup cannot know a
        # newer external revision, so it deliberately does not retry on a
        # conflict.
        try:
            live = {item["id"] for item in service.live_list_objects(limit = 1000).get("objects", [])}
            stale_ids = [item for item in created_ids if item in live]
            if stale_ids:
                current = service.native_bridge.request("document.revision", {})["revision"]
                service.live_delete_objects(stale_ids, current)
        finally:
            service.close()
