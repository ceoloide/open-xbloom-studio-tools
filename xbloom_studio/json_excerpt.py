"""Bounded JSON tokenization, pointer location and redacted local excerpts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .redaction import (
    _SAFE_MALFORMED_KEY,
    MAX_EXCERPT_WIDTH,
    _has_sensitive_value,
    _is_sensitive_key,
    _is_unsafe_char,
    _pointer_segments,
)

MAX_JSON_LOCATE_BYTES = 256 * 1024


MAX_JSON_LOCATE_DEPTH = 100


_JSON_TOKEN = re.compile(
    r"""
    (?P<ws>[ \t\r\n]+)
    |(?P<string>"(?:\\.|[^"\\\x00-\x1f])*")
    |(?P<punct>[{}\[\]:,])
    |(?P<atom>true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)
    """,
    re.VERBOSE,
)


def _split_lf_lines(text: str) -> list[tuple[int, int, str]]:
    lines: list[tuple[int, int, str]] = []
    start = 0
    length = len(text)
    while start < length:
        nl = text.find("\n", start)
        if nl == -1:
            end = length
            next_start = length
        else:
            end = nl - 1 if nl > start and text[nl - 1] == "\r" else nl
            next_start = nl + 1
        lines.append((start, end, text[start:end]))
        start = next_start
    return lines


def _window_line(text: str, column: int | None) -> tuple[str, int | None]:
    if len(text) <= MAX_EXCERPT_WIDTH:
        safe_col = (
            column if column is not None and 1 <= column <= len(text) + 1 else None
        )
        return text, safe_col
    if column is None:
        return text[: MAX_EXCERPT_WIDTH - 3] + "...", None
    zero_col = max(0, min(column - 1, len(text)))
    half = (MAX_EXCERPT_WIDTH - 6) // 2
    start = max(0, zero_col - half)
    end = min(len(text), start + MAX_EXCERPT_WIDTH - 6)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    snippet = prefix + text[start:end] + suffix
    adjusted = len(prefix) + (zero_col - start) + 1
    return snippet, adjusted


def _tokenize_json(text: str) -> list[tuple[str, str, int, int]] | None:
    tokens: list[tuple[str, str, int, int]] = []
    pos = 0
    for match in _JSON_TOKEN.finditer(text):
        if match.start() != pos:
            return None
        pos = match.end()
        kind = match.lastgroup
        if kind and kind != "ws":
            tokens.append((kind, match.group(), match.start(), match.end()))
    if pos != len(text) or not tokens:
        return None
    return tokens


def _parse_json_structure(
    tokens: Sequence[tuple[str, str, int, int]],
    *,
    target_segments: list[str] | None = None,
) -> tuple[int | None, list[tuple[int, int]], set[int]] | None:
    index = 0
    sensitive_spans: list[tuple[int, int]] = []
    key_indices: set[int] = set()

    def parse_value(path: list[str], depth: int) -> tuple[bool, int, int | None]:
        nonlocal index
        if depth > MAX_JSON_LOCATE_DEPTH or index >= len(tokens):
            return False, 0, None
        kind, raw, start, end = tokens[index]
        self_match = (
            start if target_segments is not None and path == target_segments else None
        )
        if kind == "string":
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                return False, 0, None
            if isinstance(decoded, str) and _has_sensitive_value(decoded):
                sensitive_spans.append((start, end))
            index += 1
            return True, end, self_match
        if kind == "atom":
            index += 1
            return True, end, self_match
        if kind == "punct" and raw == "{":
            index += 1
            if index < len(tokens) and tokens[index][1] == "}":
                close_end = tokens[index][3]
                index += 1
                return True, close_end, self_match
            matches_by_key: dict[str, int | None] = {}
            while index < len(tokens):
                if tokens[index][0] != "string":
                    return False, 0, None
                key_idx = index
                _, key_raw, key_start, _ = tokens[index]
                try:
                    key = json.loads(key_raw)
                except json.JSONDecodeError:
                    return False, 0, None
                if not isinstance(key, str):
                    return False, 0, None
                if not any(_is_unsafe_char(ch) for ch in key):
                    key_indices.add(key_idx)
                key_sensitive = (
                    _is_sensitive_key(key) or _has_sensitive_value(key) or len(key) > 64
                )
                index += 1
                if index >= len(tokens) or tokens[index][1] != ":":
                    return False, 0, None
                index += 1
                ok, val_end, child_match = parse_value([*path, key], depth + 1)
                if not ok:
                    return False, 0, None
                # Match json.loads last-key-wins semantics so dead overwritten
                # duplicate keys never hijack pointer locations.
                matches_by_key[key] = child_match
                if key_sensitive:
                    sensitive_spans.append((key_start, val_end))
                if index >= len(tokens):
                    return False, 0, None
                sep = tokens[index][1]
                sep_end = tokens[index][3]
                index += 1
                if sep == "}":
                    resolved = (
                        self_match
                        if self_match is not None
                        else next(
                            (pos for pos in matches_by_key.values() if pos is not None),
                            None,
                        )
                    )
                    return True, sep_end, resolved
                if sep != ",":
                    return False, 0, None
            return False, 0, None
        if kind == "punct" and raw == "[":
            index += 1
            if index < len(tokens) and tokens[index][1] == "]":
                close_end = tokens[index][3]
                index += 1
                return True, close_end, self_match
            item = 0
            arr_match = self_match
            while index < len(tokens):
                ok, _, child_match = parse_value([*path, str(item)], depth + 1)
                if not ok:
                    return False, 0, None
                if arr_match is None and child_match is not None:
                    arr_match = child_match
                item += 1
                if index >= len(tokens):
                    return False, 0, None
                sep = tokens[index][1]
                sep_end = tokens[index][3]
                index += 1
                if sep == "]":
                    return True, sep_end, arr_match
                if sep != ",":
                    return False, 0, None
            return False, 0, None
        return False, 0, None

    ok, _, found = parse_value([], 0)
    if not ok or index != len(tokens):
        return None
    return found, sensitive_spans, key_indices


def _proven_malformed_key_indices(
    tokens: Sequence[tuple[str, str, int, int]],
    decoded_strings: Mapping[int, str],
) -> set[int]:
    proven: set[int] = set()
    stack: list[str] = ["val"]

    def finish_value() -> bool:
        if not stack:
            return False
        top = stack[-1]
        if top == "val":
            stack[-1] = "done"
            return True
        if top == "obj_val":
            stack[-1] = "obj_comma_or_end"
            return True
        if top in ("arr_val_or_end", "arr_val"):
            stack[-1] = "arr_comma_or_end"
            return True
        return False

    for idx, (kind, raw, _, _) in enumerate(tokens):
        if not stack or len(stack) > MAX_JSON_LOCATE_DEPTH:
            break
        state = stack[-1]
        if state in ("val", "obj_val", "arr_val_or_end", "arr_val"):
            if kind == "punct" and raw == "]" and state == "arr_val_or_end":
                stack.pop()
                if not finish_value():
                    break
            elif kind == "punct" and raw == "{":
                stack.append("obj_key_or_end")
            elif kind == "punct" and raw == "[":
                stack.append("arr_val_or_end")
            elif kind in ("string", "atom"):
                if not finish_value():
                    break
            else:
                break
        elif state in ("obj_key_or_end", "obj_key"):
            if kind == "punct" and raw == "}" and state == "obj_key_or_end":
                stack.pop()
                if not finish_value():
                    break
            elif kind == "string":
                if idx + 1 < len(tokens) and tokens[idx + 1][1] == ":":
                    decoded = decoded_strings.get(idx)
                    if (
                        decoded is not None
                        and _SAFE_MALFORMED_KEY.fullmatch(decoded) is not None
                    ):
                        proven.add(idx)
                    stack[-1] = "obj_colon"
                else:
                    break
            else:
                break
        elif state == "obj_colon":
            if kind == "punct" and raw == ":":
                stack[-1] = "obj_val"
            else:
                break
        elif state == "obj_comma_or_end":
            if kind == "punct" and raw == ",":
                stack[-1] = "obj_key"
            elif kind == "punct" and raw == "}":
                stack.pop()
                if not finish_value():
                    break
            else:
                break
        elif state == "arr_comma_or_end":
            if kind == "punct" and raw == ",":
                stack[-1] = "arr_val"
            elif kind == "punct" and raw == "]":
                stack.pop()
                if not finish_value():
                    break
            else:
                break
        else:
            break
    return proven


def _reconstruct_excerpt_line(
    line_text: str,
    line_start: int,
    line_tokens: Sequence[tuple[int, str, str, int, int]],
    *,
    keep_string_indices: set[int],
    sensitive_spans: Sequence[tuple[int, int]],
    target_column: int | None,
) -> tuple[str, int | None] | None:
    line_end = line_start + len(line_text)
    for tok_idx, _, _, tok_start, tok_end in line_tokens:
        if tok_start < line_start or tok_end > line_end:
            return None
        _ = tok_idx
    for s_start, s_end in sensitive_spans:
        if s_start < line_end and s_end > line_start:
            return "<redacted>", None

    target_offset = (
        line_start + (target_column - 1) if target_column is not None else None
    )
    out: list[str] = []
    out_len = 0
    cursor = line_start
    adjusted_col: int | None = None

    def emit_plain(segment: str, seg_start: int) -> None:
        nonlocal out_len, adjusted_col
        for idx, ch in enumerate(segment):
            if target_offset is not None and seg_start + idx == target_offset:
                adjusted_col = out_len + 1
            rep = "  " if ch == "\t" else " " if _is_unsafe_char(ch) else ch
            out.append(rep)
            out_len += len(rep)

    for tok_idx, kind, raw, tok_start, tok_end in line_tokens:
        if tok_start > cursor:
            gap = line_text[cursor - line_start : tok_start - line_start]
            if any(ch not in " \t\r" for ch in gap):
                return None
            emit_plain(gap, cursor)
        if kind == "string" and tok_idx not in keep_string_indices:
            if target_offset is not None and tok_start <= target_offset < tok_end:
                adjusted_col = out_len + 1 if target_offset == tok_start else None
            replacement = '"<redacted>"'
            out.append(replacement)
            out_len += len(replacement)
        else:
            emit_plain(raw, tok_start)
        cursor = tok_end

    if cursor < line_end:
        tail = line_text[cursor - line_start :]
        if any(ch not in " \t\r" for ch in tail):
            return None
        emit_plain(tail, cursor)
    if target_offset is not None and target_offset >= line_end and adjusted_col is None:
        adjusted_col = out_len + 1

    return _window_line("".join(out), adjusted_col)


def extract_json_excerpt(
    text: str,
    line: int,
    column: int | None = None,
    *,
    radius: int = 1,
) -> list[dict[str, Any]] | None:
    """Return a minimal fail-closed structural/redacted excerpt for local JSON."""
    if not text or line < 1 or len(text.encode("utf-8")) > MAX_JSON_LOCATE_BYTES:
        return None
    lf_lines = _split_lf_lines(text)
    if line > len(lf_lines):
        return None
    _, _, target_raw = lf_lines[line - 1]
    if column is None and len(target_raw) > 200:
        return None

    tokens = _tokenize_json(text)
    if tokens is None:
        return None

    parsed = _parse_json_structure(tokens)
    if parsed is not None:
        _, sensitive_spans, keep_string_indices = parsed
    else:
        decoded_strings: dict[int, str] = {}
        for idx, (kind, raw, _, _) in enumerate(tokens):
            if kind != "string":
                continue
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                return None
            if not isinstance(decoded, str):
                return None
            if _is_sensitive_key(decoded) or _has_sensitive_value(decoded):
                return None
            decoded_strings[idx] = decoded
        sensitive_spans = []
        keep_string_indices = _proven_malformed_key_indices(tokens, decoded_strings)

    start = max(1, line - radius)
    end = min(len(lf_lines), line + radius)
    excerpt: list[dict[str, Any]] = []
    for number in range(start, end + 1):
        is_target = number == line
        l_start, l_end, l_text = lf_lines[number - 1]
        l_tokens: list[tuple[int, str, str, int, int]] = []
        for idx, (kind, raw, t_start, t_end) in enumerate(tokens):
            if t_end <= l_start or t_start >= l_end:
                continue
            if t_start < l_start or t_end > l_end:
                return None
            l_tokens.append((idx, kind, raw, t_start, t_end))
        rebuilt_line = _reconstruct_excerpt_line(
            l_text,
            l_start,
            l_tokens,
            keep_string_indices=keep_string_indices,
            sensitive_spans=sensitive_spans,
            target_column=column if is_target else None,
        )
        if rebuilt_line is None:
            return None
        cleaned, col = rebuilt_line
        entry: dict[str, Any] = {
            "line": number,
            "text": cleaned,
            "highlight": is_target,
        }
        if is_target and col is not None:
            entry["column"] = col
        excerpt.append(entry)
    return excerpt


def locate_json_pointer_line(text: str, pointer: str) -> tuple[int, int] | None:
    """Return exact 1-based (line, column) for an existing JSON Pointer in ``text``."""
    segments = _pointer_segments(pointer)
    if (
        segments is None
        or not text
        or len(text.encode("utf-8")) > MAX_JSON_LOCATE_BYTES
    ):
        return None
    tokens = _tokenize_json(text)
    if tokens is None:
        return None
    parsed = _parse_json_structure(tokens, target_segments=segments)
    if parsed is None or parsed[0] is None:
        return None
    found = parsed[0]
    line = text.count("\n", 0, found) + 1
    last_nl = text.rfind("\n", 0, found)
    return line, found - last_nl
