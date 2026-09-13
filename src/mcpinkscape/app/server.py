"""FastMCP server exposing the typed mcpInkscape service."""

from __future__ import annotations

import contextvars
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.transforms import Transform

from mcpinkscape.config.api_keys import match_api_key
from mcpinkscape.config.config_manager import generate_and_store_api_key, load_config, parse_arguments, setup_logging
from mcpinkscape.config.schema import APIKeyConfig, AccessConfig, Config
from mcpinkscape.service import InkscapeService, NATIVE_TOOL_METHODS


logger = logging.getLogger(__name__)
ACTIVE_ACCESS: contextvars.ContextVar[AccessConfig | None] = contextvars.ContextVar("mcpinkscape_access", default = None)


class _ToolAccessTransform(Transform):
    """Apply the active access policy to FastMCP tool discovery and calls."""

    def __init__(self, tool_allowed):
        self._tool_allowed = tool_allowed

    async def list_tools(self, tools):
        return [tool for tool in tools if self._tool_allowed(tool.name)]

    async def get_tool(self, name, call_next, *, version = None):
        if not self._tool_allowed(name):
            return None
        return await call_next(name, version = version)


DOCUMENT_WRITE_TOOLS = frozenset({
    "create_document", "open_document", "save_document", "close_document",
    "create_layer", "select_objects", "clear_selection", "create_shape",
    "create_rectangle", "create_ellipse", "create_circle", "create_line",
    "create_polyline", "create_polygon", "create_path", "create_text",
    "set_text", "set_style", "set_fill", "set_stroke", "set_opacity",
    "set_object_attribute", "set_page_background", "move_objects",
    "create_gradient", "set_gradient_stops", "apply_gradient",
    "import_image",
    "rotate_objects", "scale_objects", "set_object_transform",
    "duplicate_objects", "delete_objects", "group_objects", "ungroup_objects",
    "raise_objects", "lower_objects", "export_document",
})
SNAPSHOT_TOOLS = frozenset({"render_snapshot", "list_snapshots", "get_snapshot_base64"})
NATIVE_BRIDGE_TOOLS = frozenset(NATIVE_TOOL_METHODS)


def _tool_allowed_for_access(name: str, access: AccessConfig, native_bridge_available: bool = False) -> bool:
    """Return whether a policy permits advertising and calling *name*."""
    if not name:
        return False
    if name in DOCUMENT_WRITE_TOOLS and not access.allow_document_write:
        return False
    if name in SNAPSHOT_TOOLS and not access.allow_snapshots:
        return False
    if name.startswith("live_") and not access.allow_live_control:
        return False
    if name in NATIVE_BRIDGE_TOOLS and not native_bridge_available:
        return False
    return True


@dataclass
class ServerState:
    config: Config
    stdio_service: InkscapeService
    remote_services: dict[str, InkscapeService] = field(default_factory = dict)

    def close(self) -> None:
        self.stdio_service.close()
        for service in self.remote_services.values():
            service.close()
        self.remote_services.clear()

    def service_for_access(self, access: AccessConfig) -> InkscapeService:
        if access is self.config.stdio:
            return self.stdio_service
        if not isinstance(access, APIKeyConfig):
            return self.stdio_service
        service = self.remote_services.get(access.id)
        if service is None:
            service = InkscapeService(access, self.config.bridge)
            self.remote_services[access.id] = service
        return service


def _access_from_context(ctx: Context | None, state: ServerState) -> AccessConfig:
    request = getattr(getattr(ctx, "request_context", None), "request", None)
    if request is not None:
        access = getattr(getattr(request, "state", object()), "mcpinkscape_access", None)
        if isinstance(access, AccessConfig):
            return access
    return ACTIVE_ACCESS.get() or state.config.stdio


def build_server(config: Config) -> tuple[FastMCP, ServerState]:
    state = ServerState(config, InkscapeService(config.stdio, config.bridge))

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        try:
            yield state
        finally:
            state.close()

    server = FastMCP("mcpinkscape", lifespan = lifespan)
    server.add_transform(
        _ToolAccessTransform(
            lambda name: _tool_allowed_for_access(
                name,
                ACTIVE_ACCESS.get() or state.config.stdio,
                state.service_for_access(ACTIVE_ACCESS.get() or state.config.stdio).native_supports_tool(name),
            )
        )
    )
    _register_tools(server, state)
    return server, state


