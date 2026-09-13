"""JSON configuration loading, logging setup, and command-line parsing."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from pydantic import ValidationError

from mcpinkscape.config.api_keys import derive_argon2id_hash, ensure_kdf_defaults, generate_random_api_key
from mcpinkscape.config.schema import Config


DEFAULT_CONFIG_PATH = str(Path.home() / ".config" / "mcpinkscape.conf")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description = "MCP Inkscape Server")
    parser.add_argument("--config", default = DEFAULT_CONFIG_PATH, help = "JSON configuration file")
    parser.add_argument("--log-level", choices = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    parser.add_argument("--logfile", help = "Override log file")
    parser.add_argument("--transport", choices = ["stdio", "remotehttp"], default = None)
    parser.add_argument("--remote", action = "store_true", help = "Shortcut for --transport remotehttp")
    parser.add_argument("--genkey", metavar = "ID", help = "Generate or rotate a configured remote API key")
    args = parser.parse_args()
    if args.remote:
        args.transport = "remotehttp"
    return args


def load_config(path: str) -> Config:
    try:
        with open(path, "r", encoding = "utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"configuration file not found at {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON configuration at {path}: {exc}") from exc
    try:
        return Config(**raw)
    except ValidationError as exc:
        raise ValueError(f"invalid configuration: {exc}") from exc


def setup_logging(config: Config, log_level: str | None = None, logfile: str | None = None) -> None:
    level_name = (log_level or config.logging.level).upper()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    target_logfile = logfile or config.logging.logfile
    if target_logfile:
        handlers.append(logging.FileHandler(Path(target_logfile).expanduser(), mode = "a"))
    logging.basicConfig(
        level = getattr(logging, level_name, logging.INFO),
        format = "%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers = handlers,
        force = True,
    )


def generate_and_store_api_key(config_path: str, key_id: str) -> str:
    with open(config_path, "r", encoding = "utf-8") as handle:
        raw = json.load(handle)
    entries = raw.get("api_keys") or []
    entry = next((value for value in entries if value.get("id") == key_id), None)
    if entry is None:
        raise KeyError(f"API key ID '{key_id}' is not configured")
    kdf = ensure_kdf_defaults(entry.get("kdf"))
    token = generate_random_api_key()
    kdf["hash"] = derive_argon2id_hash(token, kdf)
    entry["kdf"] = kdf
    Config(**raw)
    with open(config_path, "w", encoding = "utf-8") as handle:
        json.dump(raw, handle, indent = 2)
        handle.write("\n")
    return token
