"""Argon2id API-key generation and verification for remote MCP transport."""

from __future__ import annotations

import base64
import hmac
import os
from typing import Iterable

from argon2.low_level import Type, hash_secret_raw

from mcpinkscape.config.schema import APIKeyConfig


DEFAULT_KDF = {
    "algorithm": "argon2id",
    "salt": None,
    "time_cost": 3,
    "memory_cost": 65536,
    "parallelism": 1,
    "hash_len": 32,
}


def ensure_kdf_defaults(kdf: dict | None) -> dict:
    result = DEFAULT_KDF.copy()
    result.update(kdf or {})
    if result["algorithm"] != "argon2id":
        raise ValueError("only argon2id API key derivation is supported")
    if result.get("salt") is None:
        result["salt"] = base64.b64encode(os.urandom(16)).decode("ascii")
    return result


def generate_random_api_key(length: int = 48) -> str:
    return base64.urlsafe_b64encode(os.urandom(length)).decode("ascii").rstrip("=")


def derive_argon2id_hash(token: str, kdf: dict) -> str:
    raw = hash_secret_raw(
        secret = token.encode("utf-8"),
        salt = base64.b64decode(kdf["salt"]),
        time_cost = int(kdf["time_cost"]),
        memory_cost = int(kdf["memory_cost"]),
        parallelism = int(kdf["parallelism"]),
        hash_len = int(kdf["hash_len"]),
        type = Type.ID,
    )
    return base64.b64encode(raw).decode("ascii")


def match_api_key(token: str, entries: Iterable[APIKeyConfig]) -> APIKeyConfig | None:
    if not token:
        return None
    for entry in entries:
        kdf = entry.kdf or {}
        if (kdf.get("algorithm") != "argon2id") or (not kdf.get("hash")):
            continue
        try:
            calculated = derive_argon2id_hash(token, kdf)
        except (KeyError, ValueError, TypeError):
            continue
        if hmac.compare_digest(calculated, str(kdf["hash"])):
            return entry
    return None
