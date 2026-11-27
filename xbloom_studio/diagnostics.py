"""Canonical diagnostics API, remote sanitization and human stderr display."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .diagnostic_catalog import (
    _LOCAL_ONLY_DIAGNOSTIC_CODES,
    _valid_pointer_value,
)
from .diagnostic_catalog import (
    CATALOG as CATALOG,
)
from .diagnostic_catalog import (
    CONTEXT_KEYS as CONTEXT_KEYS,
)
from .diagnostic_catalog import (
    DIAGNOSTIC_KEYS as DIAGNOSTIC_KEYS,
)
from .diagnostic_catalog import (
    DOCS_BASE_URL as DOCS_BASE_URL,
)
from .diagnostic_catalog import (
    DOCS_LOCAL_PATH as DOCS_LOCAL_PATH,
)
from .diagnostic_catalog import (
    EXCERPT_LINE_KEYS as EXCERPT_LINE_KEYS,
)
from .diagnostic_catalog import (
    FALLBACK_BY_ERROR_CODE as FALLBACK_BY_ERROR_CODE,
)
from .diagnostic_catalog import (
    LOCATION_KEYS as LOCATION_KEYS,
)
from .diagnostic_catalog import (
    REPAIR_GUIDE_KEYS as REPAIR_GUIDE_KEYS,
)
from .diagnostic_catalog import (
    SPECS as SPECS,
)
from .diagnostic_catalog import (
    DiagnosticContext as DiagnosticContext,
)
from .diagnostic_catalog import (
    DiagnosticSpec as DiagnosticSpec,
)
from .diagnostic_catalog import (
    InputFileReadError as InputFileReadError,
)
from .diagnostic_catalog import (
    LocalJsonSyntaxError as LocalJsonSyntaxError,
)
from .diagnostic_catalog import (
    _format_expected as _format_expected,
)
from .diagnostic_catalog import (
    attach_local_context as attach_local_context,
)
from .diagnostic_catalog import (
    build_diagnostic as build_diagnostic,
)
from .diagnostic_catalog import (
    has_valid_diagnostic_types as has_valid_diagnostic_types,
)
from .diagnostic_catalog import (
    preset_transport_fix as preset_transport_fix,
)
from .diagnostic_catalog import (
    validation_diagnostic as validation_diagnostic,
)
from .json_excerpt import (
    MAX_JSON_LOCATE_BYTES as MAX_JSON_LOCATE_BYTES,
)
from .json_excerpt import (
    MAX_JSON_LOCATE_DEPTH as MAX_JSON_LOCATE_DEPTH,
)
from .json_excerpt import (
    _reconstruct_excerpt_line as _reconstruct_excerpt_line,
)
from .json_excerpt import (
    _tokenize_json as _tokenize_json,
)
from .json_excerpt import (
    extract_json_excerpt as extract_json_excerpt,
)
from .json_excerpt import (
    locate_json_pointer_line as locate_json_pointer_line,
)
from .machine_controls import SLOTS
from .preset_diagnostics import (
    _PRESET_GUIDANCE_KEYS,
    _apply_preset_write_policy,
    _evaluate_preset_write,
    _extract_preset_write_layers,
    _normalize_preset,
    _PresetWritePolicy,
    _sanitize_preset_write_mapping,
    _single_slot,
    _untrusted_preset_write_cause,
    _validate_preset_guidance_types,
)
from .preset_diagnostics import (
    preset_write_diagnostic as preset_write_diagnostic,
)
from .preset_diagnostics import (
    render_preset_write_next_step as render_preset_write_next_step,
)
from .redaction import (
    _MAX_TRUNCATE_SCAN as _MAX_TRUNCATE_SCAN,
)
from .redaction import (
    MAX_EXCERPT_WIDTH as MAX_EXCERPT_WIDTH,
)
from .redaction import (
    MAX_SANITISED_TEXT as MAX_SANITISED_TEXT,
)
from .redaction import (
    MAX_SUMMARY_WIDTH as MAX_SUMMARY_WIDTH,
)
from .redaction import (
    _has_sensitive_value as _has_sensitive_value,
)
from .redaction import (
    _is_sensitive_key,
)
from .redaction import (
    _redact_text as _redact_text,
)
from .redaction import (
    _truncate as _truncate,
)
from .redaction import (
    is_safe_human_note as is_safe_human_note,
)
from .redaction import (
    safe_identifier as safe_identifier,
)

_SAFE_PREPARED_INVALIDATED_RECOVERY = (
    "Disconnect and explicitly upload again; retained machine content is not verified"
)


_SAFE_PREPARED_FALLBACK_RECOVERY = (
    "Inspect `machine_state` and disconnect before uploading again; retained "
    "machine content is not verified."
)


def request_provenance(
    method: str | None, params: Mapping[str, Any] | None
) -> tuple[
    str | None,
    tuple[str, str, int] | None,
    str | None,
    frozenset[str] | None,
]:
    """Extract truthful local request provenance for preset-write verification."""
    if not isinstance(params, Mapping):
        return None, None, None, None
    if method == "machine_command":
        raw_cmd = params.get("command")
        req_cmd = raw_cmd if type(raw_cmd) is str and raw_cmd.strip() else None
        cmd_params = params.get("params")
        if req_cmd == "write_preset":
            preset = (
                _normalize_preset(cmd_params.get("preset"))
                if isinstance(cmd_params, Mapping)
                else None
            )
            slot = preset[0] if preset is not None else None
            return req_cmd, preset, slot, (frozenset({slot}) if slot else None)
        if req_cmd == "write_presets":
            raw_presets = (
                cmd_params.get("presets") if isinstance(cmd_params, Mapping) else None
            )
            slots: frozenset[str] | None = None
            if isinstance(raw_presets, list) and raw_presets:
                extracted = [
                    _single_slot(item.get("slot"))
                    if isinstance(item, Mapping)
                    else None
                    for item in raw_presets
                ]
                if all(slot is not None for slot in extracted):
                    slots = frozenset(slot for slot in extracted if slot is not None)
            return req_cmd, None, None, slots
        return req_cmd, None, None, None
    if method == "machine_preset_writes":
        slot = _single_slot(params.get("slot"))
        return None, None, slot, (frozenset({slot}) if slot else None)
    return None, None, None, None


def request_context(
    method: str | None,
    params: Mapping[str, Any] | None = None,
    *,
    command: str | None = None,
    tool: str | None = None,
    parameter: str | None = None,
    source_path: str | None = None,
    source_text: str | None = None,
    source_kind: str | None = None,
    allow_excerpt: bool = False,
) -> DiagnosticContext:
    """Build a :class:`DiagnosticContext` with truthful local request provenance."""
    req_cmd, req_preset, req_slot, req_slots = request_provenance(method, params)
    req_ack: str | None = None
    if (
        method == "machine_command"
        and req_cmd == "write_preset"
        and isinstance(params, Mapping)
    ):
        cmd_params = params.get("params")
        if isinstance(cmd_params, Mapping):
            raw_ack = cmd_params.get("acknowledge_incomplete")
            if type(raw_ack) is str and raw_ack.strip():
                req_ack = raw_ack
    return DiagnosticContext(
        command=command,
        method=method,
        tool=tool,
        parameter=parameter,
        source_path=source_path,
        source_text=source_text,
        source_kind=source_kind,
        allow_excerpt=allow_excerpt,
        requested_command=req_cmd,
        requested_preset=req_preset,
        requested_slot=req_slot,
        requested_slots=req_slots,
        requested_acknowledge=req_ack,
    )


def _valid_slot_value(value: Any) -> str | None:
    if type(value) is not str:
        return None
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if parts and all(part in SLOTS for part in parts):
        return ",".join(parts)
    return None


def sanitize_diagnostic(
    raw: Any,
    *,
    enclosing_error_code: str | None = None,
    context: DiagnosticContext | None = None,
) -> dict[str, Any] | None:
    """Validate and rebuild a diagnostic received from an untrusted remote peer.

    Remote peers may never inject fixes, docs/repair URLs, local paths,
    excerpts, or fabricated command/method/tool/parameter routes.
    """
    if not has_valid_diagnostic_types(raw):
        return None
    code = raw["code"]
    if code not in CATALOG or code in _LOCAL_ONLY_DIAGNOSTIC_CODES:
        return None
    spec = CATALOG[code]
    if enclosing_error_code is not None and spec.error_code != enclosing_error_code:
        return None
    cause = raw.get("cause")
    if spec.code == "XB_INTERNAL_ERROR":
        return attach_local_context(
            build_diagnostic("XB_INTERNAL_ERROR", cause=spec.default_cause),
            context,
        )
    result: dict[str, Any] = {
        "code": spec.code,
        "cause": _truncate(
            cause if isinstance(cause, str) and cause.strip() else spec.default_cause,
            MAX_SANITISED_TEXT,
        ),
        "fix": spec.default_fix,
        "docs_url": spec.docs_url,
        "repair_guide": spec.repair_guide(),
    }
    raw_loc = raw.get("location")
    if isinstance(raw_loc, Mapping):
        safe_loc: dict[str, Any] = {}
        pointer = _valid_pointer_value(raw_loc.get("pointer"))
        if pointer is not None:
            safe_loc["kind"] = "json_pointer"
            safe_loc["pointer"] = pointer
        slot = _valid_slot_value(raw_loc.get("slot"))
        if slot is not None:
            safe_loc.setdefault("kind", "slot")
            safe_loc["slot"] = slot
        op_id = safe_identifier(raw_loc.get("operation_id"))
        if op_id is not None:
            safe_loc["operation_id"] = op_id
        if spec.code == "XB_JSON_SYNTAX":
            line = raw_loc.get("line")
            col = raw_loc.get("column")
            if type(line) is int and line >= 1:
                safe_loc.setdefault("kind", "json_parse")
                safe_loc["line"] = line
                if type(col) is int and col >= 1:
                    safe_loc["column"] = col
        if safe_loc:
            result["location"] = safe_loc
    return attach_local_context(result, context)


def _local_fix_for_remote(
    diag_code: str,
    details: Mapping[str, Any] | None,
    *,
    error_type: str | None = None,
    message: str = "",
    context: DiagnosticContext | None = None,
) -> str | None:
    if diag_code == "XB_TRANSPORT_UNCERTAIN":
        return preset_transport_fix(context)
    if (
        diag_code == "XB_RESPONSE_TOO_LARGE"
        and isinstance(details, Mapping)
        and isinstance(details.get("operation_id"), str)
        and details["operation_id"].strip()
    ):
        return (
            "The operation was accepted. Inspect history_get using operation_id "
            "or replay the same idempotency key to retrieve it. Never start "
            "again with a new key."
        )
    if diag_code == "XB_DIRECTORY_PERMISSION_REQUIRED":
        lower_msg = message.lower()
        if error_type == "LeaseDirectoryError" or "lease directory" in lower_msg:
            return (
                "Check ownership and mode 0700 for the lease directory under "
                "the account's home. Ensure the daemon user can write there."
            )
        if error_type == "SocketDirectoryError" or "socket directory" in lower_msg:
            return (
                "Select a private socket directory owned by the daemon user "
                "(mode 0700)."
            )
    return None


def _preset_write_error_policy(
    details: Mapping[str, Any] | None,
    *,
    refused: bool,
    context: DiagnosticContext | None = None,
) -> _PresetWritePolicy:
    if not isinstance(details, Mapping):
        return _evaluate_preset_write(
            None, refused=refused, require_request_match=True, context=context
        )
    record, layers = _extract_preset_write_layers(details, prefer_top_attempt=False)
    return _evaluate_preset_write(
        record,
        enclosing_layers=layers,
        refused=refused,
        require_request_match=True,
        context=context,
    )


def _rebuild_remote_diagnostic(
    detail: Mapping[str, Any],
    *,
    context: DiagnosticContext | None = None,
) -> tuple[dict[str, Any], _PresetWritePolicy | None]:
    raw_code = detail.get("code")
    error_code = raw_code if type(raw_code) is str else "internal_error"
    raw_type = detail.get("type")
    error_type = raw_type if type(raw_type) is str else None
    raw_message = detail.get("message")
    message = (
        _truncate(raw_message, MAX_SANITISED_TEXT)
        if isinstance(raw_message, str)
        else ""
    )
    raw_details = detail.get("details")
    details = raw_details if isinstance(raw_details, Mapping) else None

    diag_code = _infer_code(
        error_code, message, details, error_type=error_type, context=context
    )
    raw_diag = detail.get("diagnostic")
    if has_valid_diagnostic_types(raw_diag):
        assert isinstance(raw_diag, Mapping)
        candidate = raw_diag["code"]
        if (
            candidate in CATALOG
            and candidate not in _LOCAL_ONLY_DIAGNOSTIC_CODES
            and CATALOG[candidate].error_code == error_code
        ):
            if error_code == "validation_error":
                if context is not None and context.method == "machine_command":
                    diag_code = "XB_COMMAND_VALIDATION"
                elif context is not None and context.method in (
                    "recipe_validate",
                    "recipe_tea_validate",
                    "recipe_preview",
                    "recipe_create",
                    "recipe_update",
                ):
                    diag_code = "XB_RECIPE_VALIDATION"
                else:
                    diag_code = candidate
            elif (
                error_code == "permission_required"
                and diag_code == "XB_DIRECTORY_PERMISSION_REQUIRED"
            ):
                diag_code = "XB_DIRECTORY_PERMISSION_REQUIRED"
            elif candidate == "XB_PRESET_WRITE_REFUSED":
                if (
                    isinstance(details, Mapping)
                    and ("preset_write" in details or "attempt" in details)
                ) or (context is not None and context.method == "machine_command"):
                    diag_code = candidate
            else:
                diag_code = candidate

    if isinstance(details, Mapping):
        validation = details.get("validation")
        if (
            error_code == "validation_error"
            and isinstance(validation, Mapping)
            and isinstance(validation.get("errors"), list)
        ):
            return (
                validation_diagnostic(
                    validation["errors"],
                    command_validation=diag_code == "XB_COMMAND_VALIDATION",
                    context=context,
                    trusted=False,
                ),
                None,
            )

    if diag_code in ("XB_PRESET_WRITE_REFUSED", "XB_PRESET_WRITE_INCOMPLETE"):
        refused = diag_code == "XB_PRESET_WRITE_REFUSED"
        policy = _preset_write_error_policy(details, refused=refused, context=context)
        return (
            preset_write_diagnostic(
                refused=refused,
                cause=message or CATALOG[diag_code].default_cause,
                context=context,
                trusted=False,
                policy=policy,
            ),
            policy,
        )

    if diag_code == "XB_INTERNAL_ERROR" or error_code == "internal_error":
        diagnostic = build_diagnostic(
            "XB_INTERNAL_ERROR",
            cause=CATALOG["XB_INTERNAL_ERROR"].default_cause,
        )
        return attach_local_context(diagnostic, context), None

    location: dict[str, Any] = {}
    ctx_payload: dict[str, Any] = {}
    if isinstance(details, Mapping):
        op_id = safe_identifier(details.get("operation_id"))
        if op_id is not None:
            location["kind"] = "method"
            location["operation_id"] = op_id
        http_status = details.get("http_status")
        if type(http_status) is int:
            ctx_payload["summary"] = f"http_status={http_status}"
        protocol = details.get("protocol")
        if isinstance(protocol, Mapping):
            proto_bits: list[str] = []
            pending = protocol.get("pending_command")
            if type(pending) is int:
                proto_bits.append(f"protocol_command={pending}")
            for k, v in protocol.items():
                if len(proto_bits) >= 4:
                    break
                if (
                    isinstance(k, str)
                    and k != "pending_command"
                    and not _is_sensitive_key(k)
                    and type(v) in (int, float, bool)
                ):
                    proto_bits.append(f"{k}={v}")
            if proto_bits:
                ctx_payload["summary"] = "; ".join(proto_bits)
        raw_errors = details.get("errors")
        if isinstance(raw_errors, list):
            first_err = next(
                (item for item in raw_errors if isinstance(item, Mapping)), None
            )
            if first_err is not None:
                ptr = _valid_pointer_value(first_err.get("path"))
                if ptr is not None:
                    location["kind"] = "json_pointer"
                    location["pointer"] = ptr
                    if isinstance(first_err.get("message"), str):
                        ctx_payload["summary"] = first_err["message"]

    if diag_code == "XB_JSON_SYNTAX" and has_valid_diagnostic_types(raw_diag):
        assert isinstance(raw_diag, Mapping)
        raw_loc = raw_diag.get("location")
        if isinstance(raw_loc, Mapping) and type(raw_loc.get("line")) is int:
            location.setdefault("kind", "json_parse")
            location["line"] = raw_loc["line"]
            if type(raw_loc.get("column")) is int:
                location["column"] = raw_loc["column"]

    diagnostic = build_diagnostic(
        diag_code,
        cause=message or None,
        fix=_local_fix_for_remote(
            diag_code, details, error_type=error_type, message=message, context=context
        ),
        location=location or None,
        context=ctx_payload or None,
    )
    return attach_local_context(diagnostic, context), None


def rebuild_remote_diagnostic(
    detail: Mapping[str, Any],
    *,
    context: DiagnosticContext | None = None,
) -> dict[str, Any]:
    """Rebuild a trusted diagnostic from an enclosing validated remote envelope."""
    return _rebuild_remote_diagnostic(detail, context=context)[0]


def sanitize_remote_error(
    filtered: dict[str, Any],
    *,
    context: DiagnosticContext | None = None,
) -> dict[str, Any]:
    """Sanitize a validated remote error envelope across outer and nested fields."""
    rebuilt, policy = _rebuild_remote_diagnostic(filtered, context=context)
    if "retryable" in filtered:
        filtered["retryable"] = False
    if (
        "correlation_id" in filtered
        and safe_identifier(filtered["correlation_id"]) is None
    ):
        filtered.pop("correlation_id")
    is_internal = (
        rebuilt["code"] == "XB_INTERNAL_ERROR"
        or filtered.get("code") == "internal_error"
    )
    if is_internal:
        if "message" in filtered:
            filtered["message"] = rebuilt["cause"]
        filtered["details"] = {}
    else:
        details = filtered.get("details")
        if isinstance(details, Mapping):
            has_embedded = any(key in details for key in _PRESET_GUIDANCE_KEYS)
            if policy is None and has_embedded:
                policy = _preset_write_error_policy(
                    details, refused=False, context=context
                )
            if policy is not None:
                details_copy, changed = _sanitize_preset_write_mapping(
                    details, policy, strict=False, include_details=False
                )
                if changed:
                    filtered["details"] = details_copy
        message_replaced = False
        if policy is not None:
            is_preset_diag = rebuilt["code"] in (
                "XB_PRESET_WRITE_REFUSED",
                "XB_PRESET_WRITE_INCOMPLETE",
            )
            if policy.conflict or policy.saved_cleanup_failed or is_preset_diag:
                local_cause = (
                    rebuilt["cause"]
                    if is_preset_diag
                    else _untrusted_preset_write_cause(policy, refused=False)
                )
                if "message" in filtered:
                    filtered["message"] = local_cause
                    message_replaced = True
                if not is_preset_diag:
                    rebuilt = dict(rebuilt, cause=local_cause)
        if not message_replaced and isinstance(filtered.get("message"), str):
            filtered["message"] = _truncate(filtered["message"], MAX_SANITISED_TEXT)
    filtered["diagnostic"] = rebuilt
    if "next_step" in filtered:
        filtered["next_step"] = rebuilt["fix"]
    return filtered


def sanitize_remote_result(
    method: str,
    data: Any,
    *,
    context: DiagnosticContext | None = None,
    strict: bool = False,
) -> Any:
    """Sanitize and rebuild diagnostics and machine recovery text on remote results."""
    if not isinstance(data, Mapping):
        return data
    if method in ("recipe_validate", "recipe_tea_validate"):
        cleaned = dict(data)
        has_diag = "diagnostic" in cleaned and cleaned["diagnostic"] is not None
        if (
            has_diag
            and strict
            and not has_valid_diagnostic_types(cleaned["diagnostic"])
        ):
            raise ValueError("Remote diagnostic contains unexpected nested types")
        if cleaned.get("valid") is False:
            raw_errors = cleaned.get("errors")
            errors = raw_errors if isinstance(raw_errors, list) else []
            cleaned["diagnostic"] = validation_diagnostic(
                errors,
                tea=method == "recipe_tea_validate",
                context=context,
                trusted=False,
            )
        else:
            cleaned.pop("diagnostic", None)
        return cleaned
    if method == "machine_command":
        cleaned = dict(data)
        has_diag = "diagnostic" in cleaned and cleaned["diagnostic"] is not None
        if (
            has_diag
            and strict
            and not has_valid_diagnostic_types(cleaned["diagnostic"])
        ):
            raise ValueError("Remote diagnostic contains unexpected nested types")
        payload = (
            cleaned.get("details")
            if isinstance(cleaned.get("details"), Mapping)
            else None
        )
        is_failed = cleaned.get("status") == "failed"
        has_preset_fields = (
            is_failed
            or any(key in cleaned for key in _PRESET_GUIDANCE_KEYS)
            or (
                payload is not None
                and any(key in payload for key in _PRESET_GUIDANCE_KEYS)
            )
        )
        policy: _PresetWritePolicy | None = None
        if has_preset_fields:
            record, enclosing_layers = _extract_preset_write_layers(
                cleaned, prefer_top_attempt=True
            )
            policy = _evaluate_preset_write(
                record,
                enclosing_layers=enclosing_layers,
                require_failed_status=True,
                require_request_match=True,
                context=context,
            )
            cleaned, _ = _sanitize_preset_write_mapping(
                cleaned, policy, strict=strict, include_details=True
            )
            if "error" in cleaned and (
                policy.conflict or policy.saved_cleanup_failed or policy.valid
            ):
                cleaned["error"] = _untrusted_preset_write_cause(policy, refused=False)
        if is_failed:
            raw_error = cleaned.get("error")
            cause = (
                raw_error
                if isinstance(raw_error, str) and raw_error.strip()
                else "Preset write did not finish."
            )
            cleaned["diagnostic"] = preset_write_diagnostic(
                refused=False,
                cause=cause,
                context=context,
                trusted=False,
                policy=policy,
            )
        else:
            cleaned.pop("diagnostic", None)
        return cleaned
    if method == "machine_preset_writes":
        cleaned = dict(data)
        if any(key in cleaned for key in ("next_step", "retry_allowed", "retryable")):
            if strict:
                _validate_preset_guidance_types(cleaned)
            top_policy = _evaluate_preset_write(None, context=context)
            _apply_preset_write_policy(cleaned, top_policy)
        raw_writes = cleaned.get("writes")
        if isinstance(raw_writes, list):
            sanitized_writes: list[Any] = []
            for item in raw_writes:
                if isinstance(item, Mapping):
                    item_copy = dict(item)
                    if strict:
                        _validate_preset_guidance_types(item_copy)
                    item_policy = _evaluate_preset_write(
                        item_copy,
                        context=context,
                        require_request_match=False,
                    )
                    _apply_preset_write_policy(item_copy, item_policy)
                    sanitized_writes.append(item_copy)
                else:
                    sanitized_writes.append(item)
            cleaned["writes"] = sanitized_writes
        return cleaned
    if method in ("machine_state", "machine_connect", "operation_pause_resume"):
        cleaned = dict(data)
        raw_prepared = cleaned.get("prepared")
        if isinstance(raw_prepared, Mapping) and "recovery" in raw_prepared:
            prep_copy = dict(raw_prepared)
            if prep_copy["recovery"] is not None:
                if strict and type(prep_copy["recovery"]) is not str:
                    raise ValueError("Remote prepared recovery must be a string")
                prep_copy["recovery"] = (
                    _SAFE_PREPARED_INVALIDATED_RECOVERY
                    if prep_copy.get("status") == "invalidated"
                    else _SAFE_PREPARED_FALLBACK_RECOVERY
                )
            cleaned["prepared"] = prep_copy
        return cleaned
    return data


def diagnostic_from_detail(
    detail: Mapping[str, Any],
    context: DiagnosticContext | None = None,
    *,
    trusted_local: bool = False,
) -> dict[str, Any]:
    """Return a canonical diagnostic for any error envelope, including older peers."""
    if not isinstance(detail, Mapping):
        return attach_local_context(build_diagnostic("XB_INTERNAL_ERROR"), context)
    if trusted_local:
        existing = detail.get("diagnostic")
        if has_valid_diagnostic_types(existing):
            assert isinstance(existing, Mapping)
            code = existing["code"]
            if code in CATALOG:
                spec = CATALOG[code]
                cause = existing.get("cause")
                fix = existing.get("fix")
                rebuilt = build_diagnostic(
                    code,
                    cause=cause
                    if isinstance(cause, str) and cause.strip()
                    else spec.default_cause,
                    fix=fix
                    if isinstance(fix, str) and fix.strip()
                    else spec.default_fix,
                    location=existing.get("location")
                    if isinstance(existing.get("location"), Mapping)
                    else None,
                    context=existing.get("context")
                    if isinstance(existing.get("context"), Mapping)
                    else None,
                )
                return attach_local_context(rebuilt, context)
    return rebuild_remote_diagnostic(detail, context=context)


def _infer_code(
    error_code: str,
    message: str,
    details: Any,
    *,
    error_type: str | None = None,
    context: DiagnosticContext | None = None,
) -> str:
    if error_code == "not_found":
        if "Recipe" in message:
            return "XB_RECIPE_NOT_FOUND"
        if "Operation" in message:
            return "XB_OPERATION_NOT_FOUND"
        if "Import" in message:
            return "XB_IMPORT_NOT_FOUND"
        return "XB_NOT_FOUND"
    if error_code == "validation_error":
        if context is not None and context.method == "machine_command":
            return "XB_COMMAND_VALIDATION"
        if context is not None and context.method in (
            "recipe_validate",
            "recipe_tea_validate",
            "recipe_preview",
            "recipe_create",
            "recipe_update",
        ):
            return "XB_RECIPE_VALIDATION"
        if error_type == "CommandValidationError":
            return "XB_COMMAND_VALIDATION"
        if isinstance(details, Mapping):
            validation = details.get("validation")
            if isinstance(validation, Mapping) and isinstance(
                validation.get("errors"), list
            ):
                first = next(
                    (
                        item
                        for item in validation["errors"]
                        if isinstance(item, Mapping)
                    ),
                    None,
                )
                if first is not None:
                    path = first.get("path")
                    if isinstance(path, str) and path.startswith("/params"):
                        return "XB_COMMAND_VALIDATION"
        return "XB_RECIPE_VALIDATION"
    if error_code == "permission_required":
        lower_msg = message.lower()
        if error_type in ("LeaseDirectoryError", "SocketDirectoryError") or (
            "lease directory" in lower_msg or "socket directory" in lower_msg
        ):
            return "XB_DIRECTORY_PERMISSION_REQUIRED"
        return "XB_CONTROL_PERMISSION_REQUIRED"
    if error_code == "busy":
        return {
            "InsufficientWaterError": "XB_MACHINE_WATER_INSUFFICIENT",
            "IdleStateRequiredError": "XB_MACHINE_NOT_IDLE",
            "LeaseBusyError": "XB_OWNER_LEASE_BUSY",
        }.get(error_type or "", "XB_MACHINE_BUSY")
    if error_code == "invalid_argument":
        if error_type == "SocketPathError":
            return "XB_SOCKET_PATH_TOO_LONG"
        if error_type in ("JSONDecodeError", "LocalJsonSyntaxError"):
            return "XB_JSON_SYNTAX"
        return "XB_INVALID_ARGUMENT"
    if error_code == "conflict":
        if error_type == "PresetWriteRefused" or (
            isinstance(details, Mapping)
            and ("preset_write" in details or "attempt" in details)
        ):
            return "XB_PRESET_WRITE_REFUSED"
        if error_type == "FileExistsError":
            return "XB_SOCKET_EXISTS"
        return "XB_CONFLICT"
    if error_code == "connection_unavailable":
        return {
            "SessionError": "XB_SESSION_UNAVAILABLE",
            "FileNotFoundError": "XB_DAEMON_UNAVAILABLE",
            "ConnectionRefusedError": "XB_DAEMON_UNAVAILABLE",
        }.get(error_type or "", "XB_TRANSPORT_UNCERTAIN")
    return FALLBACK_BY_ERROR_CODE.get(error_code, "XB_INTERNAL_ERROR")


def _format_location(location: Mapping[str, Any] | None) -> str | None:
    if not isinstance(location, Mapping) or not location:
        return None
    parts: list[str] = []
    path = location.get("path")
    line = location.get("line")
    column = location.get("column")
    pointer = location.get("pointer")
    command = location.get("command")
    method = location.get("method")
    tool = location.get("tool")
    parameter = location.get("parameter")
    slot = location.get("slot")
    operation_id = location.get("operation_id")

    if isinstance(path, str):
        coord = path
        if type(line) is int:
            coord += f":{line}"
            if type(column) is int:
                coord += f":{column}"
        parts.append(coord)
    elif type(line) is int:
        coord = f"line {line}"
        if type(column) is int:
            coord += f", column {column}"
        parts.append(coord)

    if isinstance(pointer, str):
        parts.append(f"({pointer or '/'})" if parts else (pointer or "/"))

    if not parts:
        if isinstance(command, str):
            parts.append(f"xbloom-studio {command}")
        elif isinstance(tool, str):
            parts.append(f"tool {tool}")
        elif isinstance(method, str):
            parts.append(f"method {method}")

    qualifiers: list[str] = []
    if isinstance(parameter, str):
        qualifiers.append(f"parameter: {parameter}")
    if isinstance(slot, str):
        qualifiers.append(f"slot: {slot}")
    if isinstance(operation_id, str):
        qualifiers.append(f"operation_id: {operation_id}")
    if qualifiers:
        joined = ", ".join(qualifiers)
        parts.append(f"({joined})" if parts else joined)

    return " ".join(parts) if parts else None


def _render_excerpt(excerpt: Sequence[Mapping[str, Any]]) -> list[str]:
    width = max(len(str(item["line"])) for item in excerpt)
    rendered: list[str] = []
    for item in excerpt:
        marker = ">" if item.get("highlight") else " "
        num = str(item["line"]).rjust(width)
        rendered.append(f"{marker} {num} | {item['text']}")
        col = item.get("column")
        if item.get("highlight") and type(col) is int and col >= 1:
            pad = " " * (col - 1)
            rendered.append(f"  {' ' * width} | {pad}^")
    return rendered


def render_human_diagnostic(
    detail: Mapping[str, Any],
    *,
    context: DiagnosticContext | None = None,
    trusted_local: bool = False,
) -> str:
    """Format a plain-text Solid-inspired diagnostic block for human stderr."""
    diagnostic = diagnostic_from_detail(
        detail, context=context, trusted_local=trusted_local
    )
    code = diagnostic["code"]
    cause = diagnostic["cause"]
    fix = diagnostic["fix"]
    docs_url = diagnostic["docs_url"]
    guide = diagnostic["repair_guide"]

    blocks: list[str] = []
    ctx = diagnostic.get("context")
    if isinstance(ctx, Mapping) and isinstance(ctx.get("excerpt"), list):
        blocks.append("\n".join(_render_excerpt(ctx["excerpt"])))

    blocks.append(f"[{code}] {cause}")

    meta: list[str] = []
    loc_text = _format_location(diagnostic.get("location"))
    if loc_text:
        meta.append(f"Location: {loc_text}")
    if isinstance(ctx, Mapping) and isinstance(ctx.get("summary"), str):
        meta.append(f"Context: {ctx['summary']}")
    raw_corr = detail.get("correlation_id") if isinstance(detail, Mapping) else None
    correlation_id = safe_identifier(raw_corr)
    if correlation_id is not None:
        meta.append(f"Correlation ID: {correlation_id}")
    if meta:
        blocks.append("\n".join(meta))

    guide_line = f"{guide['url']} (repo checkout: {guide['local']}"
    if "skill" in guide:
        guide_line += f"; skill: {guide['skill']}"
    guide_line += ")"

    blocks.append(
        "\n".join(
            [
                f"Fix: {fix}",
                f"[{code}] what it means and how to fix it: {docs_url}",
                f"[{code}] agent repair guide: {guide_line}",
            ]
        )
    )
    return "\n\n".join(blocks)
