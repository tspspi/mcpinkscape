"""High-level typed operations shared by MCP transports."""

from __future__ import annotations

import base64
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from mcpinkscape.config.schema import AccessConfig, BridgeConfig
from mcpinkscape.errors import (
    DocumentNotFoundError,
    InvalidDrawingValueError,
    LiveActionUnsupportedError,
    LiveBackendUnavailableError,
    PolicyDeniedError,
)
from mcpinkscape.native_bridge import NativeBridgeClient
from mcpinkscape.renderer import InkscapeCLI
from mcpinkscape.storage import DocumentRoot, SnapshotRegistry
from mcpinkscape.svg_document import SVGDocument


# These methods have no active-window CLI fallback.  Keep their MCP tool
# names next to the service routes so dynamic tool discovery cannot drift from
# the request implementation.
NATIVE_TOOL_METHODS: dict[str, str] = {
    "live_poll_changes": "document.poll_changes",
    "live_list_objects": "object.list",
    "live_create_shape": "shape.create",
    "live_create_text": "text.create",
    "live_set_text": "text.set",
    "live_set_page_background": "document.set_background",
    "live_create_gradient": "gradient.create",
    "live_set_gradient_stops": "gradient.set_stops",
    "live_apply_gradient": "gradient.apply",
    "live_import_image": "image.import",
    "live_delete_objects": "object.delete",
    "live_duplicate_objects": "object.duplicate",
    "live_group_objects": "object.group",
    "live_ungroup_objects": "object.ungroup",
    "live_raise_objects": "object.raise",
    "live_lower_objects": "object.lower",
}


