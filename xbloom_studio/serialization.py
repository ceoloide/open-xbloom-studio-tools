"""Canonical JSON shared by persistence, hashes and transport-only adapters."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def dumps(value: Any) -> str:
    """Return canonical JSON: sorted keys, no whitespace, no NaN or infinity."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    """Return the SHA-256 hex digest of the canonical JSON of ``value``."""
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


MAX_DATA_FILE = 16 * 1024 * 1024


def read_json_file(path: str | os.PathLike[str], limit: int = MAX_DATA_FILE) -> Any:
    """Read one bounded JSON document from a user-supplied file."""
    try:
        with Path(path).expanduser().open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError(f"Data file exceeds {limit} bytes")
        return json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Unable to read a bounded JSON data file") from exc
