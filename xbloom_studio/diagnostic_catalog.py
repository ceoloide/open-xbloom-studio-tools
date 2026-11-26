"""Canonical diagnostic catalog, spec metadata and schema validation diagnostics."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .json_excerpt import extract_json_excerpt, locate_json_pointer_line
from .redaction import (
    MAX_EXCERPT_WIDTH,
    MAX_SANITISED_TEXT,
    MAX_SUMMARY_WIDTH,
    _is_sensitive_pointer,
    _truncate,
    _truncate_line,
)

DOCS_LOCAL_PATH = "docs/diagnostics.md"


DOCS_BASE_URL = (
    "https://github.com/ceoloide/open-xbloom-studio-tools/blob/main/" + DOCS_LOCAL_PATH
)


LOCATION_KEYS = frozenset(
    {
        "kind",
        "command",
        "method",
        "tool",
        "parameter",
        "pointer",
        "path",
        "line",
        "column",
        "slot",
        "operation_id",
    }
)


REPAIR_GUIDE_KEYS = frozenset({"local", "url", "skill"})


CONTEXT_KEYS = frozenset({"summary", "excerpt"})


EXCERPT_LINE_KEYS = frozenset({"line", "text", "highlight", "column"})


DIAGNOSTIC_KEYS = frozenset(
    {"code", "cause", "location", "fix", "docs_url", "repair_guide", "context"}
)


_LOCAL_ONLY_DIAGNOSTIC_CODES = frozenset(
    {
        "XB_INPUT_READ_FAILED",
        "XB_MCP_UNKNOWN_TOOL",
        "XB_MCP_OUTPUT_CONTRACT",
        "XB_OPERATION_FAILED",
    }
)


class InputFileReadError(ValueError):
    """A local CLI JSON input file could not be read or decoded as UTF-8."""

    def __init__(self, message: str, *, display_path: str) -> None:
        super().__init__(message)
        self.display_path = display_path


class LocalJsonSyntaxError(json.JSONDecodeError):
    """A local CLI JSON input file contained invalid JSON syntax."""

    def __init__(self, msg: str, doc: str, pos: int, *, display_path: str) -> None:
        super().__init__(msg, doc, pos)
        self.display_path = display_path


@dataclass(frozen=True)
class DiagnosticContext:
    """Truthful invocation context attached by CLI, RPC or MCP adapters."""

    command: str | None = None
    method: str | None = None
    tool: str | None = None
    parameter: str | None = None
    pointer: str | None = None
    source_path: str | None = None
    source_text: str | None = None
    source_kind: str | None = None
    allow_excerpt: bool = False
    requested_command: str | None = None
    requested_preset: tuple[str, str, int] | None = None
    requested_slot: str | None = None
    requested_slots: frozenset[str] | None = None
    requested_acknowledge: str | None = None


@dataclass(frozen=True)
class DiagnosticSpec:
    """Static metadata for one canonical diagnostic code."""

    code: str
    error_code: str
    title: str
    default_cause: str
    default_fix: str
    skill: str | None = None

    @property
    def anchor(self) -> str:
        return self.code.lower()

    @property
    def docs_url(self) -> str:
        return f"{DOCS_BASE_URL}#{self.anchor}"

    def repair_guide(self) -> dict[str, str]:
        guide: dict[str, str] = {
            "local": f"{DOCS_LOCAL_PATH}#{self.anchor}-agent-repair",
            "url": f"{DOCS_BASE_URL}#{self.anchor}-agent-repair",
        }
        if self.skill is not None:
            guide["skill"] = self.skill
        return guide


SPECS: tuple[DiagnosticSpec, ...] = (
    DiagnosticSpec(
        code="XB_RECIPE_VALIDATION",
        error_code="validation_error",
        title="Recipe validation failed",
        default_cause="The recipe document failed authoring validation.",
        default_fix=(
            "Fix the indicated JSON Pointer fields using the allowed bounds from "
            "`recipe_constraints` (`xbloom-studio recipe constraints`), then "
            "validate and preview again."
        ),
        skill="skills/xbloom-studio-recipes/SKILL.md#draft-and-validate",
    ),
    DiagnosticSpec(
        code="XB_COMMAND_VALIDATION",
        error_code="validation_error",
        title="Machine command parameters failed validation",
        default_cause="The machine command parameters do not match the command schema.",
        default_fix=(
            "Inspect `machine_capabilities` (`xbloom-studio machine capabilities`) "
            "for this command's input schema, correct the indicated JSON Pointer "
            "fields, and resubmit."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#settings-and-scale",
    ),
    DiagnosticSpec(
        code="XB_JSON_SYNTAX",
        error_code="invalid_argument",
        title="Invalid JSON syntax",
        default_cause="The JSON input could not be parsed.",
        default_fix=(
            "Fix the JSON syntax at the reported line and column, then re-run "
            "the command. After any uncertain machine action, inspect history "
            "with the existing idempotency key instead of starting again."
        ),
        skill="skills/xbloom-studio-recipes/SKILL.md#draft-and-validate",
    ),
    DiagnosticSpec(
        code="XB_INPUT_READ_FAILED",
        error_code="invalid_argument",
        title="Unable to read local JSON input file",
        default_cause="The local JSON file could not be read or decoded as UTF-8.",
        default_fix=(
            "Verify that the path exists, points to a readable UTF-8 JSON file "
            "(not a directory), and try again."
        ),
    ),
    DiagnosticSpec(
        code="XB_SOCKET_PATH_TOO_LONG",
        error_code="invalid_argument",
        title="Daemon Unix socket path exceeds platform limit",
        default_cause="The configured Unix socket path is longer than the OS allows.",
        default_fix=(
            "Pass a shorter `--socket` path inside a private 0700 directory for "
            "both the daemon and clients."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#operate-the-machine",
    ),
    DiagnosticSpec(
        code="XB_INVALID_ARGUMENT",
        error_code="invalid_argument",
        title="Invalid command option or parameter",
        default_cause="A command option, method name, or argument value was rejected.",
        default_fix=(
            "Check the command usage, parameter bounds and `machine_capabilities` "
            "(or `recipe_validate` for recipes), then correct the invalid argument."
        ),
    ),
    DiagnosticSpec(
        code="XB_RECIPE_NOT_FOUND",
        error_code="not_found",
        title="Recipe or revision not found",
        default_cause="No saved recipe or requested revision matched the reference.",
        default_fix=(
            "List saved recipes with `xbloom-studio recipe list "
            "--include-temporary` (or `recipe_list(include_temporary=true)`), "
            "then pass the exact recipe ID, alias, or unambiguous name and revision."
        ),
        skill="skills/xbloom-studio-recipes/SKILL.md#store-and-find",
    ),
    DiagnosticSpec(
        code="XB_OPERATION_NOT_FOUND",
        error_code="not_found",
        title="Operation not found",
        default_cause="No recorded operation matched the requested operation ID.",
        default_fix=(
            "List recorded attempts with `xbloom-studio history list --page` "
            "(or `history_list`) or look up by key with `operation_lookup`, "
            "then pass the exact durable operation ID."
        ),
        skill="skills/xbloom-studio-history/SKILL.md#find-the-brew",
    ),
    DiagnosticSpec(
        code="XB_IMPORT_NOT_FOUND",
        error_code="not_found",
        title="Retained source import not found",
        default_cause="No retained recipe source import matched the requested ID.",
        default_fix=(
            "Verify the retained import ID returned by `recipe_import` or "
            "`recipe_import_export` before calling `recipe_import_get`."
        ),
        skill="skills/xbloom-studio-recipes/SKILL.md#import",
    ),
    DiagnosticSpec(
        code="XB_NOT_FOUND",
        error_code="not_found",
        title="Requested resource not found",
        default_cause=(
            "The requested recipe, operation, or retained import was not found."
        ),
        default_fix=(
            "Search `recipe_list`, `history_list`, or retained imports, then pass "
            "the exact ID and revision."
        ),
    ),
    DiagnosticSpec(
        code="XB_CONFLICT",
        error_code="conflict",
        title="State, revision, or idempotency conflict",
        default_cause="The request conflicted with current saved or machine state.",
        default_fix=(
            "Resolve ambiguous names with a recipe ID, reload changed revisions "
            "and previews before updating or starting, and keep idempotency keys "
            "bound to one exact attempt."
        ),
        skill="skills/xbloom-studio-recipes/SKILL.md#store-and-find",
    ),
    DiagnosticSpec(
        code="XB_SOCKET_EXISTS",
        error_code="conflict",
        title="Daemon socket or destination already exists",
        default_cause="A local Unix socket or target file already exists at that path.",
        default_fix=(
            "Check whether an active daemon owns the socket. Only for an "
            "abandoned same-user socket, run `xbloom-studio serve "
            "--recover-stale-socket`; never delete an active socket."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#operate-the-machine",
    ),
    DiagnosticSpec(
        code="XB_PRESET_WRITE_REFUSED",
        error_code="conflict",
        title="Preset write refused by journal policy",
        default_cause=(
            "The preset write policy refused this request before sending any frame."
        ),
        default_fix=(
            "Nothing was sent. Inspect `machine_preset_writes` (`xbloom-studio "
            "machine preset-writes`) for the slot's write ID, outcomes and exact "
            "contents; repeat only an identical write when `retry_allowed` is "
            "true, or ask the user before passing `acknowledge_incomplete`."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#settings-and-scale",
    ),
    DiagnosticSpec(
        code="XB_PRESET_WRITE_INCOMPLETE",
        error_code="preset_write_incomplete",
        title="Preset write incomplete; stored slot unverified",
        default_cause="The preset write did not confirm completion on the machine.",
        default_fix=(
            "Inspect the reported outcome, backup and progress, then disconnect "
            "and check `machine_preset_writes`. Never roll back automatically; "
            "resubmit only an identical single-slot write when `retry_allowed` "
            "is true."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#settings-and-scale",
    ),
    DiagnosticSpec(
        code="XB_MACHINE_BUSY",
        error_code="busy",
        title="Machine is busy with owned work",
        default_cause=(
            "Another operation, prepared upload, or lifecycle transition owns the "
            "machine; requests are never queued."
        ),
        default_fix=(
            "Inspect `machine_state` (`xbloom-studio machine state`); wait for, "
            "stop, or cancel the matching owned work before starting another "
            "command."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#start-watch-stop",
    ),
    DiagnosticSpec(
        code="XB_MACHINE_WATER_INSUFFICIENT",
        error_code="busy",
        title="Machine reports insufficient water",
        default_cause=(
            "The machine reported insufficient water for this action; nothing was sent."
        ),
        default_fix=(
            "Refill the water tank, then check `machine_state` (`xbloom-studio "
            "machine state`) until water is sufficient and the machine reports "
            "idle, disconnecting and reconnecting if no new report arrives. "
            "Then resubmit the identical request."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#settings-and-scale",
    ),
    DiagnosticSpec(
        code="XB_MACHINE_NOT_IDLE",
        error_code="busy",
        title="Machine has not reported an idle state",
        default_cause=(
            "The machine has not reported a current recognized idle state; "
            "nothing was sent."
        ),
        default_fix=(
            "Check `machine_state` (`xbloom-studio machine state`) and the "
            "physical machine, including its water. No owned work is running, "
            "so do not stop anything. If an awake machine sent no idle report "
            "after a reconnect, send `back_home` or `wake` with the user's "
            "permission, or disconnect and reconnect, then resubmit."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#operate-the-machine",
    ),
    DiagnosticSpec(
        code="XB_OWNER_LEASE_BUSY",
        error_code="busy",
        title="Database or machine owner lease is held",
        default_cause="Another daemon or direct session already owns this lease.",
        default_fix=(
            "Use the existing daemon over its socket or stop it cleanly before "
            "starting another daemon or direct command."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#operate-the-machine",
    ),
    DiagnosticSpec(
        code="XB_CONTROL_PERMISSION_REQUIRED",
        error_code="permission_required",
        title="Explicit control permission required",
        default_cause="This physical machine action requires explicit control opt-in.",
        default_fix=(
            "Confirm the user explicitly asked for this physical action, then "
            "pass `--allow-control` (`allow_control=true`)."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#permission-comes-first",
    ),
    DiagnosticSpec(
        code="XB_DIRECTORY_PERMISSION_REQUIRED",
        error_code="permission_required",
        title="Private 0700 directory permission required",
        default_cause="The lease or socket directory is not private to this user.",
        default_fix=(
            "Ensure the lease or socket directory is owned by the daemon user, "
            "writable, and set to mode 0700."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#operate-the-machine",
    ),
    DiagnosticSpec(
        code="XB_EXPERIMENTAL_REQUIRED",
        error_code="experimental_required",
        title="Explicit experimental opt-in required",
        default_cause=(
            "This route or command is not hardware-validated and requires "
            "experimental opt-in."
        ),
        default_fix=(
            "Check `machine_prepare` or `machine_capabilities`. Only pass "
            "`--allow-untested` (`allow_untested=true`) when the user explicitly "
            "accepts experimental behavior."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#permission-comes-first",
    ),
    DiagnosticSpec(
        code="XB_DAEMON_UNAVAILABLE",
        error_code="connection_unavailable",
        title="Configured local daemon is unavailable",
        default_cause="The local daemon socket is missing or refusing connections.",
        default_fix=(
            "Start or verify `xbloom-studio serve` and its `--socket` path. Do "
            "not open a competing Bluetooth connection."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#operate-the-machine",
    ),
    DiagnosticSpec(
        code="XB_TRANSPORT_UNCERTAIN",
        error_code="connection_unavailable",
        title="Transport failed; delivery may be uncertain",
        default_cause=(
            "The daemon, SSH relay, or transport did not complete the request."
        ),
        default_fix=(
            "Run read-only `operation_lookup` with the original idempotency key "
            "and check `machine_state` and history. Never automatically replay "
            "a start."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#after-any-uncertainty",
    ),
    DiagnosticSpec(
        code="XB_SESSION_UNAVAILABLE",
        error_code="connection_unavailable",
        title="Machine session unavailable or needs recovery",
        default_cause=(
            "No ready machine session is open, or the session requires recovery."
        ),
        default_fix=(
            "Inspect `machine_state` and the physical machine; explicitly "
            "disconnect and reconnect only when safe. Never replay a start "
            "automatically."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#after-any-uncertainty",
    ),
    DiagnosticSpec(
        code="XB_MACHINE_PROTOCOL_ERROR",
        error_code="machine_protocol_error",
        title="Machine reported a protocol error",
        default_cause=(
            "The machine returned a protocol error; physical effects may be uncertain."
        ),
        default_fix=(
            "Inspect `machine_state`, `history_get`, and the physical machine. "
            "Reuse an existing idempotency key only to inspect its attempt; "
            "never force a new start."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#after-any-uncertainty",
    ),
    DiagnosticSpec(
        code="XB_PROVIDER_ERROR",
        error_code="provider_error",
        title="Public recipe provider rejected or failed request",
        default_cause=(
            "The public recipe provider rejected the request or returned "
            "unsupported data."
        ),
        default_fix=(
            "Check `provider_capabilities` and the public URL; inspect any "
            "retained source import with `recipe_import_get` instead of guessing "
            "a conversion."
        ),
        skill="skills/xbloom-studio-recipes/SKILL.md#import",
    ),
    DiagnosticSpec(
        code="XB_PROVIDER_UNAVAILABLE",
        error_code="provider_unavailable",
        title="Public recipe provider unreachable",
        default_cause=(
            "The public recipe provider could not be reached over the network."
        ),
        default_fix=(
            "Check network and provider availability; local recipes and cached "
            "history remain usable offline."
        ),
        skill="skills/xbloom-studio-recipes/SKILL.md#import",
    ),
    DiagnosticSpec(
        code="XB_RESPONSE_TOO_LARGE",
        error_code="response_too_large",
        title="RPC response exceeded size limit",
        default_cause="The serialized RPC response exceeded the transport frame limit.",
        default_fix=(
            "Use cursor pagination (`--page` / `--limit` / `detail=compact`). "
            "If an operation start was accepted, inspect `history_get` with the "
            "reported `operation_id` and never start again with a new key."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#after-any-uncertainty",
    ),
    DiagnosticSpec(
        code="XB_MCP_UNKNOWN_TOOL",
        error_code="unknown_tool",
        title="Unknown MCP tool in configured groups",
        default_cause="The requested MCP tool is not registered in this server.",
        default_fix=(
            "List the server's advertised tools or configure `xbloom-studio-mcp` "
            "with the required `--group` (`recipes`, `history`, or `machine`)."
        ),
    ),
    DiagnosticSpec(
        code="XB_MCP_OUTPUT_CONTRACT",
        error_code="output_contract_error",
        title="Unexpected application output shape",
        default_cause=(
            "The application returned an unexpected result shape; effects may "
            "already have occurred."
        ),
        default_fix=(
            "Inspect `history_get` with any reported `operation_id`, "
            "`operation_lookup` with the original key, and `machine_state`; "
            "check server logs using the correlation ID. Never force a new start."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#after-any-uncertainty",
    ),
    DiagnosticSpec(
        code="XB_OPERATION_FAILED",
        error_code="operation_failed",
        title="Watched machine operation did not complete",
        default_cause=(
            "The watched machine operation ended without observed completion."
        ),
        default_fix=(
            "Inspect read-only `history_get` (`xbloom-studio history get ID`), "
            "`history_events` (`xbloom-studio history events ID`), `machine_state` "
            "(`xbloom-studio machine state`), and the physical machine. Never "
            "automatically replay a start with a new idempotency key."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#after-any-uncertainty",
    ),
    DiagnosticSpec(
        code="XB_INTERNAL_ERROR",
        error_code="internal_error",
        title="Unexpected internal error",
        default_cause="An unexpected internal error prevented a reliable response.",
        default_fix=(
            "Check service logs with the correlation ID and inspect "
            "`operation_lookup`, `machine_state`, and history before retrying "
            "any machine action."
        ),
        skill="skills/xbloom-studio-operate/SKILL.md#after-any-uncertainty",
    ),
)


CATALOG: dict[str, DiagnosticSpec] = {spec.code: spec for spec in SPECS}

PRESET_TRANSPORT_UNCERTAIN_FIX = (
    "The preset write may have reached the machine. Inspect read-only "
    "`machine_preset_writes` (`xbloom-studio machine preset-writes`) for the "
    "slot's write ID and outcome, `machine_state`, and the physical machine. "
    "Never resend a preset write automatically."
)


def preset_transport_fix(context: DiagnosticContext | None) -> str | None:
    """Return the transport recovery for a preset-write request, else None."""
    if context is not None and context.requested_command in (
        "write_preset",
        "write_presets",
    ):
        return PRESET_TRANSPORT_UNCERTAIN_FIX
    return None


FALLBACK_BY_ERROR_CODE: dict[str, str] = {
    "validation_error": "XB_RECIPE_VALIDATION",
    "not_found": "XB_NOT_FOUND",
    "conflict": "XB_CONFLICT",
    "busy": "XB_MACHINE_BUSY",
    "experimental_required": "XB_EXPERIMENTAL_REQUIRED",
    "invalid_argument": "XB_INVALID_ARGUMENT",
    "connection_unavailable": "XB_TRANSPORT_UNCERTAIN",
    "machine_protocol_error": "XB_MACHINE_PROTOCOL_ERROR",
    "preset_write_incomplete": "XB_PRESET_WRITE_INCOMPLETE",
    "permission_required": "XB_CONTROL_PERMISSION_REQUIRED",
    "internal_error": "XB_INTERNAL_ERROR",
    "provider_error": "XB_PROVIDER_ERROR",
    "provider_unavailable": "XB_PROVIDER_UNAVAILABLE",
    "response_too_large": "XB_RESPONSE_TOO_LARGE",
    "unknown_tool": "XB_MCP_UNKNOWN_TOOL",
    "output_contract_error": "XB_MCP_OUTPUT_CONTRACT",
}


def _sequence_values(value: Any, *, limit: int = 8) -> str | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    if not value:
        return None
    items = [str(v) for v in value[:limit] if type(v) in (str, int, float, bool)]
    if not items:
        return None
    text = ", ".join(items)
    if len(value) > limit:
        text += ", ..."
    return text


def _format_expected(expected: Any) -> str | None:
    if not isinstance(expected, Mapping):
        return None
    parts: list[str] = []
    enum_vals = _sequence_values(expected.get("enum"))
    if enum_vals is not None:
        parts.append(f"one of [{enum_vals}]")
    elif (
        "minimum" in expected
        and "maximum" in expected
        and type(expected["minimum"]) in (int, float)
        and type(expected["maximum"]) in (int, float)
    ):
        raw_kind = expected.get("type", "number")
        kind = (
            "integer"
            if raw_kind == "tea_temperature"
            else raw_kind
            if type(raw_kind) is str and raw_kind.strip()
            else "number"
        )
        bound = f"{kind} {expected['minimum']}..{expected['maximum']}"
        alts = _sequence_values(expected.get("enum_alternatives"))
        if alts is not None:
            bound += f" or {alts}"
        excl = _sequence_values(expected.get("numeric_exclusions"))
        if excl is not None:
            bound += f" (excluding {excl})"
        parts.append(bound)
    elif (
        "min_items" in expected
        and "max_items" in expected
        and type(expected["min_items"]) is int
        and type(expected["max_items"]) is int
    ):
        parts.append(f"{expected['min_items']}..{expected['max_items']} items")
    elif (
        "minItems" in expected
        and "maxItems" in expected
        and type(expected["minItems"]) is int
        and type(expected["maxItems"]) is int
    ):
        parts.append(f"{expected['minItems']}..{expected['maxItems']} items")
    elif (fields := _sequence_values(expected.get("allowed_fields"))) is not None:
        parts.append(f"fields [{fields}]")
    elif (props := _sequence_values(expected.get("properties"))) is not None:
        parts.append(f"properties [{props}]")
    elif "total_ml" in expected and type(expected["total_ml"]) in (int, float):
        parts.append(f"total_ml={expected['total_ml']}")
    elif "steep_count" in expected and type(expected["steep_count"]) is int:
        parts.append(f"steep_count={expected['steep_count']}")
    elif (
        "type" in expected
        and type(expected["type"]) is str
        and expected["type"].strip()
    ):
        parts.append(expected["type"])
    return "; ".join(parts) if parts else None


def _summarize_validation_issue(
    issues: Sequence[Any],
) -> tuple[str | None, str | None]:
    valid_issues = [item for item in issues if isinstance(item, Mapping)]
    if not valid_issues:
        return None, None
    first = valid_issues[0]
    pointer = first.get("path")
    pointer_str = pointer if isinstance(pointer, str) else None
    bits: list[str] = []
    is_missing = first.get("actual_type") == "missing" or (
        first.get("code") in ("missing", "missing_field")
        and first.get("value", "<missing>") == "<missing>"
    )
    if is_missing:
        bits.append("missing required field")
    elif "value" in first:
        val = first["value"]
        if _is_sensitive_pointer(pointer_str):
            bits.append("got <redacted>")
        elif type(val) is bool:
            bits.append(f"got {'true' if val else 'false'}")
        elif type(val) in (int, float):
            bits.append(f"got {val}")
        elif val is None:
            bits.append("got null")
        elif isinstance(val, str):
            bits.append("got <string>")
        else:
            bits.append(f"got <{type(val).__name__}>")
    expected = _format_expected(first.get("expected")) or _format_expected(
        first.get("allowed")
    )
    if expected:
        bits.append(f"expected {expected}")
    if len(valid_issues) > 1:
        bits.append(f"1 of {len(valid_issues)} errors")
    summary = _truncate("; ".join(bits), MAX_SUMMARY_WIDTH) if bits else None
    return pointer_str, summary


def build_diagnostic(
    code: str,
    *,
    cause: str | None = None,
    fix: str | None = None,
    location: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct one canonical diagnostic dictionary from a catalog code."""
    spec = CATALOG[code]
    result: dict[str, Any] = {
        "code": spec.code,
        "cause": _truncate(cause or spec.default_cause, MAX_SANITISED_TEXT),
        "fix": _truncate(fix or spec.default_fix, MAX_SANITISED_TEXT),
        "docs_url": spec.docs_url,
        "repair_guide": spec.repair_guide(),
    }
    clean_loc = _clean_location(location)
    if clean_loc:
        result["location"] = clean_loc
    clean_ctx = _clean_context(context)
    if clean_ctx:
        result["context"] = clean_ctx
    return result


