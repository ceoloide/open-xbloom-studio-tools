"""Safe application and RPC error contracts, independent of the optional MCP SDK.

Known domain errors keep their message and recovery guidance; anything else
becomes an ``internal_error`` with a correlation ID and no internal detail.
No guidance ever suggests automatically repeating a physical action.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from urllib.error import HTTPError, URLError

from .client import ProtocolCommandError, SessionError
from .command_schema import CommandValidationError
from .control import BusyError, IdleStateRequiredError, InsufficientWaterError
from .diagnostics import (
    CATALOG,
    DiagnosticContext,
    InputFileReadError,
    LocalJsonSyntaxError,
    build_diagnostic,
    extract_json_excerpt,
    preset_transport_fix,
    preset_write_diagnostic,
    sanitize_remote_error,
    validation_diagnostic,
)
from .machine_controls import (
    ControlPermissionError,
    ExperimentalOptInError,
    PresetModeRestoreError,
    PresetWriteError,
    UntestedOperationError,
)
from .ownership import LeaseBusyError, LeaseDirectoryError
from .preset_writes import PresetWriteRefused
from .providers import ProviderError
from .recipe_validation import RecipeValidationError
from .rpc_types import RemoteError, SocketDirectoryError, SocketPathError
from .storage import ConflictError, NotFoundError, NotFoundResource

CODES = frozenset(
    {
        "validation_error",
        "not_found",
        "conflict",
        "busy",
        "experimental_required",
        "invalid_argument",
        "connection_unavailable",
        "machine_protocol_error",
        "preset_write_incomplete",
        "permission_required",
        "internal_error",
        "provider_error",
        "provider_unavailable",
        "response_too_large",
    }
)
FIELDS = (
    "type",
    "code",
    "message",
    "details",
    "next_step",
    "retryable",
    "correlation_id",
    "diagnostic",
)
_NOT_FOUND_DIAGNOSTIC_CODES: dict[NotFoundResource, str] = {
    "recipe": "XB_RECIPE_NOT_FOUND",
    "operation": "XB_OPERATION_NOT_FOUND",
    "import": "XB_IMPORT_NOT_FOUND",
}


def _detail(
    name: str,
    code: str,
    message: str,
    next_step: str,
    details: dict[str, Any] | None = None,
    *,
    diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "type": name,
        "code": code,
        "message": message,
        "details": details or {},
        "next_step": next_step,
        "retryable": False,
    }
    if diagnostic is not None:
        envelope["diagnostic"] = diagnostic
    return envelope


def _context_location(context: DiagnosticContext | None) -> dict[str, Any] | None:
    if context is None:
        return None
    location: dict[str, Any] = {}
    if context.command:
        location["command"] = context.command
    if context.method:
        location["method"] = context.method
    if context.tool:
        location["tool"] = context.tool
    if context.parameter:
        location["parameter"] = context.parameter
    if context.pointer is not None:
        location["pointer"] = context.pointer
        location["kind"] = "json_pointer"
    elif "command" in location:
        location["kind"] = "command"
    elif "tool" in location:
        location["kind"] = "tool"
    elif "method" in location:
        location["kind"] = "method"
    return location or None


def _internal(context: DiagnosticContext | None = None) -> dict[str, Any]:
    message = "An internal error prevented a reliable response."
    next_step = (
        "Check service logs with the correlation ID and inspect history before "
        "retrying any machine action."
    )
    return {
        **_detail(
            "InternalError",
            "internal_error",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_INTERNAL_ERROR",
                cause=message,
                fix=next_step,
                location=_context_location(context),
            ),
        ),
        "correlation_id": uuid.uuid4().hex,
    }


def _validate_remote_envelope(detail: Any) -> dict[str, Any] | None:
    if not isinstance(detail, dict):
        return None
    code = detail.get("code")
    if type(code) is not str or code not in CODES:
        return None
    filtered: dict[str, Any] = {}
    for key in FIELDS:
        if key not in detail:
            continue
        value = detail[key]
        if key in ("type", "code", "message", "next_step", "correlation_id"):
            if type(value) is not str:
                return None
        elif key == "retryable":
            if type(value) is not bool:
                return None
        elif key == "details":
            if not isinstance(value, dict):
                return None
        filtered[key] = value
    return filtered


def error_detail(
    exc: BaseException,
    *,
    log_unexpected: bool = True,
    context: DiagnosticContext | None = None,
) -> dict[str, Any]:
    """Return known domain errors with recovery guidance; hide unexpected internals.

    Preset backup identity and progress are deliberately retained. No retry
    advice can turn an uncertain machine action into another physical attempt.
    """
    if isinstance(exc, RemoteError):
        filtered = _validate_remote_envelope(exc.detail)
        if filtered is not None:
            return sanitize_remote_error(filtered, context=context)
        return _internal(context)
    name = type(exc).__name__
    location = _context_location(context)
    if isinstance(exc, (RecipeValidationError, CommandValidationError)):
        command_validation = isinstance(exc, CommandValidationError)
        next_step = (
            "Correct the indicated JSON Pointer fields using their allowed values; "
            "preview again."
        )
        raw_errors = exc.report.get("errors", [])
        validation_errors = (
            [item for item in raw_errors if isinstance(item, dict)]
            if isinstance(raw_errors, list)
            else []
        )
        return _detail(
            name,
            "validation_error",
            str(exc),
            next_step,
            {"validation": exc.report},
            diagnostic=validation_diagnostic(
                validation_errors,
                command_validation=command_validation,
                context=context,
            ),
        )
    if isinstance(exc, PresetWriteError):
        message = "Preset write did not finish; stored contents are unverified."
        next_step = (
            "Inspect the outcome, backup and progress; disconnect and inspect the "
            "machine before recovery. Never roll back automatically; repeat only an "
            "identical single-slot write that machine_preset_writes reports as "
            "retry_allowed."
        )
        if isinstance(exc, PresetModeRestoreError):
            spec = CATALOG["XB_PRESET_MODE_NOT_RESTORED"]
            message, next_step = spec.default_cause, spec.default_fix
        return _detail(
            name,
            "preset_write_incomplete",
            message,
            next_step,
            {"preset_write": exc.result},
            diagnostic=preset_write_diagnostic(
                refused=False, cause=message, payload=exc.result, context=context
            ),
        )
    if isinstance(exc, PresetWriteRefused):
        message = str(exc)
        diag = preset_write_diagnostic(
            refused=True, cause=message, payload=exc.write, context=context
        )
        return _detail(
            name,
            "conflict",
            message,
            diag["fix"],
            {"preset_write": exc.write},
            diagnostic=diag,
        )
    if isinstance(exc, ControlPermissionError):
        message = str(exc)
        next_step = (
            "Confirm the user explicitly asked for this physical action, then "
            "pass `--allow-control` (`allow_control=true`)."
        )
        return _detail(
            name,
            "permission_required",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_CONTROL_PERMISSION_REQUIRED",
                cause=message,
                fix=next_step,
                location=location,
            ),
        )
    if isinstance(exc, (UntestedOperationError, ExperimentalOptInError)):
        message = str(exc)
        next_step = (
            "Only set allow_untested=true if experimental behavior is explicitly "
            "intended."
        )
        return _detail(
            name,
            "experimental_required",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_EXPERIMENTAL_REQUIRED",
                cause=message,
                fix=next_step,
                location=location,
            ),
        )
    if isinstance(exc, ProtocolCommandError):
        message = "Machine reported a protocol error; command effects may be uncertain."
        next_step = (
            "Inspect machine_state/history and the physical machine. Reuse an "
            "existing idempotency key to inspect its attempt; never force a new "
            "start."
        )
        summary_bits: list[str] = []
        pending = exc.details.get("pending_command")
        if type(pending) is int:
            summary_bits.append(f"protocol_command={pending}")
        for k, v in exc.details.items():
            if len(summary_bits) >= 4:
                break
            if (
                isinstance(k, str)
                and k != "pending_command"
                and isinstance(v, (str, int, float, bool))
            ):
                summary_bits.append(f"{k}={v}")
        return _detail(
            name,
            "machine_protocol_error",
            message,
            next_step,
            {"protocol": exc.details},
            diagnostic=build_diagnostic(
                "XB_MACHINE_PROTOCOL_ERROR",
                cause=message,
                fix=next_step,
                location=location,
                context={"summary": "; ".join(summary_bits)} if summary_bits else None,
            ),
        )
    if isinstance(exc, LeaseBusyError):
        message = str(exc)
        next_step = (
            "Use the existing owner or stop it cleanly before starting a new service."
        )
        return _detail(
            name,
            "busy",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_OWNER_LEASE_BUSY", cause=message, fix=next_step, location=location
            ),
        )
    if isinstance(exc, LeaseDirectoryError):
        message = str(exc)
        next_step = (
            "Check ownership and mode 0700 for the lease directory under "
            "the account's home. Ensure the daemon user can write there."
        )
        return _detail(
            name,
            "permission_required",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_DIRECTORY_PERMISSION_REQUIRED",
                cause=message,
                fix=next_step,
                location=location,
            ),
        )
    if isinstance(exc, SocketDirectoryError):
        message = str(exc)
        next_step = (
            "Select a private socket directory owned by the daemon user (mode 0700)."
        )
        return _detail(
            name,
            "permission_required",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_DIRECTORY_PERMISSION_REQUIRED",
                cause=message,
                fix=next_step,
                location=location,
            ),
        )
    if isinstance(exc, SocketPathError):
        message = str(exc)
        next_step = (
            "Use a shorter --socket path in a private directory for the daemon "
            "and clients."
        )
        return _detail(
            name,
            "invalid_argument",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_SOCKET_PATH_TOO_LONG",
                cause=message,
                fix=next_step,
                location=location,
            ),
        )
    if isinstance(exc, (InsufficientWaterError, IdleStateRequiredError)):
        message = str(exc)
        diag = build_diagnostic(
            "XB_MACHINE_WATER_INSUFFICIENT"
            if isinstance(exc, InsufficientWaterError)
            else "XB_MACHINE_NOT_IDLE",
            cause=message,
            location=location,
        )
        return _detail(name, "busy", message, diag["fix"], diagnostic=diag)
    if isinstance(exc, BusyError):
        message = str(exc)
        next_step = (
            "Inspect machine_state; finish, stop or cancel the matching owned work "
            "before another command."
        )
        return _detail(
            name,
            "busy",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_MACHINE_BUSY", cause=message, fix=next_step, location=location
            ),
        )
    if isinstance(exc, ConflictError):
        message = str(exc)
        next_step = (
            "Use a recipe ID to resolve ambiguous names, reload changed revisions, "
            "and preserve keys for the same intended attempt."
        )
        return _detail(
            name,
            "conflict",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_CONFLICT", cause=message, fix=next_step, location=location
            ),
        )
    if isinstance(exc, NotFoundError):
        message = str(exc.args[0])
        next_step = (
            "Search recipe_list/history_list or retained imports, then use the "
            "exact ID and revision."
        )
        diag_code = _NOT_FOUND_DIAGNOSTIC_CODES[exc.resource]
        return _detail(
            name,
            "not_found",
            message,
            next_step,
            diagnostic=build_diagnostic(diag_code, cause=message, location=location),
        )
    if isinstance(exc, ProviderError):
        message = str(exc)
        next_step = (
            "Check provider_capabilities and the supported public recipe URL; "
            "retain blocked source data rather than guessing a conversion."
        )
        return _detail(
            name,
            "provider_error",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_PROVIDER_ERROR", cause=message, fix=next_step, location=location
            ),
        )
    if isinstance(exc, HTTPError):
        message = "The public recipe provider rejected the request."
        next_step = (
            "Check the public URL and provider availability; no credentials or "
            "machine action were retried."
        )
        return _detail(
            name,
            "provider_error",
            message,
            next_step,
            {"http_status": exc.code},
            diagnostic=build_diagnostic(
                "XB_PROVIDER_ERROR",
                cause=message,
                fix=next_step,
                location=location,
                context={"summary": f"http_status={exc.code}"},
            ),
        )
    if isinstance(exc, URLError):
        message = "The public recipe provider could not be reached."
        next_step = (
            "Check network/provider availability; local recipes and cached history "
            "remain available."
        )
        return _detail(
            name,
            "provider_unavailable",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_PROVIDER_UNAVAILABLE",
                cause=message,
                fix=next_step,
                location=location,
            ),
        )
    if isinstance(exc, SessionError):
        message = str(exc)
        next_step = (
            "Inspect cached state and the machine; explicitly disconnect/reconnect "
            "only when appropriate. Do not replay a start automatically."
        )
        return _detail(
            name,
            "connection_unavailable",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_SESSION_UNAVAILABLE",
                cause=message,
                fix=next_step,
                location=location,
            ),
        )
    if isinstance(exc, FileExistsError):
        message = "A local socket or destination already exists."
        next_step = (
            "Inspect its owner/destination. For an abandoned daemon socket use "
            "xbloom-studio serve --recover-stale-socket; never remove an active "
            "socket."
        )
        return _detail(
            name,
            "conflict",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_SOCKET_EXISTS", cause=message, fix=next_step, location=location
            ),
        )
    if isinstance(exc, (FileNotFoundError, ConnectionRefusedError)):
        message = "The configured local daemon is unavailable."
        next_step = (
            "Start or check xbloom-studio serve and its configured socket. Do not "
            "open a competing Bluetooth connection."
        )
        return _detail(
            name,
            "connection_unavailable",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_DAEMON_UNAVAILABLE",
                cause=message,
                fix=next_step,
                location=location,
            ),
        )
    if isinstance(exc, (TimeoutError, ConnectionError)):
        message = "The daemon or transport did not complete the request."
        preset_fix = preset_transport_fix(context)
        next_step = preset_fix or (
            "Check operation_lookup with the original idempotency key, "
            "machine_state and history. An accepted operation may continue. Do not "
            "automatically replay a start: an absent key can still start if the "
            "original was never accepted."
        )
        return _detail(
            name,
            "connection_unavailable",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_TRANSPORT_UNCERTAIN",
                cause=message,
                fix=preset_fix,
                location=location,
            ),
        )
    if isinstance(exc, json.JSONDecodeError):
        next_step = (
            "Correct the JSON syntax and retry the same read or inspect the "
            "existing operation key."
        )
        parse_loc: dict[str, Any] = {
            **(location or {}),
            "kind": "json_parse",
            "line": exc.lineno,
            "column": exc.colno,
        }
        parse_ctx: dict[str, Any] | None = None
        if isinstance(exc, LocalJsonSyntaxError):
            message = (
                f"Invalid JSON input: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno})."
            )
            parse_loc["path"] = exc.display_path
            excerpt = extract_json_excerpt(exc.doc, exc.lineno, exc.colno)
            if excerpt:
                parse_ctx = {"excerpt": excerpt}
        else:
            message = "Invalid JSON request."
        return _detail(
            name,
            "invalid_argument",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_JSON_SYNTAX",
                cause=message,
                location=parse_loc,
                context=parse_ctx,
            ),
        )
    if isinstance(exc, InputFileReadError):
        message = str(exc)
        next_step = (
            "Verify that the path points to a readable UTF-8 JSON file and try again."
        )
        file_loc: dict[str, Any] = {
            **(location or {}),
            "kind": "file",
            "path": exc.display_path,
        }
        return _detail(
            name,
            "invalid_argument",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_INPUT_READ_FAILED",
                cause=message,
                fix=next_step,
                location=file_loc,
            ),
        )
    if type(exc) in (ValueError, TypeError):
        message = str(exc)
        next_step = (
            "Check tool parameter choices/bounds and machine_capabilities; use "
            "recipe_validate for full recipe diagnostics."
        )
        return _detail(
            name,
            "invalid_argument",
            message,
            next_step,
            diagnostic=build_diagnostic(
                "XB_INVALID_ARGUMENT", cause=message, location=location
            ),
        )
    detail = _internal(context)
    if log_unexpected:
        logging.getLogger(__name__).error(
            "Unexpected application error; correlation_id=%s",
            detail["correlation_id"],
            exc_info=exc,
        )
    return detail
