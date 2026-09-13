"""Document-root confinement and PNG snapshot lifecycle."""

from __future__ import annotations

import base64
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from mcpinkscape.errors import DocumentNotFoundError, InvalidDrawingValueError, PolicyDeniedError


def ensure_private_directory(path: Path) -> Path:
    path.mkdir(parents = True, exist_ok = True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


class DocumentRoot:
    """Constrain all server-owned document paths to one configured root."""

    def __init__(self, root: str | Path):
        self.root = ensure_private_directory(Path(root).expanduser().resolve())

    def resolve(self, relative_path: str | Path, suffixes: tuple[str, ...] = (".svg",)) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute():
            raise PolicyDeniedError("absolute document paths are not allowed")
        candidate = (self.root / raw).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PolicyDeniedError("document path escapes configured document_root") from exc
        if suffixes and candidate.suffix.lower() not in suffixes:
            raise InvalidDrawingValueError(f"document path must use one of: {', '.join(suffixes)}")
        return candidate


@dataclass(frozen = True)
class Snapshot:
    snapshot_id: str
    path: Path
    mime_type: str
    width_px: int | None = None
    height_px: int | None = None


class SnapshotRegistry:
    """Own registered PNG files; never expose arbitrary paths to callers."""

    def __init__(self, root: Path):
        self.root = ensure_private_directory(root / "snapshots")
        self._snapshots: dict[str, Snapshot] = {}

    def register_png(self, source_path: Path, width_px: int | None = None, height_px: int | None = None) -> Snapshot:
        if (not source_path.is_file()) or source_path.stat().st_size == 0:
            raise InvalidDrawingValueError("renderer did not produce a non-empty PNG")
        snapshot_id = uuid.uuid4().hex
        destination = self.root / f"{snapshot_id}.png"
        shutil.copyfile(source_path, destination)
        try:
            destination.chmod(0o600)
        except OSError:
            pass
        snapshot = Snapshot(snapshot_id, destination, "image/png", width_px, height_px)
        self._snapshots[snapshot_id] = snapshot
        return snapshot

    def get(self, snapshot_id: str) -> Snapshot:
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None:
            raise DocumentNotFoundError(f"snapshot '{snapshot_id}' was not found")
        return snapshot

    def list(self) -> list[dict[str, object]]:
        return [self.describe(snapshot) for snapshot in self._snapshots.values()]

    def describe(self, snapshot: Snapshot) -> dict[str, object]:
        return {
            "snapshot_id": snapshot.snapshot_id,
            "mime_type": snapshot.mime_type,
            "width_px": snapshot.width_px,
            "height_px": snapshot.height_px,
        }

    def base64_payload(self, snapshot_id: str) -> dict[str, object]:
        snapshot = self.get(snapshot_id)
        data = base64.b64encode(snapshot.path.read_bytes()).decode("ascii")
        return {**self.describe(snapshot), "base64": data}

    def cleanup(self) -> None:
        for snapshot in self._snapshots.values():
            try:
                snapshot.path.unlink()
            except FileNotFoundError:
                pass
        self._snapshots.clear()