def _clean_location(location: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(location, Mapping):
        return None
    cleaned: dict[str, Any] = {}
    for key, value in location.items():
        if key not in LOCATION_KEYS or value is None:
            continue
        if key in ("line", "column"):
            if type(value) is int and value >= 1:
                cleaned[key] = value
        elif isinstance(value, str):
            text = _truncate(value, MAX_SANITISED_TEXT)
            if text or key == "pointer":
                cleaned[key] = text
    return cleaned or None


def _clean_context(context: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(context, Mapping):
        return None
    cleaned: dict[str, Any] = {}
    summary = context.get("summary")
    if isinstance(summary, str) and summary.strip():
        cleaned["summary"] = _truncate(summary, MAX_SUMMARY_WIDTH)
    excerpt = context.get("excerpt")
    if isinstance(excerpt, list) and excerpt:
        lines: list[dict[str, Any]] = []
        for item in excerpt[:5]:
            if (
                isinstance(item, Mapping)
                and type(item.get("line")) is int
                and item["line"] >= 1
                and isinstance(item.get("text"), str)
                and type(item.get("highlight")) is bool
            ):
                entry: dict[str, Any] = {
                    "line": item["line"],
                    "text": _truncate_line(item["text"], MAX_EXCERPT_WIDTH),
                    "highlight": item["highlight"],
                }
                col = item.get("column")
                if type(col) is int and col >= 1:
                    entry["column"] = col
                lines.append(entry)
        if lines:
            cleaned["excerpt"] = lines
    return cleaned or None


def has_valid_diagnostic_types(raw: Any) -> bool:
    """Strictly check nested field types of a candidate diagnostic mapping."""
    if not isinstance(raw, Mapping):
        return False
    if "code" not in raw or type(raw["code"]) is not str:
        return False
    for text_key in ("cause", "fix", "docs_url"):
        if text_key in raw and type(raw[text_key]) is not str:
            return False
    if "repair_guide" in raw:
        guide = raw["repair_guide"]
        if not isinstance(guide, Mapping):
            return False
        for gk, gv in guide.items():
            if type(gk) is not str or type(gv) is not str:
                return False
    if "location" in raw and raw["location"] is not None:
        loc = raw["location"]
        if not isinstance(loc, Mapping):
            return False
        for lk, lv in loc.items():
            if type(lk) is not str:
                return False
            if lk not in LOCATION_KEYS or lv is None:
                continue
            if lk in ("line", "column"):
                if type(lv) is not int or lv < 1:
                    return False
            elif type(lv) is not str:
                return False
    if "context" in raw and raw["context"] is not None:
        ctx = raw["context"]
        if not isinstance(ctx, Mapping):
            return False
        if (
            "summary" in ctx
            and ctx["summary"] is not None
            and type(ctx["summary"]) is not str
        ):
            return False
        if "excerpt" in ctx and ctx["excerpt"] is not None:
            excerpt = ctx["excerpt"]
            if not isinstance(excerpt, list):
                return False
            for item in excerpt:
                if (
                    not isinstance(item, Mapping)
                    or type(item.get("line")) is not int
                    or item["line"] < 1
                    or type(item.get("text")) is not str
                    or type(item.get("highlight")) is not bool
                ):
                    return False
                if (
                    "column" in item
                    and item["column"] is not None
                    and (type(item["column"]) is not int or item["column"] < 1)
                ):
                    return False
    return True


def _valid_pointer_value(value: Any) -> str | None:
    if type(value) is not str:
        return None
    if value == "" or value.startswith("/"):
        return _truncate(value, MAX_SANITISED_TEXT)
    return None


def validation_diagnostic(
    errors: Sequence[Any],
    *,
    command_validation: bool = False,
    tea: bool = False,
    context: DiagnosticContext | None = None,
    trusted: bool = True,
) -> dict[str, Any]:
    """Build a canonical diagnostic for recipe or machine-command validation."""
    code = "XB_COMMAND_VALIDATION" if command_validation else "XB_RECIPE_VALIDATION"
    valid_errors = [item for item in errors if isinstance(item, Mapping)]
    pointer, summary = _summarize_validation_issue(valid_errors)
    if not trusted:
        pointer = _valid_pointer_value(pointer)
    cause = None
    fix = None
    if valid_errors:
        first = valid_errors[0]
        label = pointer or "/"
        msg = first.get("message")
        if isinstance(msg, str) and msg.strip():
            cause = f"{label}: {msg}"
        if trusted and tea and isinstance(first.get("repair_hint"), str):
            fix = first["repair_hint"]
    location: dict[str, Any] = {}
    if pointer is not None:
        location["kind"] = "json_pointer"
        location["pointer"] = pointer
    if context is not None:
        if context.command:
            location["command"] = context.command
        if context.method:
            location["method"] = context.method
        if context.tool:
            location["tool"] = context.tool
    ctx_payload: dict[str, Any] = {}
    if summary:
        ctx_payload["summary"] = summary
    diagnostic = build_diagnostic(
        code,
        cause=cause,
        fix=fix,
        location=location or None,
        context=ctx_payload or None,
    )
    if context is not None:
        _apply_local_pointer_excerpt(diagnostic, context, pointer)
    return diagnostic


def _map_pointer_to_source(
    diag_code: str, pointer: str, context: DiagnosticContext
) -> str | None:
    if not context.allow_excerpt or not context.source_path or not context.source_text:
        return None
    if context.source_kind == "recipe_document":
        return pointer if diag_code == "XB_RECIPE_VALIDATION" else None
    if context.source_kind == "command_params":
        if diag_code != "XB_COMMAND_VALIDATION":
            return None
        if pointer == "/params":
            return ""
        if pointer.startswith("/params/"):
            return pointer.removeprefix("/params")
        return None
    if context.source_kind == "method_params":
        if diag_code == "XB_COMMAND_VALIDATION" and context.method == "machine_command":
            return pointer
        if diag_code == "XB_RECIPE_VALIDATION":
            if context.method in (
                "recipe_validate",
                "recipe_tea_validate",
                "recipe_preview",
                "recipe_create",
                "recipe_update",
            ):
                return "/document" if pointer == "" else "/document" + pointer
            if context.method in ("operation_start", "recipe_upload"):
                return (
                    "/request/document"
                    if pointer == ""
                    else "/request/document" + pointer
                )
        return None
    return None


def _apply_local_pointer_excerpt(
    diagnostic: dict[str, Any],
    context: DiagnosticContext,
    pointer: str | None,
) -> None:
    if pointer is None:
        return
    lookup_pointer = _map_pointer_to_source(diagnostic["code"], pointer, context)
    if lookup_pointer is None:
        return
    assert context.source_text is not None and context.source_path is not None
    located = locate_json_pointer_line(context.source_text, lookup_pointer)
    if located is None:
        return
    line, col = located
    location = diagnostic.setdefault("location", {"kind": "json_pointer"})
    location["path"] = context.source_path
    location["line"] = line
    location["column"] = col
    excerpt = extract_json_excerpt(context.source_text, line, col)
    if excerpt:
        diagnostic.setdefault("context", {})["excerpt"] = excerpt


def attach_local_context(
    diagnostic: dict[str, Any], context: DiagnosticContext | None
) -> dict[str, Any]:
    """Enrich an existing diagnostic with truthful local CLI/RPC/MCP context."""
    if context is None:
        return diagnostic
    enriched = dict(diagnostic)
    location = dict(enriched.get("location") or {})
    if context.command and "command" not in location:
        location["command"] = context.command
    if context.method and "method" not in location:
        location["method"] = context.method
    if context.tool and "tool" not in location:
        location["tool"] = context.tool
    if context.parameter and "parameter" not in location and "pointer" not in location:
        location["parameter"] = context.parameter
    if location and "kind" not in location:
        if "command" in location:
            location["kind"] = "command"
        elif "tool" in location:
            location["kind"] = "tool"
        else:
            location["kind"] = "method"
    if location:
        enriched["location"] = location
    pointer = location.get("pointer")
    if isinstance(pointer, str):
        _apply_local_pointer_excerpt(enriched, context, pointer)
    elif (
        context.allow_excerpt
        and context.source_path
        and enriched["code"]
        in (
            "XB_JSON_SYNTAX",
            "XB_INPUT_READ_FAILED",
        )
    ):
        enriched.setdefault("location", {"kind": "file"})["path"] = context.source_path
    return enriched
