"""Offline SVG document model used by the MCP service.

The model deliberately exposes only typed drawing operations.  It does not
accept arbitrary XML fragments or filesystem paths from callers.
"""

from __future__ import annotations

import copy
import math
import re
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from mcpinkscape.errors import InvalidDrawingValueError, ObjectNotFoundError, RevisionConflictError


SVG_NAMESPACE = "http://www.w3.org/2000/svg"
INKSCAPE_NAMESPACE = "http://www.inkscape.org/namespaces/inkscape"
SODIPODI_NAMESPACE = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"

ElementTree.register_namespace("", SVG_NAMESPACE)
ElementTree.register_namespace("inkscape", INKSCAPE_NAMESPACE)
ElementTree.register_namespace("sodipodi", SODIPODI_NAMESPACE)

SVG = f"{{{SVG_NAMESPACE}}}"
INKSCAPE = f"{{{INKSCAPE_NAMESPACE}}}"

_COLOR_PATTERN = re.compile(
    r"^(?:none|#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}|"
    r"(?:rgb|rgba|hsl|hsla)\([^\r\n;{}]+\)|[a-zA-Z]+)$"
)
_LENGTH_PATTERN = re.compile(r"^(-?(?:\d+(?:\.\d*)?|\.\d+))(px|mm|cm|in|pt|pc)?$")
_LENGTH_FACTORS = {
    "": 1.0,
    "px": 1.0,
    "mm": 96.0 / 25.4,
    "cm": 96.0 / 2.54,
    "in": 96.0,
    "pt": 96.0 / 72.0,
    "pc": 16.0,
}
_ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")
_TRANSFORM_FUNCTION_PATTERN = re.compile(r"([A-Za-z]+)\s*\(([^()]*)\)")
_TRANSFORM_NUMBER_PATTERN = re.compile(
    r"[+-]?(?:(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)"
)
_ATTRIBUTE_ALLOWLIST = {
    "display", "fill-rule", "font-family", "font-size", "font-style",
    "font-weight", "letter-spacing", "opacity", "pointer-events",
    "stroke-dasharray", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
    "stroke-width", "text-anchor", "visibility",
}


