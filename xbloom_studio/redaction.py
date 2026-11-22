"""Sensitive-value redaction, identifier checks and bounded text truncation."""

from __future__ import annotations

import base64
import re
import unicodedata
from collections.abc import Iterator
from typing import Any

from .provider_exports import credential_key

MAX_EXCERPT_WIDTH = 120


MAX_SUMMARY_WIDTH = 220


MAX_SANITISED_TEXT = 512


_MAX_TRUNCATE_SCAN = 4096


_EXTRA_SENSITIVE_KEY_EXACT = frozenset(
    {"passwd", "pwd", "pin", "passphrase", "private", "cert", "certificate"}
)


_EXTRA_SENSITIVE_KEY_PARTS = (
    "passwd",
    "passphrase",
    "privatekey",
    "secretkey",
    "accesskey",
    "authkey",
    "signingkey",
    "sessionkey",
    "clientkey",
    "encryptionkey",
    "passkey",
)


_SAFE_MALFORMED_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


_PEM_BEGIN = re.compile(
    r"(?i)-----BEGIN (?:[A-Z0-9]+ ){0,6}PRIVATE KEY(?: BLOCK)?-----"
)


_PEM_END = re.compile(r"(?i)-----END (?:[A-Z0-9]+ ){0,6}PRIVATE KEY(?: BLOCK)?-----")


_REDACT_VALUE = re.compile(
    r"(?i)(?:"
    r"\b(?:gh[opsur]_[A-Za-z0-9]{16,}|github_pat_\w{20,})"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bxox[abposr]-[A-Za-z0-9-]{10,}"
    r"|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\bpypi-AgEI[A-Za-z0-9_-]{20,}"
    r"|\bAIza[0-9A-Za-z_-]{20,}"
    r"|\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|\bbasic\s+(?P<basic_tok>[A-Za-z0-9+/_-]+={0,2})"
    r"|https?://[^/\s:@]+:[^/\s@]+@"
    r")"
)


_JWT_PAYLOAD_DOT = re.compile(r"(?i)\.eyJ[A-Za-z0-9_-]{8,}")


_JWT_HEADER_PREFIX = re.compile(r"(?i)(?<![A-Za-z0-9_])eyJ")


_JWT_CHAR_SET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)


_MAX_BASIC_DECODE_LEN = 512


_BASIC_URLSAFE_TRANS = str.maketrans("-_", "+/")


_BASIC_HARD_CONT_CHAR_SET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)


_BASIC_CONT_CHAR_SET = frozenset("_-~")


_BASIC_DOT_CONT_CHAR_SET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_~+/=-"
)


_TRUNCATED_SECRET_TAIL = re.compile(
    r"(?i)(?:"
    r"https?://[^/\s@]*$"
    r"|(?<!-)-{5,}(?:[A-Z0-9 ]{1,60}-{0,5})?$"
    r"|\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]*$"
    r")"
)


_LEADING_WS = re.compile(r"\s+")


_TAIL_TOKEN = re.compile(r"(?<![A-Za-z0-9._~+/=-])[A-Za-z0-9._~+/=-]+$")


_TRUNCATED_TOKEN_PREFIX = re.compile(
    r"(?i)\b(?:gh[opsur]_|github_pat_|AKIA|ASIA|xox[abposr]-|sk-|pypi-AgEI|AIza|eyJ)"
)


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


_SHELL_META = re.compile(r"[;|&$`<>\\]")


def _is_unsafe_char(ch: str) -> bool:
    return unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp")


def safe_identifier(value: Any) -> str | None:
    """Return ``value`` only when it is a plain, shell-safe identifier."""
    if type(value) is str and _SAFE_IDENTIFIER.fullmatch(value):
        return value
    return None


def is_safe_human_note(value: Any) -> bool:
    """Return whether ``value`` is plain text free of shell meta/controls/secrets."""
    if type(value) is not str or not value.strip():
        return False
    if _SHELL_META.search(value) or _has_sensitive_value(value):
        return False
    return not any(_is_unsafe_char(ch) for ch in value)


def _is_sensitive_key(key: str) -> bool:
    if credential_key(key):
        return True
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _EXTRA_SENSITIVE_KEY_EXACT or any(
        part in normalized for part in _EXTRA_SENSITIVE_KEY_PARTS
    )


def _pointer_segments(pointer: str) -> list[str] | None:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        return None
    return [
        part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")
    ]


def _is_sensitive_pointer(pointer: str | None) -> bool:
    if not isinstance(pointer, str):
        return False
    segments = _pointer_segments(pointer)
    return segments is not None and any(_is_sensitive_key(seg) for seg in segments)


def _is_decoded_basic_credential(token: str) -> bool:
    n = len(token)
    if n < 4 or n > _MAX_BASIC_DECODE_LEN:
        return False
    stripped = token.rstrip("=")
    pad_len = n - len(stripped)
    rem = len(stripped) % 4
    needed_pad = (4 - rem) % 4
    if rem == 1 or pad_len > needed_pad or "=" in stripped:
        return False
    has_std = "+" in stripped or "/" in stripped
    has_url = "-" in stripped or "_" in stripped
    if has_std and has_url:
        return False
    normalized = stripped.translate(_BASIC_URLSAFE_TRANS) if has_url else stripped
    padded = normalized + ("=" * needed_pad)
    try:
        decoded = base64.b64decode(padded, validate=True)
    except Exception:
        return False
    return b":" in decoded and all(32 <= byte <= 126 for byte in decoded)


