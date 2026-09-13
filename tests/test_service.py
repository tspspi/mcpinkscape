from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcpinkscape.config.schema import AccessConfig, BridgeConfig
from mcpinkscape.errors import LiveActionUnsupportedError, PolicyDeniedError, RevisionConflictError
from mcpinkscape.service import InkscapeService


PNG_1X1 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL9mAAAAABJRU5ErkJggg==")


def make_service(tmp_path: Path) -> InkscapeService:
    return InkscapeService(
        AccessConfig(document_root = str(tmp_path / "library")),
        BridgeConfig(mode = "offline", executable = "missing-inkscape"),
    )


def test_service_offline_document_lifecycle(tmp_path):
    service = make_service(tmp_path)
    try:
        document = service.create_document("drawing", 100, 80)
        document_id = document["document_id"]
        rectangle = service.create_shape(
            document_id,
            "rectangle",
            {"x": 1, "y": 2, "width": 30, "height": 20},
            style = {"fill": "#112233"},
            expected_revision = 0,
        )
        assert rectangle["revision"] == 1
        changed_id = rectangle["changed_ids"][0]
        move = service.transform(document_id, "move", [changed_id], {"dx": 4, "dy": 5}, expected_revision = 1)
        assert move["revision"] == 2
        saved = service.save_document(document_id)
        assert saved["saved_path"] == "drawing.svg"
        service.close_document(document_id)
        opened = service.open_document("drawing.svg")
        assert service.get_object(opened["document_id"], changed_id)["attributes"]["fill"] == "#112233"
    finally:
        service.close()


def test_service_conflict_and_policy(tmp_path):
    service = make_service(tmp_path)
    try:
        document_id = service.create_document("drawing")["document_id"]
        with pytest.raises(RevisionConflictError):
            service.create_text(document_id, "x", 0, 0, expected_revision = 2)
    finally:
        service.close()

    denied = InkscapeService(
        AccessConfig(document_root = str(tmp_path / "denied"), allow_document_write = False),
        BridgeConfig(mode = "offline", executable = "missing-inkscape"),
    )
    try:
        with pytest.raises(PolicyDeniedError):
            denied.create_document("nope")
        with pytest.raises(PolicyDeniedError):
            denied.select_objects("missing", [])
    finally:
        denied.close()


def test_live_cli_rejects_expected_revision_instead_of_ignoring_it(tmp_path):
    service = InkscapeService(
        AccessConfig(document_root = str(tmp_path / "library")),
        BridgeConfig(mode = "active_window", executable = "missing-inkscape"),
    )
    try:
        service.cli = SimpleNamespace(capabilities = SimpleNamespace(available = True))
        with pytest.raises(LiveActionUnsupportedError, match = "cannot enforce expected_revision"):
            service.live_select(["rect-1"], expected_revision = 3)
    finally:
        service.close()


def test_service_imports_configured_png_as_portable_embedded_svg_image(tmp_path):
    service = make_service(tmp_path)
    try:
        asset = service.document_root.root / "assets" / "pixel.png"
        asset.parent.mkdir()
        asset.write_bytes(PNG_1X1)
        document_id = service.create_document("drawing") ["document_id"]
        imported = service.import_image(document_id, "assets/pixel.png", 10, 20, object_id = "pixel", expected_revision = 0)
        assert imported["result"]["embedded"] is True
        assert imported["result"]["intrinsic_width_px"] == 1
        image = service.get_object(document_id, "pixel")
        assert image["type"] == "image"
        assert image["attributes"]["href"].startswith("data:image/png;base64,")
        assert image["attributes"]["width"] == "1"
    finally:
        service.close()