def _as_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise InvalidDrawingValueError(f"{field_name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidDrawingValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(result):
        raise InvalidDrawingValueError(f"{field_name} must be finite")
    return result


def _format_number(value: float) -> str:
    return format(value, ".12g")


def _validate_length(value: Any, field_name: str) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _format_number(_as_float(value, field_name))
    text = str(value).strip()
    if not _LENGTH_PATTERN.fullmatch(text):
        raise InvalidDrawingValueError(f"{field_name} must be a finite SVG length")
    return text


def _as_length_float(value: Any, field_name: str) -> float:
    """Return an absolute SVG length in CSS px for bounds and viewBox maths."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _as_float(value, field_name)
    text = str(value).strip()
    match = _LENGTH_PATTERN.fullmatch(text)
    if match is None:
        raise InvalidDrawingValueError(f"{field_name} must be a finite absolute SVG length")
    number = _as_float(match.group(1), field_name)
    return number * _LENGTH_FACTORS[match.group(2) or ""]


def _validate_color(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not _COLOR_PATTERN.fullmatch(text):
        raise InvalidDrawingValueError(f"{field_name} is not an accepted CSS colour")
    return text


def _local_name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


AffineMatrix = tuple[float, float, float, float, float, float]
_IDENTITY_MATRIX: AffineMatrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _matrix_multiply(left: AffineMatrix, right: AffineMatrix) -> AffineMatrix:
    """Return the SVG affine matrix for applying right, then left."""
    a1, b1, c1, d1, e1, f1 = left
    a2, b2, c2, d2, e2, f2 = right
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _matrix_apply(matrix: AffineMatrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return (a * x + c * y + e, b * x + d * y + f)


def _transform_numbers(text: str) -> list[float]:
    values: list[float] = []
    position = 0
    for match in _TRANSFORM_NUMBER_PATTERN.finditer(text):
        if not re.fullmatch(r"[\s,]*", text[position:match.start()]):
            raise InvalidDrawingValueError("transform arguments must be SVG numbers")
        values.append(_as_float(match.group(0), "transform argument"))
        position = match.end()
    if (not values) or (not re.fullmatch(r"[\s,]*", text[position:])):
        raise InvalidDrawingValueError("transform arguments must be SVG numbers")
    return values


def _parse_transform(text: str) -> AffineMatrix:
    """Parse the bounded SVG transform subset accepted by the typed API."""
    position = 0
    result = _IDENTITY_MATRIX
    for match in _TRANSFORM_FUNCTION_PATTERN.finditer(text):
        if not re.fullmatch(r"\s*", text[position:match.start()]):
            raise InvalidDrawingValueError("transform syntax is invalid")
        name = match.group(1)
        values = _transform_numbers(match.group(2))
        if name == "matrix" and len(values) == 6:
            matrix: AffineMatrix = tuple(values)  # type: ignore[assignment]
        elif name == "translate" and len(values) in (1, 2):
            matrix = (1.0, 0.0, 0.0, 1.0, values[0], values[1] if len(values) == 2 else 0.0)
        elif name == "scale" and len(values) in (1, 2):
            matrix = (values[0], 0.0, 0.0, values[1] if len(values) == 2 else values[0], 0.0, 0.0)
        elif name == "rotate" and len(values) in (1, 3):
            radians = math.radians(values[0])
            rotation: AffineMatrix = (math.cos(radians), math.sin(radians), -math.sin(radians), math.cos(radians), 0.0, 0.0)
            if len(values) == 3:
                matrix = _matrix_multiply(
                    _matrix_multiply((1.0, 0.0, 0.0, 1.0, values[1], values[2]), rotation),
                    (1.0, 0.0, 0.0, 1.0, -values[1], -values[2]),
                )
            else:
                matrix = rotation
        elif name == "skewX" and len(values) == 1:
            matrix = (1.0, 0.0, math.tan(math.radians(values[0])), 1.0, 0.0, 0.0)
        elif name == "skewY" and len(values) == 1:
            matrix = (1.0, math.tan(math.radians(values[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            raise InvalidDrawingValueError("transform uses an unsupported SVG operation or argument count")
        result = _matrix_multiply(result, matrix)
        position = match.end()
    if not re.fullmatch(r"\s*", text[position:]):
        raise InvalidDrawingValueError("transform syntax is invalid")
    return result


@dataclass
class SVGDocument:
    """Mutable in-memory SVG document with stable object identifiers."""

    document_id: str
    root: ElementTree.Element
    source_path: Path | None = None
    revision: int = 0
    selection: list[str] = field(default_factory=list)
    _id_counter: int = 1

    @classmethod
    def create(
        cls,
        document_id: str,
        width: Any = 800,
        height: Any = 600,
        name: str | None = None,
    ) -> "SVGDocument":
        root = ElementTree.Element(
            SVG + "svg",
            {
                "width": _validate_length(width, "width"),
                "height": _validate_length(height, "height"),
                "viewBox": f"0 0 {_format_number(_as_length_float(width, 'width'))} {_format_number(_as_length_float(height, 'height'))}",
                "version": "1.1",
            },
        )
        if name:
            root.set(INKSCAPE + "document-name", str(name))
        return cls(document_id = document_id, root = root)

    @classmethod
    def load(cls, document_id: str, source_path: Path) -> "SVGDocument":
        try:
            tree = ElementTree.parse(source_path)
        except (OSError, ElementTree.ParseError) as exc:
            raise InvalidDrawingValueError(f"cannot open SVG document: {exc}") from exc
        root = tree.getroot()
        if _local_name(root) != "svg":
            raise InvalidDrawingValueError("document root must be an SVG element")
        document = cls(document_id = document_id, root = root, source_path = source_path)
        document._id_counter = document._find_next_counter()
        return document

    def _find_next_counter(self) -> int:
        maximum = 0
        for element in self.root.iter():
            element_id = element.get("id", "")
            match = re.search(r"-(\d+)$", element_id)
            if match:
                maximum = max(maximum, int(match.group(1)))
        return maximum + 1

    def _next_id(self, prefix: str) -> str:
        while True:
            candidate = f"mcp-{prefix}-{self._id_counter}"
            self._id_counter += 1
            if self.find(candidate, required = False) is None:
                return candidate

    def _mutated(self) -> int:
        self.revision += 1
        return self.revision

    def require_revision(self, expected_revision: int | None) -> None:
        if (expected_revision is not None) and (expected_revision != self.revision):
            raise RevisionConflictError(
                f"document revision is {self.revision}, expected {expected_revision}"
            )

    def find(self, object_id: str, required: bool = True) -> ElementTree.Element | None:
        if not _ID_PATTERN.fullmatch(str(object_id)):
            raise InvalidDrawingValueError("object_id is not a valid SVG ID")
        for element in self.root.iter():
            if element.get("id") == object_id:
                return element
        if required:
            raise ObjectNotFoundError(f"object '{object_id}' was not found")
        return None

    def _parent_map(self) -> dict[ElementTree.Element, ElementTree.Element]:
        parents: dict[ElementTree.Element, ElementTree.Element] = {}
        for parent in self.root.iter():
            for child in parent:
                parents[child] = parent
        return parents

    def _target_parent(self, layer_id: str | None) -> ElementTree.Element:
        if layer_id is None:
            return self.root
        layer = self.find(layer_id)
        if _local_name(layer) != "g":
            raise InvalidDrawingValueError("layer_id must identify a group/layer")
        return layer

    def create_layer(self, label: str, layer_id: str | None = None) -> dict[str, Any]:
        if not str(label).strip():
            raise InvalidDrawingValueError("layer label must not be empty")
        object_id = layer_id or self._next_id("layer")
        if self.find(object_id, required = False) is not None:
            raise InvalidDrawingValueError(f"object ID '{object_id}' already exists")
        layer = ElementTree.SubElement(
            self.root,
            SVG + "g",
            {
                "id": object_id,
                INKSCAPE + "groupmode": "layer",
                INKSCAPE + "label": str(label),
                "data-mcp-created": "true",
            },
        )
        self._mutated()
        return self.describe(layer)

    def create_shape(
        self,
        kind: str,
        values: dict[str, Any],
        object_id: str | None = None,
        layer_id: str | None = None,
        style: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        element_name, attributes = self._shape_attributes(kind, values)
        result_id = object_id or self._next_id(kind)
        if self.find(result_id, required = False) is not None:
            raise InvalidDrawingValueError(f"object ID '{result_id}' already exists")
        attributes["id"] = result_id
        attributes["data-mcp-created"] = "true"
        element = ElementTree.SubElement(self._target_parent(layer_id), SVG + element_name, attributes)
        if style:
            self._set_style_on_element(element, style)
        self._mutated()
        return self.describe(element)

    def _shape_attributes(self, kind: str, values: dict[str, Any]) -> tuple[str, dict[str, str]]:
        kind = str(kind).strip().lower()
        specs: dict[str, tuple[str, tuple[str, ...]]] = {
            "rectangle": ("rect", ("x", "y", "width", "height")),
            "ellipse": ("ellipse", ("cx", "cy", "rx", "ry")),
            "circle": ("circle", ("cx", "cy", "r")),
            "line": ("line", ("x1", "y1", "x2", "y2")),
        }
        if kind in specs:
            element_name, fields = specs[kind]
            attributes: dict[str, str] = {}
            for field in fields:
                if field not in values:
                    raise InvalidDrawingValueError(f"{kind} requires {field}")
                attributes[field] = _validate_length(values[field], field)
            if kind == "rectangle":
                for field in ("width", "height"):
                    if _as_length_float(values[field], field) < 0:
                        raise InvalidDrawingValueError(f"{field} must not be negative")
                for field in ("rx", "ry"):
                    if field not in values:
                        continue
                    attributes[field] = _validate_length(values[field], field)
                    if _as_length_float(values[field], field) < 0:
                        raise InvalidDrawingValueError(f"{field} must not be negative")
            if kind in ("ellipse", "circle"):
                for field in ("r", "rx", "ry"):
                    if (field in values) and (_as_length_float(values[field], field) < 0):
                        raise InvalidDrawingValueError(f"{field} must not be negative")
            return element_name, attributes
        if kind in ("polyline", "polygon"):
            points = values.get("points")
            if not isinstance(points, list) or len(points) < 2:
                raise InvalidDrawingValueError(f"{kind} requires at least two points")
            encoded: list[str] = []
            for point in points:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    raise InvalidDrawingValueError("each point must contain x and y")
                encoded.append(f"{_format_number(_as_float(point[0], 'x'))},{_format_number(_as_float(point[1], 'y'))}")
            return kind, {"points": " ".join(encoded)}
        if kind == "path":
            path_data = str(values.get("d", "")).strip()
            if (not path_data) or any(character in path_data for character in "\r\n<>"):
                raise InvalidDrawingValueError("path d must be a non-empty SVG path string")
            return "path", {"d": path_data}
        raise InvalidDrawingValueError(f"unsupported shape kind '{kind}'")

    def create_text(
        self,
        text: str,
        x: Any,
        y: Any,
        object_id: str | None = None,
        layer_id: str | None = None,
        style: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(text, str):
            raise InvalidDrawingValueError("text must be a string")
        result_id = object_id or self._next_id("text")
        if self.find(result_id, required = False) is not None:
            raise InvalidDrawingValueError(f"object ID '{result_id}' already exists")
        element = ElementTree.SubElement(
            self._target_parent(layer_id),
            SVG + "text",
            {
                "id": result_id,
                "x": _validate_length(x, "x"),
                "y": _validate_length(y, "y"),
                "xml:space": "preserve",
                "data-mcp-created": "true",
            },
        )
        element.text = text
        if style:
            self._set_style_on_element(element, style)
        self._mutated()
        return self.describe(element)

    def set_text(self, object_id: str, text: str) -> dict[str, Any]:
        element = self.find(object_id)
        if _local_name(element) != "text":
            raise InvalidDrawingValueError("set_text requires an SVG text object")
        if not isinstance(text, str):
            raise InvalidDrawingValueError("text must be a string")
        element.text = text
        self._mutated()
        return self.describe(element)

    def set_style(self, object_ids: Iterable[str], style: dict[str, Any]) -> list[dict[str, Any]]:
        elements = [self.find(object_id) for object_id in object_ids]
        if not elements:
            raise InvalidDrawingValueError("object_ids must not be empty")
        for element in elements:
            self._set_style_on_element(element, style)
        self._mutated()
        return [self.describe(element) for element in elements]

    def _set_style_on_element(self, element: ElementTree.Element, style: dict[str, Any]) -> None:
        allowed = {
            "fill", "fill_opacity", "stroke", "stroke_opacity", "stroke_width",
            "stroke_dasharray", "stroke_dashoffset", "stroke_linecap", "stroke_linejoin", "stroke_miterlimit",
            "opacity", "font_family", "font_size", "font_style", "font_weight",
            "text_anchor", "letter_spacing",
        }
        unknown = set(style) - allowed
        if unknown:
            raise InvalidDrawingValueError(f"unsupported style fields: {', '.join(sorted(unknown))}")
        color_fields = {"fill": "fill", "stroke": "stroke"}
        for input_name, attribute_name in color_fields.items():
            if input_name in style:
                color = _validate_color(style[input_name], input_name)
                if color is not None:
                    element.set(attribute_name, color)
        scalar_fields = {
            "fill_opacity": "fill-opacity",
            "stroke_opacity": "stroke-opacity",
            "opacity": "opacity",
        }
        for input_name, attribute_name in scalar_fields.items():
            if input_name in style:
                value = _as_float(style[input_name], input_name)
                if (value < 0.0) or (value > 1.0):
                    raise InvalidDrawingValueError(f"{input_name} must be between 0 and 1")
                element.set(attribute_name, _format_number(value))
        for input_name, attribute_name in {
            "stroke_width": "stroke-width", "stroke_dashoffset": "stroke-dashoffset",
            "font_size": "font-size", "letter_spacing": "letter-spacing"
        }.items():
            if input_name in style:
                element.set(attribute_name, _validate_length(style[input_name], input_name))
        if "stroke_dasharray" in style:
            raw_dasharray = str(style["stroke_dasharray"]).strip()
            if raw_dasharray == "none":
                element.set("stroke-dasharray", raw_dasharray)
            else:
                parts = [part for part in re.split(r"[ ,]+", raw_dasharray) if part]
                if not parts:
                    raise InvalidDrawingValueError("stroke_dasharray must be 'none' or SVG lengths")
                element.set("stroke-dasharray", " ".join(_validate_length(part, "stroke_dasharray") for part in parts))
        for input_name, attribute_name, accepted in (
            ("stroke_linecap", "stroke-linecap", {"butt", "round", "square"}),
            ("stroke_linejoin", "stroke-linejoin", {"miter", "round", "bevel", "miter-clip", "arcs"}),
        ):
            if input_name in style:
                value = str(style[input_name]).strip().lower()
                if value not in accepted:
                    raise InvalidDrawingValueError(f"{input_name} is invalid")
                element.set(attribute_name, value)
        if "stroke_miterlimit" in style:
            value = _as_float(style["stroke_miterlimit"], "stroke_miterlimit")
            if value < 1:
                raise InvalidDrawingValueError("stroke_miterlimit must be at least 1")
            element.set("stroke-miterlimit", _format_number(value))
        for input_name, attribute_name in {
            "font_family": "font-family", "font_style": "font-style", "font_weight": "font-weight",
            "text_anchor": "text-anchor",
        }.items():
            if input_name in style:
                value = str(style[input_name]).strip()
                if (not value) or any(character in value for character in "\r\n;{}<>"):
                    raise InvalidDrawingValueError(f"{input_name} is invalid")
                element.set(attribute_name, value)

    def set_attribute(self, object_ids: Iterable[str], attribute: str, value: Any) -> list[dict[str, Any]]:
        attribute = str(attribute).strip()
        if attribute not in _ATTRIBUTE_ALLOWLIST:
            raise InvalidDrawingValueError(f"attribute '{attribute}' is not allowed")
        text = str(value).strip()
        if (not text) or any(character in text for character in "\r\n;{}<>"):
            raise InvalidDrawingValueError("attribute value is invalid")
        elements = [self.find(object_id) for object_id in object_ids]
        if not elements:
            raise InvalidDrawingValueError("object_ids must not be empty")
        for element in elements:
            element.set(attribute, text)
        self._mutated()
        return [self.describe(element) for element in elements]

    def set_page_background(self, colour: str, opacity: Any = 1.0) -> dict[str, Any]:
        background = _validate_color(colour, "colour")
        alpha = _as_float(opacity, "opacity")
        if (alpha < 0.0) or (alpha > 1.0):
            raise InvalidDrawingValueError("opacity must be between 0 and 1")
        named_view = None
        for element in self.root:
            if _local_name(element) == "namedview":
                named_view = element
                break
        if named_view is None:
            named_view = ElementTree.Element("{" + SODIPODI_NAMESPACE + "}namedview")
            self.root.insert(0, named_view)
        named_view.set("pagecolor", background or "none")
        # `borderopacity` controls the canvas page border.  Inkscape stores
        # the actual page background alpha in its namespaced `pageopacity`.
        named_view.set(INKSCAPE + "pageopacity", _format_number(alpha))
        self._mutated()
        return {"colour": background, "opacity": alpha, "revision": self.revision}

    def create_gradient(
        self,
        kind: str,
        stops: list[dict[str, Any]],
        geometry: dict[str, Any] | None = None,
        gradient_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a deterministic user-space SVG linear or radial gradient."""
        normalized_kind = str(kind).strip().lower()
        names = {"linear": "linearGradient", "radial": "radialGradient"}
        if normalized_kind not in names:
            raise InvalidDrawingValueError("gradient kind must be linear or radial")
        result_id = gradient_id or self._next_id(f"{normalized_kind}-gradient")
        if self.find(result_id, required = False) is not None:
            raise InvalidDrawingValueError(f"object ID '{result_id}' already exists")
        definitions = self._defs()
        if any(child.get("id") == result_id for child in definitions):
            raise InvalidDrawingValueError(f"gradient ID '{result_id}' already exists")
        attributes = {"id": result_id, "gradientUnits": "userSpaceOnUse", "data-mcp-created": "true"}
        attributes.update(self._gradient_geometry(normalized_kind, geometry or {}))
        gradient = ElementTree.SubElement(definitions, SVG + names[normalized_kind], attributes)
        self._replace_gradient_stops(gradient, stops)
        self._mutated()
        return self.describe(gradient)

    def set_gradient_stops(self, gradient_id: str, stops: list[dict[str, Any]]) -> dict[str, Any]:
        gradient = self._gradient(gradient_id)
        self._replace_gradient_stops(gradient, stops)
        self._mutated()
        return self.describe(gradient)

    def apply_gradient(
        self,
        object_ids: Iterable[str],
        gradient_id: str,
        target: str = "fill",
    ) -> list[dict[str, Any]]:
        gradient = self._gradient(gradient_id)
        target = str(target).strip().lower()
        if target not in {"fill", "stroke"}:
            raise InvalidDrawingValueError("gradient target must be fill or stroke")
        elements = [self.find(object_id) for object_id in object_ids]
        if not elements:
            raise InvalidDrawingValueError("object_ids must not be empty")
        reference = f"url(#{gradient.get('id')})"
        for element in elements:
            element.set(target, reference)
        self._mutated()
        return [self.describe(element) for element in elements]

    def create_embedded_image(
        self, data_uri: str, mime_type: str, x: Any, y: Any, width: Any, height: Any,
        object_id: str | None = None, layer_id: str | None = None,
    ) -> dict[str, Any]:
        """Insert a server-validated, embedded PNG/JPEG as a normal SVG image."""
        if mime_type not in {"image/png", "image/jpeg"}:
            raise InvalidDrawingValueError("embedded image MIME type must be image/png or image/jpeg")
        if not isinstance(data_uri, str) or not data_uri.startswith(f"data:{mime_type};base64,") or len(data_uri) > 40_000_000:
            raise InvalidDrawingValueError("embedded image data URI is invalid or too large")
        result_id = object_id or self._next_id("image")
        if self.find(result_id, required = False) is not None:
            raise InvalidDrawingValueError(f"object ID '{result_id}' already exists")
        attributes = {
            "id": result_id, "x": _validate_length(x, "x"), "y": _validate_length(y, "y"),
            "width": _validate_length(width, "width"), "height": _validate_length(height, "height"),
            "href": data_uri, "preserveAspectRatio": "none", "data-mcp-created": "true",
        }
        if _as_length_float(width, "width") <= 0 or _as_length_float(height, "height") <= 0:
            raise InvalidDrawingValueError("embedded image width and height must be positive")
        element = ElementTree.SubElement(self._target_parent(layer_id), SVG + "image", attributes)
        self._mutated()
        return self.describe(element)

    def _defs(self) -> ElementTree.Element:
        for element in self.root:
            if _local_name(element) == "defs":
                return element
        definitions = ElementTree.Element(SVG + "defs", {"id": self._next_id("defs")})
        self.root.insert(0, definitions)
        return definitions

    def _gradient(self, gradient_id: str) -> ElementTree.Element:
        if not _ID_PATTERN.fullmatch(str(gradient_id)):
            raise InvalidDrawingValueError("gradient ID is invalid")
        for definitions in self.root:
            if _local_name(definitions) != "defs":
                continue
            for child in definitions:
                if child.get("id") == gradient_id and _local_name(child) in {"linearGradient", "radialGradient"}:
                    return child
        raise ObjectNotFoundError(f"gradient '{gradient_id}' does not exist")

    def _gradient_geometry(self, kind: str, geometry: dict[str, Any]) -> dict[str, str]:
        if not isinstance(geometry, dict):
            raise InvalidDrawingValueError("gradient geometry must be an object")
        fields = {
            "linear": ("x1", "y1", "x2", "y2"),
            "radial": ("cx", "cy", "r", "fx", "fy"),
        }[kind]
        defaults = {
            "linear": {"x1": "0", "y1": "0", "x2": "1", "y2": "0"},
            "radial": {"cx": "0.5", "cy": "0.5", "r": "0.5", "fx": "0.5", "fy": "0.5"},
        }[kind]
        unknown = set(geometry) - set(fields)
        if unknown:
            raise InvalidDrawingValueError(f"unsupported {kind} gradient geometry fields: {', '.join(sorted(unknown))}")
        result: dict[str, str] = {}
        for field in fields:
            raw = geometry.get(field, defaults[field])
            result[field] = _validate_length(raw, field)
            if field == "r" and _as_length_float(raw, field) <= 0:
                raise InvalidDrawingValueError("radial gradient r must be positive")
        return result

    def _replace_gradient_stops(self, gradient: ElementTree.Element, stops: list[dict[str, Any]]) -> None:
        if not isinstance(stops, list) or not (2 <= len(stops) <= 32):
            raise InvalidDrawingValueError("gradient requires between 2 and 32 stops")
        normalized: list[tuple[float, str, float]] = []
        previous = -1.0
        for index, stop in enumerate(stops):
            if not isinstance(stop, dict):
                raise InvalidDrawingValueError(f"gradient stop {index} must be an object")
            if set(stop) - {"offset", "colour", "color", "opacity"}:
                raise InvalidDrawingValueError(f"gradient stop {index} has unsupported fields")
            if "offset" not in stop or ("colour" not in stop and "color" not in stop):
                raise InvalidDrawingValueError(f"gradient stop {index} requires offset and colour")
            offset = _as_float(stop["offset"], f"gradient stop {index} offset")
            opacity = _as_float(stop.get("opacity", 1.0), f"gradient stop {index} opacity")
            colour = _validate_color(stop.get("colour", stop.get("color")), f"gradient stop {index} colour")
            if colour is None or not (0 <= offset <= 1) or not (0 <= opacity <= 1) or offset < previous:
                raise InvalidDrawingValueError(f"gradient stop {index} is invalid or out of order")
            normalized.append((offset, colour, opacity))
            previous = offset
        for child in list(gradient):
            if _local_name(child) == "stop":
                gradient.remove(child)
        for offset, colour, opacity in normalized:
            ElementTree.SubElement(gradient, SVG + "stop", {
                "offset": _format_number(offset), "stop-color": colour, "stop-opacity": _format_number(opacity),
            })

    def move(self, object_ids: Iterable[str], dx: Any, dy: Any) -> list[dict[str, Any]]:
        return self._transform(object_ids, f"translate({_format_number(_as_float(dx, 'dx'))},{_format_number(_as_float(dy, 'dy'))})")

    def rotate(self, object_ids: Iterable[str], degrees: Any, cx: Any | None = None, cy: Any | None = None) -> list[dict[str, Any]]:
        angle = _format_number(_as_float(degrees, "degrees"))
        if (cx is None) != (cy is None):
            raise InvalidDrawingValueError("cx and cy must be supplied together")
        if cx is None:
            transform = f"rotate({angle})"
        else:
            transform = f"rotate({angle},{_format_number(_as_float(cx, 'cx'))},{_format_number(_as_float(cy, 'cy'))})"
        return self._transform(object_ids, transform)

    def scale(self, object_ids: Iterable[str], scale_x: Any, scale_y: Any | None = None) -> list[dict[str, Any]]:
        x = _as_float(scale_x, "scale_x")
        y = x if scale_y is None else _as_float(scale_y, "scale_y")
        if (x == 0.0) or (y == 0.0):
            raise InvalidDrawingValueError("scale factors must not be zero")
        return self._transform(object_ids, f"scale({_format_number(x)},{_format_number(y)})")

    def set_transform(self, object_ids: Iterable[str], transform: str) -> list[dict[str, Any]]:
        text = str(transform).strip()
        if (not text) or any(character in text for character in "\r\n;{}<>"):
            raise InvalidDrawingValueError("transform is invalid")
        _parse_transform(text)
        elements = [self.find(object_id) for object_id in object_ids]
        if not elements:
            raise InvalidDrawingValueError("object_ids must not be empty")
        for element in elements:
            element.set("transform", text)
        self._mutated()
        return [self.describe(element) for element in elements]

    def _transform(self, object_ids: Iterable[str], operation: str) -> list[dict[str, Any]]:
        elements = [self.find(object_id) for object_id in object_ids]
        if not elements:
            raise InvalidDrawingValueError("object_ids must not be empty")
        for element in elements:
            existing = element.get("transform", "").strip()
            # SVG applies the rightmost transform first. Prefix the new matrix
            # so a requested move/rotate/scale acts in document coordinates
            # after any transform the object already has.
            element.set("transform", f"{operation} {existing}".strip())
        self._mutated()
        return [self.describe(element) for element in elements]

    def delete(self, object_ids: Iterable[str]) -> list[str]:
        elements = [self.find(object_id) for object_id in object_ids]
        if not elements:
            raise InvalidDrawingValueError("object_ids must not be empty")
        parents = self._parent_map()
        for element in elements:
            parent = parents.get(element)
            if parent is None:
                raise InvalidDrawingValueError("cannot delete SVG root")
            parent.remove(element)
        deleted = [element.get("id", "") for element in elements]
        self.selection = [item for item in self.selection if item not in deleted]
        self._mutated()
        return deleted

    def select(self, object_ids: Iterable[str]) -> dict[str, Any]:
        values = list(object_ids)
        for object_id in values:
            self.find(object_id)
        self.selection = values
        return {"selection": list(self.selection), "revision": self.revision}

    def describe(self, element: ElementTree.Element) -> dict[str, Any]:
        parents = self._parent_map()
        parent = parents.get(element)
        return {
            "id": element.get("id"),
            "type": _local_name(element),
            "label": element.get(INKSCAPE + "label"),
            "attributes": dict(element.attrib),
            "text": element.text or "",
            "parent_id": parent.get("id") if parent is not None else None,
            "bounds": self.bounds(element),
        }

    def bounds(self, element: ElementTree.Element) -> dict[str, float] | None:
        name = _local_name(element)
        try:
            local_bounds: dict[str, float] | None = None
            if name == "rect":
                local_bounds = self._box(element.get("x", 0), element.get("y", 0), element.get("width", 0), element.get("height", 0))
            elif name == "circle":
                radius = _as_length_float(element.get("r", 0), "r")
                local_bounds = self._box(_as_length_float(element.get("cx", 0), "cx") - radius, _as_length_float(element.get("cy", 0), "cy") - radius, radius * 2, radius * 2)
            elif name == "ellipse":
                rx = _as_length_float(element.get("rx", 0), "rx")
                ry = _as_length_float(element.get("ry", 0), "ry")
                local_bounds = self._box(_as_length_float(element.get("cx", 0), "cx") - rx, _as_length_float(element.get("cy", 0), "cy") - ry, rx * 2, ry * 2)
            elif name == "line":
                x1 = _as_length_float(element.get("x1", 0), "x1")
                x2 = _as_length_float(element.get("x2", 0), "x2")
                y1 = _as_length_float(element.get("y1", 0), "y1")
                y2 = _as_length_float(element.get("y2", 0), "y2")
                local_bounds = self._box(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
            elif name == "g":
                child_bounds = [self.bounds(child) for child in element]
                available = [item for item in child_bounds if item is not None]
                if available:
                    x = min(item["x"] for item in available)
                    y = min(item["y"] for item in available)
                    maximum_x = max(item["x"] + item["width"] for item in available)
                    maximum_y = max(item["y"] + item["height"] for item in available)
                    return {"x": x, "y": y, "width": maximum_x - x, "height": maximum_y - y}
            if local_bounds is not None:
                return self._transform_bounds(local_bounds, self._effective_transform(element))
        except InvalidDrawingValueError:
            return None
        return None

    def _effective_transform(self, element: ElementTree.Element) -> AffineMatrix:
        parents = self._parent_map()
        lineage: list[ElementTree.Element] = []
        current: ElementTree.Element | None = element
        while current is not None:
            lineage.append(current)
            current = parents.get(current)
        result = _IDENTITY_MATRIX
        for current in reversed(lineage):
            transform = current.get("transform", "").strip()
            if transform:
                result = _matrix_multiply(result, _parse_transform(transform))
        return result

    @staticmethod
    def _transform_bounds(bounds: dict[str, float], matrix: AffineMatrix) -> dict[str, float]:
        x = bounds["x"]
        y = bounds["y"]
        width = bounds["width"]
        height = bounds["height"]
        points = [
            _matrix_apply(matrix, x, y),
            _matrix_apply(matrix, x + width, y),
            _matrix_apply(matrix, x, y + height),
            _matrix_apply(matrix, x + width, y + height),
        ]
        min_x = min(point[0] for point in points)
        min_y = min(point[1] for point in points)
        return {
            "x": min_x,
            "y": min_y,
            "width": max(point[0] for point in points) - min_x,
            "height": max(point[1] for point in points) - min_y,
        }

    def _box(self, x: Any, y: Any, width: Any, height: Any) -> dict[str, float]:
        return {
            "x": _as_length_float(x, "x"),
            "y": _as_length_float(y, "y"),
            "width": _as_length_float(width, "width"),
            "height": _as_length_float(height, "height"),
        }

    def list_objects(self, parent_id: str | None = None) -> list[dict[str, Any]]:
        parent = self.root if parent_id is None else self.find(parent_id)
        return [self.describe(element) for element in parent if element.get("id")]

    def document_info(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "source_path": str(self.source_path) if self.source_path else None,
            "width": self.root.get("width"),
            "height": self.root.get("height"),
            "view_box": self.root.get("viewBox"),
            "revision": self.revision,
            "selection": list(self.selection),
        }

    def save(self, target_path: Path | None = None) -> Path:
        destination = target_path or self.source_path
        if destination is None:
            raise InvalidDrawingValueError("document has no save path")
        destination.parent.mkdir(parents = True, exist_ok = True)
        ElementTree.ElementTree(self.root).write(destination, encoding = "utf-8", xml_declaration = True)
        self.source_path = destination
        return destination

    def serialize(self) -> bytes:
        return ElementTree.tostring(self.root, encoding = "utf-8", xml_declaration = True)

    def duplicate(self, object_ids: Iterable[str]) -> list[dict[str, Any]]:
        elements = [self.find(object_id) for object_id in object_ids]
        parents = self._parent_map()
        duplicates: list[ElementTree.Element] = []
        for element in elements:
            parent = parents.get(element)
            if parent is None:
                raise InvalidDrawingValueError("cannot duplicate SVG root")
            duplicate = copy.deepcopy(element)
            duplicate.set("id", self._next_id(_local_name(element)))
            parent.insert(list(parent).index(element) + 1, duplicate)
            duplicates.append(duplicate)
        self._mutated()
        return [self.describe(element) for element in duplicates]

    def group(self, object_ids: Iterable[str], group_id: str | None = None) -> dict[str, Any]:
        elements = [self.find(object_id) for object_id in object_ids]
        if not elements:
            raise InvalidDrawingValueError("object_ids must not be empty")
        parents = self._parent_map()
        parent = parents.get(elements[0])
        if (parent is None) or any(parents.get(element) is not parent for element in elements):
            raise InvalidDrawingValueError("objects must share one parent to group")
        result_id = group_id or self._next_id("group")
        if self.find(result_id, required = False) is not None:
            raise InvalidDrawingValueError(f"object ID '{result_id}' already exists")
        group = ElementTree.Element(SVG + "g", {"id": result_id, "data-mcp-created": "true"})
        insertion_index = list(parent).index(elements[0])
        parent.insert(insertion_index, group)
        for element in elements:
            parent.remove(element)
            group.append(element)
        self._mutated()
        return self.describe(group)

    def ungroup(self, group_id: str) -> list[dict[str, Any]]:
        group = self.find(group_id)
        if _local_name(group) != "g":
            raise InvalidDrawingValueError("ungroup requires an SVG group")
        parents = self._parent_map()
        parent = parents.get(group)
        if parent is None:
            raise InvalidDrawingValueError("cannot ungroup SVG root")
        index = list(parent).index(group)
        children = list(group)
        parent.remove(group)
        for offset, child in enumerate(children):
            parent.insert(index + offset, child)
        self._mutated()
        return [self.describe(child) for child in children]

    def reorder(self, object_ids: Iterable[str], direction: str) -> list[dict[str, Any]]:
        elements = [self.find(object_id) for object_id in object_ids]
        if not elements:
            raise InvalidDrawingValueError("object_ids must not be empty")
        parents = self._parent_map()
        parent = parents.get(elements[0])
        if (parent is None) or any(parents.get(element) is not parent for element in elements):
            raise InvalidDrawingValueError("objects must share one parent to reorder")
        direction = str(direction).lower()
        for element in elements:
            parent.remove(element)
        if direction == "top":
            for element in elements:
                parent.append(element)
        elif direction == "bottom":
            for offset, element in enumerate(elements):
                parent.insert(offset, element)
        else:
            raise InvalidDrawingValueError("direction must be 'top' or 'bottom'")
        self._mutated()
        return [self.describe(element) for element in elements]