class InkscapeService:
    """Stateful document service with offline and active-window backends."""

    def __init__(self, access: AccessConfig, bridge: BridgeConfig):
        self.access = access
        self.bridge = bridge
        self.document_root = DocumentRoot(access.document_root)
        self.snapshots = SnapshotRegistry(self.document_root.root)
        self.documents: dict[str, SVGDocument] = {}
        self.cli = InkscapeCLI(bridge.executable, bridge.timeout_seconds)
        self.native_bridge = NativeBridgeClient(
            bridge.uds,
            bridge.timeout_seconds,
            bridge.tcp_host,
            bridge.tcp_port,
        )
        self._native_status: dict[str, Any] | None = None
        self._native_error: str | None = None
        self._probe_native_bridge()
        if self.bridge.required and self._native_status is None:
            raise LiveBackendUnavailableError(self._native_error or "native bridge is required but unavailable")

    def close(self) -> None:
        self.snapshots.cleanup()
        self.native_bridge.close()

    def _probe_native_bridge(self) -> None:
        if self.bridge.mode not in ("auto", "native_uds"):
            self._native_status = None
            self._native_error = "native bridge disabled by bridge.mode"
            return
        try:
            self._native_status = self.native_bridge.status()
            # Start the extension's document observer as soon as the MCP
            # service attaches.  This establishes the revision baseline before
            # a human or another agent can make an intervening edit.
            if "document.revision" in self._native_status.get("methods", []):
                self.native_bridge.request("document.revision", {})
            self._native_error = None
        except LiveBackendUnavailableError as exc:
            self._native_status = None
            self._native_error = str(exc)

    def server_status(self) -> dict[str, Any]:
        active_available = self._active_window_enabled() and self.cli.capabilities.available
        return {
            "package": "mcpinkscape",
            "offline_backend": True,
            "document_root": str(self.document_root.root),
            "native_bridge": {
                "configured_mode": self.bridge.mode,
                "available": self._native_status is not None,
                "status": self._native_status,
                "reason": self._native_error,
            },
            "active_window": {
                "available": active_available,
                "executable": self.cli.capabilities.executable,
                "version": self.cli.capabilities.version,
                "action_count": len(self.cli.capabilities.actions),
                "error": self.cli.capabilities.error,
            },
            "policy": self._policy_payload(),
        }

    def _policy_payload(self) -> dict[str, bool]:
        return {
            "allow_document_write": self.access.allow_document_write,
            "allow_snapshots": self.access.allow_snapshots,
            "allow_live_control": self.access.allow_live_control,
            "enable_autonomous_execution": self.access.enable_autonomous_execution,
        }

    def _require_write(self) -> None:
        if not self.access.allow_document_write:
            raise PolicyDeniedError("configured policy forbids document mutation")

    def _require_snapshots(self) -> None:
        if not self.access.allow_snapshots:
            raise PolicyDeniedError("configured policy forbids snapshots")

    def _require_live(self) -> None:
        if not self.access.allow_live_control:
            raise PolicyDeniedError("configured policy forbids live control")
        if not self._active_window_enabled():
            raise LiveBackendUnavailableError("active-window backend is disabled by bridge.mode")

    def _require_native(self) -> None:
        if not self.access.allow_live_control:
            raise PolicyDeniedError("configured policy forbids live control")
        if self._native_status is None:
            raise LiveBackendUnavailableError(self._native_error or "native bridge is unavailable")

    def _native_request(
        self,
        method: str,
        params: dict[str, Any],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self._require_native()
        if not self._native_supports(method):
            raise LiveBackendUnavailableError(f"native bridge does not advertise method '{method}'")
        return self.native_bridge.request(method, params, expected_revision)

    def _native_supports(self, method: str) -> bool:
        if self._native_status is None:
            return False
        methods = self._native_status.get("methods")
        return isinstance(methods, list) and method in methods

    def native_supports_tool(self, tool_name: str) -> bool:
        """Return availability of a native-only MCP tool for discovery filtering."""
        method = NATIVE_TOOL_METHODS.get(tool_name)
        return bool(method and self._native_supports(method))

    def _active_window_enabled(self) -> bool:
        return self.bridge.mode in ("auto", "active_window")

    @staticmethod
    def _reject_cli_revision(expected_revision: int | None) -> None:
        """Do not pretend the action-only backend can provide conflict safety."""
        if expected_revision is not None:
            raise LiveActionUnsupportedError(
                "active-window CLI cannot enforce expected_revision; use the native bridge"
            )

    def _document(self, document_id: str) -> SVGDocument:
        document = self.documents.get(document_id)
        if document is None:
            raise DocumentNotFoundError(f"document '{document_id}' was not opened")
        return document

    def create_document(self, name: str, width: Any = 800, height: Any = 600) -> dict[str, Any]:
        self._require_write()
        safe_name = str(name).strip()
        if (not safe_name) or Path(safe_name).name != safe_name:
            raise InvalidDrawingValueError("name must be a simple document name")
        if not safe_name.endswith(".svg"):
            safe_name += ".svg"
        path = self.document_root.resolve(safe_name)
        if path.exists():
            raise InvalidDrawingValueError(f"document '{safe_name}' already exists")
        document_id = uuid.uuid4().hex
        document = SVGDocument.create(document_id, width, height, safe_name)
        document.save(path)
        self.documents[document_id] = document
        return document.document_info()

    def open_document(self, path: str) -> dict[str, Any]:
        source_path = self.document_root.resolve(path)
        if not source_path.is_file():
            raise DocumentNotFoundError(f"document '{path}' was not found")
        document_id = uuid.uuid4().hex
        document = SVGDocument.load(document_id, source_path)
        self.documents[document_id] = document
        return document.document_info()

    def list_documents(self) -> list[dict[str, Any]]:
        return [document.document_info() for document in self.documents.values()]

    def close_document(self, document_id: str) -> dict[str, Any]:
        document = self._document(document_id)
        del self.documents[document.document_id]
        return {"document_id": document_id, "closed": True}

    def save_document(self, document_id: str, path: str | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        destination = self.document_root.resolve(path) if path else None
        saved = document.save(destination)
        return {**document.document_info(), "saved_path": str(saved.relative_to(self.document_root.root))}

    def get_document_info(self, document_id: str) -> dict[str, Any]:
        return self._document(document_id).document_info()

    def create_layer(self, document_id: str, label: str, layer_id: str | None = None, expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        result = document.create_layer(label, layer_id)
        return self._mutation_payload(document, [result["id"]], result)

    def list_layers(self, document_id: str) -> list[dict[str, Any]]:
        document = self._document(document_id)
        return [item for item in document.list_objects() if item["type"] == "g"]

    def create_shape(self, document_id: str, kind: str, values: dict[str, Any], object_id: str | None = None, layer_id: str | None = None, style: dict[str, Any] | None = None, expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        result = document.create_shape(kind, values, object_id, layer_id, style)
        return self._mutation_payload(document, [result["id"]], result)

    def create_text(self, document_id: str, text: str, x: Any, y: Any, object_id: str | None = None, layer_id: str | None = None, style: dict[str, Any] | None = None, expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        result = document.create_text(text, x, y, object_id, layer_id, style)
        return self._mutation_payload(document, [result["id"]], result)

    def list_objects(self, document_id: str, parent_id: str | None = None) -> list[dict[str, Any]]:
        return self._document(document_id).list_objects(parent_id)

    def get_object(self, document_id: str, object_id: str) -> dict[str, Any]:
        document = self._document(document_id)
        return document.describe(document.find(object_id))

    def get_object_bounds(self, document_id: str, object_id: str) -> dict[str, Any]:
        document = self._document(document_id)
        element = document.find(object_id)
        return {"document_id": document_id, "object_id": object_id, "bounds": document.bounds(element), "revision": document.revision}

    def get_selection(self, document_id: str) -> dict[str, Any]:
        document = self._document(document_id)
        return {"selection": list(document.selection), "revision": document.revision}

    def select_objects(self, document_id: str, object_ids: list[str]) -> dict[str, Any]:
        self._require_write()
        return self._document(document_id).select(object_ids)

    def clear_selection(self, document_id: str) -> dict[str, Any]:
        self._require_write()
        return self._document(document_id).select([])

    def set_text(self, document_id: str, object_id: str, text: str, expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        result = document.set_text(object_id, text)
        return self._mutation_payload(document, [object_id], result)

    def set_style(self, document_id: str, object_ids: list[str], style: dict[str, Any], expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        result = document.set_style(object_ids, style)
        return self._mutation_payload(document, object_ids, result)

    def set_attribute(self, document_id: str, object_ids: list[str], attribute: str, value: Any, expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        result = document.set_attribute(object_ids, attribute, value)
        return self._mutation_payload(document, object_ids, result)

    def set_page_background(self, document_id: str, colour: str, opacity: Any = 1.0, expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        result = document.set_page_background(colour, opacity)
        return self._mutation_payload(document, [], result)

    def create_gradient(
        self, document_id: str, kind: str, stops: list[dict[str, Any]], geometry: dict[str, Any] | None = None,
        gradient_id: str | None = None, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        result = document.create_gradient(kind, stops, geometry, gradient_id)
        return self._mutation_payload(document, [result["id"]], result)

    def set_gradient_stops(self, document_id: str, gradient_id: str, stops: list[dict[str, Any]], expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        result = document.set_gradient_stops(gradient_id, stops)
        return self._mutation_payload(document, [gradient_id], result)

    def apply_gradient(self, document_id: str, object_ids: list[str], gradient_id: str, target: str = "fill", expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        result = document.apply_gradient(object_ids, gradient_id, target)
        return self._mutation_payload(document, object_ids, result)

    def import_image(
        self, document_id: str, path: str, x: Any, y: Any, width: Any | None = None, height: Any | None = None,
        object_id: str | None = None, layer_id: str | None = None, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Embed a PNG/JPEG below the configured root as a portable SVG image."""
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        source = self.document_root.resolve(path, suffixes = (".png", ".jpg", ".jpeg"))
        try:
            payload = source.read_bytes()
        except OSError as exc:
            raise InvalidDrawingValueError(f"cannot read configured image asset: {exc}") from exc
        mime_type, intrinsic_width, intrinsic_height = self._raster_metadata(payload)
        if width is None and height is None:
            width, height = intrinsic_width, intrinsic_height
        elif width is None:
            height_value = float(height)
            width = height_value * intrinsic_width / intrinsic_height
        elif height is None:
            width_value = float(width)
            height = width_value * intrinsic_height / intrinsic_width
        data_uri = f"data:{mime_type};base64," + base64.b64encode(payload).decode("ascii")
        result = document.create_embedded_image(data_uri, mime_type, x, y, width, height, object_id, layer_id)
        return self._mutation_payload(document, [result["id"]], {
            **result, "source_path": str(source.relative_to(self.document_root.root)),
            "mime_type": mime_type, "intrinsic_width_px": intrinsic_width, "intrinsic_height_px": intrinsic_height,
            "embedded": True,
        })

    @staticmethod
    def _raster_metadata(payload: bytes) -> tuple[str, int, int]:
        if len(payload) > 24 and payload.startswith(b"\x89PNG\r\n\x1a\n"):
            width = int.from_bytes(payload[16:20], "big")
            height = int.from_bytes(payload[20:24], "big")
            if width > 0 and height > 0:
                return "image/png", width, height
        if len(payload) >= 4 and payload[:2] == b"\xff\xd8":
            position = 2
            while position + 9 <= len(payload):
                if payload[position] != 0xff:
                    position += 1
                    continue
                marker = payload[position + 1]
                position += 2
                while marker == 0xff and position < len(payload):
                    marker = payload[position]
                    position += 1
                if marker in {0xd8, 0xd9} or 0xd0 <= marker <= 0xd7:
                    continue
                if position + 2 > len(payload):
                    break
                length = int.from_bytes(payload[position:position + 2], "big")
                if length < 2 or position + length > len(payload):
                    break
                if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf} and length >= 7:
                    height = int.from_bytes(payload[position + 3:position + 5], "big")
                    width = int.from_bytes(payload[position + 5:position + 7], "big")
                    if width > 0 and height > 0:
                        return "image/jpeg", width, height
                    break
                position += length
        raise InvalidDrawingValueError("asset must be a structurally valid PNG or JPEG with positive dimensions")

    def transform(self, document_id: str, operation: str, object_ids: list[str], values: dict[str, Any], expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        if operation == "move":
            result = document.move(object_ids, values.get("dx"), values.get("dy"))
        elif operation == "rotate":
            result = document.rotate(object_ids, values.get("degrees"), values.get("cx"), values.get("cy"))
        elif operation == "scale":
            result = document.scale(object_ids, values.get("scale_x"), values.get("scale_y"))
        elif operation == "set_transform":
            result = document.set_transform(object_ids, str(values.get("transform", "")))
        else:
            raise InvalidDrawingValueError(f"unsupported transform operation '{operation}'")
        return self._mutation_payload(document, object_ids, result)

    def delete_objects(self, document_id: str, object_ids: list[str], expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        deleted = document.delete(object_ids)
        return self._mutation_payload(document, deleted, {"deleted_ids": deleted})

    def duplicate_objects(self, document_id: str, object_ids: list[str], expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        result = document.duplicate(object_ids)
        return self._mutation_payload(document, [item["id"] for item in result], result)

    def group_objects(self, document_id: str, object_ids: list[str], group_id: str | None = None, expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        result = document.group(object_ids, group_id)
        return self._mutation_payload(document, [result["id"]], result)

    def ungroup_objects(self, document_id: str, group_id: str, expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        result = document.ungroup(group_id)
        return self._mutation_payload(document, [item["id"] for item in result], result)

    def reorder_objects(self, document_id: str, object_ids: list[str], direction: str, expected_revision: int | None = None) -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        document.require_revision(expected_revision)
        result = document.reorder(object_ids, direction)
        return self._mutation_payload(document, object_ids, result)

    def render_snapshot(self, document_id: str, area: str = "page", width_px: int | None = None, height_px: int | None = None, dpi: float | None = None, background: str = "document") -> dict[str, Any]:
        self._require_snapshots()
        document = self._document(document_id)
        temporary_svg = self._temporary_file(".svg")
        temporary_png = self._temporary_file(".png")
        try:
            temporary_svg.write_bytes(document.serialize())
            self.cli.render_svg(temporary_svg, temporary_png, area, width_px, height_px, dpi, background)
            snapshot = self.snapshots.register_png(temporary_png, width_px, height_px)
            return {**self.snapshots.describe(snapshot), "document_id": document_id, "revision": document.revision}
        finally:
            temporary_svg.unlink(missing_ok = True)
            temporary_png.unlink(missing_ok = True)

    def get_snapshot_base64(self, snapshot_id: str) -> dict[str, Any]:
        self._require_snapshots()
        return self.snapshots.base64_payload(snapshot_id)

    def list_snapshots(self) -> list[dict[str, object]]:
        self._require_snapshots()
        return self.snapshots.list()

    def export_document(self, document_id: str, path: str, export_type: str = "svg") -> dict[str, Any]:
        self._require_write()
        document = self._document(document_id)
        export_type = str(export_type).lower()
        suffixes = {"svg": ".svg", "plain-svg": ".svg", "png": ".png", "pdf": ".pdf"}
        if export_type not in suffixes:
            raise InvalidDrawingValueError("export_type must be svg, plain-svg, png, or pdf")
        destination = self.document_root.resolve(path, suffixes = (suffixes[export_type],))
        destination.parent.mkdir(parents = True, exist_ok = True)
        if export_type == "svg":
            document.save(destination)
        else:
            temporary_svg = self._temporary_file(".svg")
            try:
                temporary_svg.write_bytes(document.serialize())
                if export_type == "png":
                    self.cli.render_svg(temporary_svg, destination)
                else:
                    arguments = [str(temporary_svg), f"--export-filename={destination}"]
                    if export_type == "plain-svg":
                        arguments.append("--export-plain-svg")
                    else:
                        arguments.append("--export-type=pdf")
                    result = self.cli._run(arguments)
                    if result.returncode != 0:
                        raise InvalidDrawingValueError((result.stderr or result.stdout or "Inkscape export failed").strip())
                    if (not destination.is_file()) or destination.stat().st_size == 0:
                        raise InvalidDrawingValueError("Inkscape export returned success without an output file")
            finally:
                temporary_svg.unlink(missing_ok = True)
        return {"document_id": document_id, "path": str(destination.relative_to(self.document_root.root)), "mime_type": {"svg": "image/svg+xml", "plain-svg": "image/svg+xml", "png": "image/png", "pdf": "application/pdf"}[export_type], "revision": document.revision}

    def live_status(self) -> dict[str, Any]:
        if not self.access.allow_live_control:
            raise PolicyDeniedError("configured policy forbids live control")
        if self._native_status is not None:
            return {"backend": "native_uds", **self.native_bridge.request("bridge.status", {})}
        self._require_live()
        return {
            "backend": "active_window",
            "available": self.cli.capabilities.available,
            "version": self.cli.capabilities.version,
            "actions": sorted(self.cli.capabilities.actions),
        }

    def live_select(self, object_ids: list[str], expected_revision: int | None = None) -> dict[str, Any]:
        if self._native_supports("selection.set"):
            return {"backend": "native_uds", **self._native_request("selection.set", {"object_ids": object_ids}, expected_revision)}
        self._require_live()
        self._reject_cli_revision(expected_revision)
        return {"backend": "active_window", **self.cli.active_select(object_ids)}

    def live_list_selection(self) -> dict[str, Any]:
        if self._native_supports("selection.get"):
            return {"backend": "native_uds", **self._native_request("selection.get", {})}
        self._require_live()
        return {"backend": "active_window", **self.cli.active_list_selection()}

    def live_list_objects(self, parent_id: str | None = None, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        if (offset < 0) or (limit < 1) or (limit > 1000):
            raise InvalidDrawingValueError("live list offset must be non-negative and limit must be 1 through 1000")
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        if parent_id is not None:
            params["parent_id"] = parent_id
        return {"backend": "native_uds", **self._native_request("object.list", params)}

    def live_poll_changes(self, after_revision: int, after_selection_generation: int) -> dict[str, Any]:
        if not isinstance(after_revision, int) or after_revision < 0:
            raise InvalidDrawingValueError("after_revision must be a non-negative integer")
        if not isinstance(after_selection_generation, int) or after_selection_generation < 0:
            raise InvalidDrawingValueError("after_selection_generation must be a non-negative integer")
        return {"backend": "native_uds", **self._native_request("document.poll_changes", {
            "after_revision": after_revision,
            "after_selection_generation": after_selection_generation,
        })}

    def live_create_shape(
        self,
        kind: str,
        values: dict[str, Any],
        object_id: str | None = None,
        layer_id: str | None = None,
        style: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        validator = SVGDocument.create("mcpinkscape-live-shape-validation")
        validated = validator.create_shape(kind, values, object_id, style = style)
        params: dict[str, Any] = {"kind": kind, "values": values, "object_id": validated["id"]}
        if layer_id is not None:
            params["layer_id"] = layer_id
        if style:
            params["style"] = style
        return {"backend": "native_uds", **self._native_request("shape.create", params, expected_revision)}

    def live_create_text(
        self,
        text: str,
        x: Any,
        y: Any,
        object_id: str | None = None,
        layer_id: str | None = None,
        style: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        validator = SVGDocument.create("mcpinkscape-live-text-validation")
        validated = validator.create_text(text, x, y, object_id, style = style)
        params: dict[str, Any] = {"text": text, "x": x, "y": y, "object_id": validated["id"]}
        if layer_id is not None:
            params["layer_id"] = layer_id
        if style:
            params["style"] = style
        return {"backend": "native_uds", **self._native_request("text.create", params, expected_revision)}

    def live_set_text(self, object_id: str, text: str, expected_revision: int | None = None) -> dict[str, Any]:
        validator = SVGDocument.create("mcpinkscape-live-text-validation")
        validator.create_text("placeholder", 0, 0, object_id)
        validator.set_text(object_id, text)
        return {"backend": "native_uds", **self._native_request("text.set", {"object_id": object_id, "text": text}, expected_revision)}

    def live_set_page_background(self, colour: str, opacity: Any = 1.0, expected_revision: int | None = None) -> dict[str, Any]:
        validator = SVGDocument.create("mcpinkscape-live-background-validation")
        validator.set_page_background(colour, opacity)
        return {"backend": "native_uds", **self._native_request("document.set_background", {"color": colour, "opacity": float(opacity)}, expected_revision)}

    def live_create_gradient(self, kind: str, stops: list[dict[str, Any]], geometry: dict[str, Any] | None = None, gradient_id: str | None = None, expected_revision: int | None = None) -> dict[str, Any]:
        validator = SVGDocument.create("mcpinkscape-live-gradient-validation")
        validated = validator.create_gradient(kind, stops, geometry, gradient_id)
        return {"backend": "native_uds", **self._native_request("gradient.create", {"kind": kind, "stops": stops, "geometry": geometry or {}, "gradient_id": validated["id"]}, expected_revision)}

    def live_set_gradient_stops(self, gradient_id: str, stops: list[dict[str, Any]], expected_revision: int | None = None) -> dict[str, Any]:
        validator = SVGDocument.create("mcpinkscape-live-gradient-validation")
        validator.create_gradient("linear", [{"offset": 0, "colour": "#000"}, {"offset": 1, "colour": "#fff"}], gradient_id = gradient_id)
        validator.set_gradient_stops(gradient_id, stops)
        return {"backend": "native_uds", **self._native_request("gradient.set_stops", {"gradient_id": gradient_id, "stops": stops}, expected_revision)}

    def live_apply_gradient(self, object_ids: list[str], gradient_id: str, target: str = "fill", expected_revision: int | None = None) -> dict[str, Any]:
        if not object_ids:
            raise InvalidDrawingValueError("object_ids must not be empty")
        if target not in {"fill", "stroke"}:
            raise InvalidDrawingValueError("gradient target must be fill or stroke")
        return {"backend": "native_uds", **self._native_request("gradient.apply", {"object_ids": object_ids, "gradient_id": gradient_id, "target": target}, expected_revision)}

    def live_import_image(
        self, path: str, x: Any, y: Any, width: Any | None = None, height: Any | None = None,
        object_id: str | None = None, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Stage one confined raster file for immediate native-bridge embedding."""
        source = self.document_root.resolve(path, suffixes = (".png", ".jpg", ".jpeg"))
        try:
            payload = source.read_bytes()
        except OSError as exc:
            raise InvalidDrawingValueError(f"cannot read configured image asset: {exc}") from exc
        mime_type, intrinsic_width, intrinsic_height = self._raster_metadata(payload)
        if width is None and height is None:
            width, height = intrinsic_width, intrinsic_height
        elif width is None:
            width = float(height) * intrinsic_width / intrinsic_height
        elif height is None:
            height = float(width) * intrinsic_height / intrinsic_width
        validator = SVGDocument.create("mcpinkscape-live-image-validation")
        validated = validator.create_embedded_image(
            f"data:{mime_type};base64,AA==", mime_type, x, y, width, height, object_id,
        )
        if os.name == "nt":
            raise LiveBackendUnavailableError("Windows native image staging is deferred with Windows bridge testing")
        imports = Path(self.bridge.uds).expanduser().parent / "imports"
        imports.mkdir(parents = True, exist_ok = True)
        try:
            imports.chmod(0o700)
        except OSError:
            pass
        staging = imports / f"{uuid.uuid4().hex}{source.suffix.lower()}"
        try:
            shutil.copyfile(source, staging)
            staging.chmod(0o600)
            result = self._native_request("image.import", {
                "staging_path": str(staging), "mime_type": mime_type, "object_id": validated["id"],
                "x": x, "y": y, "width": width, "height": height,
            }, expected_revision)
        finally:
            staging.unlink(missing_ok = True)
        return {"backend": "native_uds", **result, "source_path": str(source.relative_to(self.document_root.root)),
                "mime_type": mime_type, "intrinsic_width_px": intrinsic_width, "intrinsic_height_px": intrinsic_height,
                "embedded": True}

    def live_delete_objects(self, object_ids: list[str], expected_revision: int | None = None) -> dict[str, Any]:
        if not object_ids:
            raise InvalidDrawingValueError("object_ids must not be empty")
        return {"backend": "native_uds", **self._native_request("object.delete", {"object_ids": object_ids}, expected_revision)}

    def live_duplicate_objects(self, object_ids: list[str], expected_revision: int | None = None) -> dict[str, Any]:
        if not object_ids:
            raise InvalidDrawingValueError("object_ids must not be empty")
        return {"backend": "native_uds", **self._native_request("object.duplicate", {"object_ids": object_ids}, expected_revision)}

    def live_group_objects(self, object_ids: list[str], group_id: str, expected_revision: int | None = None) -> dict[str, Any]:
        validator = SVGDocument.create("mcpinkscape-live-group-validation")
        for index, object_id in enumerate(object_ids):
            validator.create_shape("rectangle", {"x": index, "y": 0, "width": 1, "height": 1}, object_id)
        validator.group(object_ids, group_id)
        return {"backend": "native_uds", **self._native_request("object.group", {"object_ids": object_ids, "group_id": group_id}, expected_revision)}

    def live_ungroup_objects(self, group_id: str, expected_revision: int | None = None) -> dict[str, Any]:
        if not group_id:
            raise InvalidDrawingValueError("group_id must not be empty")
        return {"backend": "native_uds", **self._native_request("object.ungroup", {"group_id": group_id}, expected_revision)}

    def live_reorder_objects(self, object_ids: list[str], direction: str, expected_revision: int | None = None) -> dict[str, Any]:
        method = {"top": "object.raise", "bottom": "object.lower"}.get(direction)
        if method is None or not object_ids:
            raise InvalidDrawingValueError("live reorder requires IDs and direction top or bottom")
        return {"backend": "native_uds", **self._native_request(method, {"object_ids": object_ids}, expected_revision)}

    def live_set_style(self, object_ids: list[str], style: dict[str, Any], expected_revision: int | None = None) -> dict[str, Any]:
        # Reuse the offline typed style validation before serialising action
        # arguments for the active-window backend.
        validator = SVGDocument.create("mcpinkscape-live-style-validation")
        target_id = "mcpinkscape-live-style-target"
        validator.create_shape("rectangle", {"x": 0, "y": 0, "width": 1, "height": 1}, target_id)
        validator.set_style([target_id], style)
        if self._native_supports("object.set_style"):
            return {"backend": "native_uds", **self._native_request("object.set_style", {"object_ids": object_ids, "style": style}, expected_revision)}
        self._require_live()
        self._reject_cli_revision(expected_revision)
        return {"backend": "active_window", **self.cli.active_set_style(object_ids, style)}

    def live_transform(
        self,
        operation: str,
        object_ids: list[str],
        values: list[float],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        method = {"move": "object.move", "rotate": "object.rotate", "scale": "object.scale"}.get(operation)
        if method is None:
            raise InvalidDrawingValueError(f"unsupported live transform '{operation}'")
        if self._native_supports(method):
            names = {"move": ("dx", "dy"), "rotate": ("degrees",), "scale": ("scale_x", "scale_y")}[operation]
            params: dict[str, Any] = {"object_ids": object_ids}
            for index, name in enumerate(names):
                if index < len(values):
                    params[name] = values[index]
            return {"backend": "native_uds", **self._native_request(method, params, expected_revision)}
        self._require_live()
        self._reject_cli_revision(expected_revision)
        return {"backend": "active_window", **self.cli.active_transform(object_ids, operation, values)}

    def live_render_snapshot(self, area: str = "page") -> dict[str, Any]:
        self._require_snapshots()
        if self._native_supports("snapshot.render"):
            result = self._native_request("snapshot.render", {"area": area})
            snapshot_id = str(result.get("snapshot_id", "")).strip()
            staging_path = result.get("staging_path")
            if (not snapshot_id) or (not isinstance(staging_path, str)):
                raise LiveBackendUnavailableError("native bridge returned an invalid snapshot descriptor")
            source = self._validate_native_snapshot_path(staging_path)
            try:
                snapshot = self.snapshots.register_png(
                    source,
                    result.get("width_px"),
                    result.get("height_px"),
                )
            finally:
                # The bridge owns the staging artifact even when importing it
                # fails, so release it in a separate best-effort request.
                try:
                    self._native_request("snapshot.release", {"snapshot_id": snapshot_id})
                except LiveBackendUnavailableError:
                    pass
            return {**self.snapshots.describe(snapshot), "backend": "native_uds", "revision": result.get("revision")}
        self._require_live()
        temporary_png = self._temporary_file(".png")
        try:
            self.cli.active_render(temporary_png, area)
            snapshot = self.snapshots.register_png(temporary_png)
            return {**self.snapshots.describe(snapshot), "backend": "active_window"}
        finally:
            temporary_png.unlink(missing_ok = True)

    def _temporary_file(self, suffix: str) -> Path:
        descriptor, name = tempfile.mkstemp(prefix = "mcpinkscape-", suffix = suffix)
        os.close(descriptor)
        path = Path(name)
        path.unlink(missing_ok = True)
        return path

    def _validate_native_snapshot_path(self, staging_path: str) -> Path:
        """Accept only an owned bridge PNG staged below the configured Unix state directory."""
        source = Path(staging_path).expanduser().resolve()
        if os.name == "nt":
            # The Windows bridge is intentionally loopback-only. Its staging
            # path contract will be validated in the Windows integration pass.
            raise LiveBackendUnavailableError("Windows native bridge snapshot import is not implemented yet")
        state_directory = Path(self.bridge.uds).expanduser().parent.resolve()
        try:
            source.relative_to(state_directory)
        except ValueError as exc:
            raise LiveBackendUnavailableError("native bridge snapshot path escapes its state directory") from exc
        try:
            information = source.stat()
        except OSError as exc:
            raise LiveBackendUnavailableError(f"native bridge snapshot is unavailable: {exc}") from exc
        if (not source.is_file()) or information.st_uid != os.getuid():
            raise LiveBackendUnavailableError("native bridge snapshot is not an owned regular file")
        if information.st_mode & 0o077:
            raise LiveBackendUnavailableError("native bridge snapshot permissions must not allow group or other access")
        return source

    @staticmethod
    def _mutation_payload(document: SVGDocument, changed_ids: list[str], result: Any) -> dict[str, Any]:
        return {
            "document_id": document.document_id,
            "revision": document.revision,
            "changed": True,
            "changed_ids": changed_ids,
            "result": result,
        }
