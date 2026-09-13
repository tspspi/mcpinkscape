"""Version-probed Inkscape CLI rendering and active-window action adapter."""

from __future__ import annotations

import os
import math
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mcpinkscape.errors import LiveActionUnsupportedError, LiveBackendUnavailableError, RendererError


Runner = Callable[..., subprocess.CompletedProcess[str]]
_SVG_ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")
_SELECT_LIST_LINE_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_.:-]*)\s+cloned:")


@dataclass
class InkscapeCapabilities:
    executable: str
    version: str | None
    actions: set[str]
    available: bool
    error: str | None = None

    def supports(self, action: str) -> bool:
        return action in self.actions


class InkscapeCLI:
    """Small wrapper around the configured Inkscape executable.

    Arguments are always passed as an argv list. Callers only provide typed
    values which this class converts to known action strings.
    """

    def __init__(self, executable: str = "inkscape", timeout_seconds: float = 30.0, runner: Runner | None = None):
        self.executable = executable
        self.timeout_seconds = float(timeout_seconds)
        self._runner = runner or subprocess.run
        self.capabilities = self.probe()

    def _run(self, arguments: list[str], timeout_seconds: float | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(
                [self.executable, *arguments],
                text = True,
                capture_output = True,
                timeout = timeout_seconds or self.timeout_seconds,
                check = False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RendererError(f"cannot run Inkscape executable '{self.executable}': {exc}") from exc

    def probe(self) -> InkscapeCapabilities:
        try:
            version_result = self._run(["--version"], timeout_seconds = min(self.timeout_seconds, 10.0))
            action_result = self._run(["--action-list"], timeout_seconds = min(self.timeout_seconds, 20.0))
        except RendererError as exc:
            return InkscapeCapabilities(self.executable, None, set(), False, str(exc))
        if (version_result.returncode != 0) or (action_result.returncode != 0):
            detail = (version_result.stderr or action_result.stderr).strip()
            return InkscapeCapabilities(self.executable, None, set(), False, detail or "Inkscape capability probe failed")
        actions: set[str] = set()
        for line in action_result.stdout.splitlines():
            if ":" in line:
                actions.add(line.split(":", 1)[0].strip())
        return InkscapeCapabilities(self.executable, version_result.stdout.strip(), actions, True)

    def render_svg(
        self,
        svg_path: Path,
        output_path: Path,
        area: str = "page",
        width_px: int | None = None,
        height_px: int | None = None,
        dpi: float | None = None,
        background: str = "document",
    ) -> None:
        if not self.capabilities.available:
            raise RendererError(self.capabilities.error or "Inkscape CLI is unavailable")
        area_map = {"page": "--export-area-page", "drawing": "--export-area-drawing"}
        if area not in area_map:
            raise RendererError("offline renderer supports page or drawing area")
        arguments = [str(svg_path), "--export-type=png", area_map[area], f"--export-filename={output_path}"]
        if width_px is not None:
            arguments.append(f"--export-width={int(width_px)}")
        if height_px is not None:
            arguments.append(f"--export-height={int(height_px)}")
        if dpi is not None:
            arguments.append(f"--export-dpi={float(dpi)}")
        if background == "transparent":
            arguments.append("--export-background-opacity=0")
        result = self._run(arguments)
        if result.returncode != 0:
            raise RendererError((result.stderr or result.stdout or "Inkscape export failed").strip())
        if (not output_path.is_file()) or output_path.stat().st_size == 0:
            raise RendererError("Inkscape export returned success without a PNG")

    def active_actions(self, actions: list[str]) -> str:
        if not self.capabilities.available:
            raise LiveBackendUnavailableError(self.capabilities.error or "Inkscape CLI is unavailable")
        action_ids = [value.split(":", 1)[0] for value in actions]
        unsupported = [action for action in action_ids if not self.capabilities.supports(action)]
        if unsupported:
            raise LiveActionUnsupportedError(f"installed Inkscape does not support: {', '.join(unsupported)}")
        result = self._run(["--active-window", f"--actions={';'.join(actions)}"])
        output = "\n".join(value for value in (result.stdout, result.stderr) if value).strip()
        if result.returncode != 0:
            raise LiveBackendUnavailableError(output or "active-window action failed")
        if "No active desktop" in output:
            raise LiveBackendUnavailableError(output)
        return output

    def active_select(self, object_ids: list[str]) -> dict[str, Any]:
        if not object_ids:
            return self._active_mutation_receipt(["select-clear"])
        return self._active_mutation_receipt(
            [f"select-by-id:{self._object_id(object_id)}" for object_id in object_ids]
        )

    def active_list_selection(self) -> dict[str, Any]:
        if not self.capabilities.supports("select-list"):
            raise LiveActionUnsupportedError("installed Inkscape does not support: select-list")
        return self._selection_receipt(self.active_actions(["select-list"]), selection_observed = True)

    def active_set_style(self, object_ids: list[str], style: dict[str, object]) -> dict[str, Any]:
        actions = [f"select-by-id:{self._object_id(object_id)}" for object_id in object_ids]
        mapping = {
            "fill": "fill", "stroke": "stroke", "stroke_width": "stroke-width",
            "opacity": "opacity", "fill_opacity": "fill-opacity", "stroke_opacity": "stroke-opacity",
            "stroke_dasharray": "stroke-dasharray", "stroke_dashoffset": "stroke-dashoffset",
            "stroke_linecap": "stroke-linecap", "stroke_linejoin": "stroke-linejoin", "stroke_miterlimit": "stroke-miterlimit",
        }
        for key, attribute in mapping.items():
            if key in style:
                value = str(style[key])
                if any(character in value for character in "\r\n;,{}<>"):
                    raise LiveActionUnsupportedError(f"invalid value for live style field '{key}'")
                actions.append(f"object-set-attribute:{attribute},{value}")
        if len(actions) == len(object_ids):
            raise LiveActionUnsupportedError("no CLI-supported style fields were supplied")
        return self._active_mutation_receipt(actions)

    def active_transform(self, object_ids: list[str], operation: str, values: list[float]) -> dict[str, Any]:
        action_map = {
            "move": "transform-translate",
            "rotate": "transform-rotate",
            "scale": "transform-scale",
        }
        action = action_map.get(operation)
        if action is None:
            raise LiveActionUnsupportedError(f"unsupported live transform '{operation}'")
        numeric_values: list[float] = []
        for value in values:
            try:
                numeric_value = float(value)
            except (TypeError, ValueError) as exc:
                raise LiveActionUnsupportedError("live transform values must be finite numbers") from exc
            if isinstance(value, bool) or not math.isfinite(numeric_value):
                raise LiveActionUnsupportedError("live transform values must be finite numbers")
            numeric_values.append(numeric_value)
        if not numeric_values:
            raise LiveActionUnsupportedError("live transform values must be finite numbers")
        encoded = ",".join(format(value, ".12g") for value in numeric_values)
        return self._active_mutation_receipt([
            *(f"select-by-id:{self._object_id(object_id)}" for object_id in object_ids),
            f"{action}:{encoded}",
        ])

    def _active_mutation_receipt(self, actions: list[str]) -> dict[str, Any]:
        """Run a mutation and capture selection when the installed version permits it.

        The receipt is not a document revision: the external CLI backend has no
        transaction/revision API.  It is nevertheless useful evidence that the
        requested target remained selected after the action sequence.
        """
        selection_observed = self.capabilities.supports("select-list")
        receipt_actions = list(actions)
        if selection_observed:
            receipt_actions.append("select-list")
        output = self.active_actions(receipt_actions)
        return self._selection_receipt(output, selection_observed)

    @staticmethod
    def _selection_receipt(output: str, selection_observed: bool) -> dict[str, Any]:
        selected_ids: list[str] = []
        if selection_observed:
            for line in output.splitlines():
                match = _SELECT_LIST_LINE_PATTERN.match(line.strip())
                if match:
                    selected_ids.append(match.group(1))
        return {
            "output": output,
            "selection_observed": selection_observed,
            "selected_ids": selected_ids,
        }

    def active_render(self, output_path: Path, area: str = "page") -> str:
        action_map = {"page": "export-area-page", "drawing": "export-area-drawing"}
        if area not in action_map:
            raise LiveActionUnsupportedError("live renderer supports page or drawing area")
        result = self.active_actions([
            "export-type:png",
            action_map[area],
            f"export-filename:{output_path}",
            "export-do",
        ])
        if (not output_path.is_file()) or output_path.stat().st_size == 0:
            raise RendererError("active Inkscape export did not create a PNG")
        return result

    def temporary_png_path(self) -> Path:
        descriptor, filename = tempfile.mkstemp(prefix = "mcpinkscape-", suffix = ".png")
        os.close(descriptor)
        path = Path(filename)
        path.unlink(missing_ok = True)
        return path

    @staticmethod
    def _object_id(value: object) -> str:
        object_id = str(value)
        if not _SVG_ID_PATTERN.fullmatch(object_id):
            raise LiveActionUnsupportedError("live object IDs must be valid SVG IDs")
        return object_id
