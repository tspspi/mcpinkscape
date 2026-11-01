"""Bounded, discriminated operation schemas for native edit batches."""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, FiniteFloat, TypeAdapter, ValidationError, model_validator

from mcpinkscape.errors import InvalidDrawingValueError
from mcpinkscape.svg_document import SVGDocument

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.:-]*$", max_length=128)]
Identifiers = Annotated[list[Identifier], Field(min_length=1, max_length=1000)]
Length = FiniteFloat | str


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Shape(Parameters):
    kind: Literal["rectangle", "ellipse", "circle", "line", "polyline", "polygon", "path"]
    values: dict[str, Any]
    object_id: Identifier
    layer_id: Identifier | None = None
    style: dict[str, Any] | None = None


class Text(Parameters):
    text: Annotated[str, Field(max_length=32768)]
    x: Length
    y: Length
    object_id: Identifier
    layer_id: Identifier | None = None
    style: dict[str, Any] | None = None


class TextSet(Parameters):
    object_id: Identifier
    text: Annotated[str, Field(max_length=32768)]


class Objects(Parameters):
    object_ids: Identifiers


class Style(Objects):
    style: dict[str, Any]


class Move(Objects):
    dx: FiniteFloat
    dy: FiniteFloat


class Align(Objects):
    axis: Literal["x", "y"]
    alignment: Literal["start", "center", "end"]
    reference: Literal["selection", "page", "object"] = "selection"
    reference_id: Identifier | None = None
    bounds: Literal["geometric", "visual"] = "geometric"

    @model_validator(mode="after")
    def check_reference(self):
        if (self.reference == "object") != (self.reference_id is not None):
            raise ValueError("reference_id is required only for reference='object'")
        if self.reference_id in self.object_ids:
            raise ValueError("reference object must not be one of the moved objects")
        return self


class Distribute(Objects):
    axis: Literal["x", "y"]
    spacing: Literal["gaps", "centers"] = "gaps"
    gap: Annotated[FiniteFloat, Field(ge=0)] | None = None
    unit: Literal["px", "mm", "cm", "in", "pt", "pc"] = "px"
    bounds: Literal["geometric", "visual"] = "geometric"

    @model_validator(mode="after")
    def check_spacing(self):
        if self.spacing == "centers" and self.gap is not None:
            raise ValueError("gap is only supported with spacing='gaps'")
        if self.gap is None and self.unit != "px":
            raise ValueError("unit is only meaningful with an explicit gap")
        if len(self.object_ids) < (2 if self.gap is not None else 3):
            raise ValueError("distribution needs three objects, or two with an explicit gap")
        return self


class Rotate(Objects):
    degrees: FiniteFloat


class Scale(Objects):
    scale_x: FiniteFloat
    scale_y: FiniteFloat


class Group(Objects):
    group_id: Identifier


class Ungroup(Parameters):
    group_id: Identifier


class Layer(Parameters):
    layer_id: Identifier
    label: Annotated[str, Field(min_length=1, max_length=1024)]
    parent_layer_id: Identifier | None = None


class Stop(Parameters):
    offset: Annotated[FiniteFloat, Field(ge=0, le=1)]
    colour: str = Field(validation_alias=AliasChoices("colour", "color"))
    opacity: Annotated[FiniteFloat, Field(ge=0, le=1)] = 1.0


class Gradient(Parameters):
    kind: Literal["linear", "radial"]
    stops: Annotated[list[Stop], Field(min_length=2, max_length=32)]
    gradient_id: Identifier
    geometry: dict[str, Length] | None = None


class GradientStops(Parameters):
    gradient_id: Identifier
    stops: Annotated[list[Stop], Field(min_length=2, max_length=32)]


class GradientApply(Objects):
    gradient_id: Identifier
    target: Literal["fill", "stroke"] = "fill"


class Background(Parameters):
    colour: str
    opacity: Annotated[FiniteFloat, Field(ge=0, le=1)] = 1.0


class ShapeOperation(Parameters):
    method: Literal["shape.create"]
    params: Shape


class TextOperation(Parameters):
    method: Literal["text.create"]
    params: Text


class TextSetOperation(Parameters):
    method: Literal["text.set"]
    params: TextSet