def _register_tools(server: FastMCP, state: ServerState) -> None:
    def service(ctx: Context | None) -> InkscapeService:
        return state.service_for_access(_access_from_context(ctx, state))

    @server.tool(name = "server_status", description = "Return offline, active-window, native-bridge, and policy status.")
    def server_status(ctx: Context = None) -> dict[str, Any]:
        return service(ctx).server_status()

    @server.tool(name = "list_documents", description = "List documents currently held by this MCP session.")
    def list_documents(ctx: Context = None) -> list[dict[str, Any]]:
        return service(ctx).list_documents()

    @server.tool(name = "create_document", description = "Create a new SVG document below the configured document root.")
    def create_document(name: str, width: float = 800, height: float = 600, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).create_document(name, width, height)

    @server.tool(name = "open_document", description = "Open an SVG below the configured document root.")
    def open_document(path: str, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).open_document(path)

    @server.tool(name = "save_document", description = "Save an opened SVG document, optionally under a new relative SVG path.")
    def save_document(document_id: str, path: str | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).save_document(document_id, path)

    @server.tool(name = "close_document", description = "Forget an opened offline document from this MCP session.")
    def close_document(document_id: str, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).close_document(document_id)

    @server.tool(name = "get_document_info", description = "Inspect one opened document, including revision and selection.")
    def get_document_info(document_id: str, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).get_document_info(document_id)

    @server.tool(name = "list_layers", description = "List top-level SVG groups/layers in a document.")
    def list_layers(document_id: str, ctx: Context = None) -> list[dict[str, Any]]:
        return service(ctx).list_layers(document_id)

    @server.tool(name = "create_layer", description = "Create an Inkscape layer with a stable SVG ID.")
    def create_layer(document_id: str, label: str, layer_id: str | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).create_layer(document_id, label, layer_id, expected_revision)

    @server.tool(name = "list_objects", description = "List direct child objects of an SVG root, group, or layer.")
    def list_objects(document_id: str, parent_id: str | None = None, ctx: Context = None) -> list[dict[str, Any]]:
        return service(ctx).list_objects(document_id, parent_id)

    @server.tool(name = "get_object", description = "Inspect an SVG object by its stable ID.")
    def get_object(document_id: str, object_id: str, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).get_object(document_id, object_id)

    @server.tool(name = "get_object_bounds", description = "Return bounds for a basic SVG object by stable ID.")
    def get_object_bounds(document_id: str, object_id: str, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).get_object_bounds(document_id, object_id)

    @server.tool(name = "get_selection", description = "Return the explicit offline-document selection.")
    def get_selection(document_id: str, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).get_selection(document_id)

    @server.tool(name = "select_objects", description = "Set the explicit offline-document selection by object ID.")
    def select_objects(document_id: str, object_ids: list[str], ctx: Context = None) -> dict[str, Any]:
        return service(ctx).select_objects(document_id, object_ids)

    @server.tool(name = "clear_selection", description = "Clear the explicit offline-document selection.")
    def clear_selection(document_id: str, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).clear_selection(document_id)

    @server.tool(name = "create_shape", description = "Create a typed rectangle, ellipse, circle, line, polyline, polygon, or SVG path.")
    def create_shape(document_id: str, kind: str, values: dict[str, Any], object_id: str | None = None, layer_id: str | None = None, style: dict[str, Any] | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).create_shape(document_id, kind, values, object_id, layer_id, style, expected_revision)

    def _create_basic_shape(kind: str, document_id: str, values: dict[str, Any], object_id: str | None, layer_id: str | None, style: dict[str, Any] | None, expected_revision: int | None, ctx: Context | None) -> dict[str, Any]:
        return service(ctx).create_shape(document_id, kind, values, object_id, layer_id, style, expected_revision)

    @server.tool(name = "create_rectangle", description = "Create a rectangle; values requires x, y, width, and height.")
    def create_rectangle(document_id: str, values: dict[str, Any], object_id: str | None = None, layer_id: str | None = None, style: dict[str, Any] | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return _create_basic_shape("rectangle", document_id, values, object_id, layer_id, style, expected_revision, ctx)

    @server.tool(name = "create_ellipse", description = "Create an ellipse; values requires cx, cy, rx, and ry.")
    def create_ellipse(document_id: str, values: dict[str, Any], object_id: str | None = None, layer_id: str | None = None, style: dict[str, Any] | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return _create_basic_shape("ellipse", document_id, values, object_id, layer_id, style, expected_revision, ctx)

    @server.tool(name = "create_circle", description = "Create a circle; values requires cx, cy, and r.")
    def create_circle(document_id: str, values: dict[str, Any], object_id: str | None = None, layer_id: str | None = None, style: dict[str, Any] | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return _create_basic_shape("circle", document_id, values, object_id, layer_id, style, expected_revision, ctx)

    @server.tool(name = "create_line", description = "Create a line; values requires x1, y1, x2, and y2.")
    def create_line(document_id: str, values: dict[str, Any], object_id: str | None = None, layer_id: str | None = None, style: dict[str, Any] | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return _create_basic_shape("line", document_id, values, object_id, layer_id, style, expected_revision, ctx)

    @server.tool(name = "create_polyline", description = "Create a polyline; values requires points as [[x,y], ...].")
    def create_polyline(document_id: str, values: dict[str, Any], object_id: str | None = None, layer_id: str | None = None, style: dict[str, Any] | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return _create_basic_shape("polyline", document_id, values, object_id, layer_id, style, expected_revision, ctx)

    @server.tool(name = "create_polygon", description = "Create a polygon; values requires points as [[x,y], ...].")
    def create_polygon(document_id: str, values: dict[str, Any], object_id: str | None = None, layer_id: str | None = None, style: dict[str, Any] | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return _create_basic_shape("polygon", document_id, values, object_id, layer_id, style, expected_revision, ctx)

    @server.tool(name = "create_path", description = "Create an SVG path; values requires d.")
    def create_path(document_id: str, values: dict[str, Any], object_id: str | None = None, layer_id: str | None = None, style: dict[str, Any] | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return _create_basic_shape("path", document_id, values, object_id, layer_id, style, expected_revision, ctx)

    @server.tool(name = "create_text", description = "Create a basic SVG text object at x/y with optional typed style.")
    def create_text(document_id: str, text: str, x: float, y: float, object_id: str | None = None, layer_id: str | None = None, style: dict[str, Any] | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).create_text(document_id, text, x, y, object_id, layer_id, style, expected_revision)

    @server.tool(name = "set_text", description = "Replace text content in a basic SVG text object.")
    def set_text(document_id: str, object_id: str, text: str, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).set_text(document_id, object_id, text, expected_revision)

    @server.tool(name = "set_style", description = "Atomically set typed fill, stroke, opacity, and text style fields on objects.")
    def set_style(document_id: str, object_ids: list[str], style: dict[str, Any], expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).set_style(document_id, object_ids, style, expected_revision)

    @server.tool(name = "set_fill", description = "Set object foreground/fill colour and optional opacity.")
    def set_fill(document_id: str, object_ids: list[str], colour: str, opacity: float | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        style: dict[str, Any] = {"fill": colour}
        if opacity is not None:
            style["fill_opacity"] = opacity
        return service(ctx).set_style(document_id, object_ids, style, expected_revision)

    @server.tool(name = "set_stroke", description = "Set object outline/stroke colour, width, and optional opacity.")
    def set_stroke(document_id: str, object_ids: list[str], colour: str, width: float | str | None = None, opacity: float | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        style: dict[str, Any] = {"stroke": colour}
        if width is not None:
            style["stroke_width"] = width
        if opacity is not None:
            style["stroke_opacity"] = opacity
        return service(ctx).set_style(document_id, object_ids, style, expected_revision)

    @server.tool(name = "set_opacity", description = "Set overall object opacity from 0 to 1.")
    def set_opacity(document_id: str, object_ids: list[str], opacity: float, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).set_style(document_id, object_ids, {"opacity": opacity}, expected_revision)

    @server.tool(name = "set_object_attribute", description = "Set an allowlisted non-structural SVG presentation attribute.")
    def set_object_attribute(document_id: str, object_ids: list[str], attribute: str, value: str, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).set_attribute(document_id, object_ids, attribute, value, expected_revision)

    @server.tool(name = "set_page_background", description = "Set Inkscape page background colour and opacity.")
    def set_page_background(document_id: str, colour: str, opacity: float = 1.0, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).set_page_background(document_id, colour, opacity, expected_revision)

    @server.tool(name = "create_gradient", description = "Create a typed linear or radial SVG gradient with ordered colour/opacity stops.")
    def create_gradient(document_id: str, kind: str, stops: list[dict[str, Any]], geometry: dict[str, Any] | None = None, gradient_id: str | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).create_gradient(document_id, kind, stops, geometry, gradient_id, expected_revision)

    @server.tool(name = "set_gradient_stops", description = "Replace a typed SVG gradient's ordered stops atomically.")
    def set_gradient_stops(document_id: str, gradient_id: str, stops: list[dict[str, Any]], expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).set_gradient_stops(document_id, gradient_id, stops, expected_revision)

    @server.tool(name = "apply_gradient", description = "Apply an existing typed gradient to object fills or strokes.")
    def apply_gradient(document_id: str, object_ids: list[str], gradient_id: str, target: str = "fill", expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).apply_gradient(document_id, object_ids, gradient_id, target, expected_revision)

    @server.tool(name = "import_image", description = "Embed a configured-root PNG or JPEG as a typed positioned SVG image.")
    def import_image(document_id: str, path: str, x: float, y: float, width: float | None = None, height: float | None = None, object_id: str | None = None, layer_id: str | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).import_image(document_id, path, x, y, width, height, object_id, layer_id, expected_revision)

    @server.tool(name = "move_objects", description = "Translate objects by dx/dy SVG user units.")
    def move_objects(document_id: str, object_ids: list[str], dx: float, dy: float, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).transform(document_id, "move", object_ids, {"dx": dx, "dy": dy}, expected_revision)

    @server.tool(name = "rotate_objects", description = "Rotate objects by degrees, optionally around cx/cy.")
    def rotate_objects(document_id: str, object_ids: list[str], degrees: float, cx: float | None = None, cy: float | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).transform(document_id, "rotate", object_ids, {"degrees": degrees, "cx": cx, "cy": cy}, expected_revision)

    @server.tool(name = "scale_objects", description = "Scale objects by non-zero scale_x and optional scale_y.")
    def scale_objects(document_id: str, object_ids: list[str], scale_x: float, scale_y: float | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).transform(document_id, "scale", object_ids, {"scale_x": scale_x, "scale_y": scale_y}, expected_revision)

    @server.tool(name = "set_object_transform", description = "Replace an object's SVG transform with a validated transform string.")
    def set_object_transform(document_id: str, object_ids: list[str], transform: str, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).transform(document_id, "set_transform", object_ids, {"transform": transform}, expected_revision)

    @server.tool(name = "duplicate_objects", description = "Duplicate objects with new stable IDs.")
    def duplicate_objects(document_id: str, object_ids: list[str], expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).duplicate_objects(document_id, object_ids, expected_revision)

    @server.tool(name = "delete_objects", description = "Delete objects by ID.")
    def delete_objects(document_id: str, object_ids: list[str], expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).delete_objects(document_id, object_ids, expected_revision)

    @server.tool(name = "group_objects", description = "Group sibling SVG objects under a new group.")
    def group_objects(document_id: str, object_ids: list[str], group_id: str | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).group_objects(document_id, object_ids, group_id, expected_revision)

    @server.tool(name = "ungroup_objects", description = "Ungroup a direct SVG group.")
    def ungroup_objects(document_id: str, group_id: str, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).ungroup_objects(document_id, group_id, expected_revision)

    @server.tool(name = "raise_objects", description = "Raise sibling objects to the top of their parent stacking order.")
    def raise_objects(document_id: str, object_ids: list[str], expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).reorder_objects(document_id, object_ids, "top", expected_revision)

    @server.tool(name = "lower_objects", description = "Lower sibling objects to the bottom of their parent stacking order.")
    def lower_objects(document_id: str, object_ids: list[str], expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).reorder_objects(document_id, object_ids, "bottom", expected_revision)

    @server.tool(name = "render_snapshot", description = "Render an offline document page or drawing through Inkscape and register a PNG snapshot.")
    def render_snapshot(document_id: str, area: str = "page", width_px: int | None = None, height_px: int | None = None, dpi: float | None = None, background: str = "document", ctx: Context = None) -> dict[str, Any]:
        return service(ctx).render_snapshot(document_id, area, width_px, height_px, dpi, background)

    @server.tool(name = "list_snapshots", description = "List server-registered PNG snapshots.")
    def list_snapshots(ctx: Context = None) -> list[dict[str, object]]:
        return service(ctx).list_snapshots()

    @server.tool(name = "export_document", description = "Export an offline document as svg, plain-svg, png, or pdf below the document root.")
    def export_document(document_id: str, path: str, export_type: str = "svg", ctx: Context = None) -> dict[str, Any]:
        return service(ctx).export_document(document_id, path, export_type)

    @server.tool(name = "get_snapshot_base64", description = "Retrieve a registered PNG snapshot as base64.")
    def get_snapshot_base64(snapshot_id: str, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).get_snapshot_base64(snapshot_id)

    @server.tool(name = "live_status", description = "Inspect the version-probed active-window CLI backend.")
    def live_status(ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_status()

    @server.tool(name = "live_select", description = "Select objects in the active Inkscape window by ID.")
    def live_select(object_ids: list[str], expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_select(object_ids, expected_revision)

    @server.tool(name = "live_list_selection", description = "Inspect the focused live Inkscape selection through the native bridge or supported active-window CLI.")
    def live_list_selection(ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_list_selection()

    @server.tool(name = "live_list_objects", description = "List objects in the focused live Inkscape document through the native bridge.")
    def live_list_objects(parent_id: str | None = None, offset: int = 0, limit: int = 100, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_list_objects(parent_id, offset, limit)

    @server.tool(name = "live_poll_changes", description = "Poll native live document and selection revisions for intervening collaborative edits.")
    def live_poll_changes(after_revision: int, after_selection_generation: int, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_poll_changes(after_revision, after_selection_generation)

    @server.tool(name = "live_create_shape", description = "Create a typed shape in the focused live document through the native bridge.")
    def live_create_shape(kind: str, values: dict[str, Any], object_id: str | None = None, layer_id: str | None = None, style: dict[str, Any] | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_create_shape(kind, values, object_id, layer_id, style, expected_revision)

    @server.tool(name = "live_create_text", description = "Create typed text in the focused live document through the native bridge.")
    def live_create_text(text: str, x: float, y: float, object_id: str | None = None, layer_id: str | None = None, style: dict[str, Any] | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_create_text(text, x, y, object_id, layer_id, style, expected_revision)

    @server.tool(name = "live_set_text", description = "Replace a basic text object's content through the native live bridge.")
    def live_set_text(object_id: str, text: str, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_set_text(object_id, text, expected_revision)

    @server.tool(name = "live_set_page_background", description = "Set page background colour and opacity through the native live bridge.")
    def live_set_page_background(colour: str, opacity: float = 1.0, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_set_page_background(colour, opacity, expected_revision)

    @server.tool(name = "live_create_gradient", description = "Create a typed linear or radial SVG gradient through the native live bridge.")
    def live_create_gradient(kind: str, stops: list[dict[str, Any]], geometry: dict[str, Any] | None = None, gradient_id: str | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_create_gradient(kind, stops, geometry, gradient_id, expected_revision)

    @server.tool(name = "live_set_gradient_stops", description = "Replace typed gradient stops through the native live bridge.")
    def live_set_gradient_stops(gradient_id: str, stops: list[dict[str, Any]], expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_set_gradient_stops(gradient_id, stops, expected_revision)

    @server.tool(name = "live_apply_gradient", description = "Apply an existing native gradient to object fills or strokes.")
    def live_apply_gradient(object_ids: list[str], gradient_id: str, target: str = "fill", expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_apply_gradient(object_ids, gradient_id, target, expected_revision)

    @server.tool(name = "live_import_image", description = "Embed a configured-root PNG or JPEG through the native live bridge.")
    def live_import_image(path: str, x: float, y: float, width: float | None = None, height: float | None = None, object_id: str | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_import_image(path, x, y, width, height, object_id, expected_revision)

    @server.tool(name = "live_delete_objects", description = "Atomically delete explicit object IDs through the native live bridge.")
    def live_delete_objects(object_ids: list[str], expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_delete_objects(object_ids, expected_revision)

    @server.tool(name = "live_duplicate_objects", description = "Duplicate explicit live objects with returned stable IDs through the native bridge.")
    def live_duplicate_objects(object_ids: list[str], expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_duplicate_objects(object_ids, expected_revision)

    @server.tool(name = "live_group_objects", description = "Group sibling live objects under an explicit stable group ID through the native bridge.")
    def live_group_objects(object_ids: list[str], group_id: str, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_group_objects(object_ids, group_id, expected_revision)

    @server.tool(name = "live_ungroup_objects", description = "Ungroup a live SVG group through the native bridge.")
    def live_ungroup_objects(group_id: str, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_ungroup_objects(group_id, expected_revision)

    @server.tool(name = "live_raise_objects", description = "Raise sibling live objects to their parent's top stacking position.")
    def live_raise_objects(object_ids: list[str], expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_reorder_objects(object_ids, "top", expected_revision)

    @server.tool(name = "live_lower_objects", description = "Lower sibling live objects to their parent's bottom stacking position.")
    def live_lower_objects(object_ids: list[str], expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_reorder_objects(object_ids, "bottom", expected_revision)

    @server.tool(name = "live_set_style", description = "Set supported fill/stroke/opacity fields on selected live objects.")
    def live_set_style(object_ids: list[str], style: dict[str, Any], expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_set_style(object_ids, style, expected_revision)

    @server.tool(name = "live_move", description = "Translate selected live objects in the active Inkscape window.")
    def live_move(object_ids: list[str], dx: float, dy: float, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_transform("move", object_ids, [dx, dy], expected_revision)

    @server.tool(name = "live_rotate", description = "Rotate selected live objects in the active Inkscape window.")
    def live_rotate(object_ids: list[str], degrees: float, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_transform("rotate", object_ids, [degrees], expected_revision)

    @server.tool(name = "live_scale", description = "Scale selected live objects in the active Inkscape window.")
    def live_scale(object_ids: list[str], scale_x: float, scale_y: float | None = None, expected_revision: int | None = None, ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_transform("scale", object_ids, [scale_x, scale_x if scale_y is None else scale_y], expected_revision)

    @server.tool(name = "live_render_snapshot", description = "Render the active Inkscape page or drawing into a registered PNG snapshot.")
    def live_render_snapshot(area: str = "page", ctx: Context = None) -> dict[str, Any]:
        return service(ctx).live_render_snapshot(area)


def build_remote_app(server: FastMCP, state: ServerState):
    """Build authenticated Streamable HTTP transport using FastMCP's HTTP app."""
    if state.config.remote_server is None:
        raise RuntimeError("remotehttp transport requires a remote_server configuration block")
    if not state.config.api_keys:
        raise RuntimeError("remotehttp transport requires at least one api_keys entry")
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise RuntimeError("remotehttp transport requires FastAPI") from exc

    mcp_app = server.http_app(path = "/", transport = "streamable-http", host_origin_protection = False)
    app = FastAPI(lifespan = mcp_app.lifespan)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.url.path == "/status":
            return await call_next(request)
        token = _request_token(request)
        access = match_api_key(token, state.config.api_keys)
        if access is None:
            return JSONResponse({"detail": "valid API key required"}, status_code = 401)
        request.state.mcpinkscape_access = access
        context_token = ACTIVE_ACCESS.set(access)
        try:
            return await call_next(request)
        finally:
            ACTIVE_ACCESS.reset(context_token)

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return state.stdio_service.server_status()

    app.mount("/mcp", mcp_app)
    return app


def _request_token(request: Any) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-api-key", "") or request.query_params.get("api_key", "") or request.query_params.get("mcp", "")


def run_mcp_server() -> None:
    args = parse_arguments()
    if args.genkey:
        print(generate_and_store_api_key(args.config, args.genkey))
        return
    config = load_config(args.config)
    setup_logging(config, args.log_level, args.logfile)
    transport = args.transport or config.mode
    server, state = build_server(config)
    if transport == "stdio":
        logger.info("starting mcpInkscape stdio server")
        server.run("stdio")
        return
    app = build_remote_app(server, state)
    endpoint = config.remote_server.resolved_endpoint() if config.remote_server else {}
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("remotehttp transport requires uvicorn") from exc
    if endpoint.get("uds"):
        uds = Path(str(endpoint["uds"])).expanduser()
        uds.parent.mkdir(parents = True, exist_ok = True)
        if uds.exists():
            uds.unlink()
        uvicorn.run(app, uds = str(uds), log_config = None)
    elif endpoint.get("port") is not None:
        uvicorn.run(app, host = str(endpoint.get("host") or "127.0.0.1"), port = int(endpoint["port"]), log_config = None)
    else:
        raise RuntimeError("remote_server must provide transport.uds or transport.host/port")


if __name__ == "__main__":
    run_mcp_server()
