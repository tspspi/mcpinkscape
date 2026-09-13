from __future__ import annotations

import pytest

from mcpinkscape.errors import InvalidDrawingValueError, RevisionConflictError
from mcpinkscape.svg_document import SVGDocument


def test_create_style_transform_and_group_round_trip(tmp_path):
    document = SVGDocument.create("doc-1", 200, 100, "example.svg")
    layer = document.create_layer("Artwork", "artwork")
    rectangle = document.create_shape(
        "rectangle",
        {"x": 10, "y": 20, "width": 30, "height": 40},
        "rect-1",
        layer["id"],
        {"fill": "#336699", "stroke": "#000", "stroke_width": "2px"},
    )
    text = document.create_text("Hello", 20, 30, "text-1", layer["id"], {"font_size": "12px"})
    changed = document.move([rectangle["id"]], 5, -2)
    assert changed[0]["attributes"]["transform"] == "translate(5,-2)"
    document.rotate([rectangle["id"]], 30)
    document.scale([rectangle["id"]], 1.5)
    group = document.group([rectangle["id"], text["id"]], "group-1")
    assert group["type"] == "g"
    assert {item["id"] for item in document.ungroup(group["id"])} == {"rect-1", "text-1"}
    destination = tmp_path / "example.svg"
    document.save(destination)
    restored = SVGDocument.load("doc-2", destination)
    assert restored.find("rect-1").get("fill") == "#336699"
    assert restored.find("text-1").text == "Hello"


def test_rectangle_corner_radii_are_serialized_and_reject_negative_values():
    document = SVGDocument.create("rounded-rectangle")
    rectangle = document.create_shape(
        "rectangle", {"x": 0, "y": 0, "width": 10, "height": 8, "rx": 2, "ry": "3px"}, "rounded",
    )
    assert rectangle["attributes"]["rx"] == "2"
    assert rectangle["attributes"]["ry"] == "3px"
    with pytest.raises(InvalidDrawingValueError, match = "rx"):
        document.create_shape("rectangle", {"x": 0, "y": 0, "width": 1, "height": 1, "rx": -1})


def test_revision_conflict_is_atomic():
    document = SVGDocument.create("doc-1")
    rectangle = document.create_shape("rectangle", {"x": 0, "y": 0, "width": 1, "height": 1})
    before = document.revision
    with pytest.raises(RevisionConflictError):
        document.require_revision(before - 1)
    assert document.revision == before
    assert document.find(rectangle["id"]) is not None


def test_style_and_attribute_validation():
    document = SVGDocument.create("doc-1")
    rectangle = document.create_shape("rectangle", {"x": 0, "y": 0, "width": 1, "height": 1})
    with pytest.raises(InvalidDrawingValueError):
        document.set_style([rectangle["id"]], {"fill": "bad;style"})
    with pytest.raises(InvalidDrawingValueError):
        document.set_attribute([rectangle["id"]], "onclick", "alert(1)")
    with pytest.raises(InvalidDrawingValueError):
        document.scale([rectangle["id"]], 0)


def test_absolute_unit_lengths_preserve_svg_text_and_use_css_px_bounds():
    document = SVGDocument.create("doc-units", "10mm", "1in")
    rectangle = document.create_shape("rectangle", {"x": "1mm", "y": "2pt", "width": "10mm", "height": "1cm"})
    assert document.document_info()["view_box"] == "0 0 37.7952755906 96"
    bounds = document.bounds(document.find(rectangle["id"]))
    assert bounds is not None
    assert bounds["width"] == pytest.approx(96.0 / 2.54)
    assert bounds["height"] == pytest.approx(96.0 / 2.54)


def test_page_background_uses_inkscape_page_opacity_not_border_opacity():
    document = SVGDocument.create("doc-background")
    result = document.set_page_background("#f5f7fa", 0.4)
    named_view = next(element for element in document.root if element.tag.endswith("namedview"))
    assert result["opacity"] == 0.4
    assert named_view.get("pagecolor") == "#f5f7fa"
    assert named_view.get("{http://www.inkscape.org/namespaces/inkscape}pageopacity") == "0.4"
    assert named_view.get("borderopacity") is None