class StyleOperation(Parameters):
    method: Literal["object.set_style"]
    params: Style


class MoveOperation(Parameters):
    method: Literal["object.move"]
    params: Move


class AlignOperation(Parameters):
    method: Literal["object.align"]
    params: Align


class DistributeOperation(Parameters):
    method: Literal["object.distribute"]
    params: Distribute


class RotateOperation(Parameters):
    method: Literal["object.rotate"]
    params: Rotate


class ScaleOperation(Parameters):
    method: Literal["object.scale"]
    params: Scale


class ObjectsOperation(Parameters):
    method: Literal["object.delete", "object.raise", "object.lower"]
    params: Objects


class GroupOperation(Parameters):
    method: Literal["object.group"]
    params: Group


class UngroupOperation(Parameters):
    method: Literal["object.ungroup"]
    params: Ungroup


class LayerOperation(Parameters):
    method: Literal["layer.create"]
    params: Layer


class GradientOperation(Parameters):
    method: Literal["gradient.create"]
    params: Gradient


class GradientStopsOperation(Parameters):
    method: Literal["gradient.set_stops"]
    params: GradientStops


class GradientApplyOperation(Parameters):
    method: Literal["gradient.apply"]
    params: GradientApply


class BackgroundOperation(Parameters):
    method: Literal["document.set_background"]
    params: Background


BatchOperation = Annotated[
    ShapeOperation | TextOperation | TextSetOperation | StyleOperation | MoveOperation |
    AlignOperation | DistributeOperation | RotateOperation | ScaleOperation | ObjectsOperation | GroupOperation | UngroupOperation |
    LayerOperation | GradientOperation | GradientStopsOperation | GradientApplyOperation | BackgroundOperation,
    Field(discriminator="method"),
]
BatchOperations = Annotated[list[BatchOperation], Field(min_length=1, max_length=100)]
_ADAPTER = TypeAdapter(BatchOperations)


def validate_operations(operations: Any) -> list[dict[str, Any]]:
    try:
        result = [item.model_dump(exclude_none=True) for item in _ADAPTER.validate_python(operations)]
    except ValidationError as exc:
        raise InvalidDrawingValueError(str(exc)) from exc

    def check_strings(value):
        if isinstance(value, str):
            if any(ord(c) < 32 and c not in "\n\r\t" or 0xD800 <= ord(c) <= 0xDFFF for c in value):
                raise InvalidDrawingValueError("batch strings must be valid XML text without control characters")
        elif isinstance(value, dict):
            for key, item in value.items():
                check_strings(key)
                check_strings(item)
        elif isinstance(value, list):
            for item in value:
                check_strings(item)
    check_strings(result)
    try:
        # Bound memory before constructing any validation drawing.
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > 256 * 1024:
            raise InvalidDrawingValueError("batch exceeds 256 KiB")
    except (ValueError, TypeError) as exc:
        raise InvalidDrawingValueError("batch values must be finite JSON data") from exc
    for operation in result:
        method, params = operation["method"], operation["params"]
        ids = params.get("object_ids", [])
        if len(ids) != len(set(ids)):
            raise InvalidDrawingValueError("batch object_ids must not contain duplicates")
        validator = SVGDocument.create("batch-validation")
        if method == "shape.create":
            validator.create_shape(params["kind"], params["values"], params["object_id"], style=params.get("style"))
        elif method == "text.create":
            validator.create_text(params["text"], params["x"], params["y"], params["object_id"], style=params.get("style"))
        elif method == "object.set_style":
            validator.create_shape("rectangle", {"x": 0, "y": 0, "width": 1, "height": 1}, "validation-object")
            validator.set_style(["validation-object"], params["style"])
        elif method in ("gradient.create", "gradient.set_stops"):
            validator.create_gradient(params.get("kind", "linear"), params["stops"], params.get("geometry"), params["gradient_id"])
        elif method == "document.set_background":
            validator.set_page_background(params["colour"], params["opacity"])
        elif method == "layer.create":
            validator.create_layer(params["label"], params["layer_id"])
        elif method == "object.scale" and (params["scale_x"] == 0 or params["scale_y"] == 0):
            raise InvalidDrawingValueError("batch scale factors must not be zero")
    return result
