"""Pydantic models for the mcpInkscape configuration file."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class LoggingConfig(BaseModel):
    level: str = "INFO"
    logfile: str | None = None


class BridgeConfig(BaseModel):
    mode: Literal["auto", "native_uds", "active_window", "offline"] = "auto"
    uds: str = str(Path.home() / ".local" / "state" / "mcpinkscape" / "bridge.sock")
    tcp_host: str = "127.0.0.1"
    tcp_port: int | None = 61779
    timeout_seconds: float = 30.0
    required: bool = False  # Require native live control, not availability at startup.
    executable: str = "inkscape"
    auto_enable: bool = True
    startup_timeout_seconds: float = Field(default = 45.0, gt = 0, le = 300)
    shutdown_timeout_seconds: float = Field(default = 20.0, gt = 0, le = 300)
    desktop_environment: dict[Literal["DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
                                      "XAUTHORITY", "XDG_RUNTIME_DIR"], str] = Field(default_factory = dict)


class TransportConfig(BaseModel):
    uds: str | None = None
    host: str | None = None
    port: int | None = None


class RemoteServerConfig(BaseModel):
    transport: TransportConfig | None = None
    uds: str | None = None
    host: str | None = None
    port: int | None = None
    url_prefix: str | None = None
    api_key_kdf: dict = Field(default_factory = dict)

    def resolved_endpoint(self) -> dict[str, str | int | None]:
        transport = self.transport
        return {
            "uds": self.uds or (transport.uds if transport else None),
            "host": self.host or (transport.host if transport else None),
            "port": self.port or (transport.port if transport else None),
        }


class AccessConfig(BaseModel):
    document_root: str = str(Path.home() / "mcpinkscape-library")
    allow_document_write: bool = True
    allow_snapshots: bool = True
    allow_live_control: bool = True
    allow_application_control: bool = True
    enable_autonomous_execution: bool = True


class APIKeyConfig(AccessConfig):
    allow_application_control: bool = False
    id: str
    kdf: dict = Field(default_factory = dict)


class Config(BaseModel):
    mode: Literal["stdio", "remotehttp"] = "stdio"
    logging: LoggingConfig = Field(default_factory = LoggingConfig)
    bridge: BridgeConfig = Field(default_factory = BridgeConfig)
    stdio: AccessConfig = Field(default_factory = AccessConfig)
    remote_server: RemoteServerConfig | None = None
    api_keys: list[APIKeyConfig] = Field(default_factory = list)