def test_typed_stroke_border_parameters_are_validated_and_serialized():
    document = SVGDocument.create("borders")
    created = document.create_shape("rectangle", {"x": 0, "y": 0, "width": 10, "height": 10}, "bordered")
    document.set_style([created["id"]], {
        "stroke": "#123456", "stroke_width": "2mm", "stroke_dasharray": "3 1mm",
        "stroke_dashoffset": "1px", "stroke_linecap": "round", "stroke_linejoin": "bevel",
        "stroke_miterlimit": 4,
    })
    element = document.find("bordered")
    assert element.get("stroke-dasharray") == "3 1mm"
    assert element.get("stroke-linecap") == "round"
    assert element.get("stroke-linejoin") == "bevel"
    assert element.get("stroke-miterlimit") == "4"
    with pytest.raises(InvalidDrawingValueError, match = "stroke_linecap"):
        document.set_style(["bordered"], {"stroke_linecap": "sharp"})


def test_typed_linear_and_radial_gradients_create_stops_and_apply_to_shapes():
    document = SVGDocument.create("gradients")
    shape = document.create_shape("rectangle", {"x": 0, "y": 0, "width": 10, "height": 10}, "gradient-target")
    linear = document.create_gradient("linear", [
        {"offset": 0, "colour": "#112233", "opacity": 0.25},
        {"offset": 1, "colour": "#ddeeff"},
    ], {"x1": 0, "y1": 0, "x2": 10, "y2": 0}, "linear-test")
    assert linear["id"] == "linear-test"
    document.apply_gradient([shape["id"]], "linear-test")
    assert document.find(shape["id"]).get("fill") == "url(#linear-test)"
    radial = document.create_gradient("radial", [
        {"offset": 0, "color": "#fff"}, {"offset": 1, "color": "#000", "opacity": 0},
    ], {"cx": 5, "cy": 5, "r": 5, "fx": 4, "fy": 4}, "radial-test")
    assert radial["type"] == "radialGradient"
    document.set_gradient_stops("radial-test", [{"offset": 0, "colour": "#f00"}, {"offset": 1, "colour": "#00f"}])
    stops = [item for item in document.find("radial-test") if item.tag.rsplit("}", 1)[-1] == "stop"]
    assert [(stop.get("offset"), stop.get("stop-color")) for stop in stops] == [("0", "#f00"), ("1", "#00f")]


def test_gradient_rejects_invalid_stop_order_and_unknown_geometry():
    document = SVGDocument.create("invalid-gradients")
    with pytest.raises(InvalidDrawingValueError, match = "out of order"):
        document.create_gradient("linear", [{"offset": 1, "colour": "#fff"}, {"offset": 0, "colour": "#000"}])
    with pytest.raises(InvalidDrawingValueError, match = "geometry"):
        document.create_gradient("linear", [{"offset": 0, "colour": "#fff"}, {"offset": 1, "colour": "#000"}], {"angle": 45})


def test_bounds_and_transform_order_follow_document_coordinates():
    document = SVGDocument.create("doc-transform-bounds")
    rectangle = document.create_shape(
        "rectangle", {"x": 1, "y": 2, "width": 3, "height": 4}, "rect-1"
    )
    document.scale([rectangle["id"]], 2)
    document.move([rectangle["id"]], 5, -1)
    bounds = document.bounds(document.find(rectangle["id"]))
    assert bounds == {"x": 7.0, "y": 3.0, "width": 6.0, "height": 8.0}
    assert document.find(rectangle["id"]).get("transform") == "translate(5,-1) scale(2,2)"


def test_bounds_include_parent_transform_and_reject_unknown_transform_syntax():
    document = SVGDocument.create("doc-parent-transform")
    rectangle = document.create_shape(
        "rectangle", {"x": 1, "y": 2, "width": 3, "height": 4}, "rect-1"
    )
    group = document.group([rectangle["id"]], "group-1")
    document.move([group["id"]], 10, 20)
    assert document.bounds(document.find(rectangle["id"])) == {
        "x": 11.0, "y": 22.0, "width": 3.0, "height": 4.0,
    }
    assert document.bounds(document.find(group["id"])) == {
        "x": 11.0, "y": 22.0, "width": 3.0, "height": 4.0,
    }
    with pytest.raises(InvalidDrawingValueError):
        document.set_transform([rectangle["id"]], "translate(1) unexpected(2)")
