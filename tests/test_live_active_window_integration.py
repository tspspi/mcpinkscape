"""Opt-in verification test against a user-authorized, already-open Inkscape document.

This test is deliberately disabled unless both variables below are supplied:
`MCPINKSCAPE_LIVE_VERIFY=1` and `MCPINKSCAPE_LIVE_VERIFY_TARGET=<svg-id>`.
It must never discover or choose an arbitrary active document itself.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from mcpinkscape.config.schema import AccessConfig, BridgeConfig
from mcpinkscape.service import InkscapeService


_LIVE_ENABLED = os.environ.get("MCPINKSCAPE_LIVE_VERIFY") == "1"
_LIVE_TARGET = os.environ.get("MCPINKSCAPE_LIVE_VERIFY_TARGET", "").strip()


@pytest.mark.skipif(
    (not _LIVE_ENABLED) or (not _LIVE_TARGET),
    reason = "set MCPINKSCAPE_LIVE_VERIFY=1 and MCPINKSCAPE_LIVE_VERIFY_TARGET to run against an authorized active document",
)
def test_active_window_selection_style_transform_and_snapshot():
    with tempfile.TemporaryDirectory(prefix = "mcpinkscape-live-verify-") as root:
        service = InkscapeService(
            AccessConfig(document_root = root),
            BridgeConfig(mode = "active_window", executable = "inkscape"),
        )
        try:
            before = service.live_list_selection()
            assert before["backend"] == "active_window"

            selected = service.live_select([_LIVE_TARGET])
            assert selected["backend"] == "active_window"
            assert selected["selection_observed"] is True
            assert selected["selected_ids"] == [_LIVE_TARGET]

            listed = service.live_list_selection()
            assert listed["selected_ids"] == [_LIVE_TARGET]

            styled = service.live_set_style(
                [_LIVE_TARGET], {"fill": os.environ.get("MCPINKSCAPE_LIVE_VERIFY_FILL", "#0066aa")}
            )
            assert styled["selected_ids"] == [_LIVE_TARGET]

            # Identity values exercise the typed action serialization without
            # moving a user-authorized fixture every time this verification runs.
            moved = service.live_transform("move", [_LIVE_TARGET], [0, 0])
            rotated = service.live_transform("rotate", [_LIVE_TARGET], [0])
            scaled = service.live_transform("scale", [_LIVE_TARGET], [1])
            assert moved["selected_ids"] == [_LIVE_TARGET]
            assert rotated["selected_ids"] == [_LIVE_TARGET]
            assert scaled["selected_ids"] == [_LIVE_TARGET]

            snapshot = service.live_render_snapshot(area = "page")
            payload = service.get_snapshot_base64(snapshot["snapshot_id"])
            assert snapshot["backend"] == "active_window"
            assert payload["base64"].startswith("iVBORw0KGgo")
        finally:
            service.close()
