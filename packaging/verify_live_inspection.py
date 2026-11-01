"""Rich native inspection and layer checks in the disposable GUI fixture."""
from __future__ import annotations

from io import BytesIO
import math

from PIL import Image


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def near(actual, expected, message):
    require(math.isclose(actual, expected, rel_tol=1e-7, abs_tol=1e-6), f"{message}: {actual} != {expected}")


async def verify_live_inspection(client, state):
    root = state.stdio_service.document_root.root
    source = root / "inspection.svg"
    source.write_text('''<svg xmlns="http://www.w3.org/2000/svg"
        xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
        width="105.833333333333mm" height="79.375mm" viewBox="0 0 200 150">
      <g id="outer" inkscape:groupmode="layer" inkscape:label="Outer Ω"
          transform="translate(10,5) scale(2)" style="fill:#13579b;stroke:#2468ac;stroke-width:2;font-family:sans-serif;opacity:0.5">
        <g id="inner" inkscape:groupmode="layer" inkscape:label="Inner">
          <g id="rotated-group" transform="translate(30,40) rotate(90)">
            <rect id="rotated" x="1" y="2" width="10" height="4"/>
          </g>
          <text id="words" x="5" y="20" style="font-size:8px;stroke:none;font-family:inherit">Measured <tspan style="font-weight:bold">text</tspan></text>
        </g>
      </g>
      <g id="hidden-layer" inkscape:groupmode="layer" inkscape:label="Hidden" style="display:none" sodipodi:insensitive="true"/>
    </svg>''')
    await client.call_tool("inkscape_start", {"path": "inspection.svg"})
    listed = (await client.call_tool("live_list_documents", {})).data["documents"]
    require(len(listed) == 1, "Expected one inspection fixture document")
    target = {key: listed[0][key] for key in ("instance_id", "document_id")}

    async def call(name, **params):
        return (await client.call_tool(name, {**target, **params})).data

    info = await call("live_get_document_info")
    require(info["revision"] == 0, "Inspection should begin at revision zero")
    coords = info["coordinates"]
    require(coords["bounds_space"] == "document_px" and coords["unit"] == "px", "Ambiguous bounds space")
    near(coords["page_width_px"], 400, "Physical width conversion")
    near(coords["page_height_px"], 300, "Physical height conversion")
    for actual, expected in zip(coords["root_to_document"], [2, 0, 0, 2, 0, 0]):
        near(actual, expected, "viewBox conversion")
    layers = await call("live_list_layers")
    by_id = {layer["id"]: layer for layer in layers["layers"]}
    require(set(by_id) == {"outer", "inner", "hidden-layer"} and by_id["inner"]["parent_layer_id"] == "outer", "Incorrect layer hierarchy")
    require(by_id["outer"]["label"] == "Outer Ω", "Layer label was not preserved")
    require(by_id["hidden-layer"]["hidden"] and by_id["hidden-layer"]["locked"], "Layer flags missing")
    page = await call("live_list_layers", offset=1, limit=1)
    require(page["total"] == 3 and [layer["id"] for layer in page["layers"]] == ["inner"], "Layer pagination failed")
    rect = (await call("live_get_object", object_id="rotated"))["object"]
    require(rect["attributes"]["width"] == "10" and rect["parent_id"] == "rotated-group", "Object attributes/parent missing")
    for key, expected in zip(("x", "y", "width", "height"), (116, 174, 16, 40)):
        near(rect["geometric_bounds"][key], expected, "Nested transformed geometry " + key)
    for key, expected in zip(("x", "y", "width", "height"), (112, 170, 24, 48)):
        near(rect["visual_bounds"][key], expected, "Transformed stroke bounds " + key)
    for actual, expected in zip(rect["item_to_document"], [0, 4, -4, 0, 140, 170]):
        near(actual, expected, "Nested transform matrix")
    style = rect["effective_style"]
    require(style["fill"] == "#13579b" and style["stroke"] == "#2468ac", "Inherited paint not resolved")
    near(style["stroke_width_local"], 2, "Inherited stroke width")
    near(style["cumulative_opacity"], 0.5, "Ancestor opacity")
    words = (await call("live_get_object", object_id="words"))["object"]
    require(words["text"] == "Measured text" and words["text_bounds"]["width"] > 50 and words["text_bounds"]["height"] > 0,
            "Text layout bounds unavailable")
    require(words["effective_style"]["font_family"] != "inherit", "Font family was not resolved")
    group = (await call("live_get_object", object_id="rotated-group"))["object"]
    require(group["child_ids"] == ["rotated"], "Group children missing")
    require(group["geometric_bounds"] == rect["geometric_bounds"], "Group bounds omit descendants")
    invalid = await client.call_tool("live_get_object", {**target, "object_id": "missing"}, raise_on_error=False)
    require(invalid.is_error, "Missing inspection target accepted")

    created = await call("live_create_layer", label="Created layer", layer_id="created", parent_layer_id="outer", expected_revision=0)
    require(created["revision"] == 1 and created["changed_ids"] == ["created"], "Layer creation receipt incorrect")
    duplicate = await client.call_tool("live_create_layer", {**target, "label": "Duplicate", "layer_id": "created", "expected_revision": 1}, raise_on_error=False)
    require(duplicate.is_error, "Duplicate layer ID accepted")
    bad_parent = await client.call_tool("live_create_layer", {**target, "label": "Bad", "layer_id": "bad-layer", "parent_layer_id": "rotated", "expected_revision": 1}, raise_on_error=False)
    require(bad_parent.is_error, "Non-layer parent accepted")
    await call("live_select", object_ids=["rotated"], expected_revision=1)
    current = await call("live_set_current_layer", layer_id="created", expected_revision=1)
    require((await call("live_list_selection"))["object_ids"] == ["rotated"], "Destination layer cleared the selection")
    require(current["current_layer_id"] == "created" and current["revision"] == 1, "Destination layer failed")
    shape = await call("live_create_shape", kind="rectangle", values={"x": 40, "y": 20, "width": 4, "height": 4},
                       object_id="default-layer-shape", style={"fill": "#ff6600", "stroke": "none"}, expected_revision=1)
    text = await call("live_create_text", text="Explicit", x=5, y=60, object_id="explicit-layer-text", layer_id="inner",
                      style={"font_size": "6px", "stroke": "none"}, expected_revision=shape["revision"])
    Image.new("RGB", (2, 2), (255, 255, 0)).save(root / "layer-image.png")
    image = await call("live_import_image", path="layer-image.png", x=70, y=10, object_id="default-layer-image", expected_revision=text["revision"])
    for object_id, parent in (("default-layer-shape", "created"), ("explicit-layer-text", "inner"), ("default-layer-image", "created")):
        require((await call("live_get_object", object_id=object_id))["object"]["parent_id"] == parent, "Drawing ignored destination layer")
    require((await call("live_get_document_info"))["current_layer_id"] == "created", "Destination layer was not retained")
    stale = await client.call_tool("live_set_current_layer", {**target, "layer_id": "outer", "expected_revision": 1}, raise_on_error=False)
    require(stale.is_error, "Destination layer ignored stale revision")
    snapshot = await call("live_render_snapshot", area="page")
    pixels = Image.open(BytesIO(state.stdio_service.snapshots.get(snapshot["snapshot_id"]).path.read_bytes())).convert("RGBA")
    require(pixels.size == (400, 300), "Snapshot physical size mismatch")
    # Independent pixel checks at analytically transformed object interiors.
    require(all(abs(a-b) <= 1 for a,b in zip(pixels.getpixel((124, 194))[:3], (19, 87, 155))), "Transformed inherited fill not rendered at inspected position")
    require(pixels.getpixel((188, 98))[:3] == (255, 102, 0), "Current-layer shape not rendered at measured position")
    require(pixels.getpixel((304, 54))[:3] == (255, 255, 0), "Current-layer image not rendered at measured position")
    ink = [(x, y) for y in range(40, 90) for x in range(400)
           if pixels.getpixel((x, y))[3] > 30 and
           all(abs(a-b) <= 4 for a,b in zip(pixels.getpixel((x, y))[:3], (19, 87, 155))) ]
    require(ink, "Laid-out text was not rendered")
    measured = words["text_bounds"]
    for actual, expected in ((min(x for x,y in ink), measured["x"]),
                             (min(y for x,y in ink), measured["y"]),
                             (max(x for x,y in ink)+1, measured["x"]+measured["width"]),
                             (max(y for x,y in ink)+1, measured["y"]+measured["height"])):
        require(abs(actual-expected) <= 2, f"Rendered text disagrees with layout bounds: {actual} vs {expected}")
    auto_first = await call("live_create_layer", label="Automatic one", expected_revision=image["revision"])
    auto_second = await call("live_create_layer", label="Automatic two", expected_revision=auto_first["revision"])
    require(auto_first["changed_ids"] != auto_second["changed_ids"], "Automatic layer IDs collided")
    stopped = (await client.call_tool("inkscape_shutdown", {})).data
    require(stopped["saved_paths"], "Inspection fixture changes not saved")
    return {"attributes_styles_transforms": True, "nested_geometric_visual_bounds": True,
            "text_layout": True, "physical_units_and_viewbox": True, "layer_hierarchy_and_creation": True,
            "drawing_destination_layers": True, "rendered_pixel_checks": True}
