from __future__ import annotations

import base64
import shutil

import pytest

from mcpinkscape.config.schema import AccessConfig, BridgeConfig
from mcpinkscape.service import InkscapeService


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
    "S1Z8fQAAAABJRU5ErkJggg=="
)
_JPEG_1X1 = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkI"
    "CQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQ"
    "EBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAAB"
    "AAEDAREAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAAB//EABQQAQAAAAAAAAAAAAAAAAAAAAD/"
    "xAAVAQEBAAAAAAAAAAAAAAAAAAAHCP/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/"
    "ABJToCf/2Q=="
)

@pytest.mark.skipif(shutil.which("inkscape") is None, reason = "Inkscape CLI is not installed")
def test_rc1_offline_drawing_and_visual_feedback_with_real_inkscape(tmp_path):
    service = InkscapeService(
        AccessConfig(document_root = str(tmp_path / "library")),
        BridgeConfig(mode = "offline", executable = "inkscape"),
    )
    try:
        document_id = service.create_document("rc1.svg", "120mm", "80mm")["document_id"]
        assets = service.document_root.root / "assets"
        assets.mkdir()
        (assets / "pixel.png").write_bytes(_PNG_1X1)
        (assets / "pixel.jpg").write_bytes(_JPEG_1X1)
        revision = 0
        shapes = (
            ("rectangle", {"x": 5, "y": 5, "width": 20, "height": 15, "rx": 2, "ry": 3}, "rect"),
            ("ellipse", {"cx": 40, "cy": 15, "rx": 8, "ry": 6}, "ellipse"),
            ("circle", {"cx": 60, "cy": 15, "r": 6}, "circle"),
            ("line", {"x1": 70, "y1": 5, "x2": 85, "y2": 20}, "line"),
            ("polyline", {"points": [[5, 35], [12, 25], [20, 35]]}, "polyline"),
            ("polygon", {"points": [[30, 25], [38, 35], [22, 35]]}, "polygon"),
            ("path", {"d": "M 45,25 L 55,35 L 65,25 Z"}, "path"),
        )
        object_ids: list[str] = []
        for kind, values, object_id in shapes:
            created = service.create_shape(document_id, kind, values, object_id, expected_revision = revision)
            revision = created["revision"]
            object_ids.append(object_id)
        text = service.create_text(document_id, "RC1", 70, 35, "text", expected_revision = revision)
        revision = text["revision"]
        object_ids.append("text")
        styled = service.set_style(
            document_id,
            object_ids,
            {
                "fill": "#1565c0", "stroke": "#0d223f", "stroke_width": "0.8mm",
                "stroke_opacity": 0.8, "stroke_dasharray": "2 1", "stroke_dashoffset": "1px",
                "stroke_linecap": "round", "stroke_linejoin": "bevel", "stroke_miterlimit": 3,
                "opacity": 0.9,
            },
            expected_revision = revision,
        )
        revision = styled["revision"]
        linear = service.create_gradient(
            document_id, "linear",
            [{"offset": 0, "colour": "#ff0000"}, {"offset": 1, "colour": "#0000ff", "opacity": 0.7}],
            {"x1": 5, "y1": 5, "x2": 25, "y2": 20}, "linear-gradient", revision,
        )
        revision = linear["revision"]
        revision = service.apply_gradient(document_id, ["rect"], "linear-gradient", "fill", revision)["revision"]
        radial = service.create_gradient(
            document_id, "radial",
            [{"offset": 0, "colour": "#ffffff"}, {"offset": 1, "colour": "#1565c0", "opacity": 0.5}],
            {"cx": 40, "cy": 15, "r": 8, "fx": 40, "fy": 15}, "radial-gradient", revision,
        )
        revision = radial["revision"]
        revision = service.apply_gradient(document_id, ["ellipse"], "radial-gradient", "fill", revision)["revision"]
        png = service.import_image(document_id, "assets/pixel.png", 90, 5, 8, 8, "pixel-png", expected_revision = revision)
        revision = png["revision"]
        jpg = service.import_image(document_id, "assets/pixel.jpg", 102, 5, 8, 8, "pixel-jpg", expected_revision = revision)
        revision = jpg["revision"]
        object_ids.extend(("pixel-png", "pixel-jpg"))
        revision = service.set_page_background(document_id, "#f5f7fa", 1, expected_revision = revision)["revision"]
        revision = service.transform(document_id, "move", ["rect"], {"dx": 1, "dy": 2}, expected_revision = revision)["revision"]
        revision = service.transform(document_id, "rotate", ["ellipse"], {"degrees": 15}, expected_revision = revision)["revision"]
        revision = service.transform(document_id, "scale", ["circle"], {"scale_x": 1.2}, expected_revision = revision)["revision"]
        drawable_ids = {
            item["id"] for item in service.list_objects(document_id)
            if item["type"] != "defs"
        }
        assert drawable_ids == set(object_ids)
        assert service.get_object_bounds(document_id, "rect")["bounds"]["width"] == 20
        assert service.get_object(document_id, "rect")["attributes"]["rx"] == "2"
        assert service.get_object(document_id, "rect")["attributes"]["fill"] == "url(#linear-gradient)"
        assert service.get_object(document_id, "ellipse")["attributes"]["fill"] == "url(#radial-gradient)"
        assert service.get_object(document_id, "pixel-png")["attributes"]["href"].startswith("data:image/png;base64,")
        assert service.get_object(document_id, "pixel-jpg")["attributes"]["href"].startswith("data:image/jpeg;base64,")
        snapshot = service.render_snapshot(document_id, area = "page", width_px = 360)
        payload = service.get_snapshot_base64(snapshot["snapshot_id"])
        assert payload["base64"].startswith("iVBORw0KGgo")
        assert service.export_document(document_id, "rc1.pdf", "pdf")["mime_type"] == "application/pdf"
        assert revision == service.get_document_info(document_id)["revision"]
    finally:
        service.close()
