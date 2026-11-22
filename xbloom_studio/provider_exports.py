"""Bounded offline Firestore exports. No credentials, files or network are opened.

An owner-supplied xRecipe document (``name`` and typed ``fields``) is checked
for size, depth and JSON types, then credential-named fields and
credential-bearing URLs are removed before conversion or persistence.
Redaction is field-based: secrets embedded in free text are not detected.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .serialization import read_json_file

MAX_EXPORT = 2 * 1024 * 1024
MAX_DEPTH = 80
EXPORT_RESOURCE = re.compile(
    r"projects/xbloom-recipe-app/databases/\(default\)/documents/"
    r"(?:recipes_master/[A-Za-z0-9_-]+|users/[A-Za-z0-9_-]+/coffees/[A-Za-z0-9_-]+)"
)
CREDENTIAL_KEYS = frozenset(
    {
        "auth",
        "oauth",
        "token",
        "headers",
        "firebaseauth",
        "jwt",
        "authentication",
        "bearer",
    }
)
CREDENTIAL_PARTS = (
    "password",
    "authorization",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "apikey",
    "cookie",
    "session",
    "credential",
    "secret",
    "signature",
)
REDACTED_URL = "[credential-bearing URL removed]"


def credential_key(key: str) -> bool:
    key = re.sub("[^a-z0-9]", "", key.lower())
    return (
        key in CREDENTIAL_KEYS
        or key.endswith("token")
        or any(part in key for part in CREDENTIAL_PARTS)
    )


def check_document_bounds(document: Any, *, measure_size: bool = True) -> None:
    """Iteratively bound depth and size; reject cycles and non-JSON values."""
    pending = [(document, 0)]
    seen: set[int] = set()
    budget = 0
    while pending:
        value, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise ValueError(f"Export nesting exceeds {MAX_DEPTH} levels")
        if isinstance(value, (dict, list)):
            if id(value) in seen:
                raise ValueError("Export must be an acyclic JSON tree")
            seen.add(id(value))
            if isinstance(value, dict):
                if any(type(k) is not str for k in value):
                    raise ValueError("Export object keys must be strings")
                budget += sum(len(k.encode("utf-8")) + 4 for k in value)
                pending.extend((v, depth + 1) for v in value.values())
            else:
                pending.extend((v, depth + 1) for v in value)
            budget += len(value) + 2
        elif type(value) is str:
            budget += len(value.encode("utf-8")) + 2
        elif value is None or type(value) in (bool, int, float):
            if type(value) is float and not math.isfinite(value):
                raise ValueError("Export numbers must be finite")
            budget += len(str(value))
        else:
            raise ValueError("Export must contain JSON values only")
        if measure_size and budget > MAX_EXPORT:
            raise ValueError("Export exceeds 2 MiB")
    if (
        measure_size
        and len(json.dumps(document, ensure_ascii=False).encode()) > MAX_EXPORT
    ):
        raise ValueError("Export exceeds 2 MiB")


def read_export(
    document: Any, decode: Callable[[dict[str, Any]], Any]
) -> dict[str, Any]:
    """Return a sanitized import source for an owner-supplied export."""
    check_document_bounds(document)
    redactions = 0

    def clean(value: Any) -> Any:
        nonlocal redactions
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if credential_key(key):
                    redactions += 1
                else:
                    result[key] = clean(item)
            return result
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str) and value.partition(":")[0].strip().lower() in (
            "http",
            "https",
        ):
            try:
                parsed = urlsplit(value)
                sensitive = (
                    parsed.username
                    or parsed.password
                    or any(
                        key.casefold() == "sig" or credential_key(key)
                        for params in (
                            parsed.query,
                            parsed.fragment,
                            parsed.fragment.partition("?")[2],
                        )
                        for key, _ in parse_qsl(params)
                    )
                )
            except ValueError:
                sensitive = True
            if sensitive:
                redactions += 1
                return REDACTED_URL
        return value

    sanitized = clean(document)
    if not isinstance(sanitized, dict):
        raise ValueError("Export must be a Firestore document object")
    name = sanitized.get("name")
    if not isinstance(name, str) or not EXPORT_RESOURCE.fullmatch(name):
        raise ValueError(
            "Export must identify an xRecipe recipes_master or users/UID/coffees "
            "document"
        )
    if not isinstance(sanitized.get("fields"), dict):
        raise ValueError("Export requires Firestore typed fields")
    source: dict[str, Any] = {
        "provider": "xrecipe-export",
        "url": None,
        "provider_id": name.rsplit("/", 1)[1],
        "export_resource": name,
        "raw_document": sanitized,
        "redacted_fields": redactions,
    }
    try:
        source["raw"] = decode({"mapValue": {"fields": sanitized["fields"]}})
    except ValueError, KeyError, TypeError, AttributeError, OverflowError:
        source.update(
            raw=None,
            decode_error="Invalid or unsupported Firestore typed value in export",
        )
    return source


def load_export(path: str | os.PathLike[str]) -> Any:
    """Read one bounded JSON export file."""
    try:
        return read_json_file(path, MAX_EXPORT)
    except ValueError as exc:
        raise ValueError("Unable to read a bounded JSON recipe export") from exc