def _is_opaque_basic_secret(token: str) -> bool:
    n = len(token)
    if n < 4 or "-" in token or "_" in token:
        return False
    stripped = token
    if "=" in token:
        stripped = token.rstrip("=")
        pad_len = n - len(stripped)
        if not (
            1 <= pad_len <= 2
            and len(stripped) >= 2
            and "=" not in stripped
            and n % 4 == 0
        ):
            return False
    if n >= 8 and (
        not stripped.isalpha()
        or not (stripped.islower() or stripped.isupper() or stripped.istitle())
    ):
        return True
    return (
        ("/" in stripped or "+" in stripped)
        and not stripped.islower()
        and not stripped.isupper()
    )


def _has_basic_token_continuation(text: str, end: int) -> bool:
    length = len(text)
    if end >= length:
        return False
    ch = text[end]
    if ch in _BASIC_CONT_CHAR_SET:
        return True
    return ch == "." and end + 1 < length and text[end + 1] in _BASIC_DOT_CONT_CHAR_SET


def _is_basic_secret_match(text: str, token: str, end: int) -> bool:
    length = len(text)
    if end < length and text[end] in _BASIC_HARD_CONT_CHAR_SET:
        return False
    if _is_decoded_basic_credential(token):
        if (
            not token.endswith("=")
            and token[:3].lower() == "eyj"
            and end + 1 < length
            and text[end] == "."
            and text[end + 1] in _BASIC_DOT_CONT_CHAR_SET
        ):
            return False
        return True
    return not _has_basic_token_continuation(text, end) and _is_opaque_basic_secret(
        token
    )


def _redact_pem(text: str) -> str:
    if "-----" not in text:
        return text
    out: list[str] = []
    pos = 0
    length = len(text)
    while pos < length:
        begin = _PEM_BEGIN.search(text, pos)
        if begin is None:
            out.append(text[pos:])
            break
        out.append(text[pos : begin.start()])
        out.append("<redacted>")
        end = _PEM_END.search(text, begin.end())
        if end is None:
            break
        pos = end.end()
    return "".join(out)


def _iter_non_jwt_sensitive_spans(text: str) -> Iterator[tuple[int, int]]:
    pos = 0
    length = len(text)
    while pos < length:
        match = _REDACT_VALUE.search(text, pos)
        if match is None:
            return
        basic_tok = match.group("basic_tok")
        if basic_tok is not None and not _is_basic_secret_match(
            text, basic_tok, match.end()
        ):
            pos = match.start("basic_tok")
            continue
        yield match.start(), match.end()
        pos = match.end()


def _iter_jwt_spans(text: str) -> Iterator[tuple[int, int]]:
    if "." not in text:
        return
    pos = 0
    length = len(text)
    while pos < length:
        payload = _JWT_PAYLOAD_DOT.search(text, pos)
        if payload is None:
            return
        dot_idx = payload.start()
        header_start = dot_idx
        while header_start > 0 and text[header_start - 1] in _JWT_CHAR_SET:
            header_start -= 1
        if dot_idx - header_start >= 11:
            prefix = _JWT_HEADER_PREFIX.search(text, header_start, dot_idx - 8)
            if prefix is not None:
                jwt_end = payload.end()
                if (
                    jwt_end + 1 < length
                    and text[jwt_end] == "."
                    and text[jwt_end + 1] in _JWT_CHAR_SET
                ):
                    sig_end = jwt_end + 2
                    while sig_end < length and text[sig_end] in _JWT_CHAR_SET:
                        sig_end += 1
                    jwt_end = sig_end
                yield prefix.start(), jwt_end
                pos = jwt_end
                continue
        pos = dot_idx + 1


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if len(spans) <= 1:
        return spans
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _has_sensitive_value(text: str) -> bool:
    if "-----" in text and _PEM_BEGIN.search(text):
        return True
    return (
        next(_iter_non_jwt_sensitive_spans(text), None) is not None
        or next(_iter_jwt_spans(text), None) is not None
    )


def _redact_text(text: str) -> str:
    cleaned = _redact_pem(text)
    spans = list(_iter_non_jwt_sensitive_spans(cleaned))
    spans.extend(_iter_jwt_spans(cleaned))
    if not spans:
        return cleaned
    out: list[str] = []
    pos = 0
    for start, end in _merge_spans(spans):
        out.append(cleaned[pos:start])
        out.append("<redacted>")
        pos = end
    out.append(cleaned[pos:])
    return "".join(out)


def _redact_truncated_tail(text: str) -> str:
    cleaned = _TRUNCATED_SECRET_TAIL.sub("<redacted>", text)
    tail = _TAIL_TOKEN.search(cleaned)
    if tail is not None:
        prefix = _TRUNCATED_TOKEN_PREFIX.search(cleaned, tail.start())
        if prefix is not None:
            return cleaned[: prefix.start()] + "<redacted>"
    return cleaned


def _truncate(text: str, limit: int) -> str:
    start = 0
    if text and text[0].isspace():
        ws = _LEADING_WS.match(text)
        start = ws.end() if ws is not None else 0
    scan_limit = max(limit * 8, _MAX_TRUNCATE_SCAN)
    cropped = (len(text) - start) > scan_limit
    sample = text[start : start + scan_limit] if cropped else text[start:]
    normalized = "".join(" " if _is_unsafe_char(ch) else ch for ch in sample).strip()
    cleaned = _redact_text(normalized)
    if cropped:
        cleaned = _redact_truncated_tail(cleaned)
        if len(cleaned) <= limit:
            if cleaned.endswith("..."):
                return cleaned
            return cleaned[: max(0, limit - 3)].rstrip() + "..."
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _truncate_line(text: str, limit: int) -> str:
    stripped = text.rstrip("\r\n")
    cleaned = "".join(
        "  " if ch == "\t" else " " if _is_unsafe_char(ch) else ch for ch in stripped
    )
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."
