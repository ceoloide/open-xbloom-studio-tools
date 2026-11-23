"""Shared bounded single-line RPC framing; no I/O or application ownership."""

from __future__ import annotations

import json
import math
from typing import Any

from .serialization import dumps

MAX_REQUEST = 2 * 1024 * 1024
MAX_RESPONSE = 32 * 1024 * 1024


def frame(
    value: Any, limit: int, error_message: str = "RPC frame exceeds size limit"
) -> bytes:
    raw = dumps(value).encode() + b"\n"
    if len(raw) > limit:
        raise ValueError(error_message)
    return raw


def _finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    return value


def _decode(raw: bytes, limit: int) -> Any:
    if not raw or len(raw) > limit or not raw.endswith(b"\n") or b"\n" in raw[:-1]:
        raise ValueError("RPC requires one complete bounded JSON line")
    try:
        return json.loads(raw, parse_constant=_finite_float, parse_float=_finite_float)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid RPC JSON frame") from exc


def request(raw: bytes, limit: int = MAX_REQUEST) -> dict[str, Any]:
    value = _decode(raw, limit)
    if (
        not isinstance(value, dict)
        or set(value) - {"method", "params"}
        or not isinstance(value.get("method"), str)
        or not value["method"].strip()
    ):
        raise ValueError("Invalid RPC request envelope")
    if value.get("params") is not None and not isinstance(value["params"], dict):
        raise ValueError("RPC parameters must be an object")
    return value


def response(raw: bytes, limit: int = MAX_RESPONSE) -> dict[str, Any]:
    value = _decode(raw, limit)
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        raise ValueError("Invalid RPC response envelope")
    if value["ok"]:
        if set(value) != {"ok", "data"}:
            raise ValueError("Invalid RPC success envelope")
    elif (
        set(value) != {"ok", "error"}
        or not isinstance(value["error"], dict)
        or not isinstance(value["error"].get("message"), str)
    ):
        raise ValueError("Invalid RPC error envelope")
    return value
