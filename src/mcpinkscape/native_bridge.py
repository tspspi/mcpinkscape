"""Client for the optional loaded Inkscape native bridge extension."""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import uuid
from pathlib import Path
from typing import Any

from mcpinkscape.errors import LiveBackendUnavailableError, RevisionConflictError


MAX_MESSAGE_BYTES = 1024 * 1024


class NativeBridgeClient:
    """Synchronous newline-JSON client for a same-user native UDS bridge.

    Authentication is intentionally filesystem ownership and peer identity, not
    a token. This client rejects non-socket, foreign-owner, or world-accessible
    endpoints before it opens them.
    """

    def __init__(
        self,
        path: str | Path,
        timeout_seconds: float = 30.0,
        tcp_host: str = "127.0.0.1",
        tcp_port: int | None = None,
        platform_name: str | None = None,
    ):
        self.path = Path(path).expanduser()
        self.timeout_seconds = float(timeout_seconds)
        self.tcp_host = str(tcp_host)
        self.tcp_port = tcp_port
        self.platform_name = platform_name or os.name
        self._socket: socket.socket | None = None
        self._reader = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        if self.platform_name == "nt":
            connection = self._connect_windows_loopback()
        else:
            connection = self._connect_unix_socket()
        self._socket = connection
        self._reader = connection.makefile("rb")

    def _connect_unix_socket(self) -> socket.socket:
        self._validate_endpoint()
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(self.timeout_seconds)
            connection.connect(str(self.path))
        except OSError as exc:
            raise LiveBackendUnavailableError(f"cannot connect to native bridge at {self.path}: {exc}") from exc
        return connection

    def _connect_windows_loopback(self) -> socket.socket:
        if self.tcp_host != "127.0.0.1":
            raise LiveBackendUnavailableError("Windows native bridge TCP host must be exactly 127.0.0.1")
        if (self.tcp_port is None) or not (1 <= int(self.tcp_port) <= 65535):
            raise LiveBackendUnavailableError("Windows native bridge TCP port must be in the range 1 through 65535")
        try:
            connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            connection.settimeout(self.timeout_seconds)
            connection.connect((self.tcp_host, int(self.tcp_port)))
        except OSError as exc:
            raise LiveBackendUnavailableError(
                f"cannot connect to Windows native bridge at {self.tcp_host}:{self.tcp_port}: {exc}"
            ) from exc
        return connection

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def __enter__(self) -> "NativeBridgeClient":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def status(self) -> dict[str, Any]:
        return self.request("bridge.hello", {})

    def request(self, method: str, params: dict[str, Any], expected_revision: int | None = None) -> dict[str, Any]:
        if self._socket is None or self._reader is None:
            self.connect()
        request_id = uuid.uuid4().hex
        payload: dict[str, Any] = {
            "type": "request",
            "id": request_id,
            "protocol_version": 1,
            "method": str(method),
            "params": dict(params),
        }
        if expected_revision is not None:
            payload["expected_revision"] = expected_revision
        encoded = (json.dumps(payload, separators = (",", ":"), ensure_ascii = False) + "\n").encode("utf-8")
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise LiveBackendUnavailableError("native bridge request exceeds 1 MiB limit")
        with self._lock:
            try:
                assert self._socket is not None
                assert self._reader is not None
                self._socket.sendall(encoded)
                while True:
                    raw = self._reader.readline(MAX_MESSAGE_BYTES + 1)
                    if not raw:
                        raise LiveBackendUnavailableError("native bridge closed the connection")
                    if len(raw) > MAX_MESSAGE_BYTES:
                        raise LiveBackendUnavailableError("native bridge response exceeds 1 MiB limit")
                    response = json.loads(raw.decode("utf-8"))
                    if response.get("type") == "event":
                        continue
                    if response.get("id") != request_id:
                        raise LiveBackendUnavailableError("native bridge returned an unexpected response ID")
                    if not response.get("ok", False):
                        error = response.get("error") or {}
                        if error.get("code") == "revision_conflict":
                            current_revision = error.get("current_revision")
                            detail = str(error.get("message") or "document changed")
                            if current_revision is not None:
                                detail += f"; current revision is {current_revision}"
                            raise RevisionConflictError(detail)
                        raise LiveBackendUnavailableError(str(error.get("message") or error.get("code") or "native bridge operation failed"))
                    result = response.get("result")
                    if not isinstance(result, dict):
                        raise LiveBackendUnavailableError("native bridge response result must be an object")
                    return result
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.close()
                raise LiveBackendUnavailableError(f"native bridge protocol failure: {exc}") from exc

    def _validate_endpoint(self) -> None:
        try:
            info = self.path.lstat()
        except OSError as exc:
            raise LiveBackendUnavailableError(f"native bridge socket is unavailable at {self.path}: {exc}") from exc
        if not stat.S_ISSOCK(info.st_mode):
            raise LiveBackendUnavailableError(f"native bridge endpoint is not a socket: {self.path}")
        if info.st_uid != os.getuid():
            raise LiveBackendUnavailableError("native bridge socket is not owned by the current user")
        if info.st_mode & 0o077:
            raise LiveBackendUnavailableError("native bridge socket permissions must not allow group or other access")
