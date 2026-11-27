"""Preset-write outcome evaluation, policy enforcement and refusal diagnostics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .diagnostic_catalog import (
    CATALOG,
    DiagnosticContext,
    build_diagnostic,
)
from .machine_controls import SLOTS
from .preset_writes import (
    MAX_ATTEMPTS,
    NEXT_STEPS,
    NO_PRESET_FRAME,
    OUTCOMES,
    RETRYABLE,
    SAVED,
)
from .redaction import safe_identifier

_VALID_OUTCOMES = frozenset(
    OUTCOMES | {"interrupted", "saved_interrupted", "saved_late", "in_progress"}
)

_SAFE_PRESET_READONLY_RECOVERY = (
    "Inspect `machine_preset_writes`, `machine_state`, and the physical "
    "machine before any further control. Do not resend or roll back "
    "automatically."
)


_SAFE_PRESET_DISCONNECT_RECOVERY = (
    "Session recovery is required: call `machine_disconnect` and inspect "
    "`machine_state`, `machine_preset_writes`, and the physical machine "
    "before any further control. Do not resend or roll back automatically."
)


_SAVED_CLEANUP_FAILED_FIX = (
    "A save event was observed (not a readback); do not replay this preset "
    "write. Because post-save cleanup failed and session recovery is "
    "required, call `machine_disconnect` and inspect `machine_state`, "
    "`machine_preset_writes`, and the physical machine before any further "
    "control."
)


def _normalize_preset(value: Any) -> tuple[str, str, int] | None:
    if not isinstance(value, Mapping):
        return None
    slot = _single_slot(value.get("slot"))
    raw_hex = value.get("payload_hex")
    flags = value.get("flags")
    if (
        slot is None
        or type(raw_hex) is not str
        or not raw_hex
        or type(flags) is not int
        or not 0 <= flags <= 255
    ):
        return None
    try:
        canonical_hex = bytes.fromhex(raw_hex).hex()
    except ValueError:
        return None
    if not canonical_hex:
        return None
    return (slot, canonical_hex, flags)


def _parse_attempt_outcomes(
    raw_attempts: Any, valid_outcomes: frozenset[str]
) -> list[str] | None:
    if not isinstance(raw_attempts, list):
        return None
    outcomes: list[str] = []
    for item in raw_attempts:
        if not isinstance(item, Mapping):
            return None
        item_out = item.get("outcome")
        if type(item_out) is not str or item_out not in valid_outcomes:
            return None
        outcomes.append(item_out)
    return outcomes


def _single_slot(value: Any) -> str | None:
    return value if type(value) is str and value in SLOTS else None


def _inspect_slots(
    mappings: Sequence[Mapping[str, Any]],
) -> tuple[bool, str | None, str | None]:
    seen_display: set[str] = set()
    single_ok = True
    for mapping in mappings:
        if "slot" in mapping:
            slot = _single_slot(mapping["slot"])
            if slot is None:
                return False, None, None
            seen_display.add(slot)
        if "requested_slots" in mapping:
            raw_slots = mapping["requested_slots"]
            if (
                not isinstance(raw_slots, list)
                or not raw_slots
                or not all(_single_slot(s) is not None for s in raw_slots)
            ):
                return False, None, None
            seen_display.add(",".join(raw_slots))
            if len(raw_slots) != 1:
                single_ok = False
        if "preset" in mapping:
            preset = mapping["preset"]
            if not isinstance(preset, Mapping):
                return False, None, None
            slot = _single_slot(preset.get("slot"))
            if slot is None:
                return False, None, None
            seen_display.add(slot)
    if not seen_display:
        return True, None, None
    if len(seen_display) > 1:
        return False, None, None
    display = next(iter(seen_display))
    single = display if single_ok and display in SLOTS else None
    return True, single, display


_PRESET_GUIDANCE_KEYS = (
    "attempt",
    "preset_write",
    "next_step",
    "retry_allowed",
    "retryable",
)


_PRESET_RECORD_FIELDS = (
    "state",
    "attempts",
    "attempts_used",
    "remaining_attempts",
    "retry_allowed",
    "next_step",
    "retryable",
)


_PresetWriteState = Literal[
    "superseded",
    "saved",
    "not_sent",
    "cancelled",
    "in_progress",
    "exhausted",
    "retryable",
]


_PRESET_WRITE_STATES: dict[str, _PresetWriteState] = {
    "superseded": "superseded",
    "saved": "saved",
    "not_sent": "not_sent",
    "cancelled": "cancelled",
    "in_progress": "in_progress",
    "exhausted": "exhausted",
    "retryable": "retryable",
}


@dataclass(frozen=True)
class _PresetWritePolicy:
    valid: bool
    next_step: str
    retry_allowed: bool
    display_slot: str | None
    recovery_required: bool
    saved: bool
    saved_cleanup_failed: bool
    conflict: bool = False
    write_id: str | None = None
    state: _PresetWriteState | None = None
    outcome: str | None = None
    attempts_used: int | None = None
    max_attempts: int | None = None
    different_contents: bool = False
    batch_refused: bool = False


def _refusal_next_step(
    *,
    valid: bool,
    conflict: bool,
    saved_cleanup_failed: bool,
    recovery_required: bool,
    slot: str | None = None,
    write_id: str | None = None,
    state: str | None = None,
    max_attempts: int | None = None,
    different_contents: bool = False,
    batch_refused: bool = False,
) -> str:
    if saved_cleanup_failed:
        return _SAVED_CLEANUP_FAILED_FIX
    if recovery_required:
        return _SAFE_PRESET_DISCONNECT_RECOVERY
    if not valid:
        if conflict:
            return _SAFE_PRESET_READONLY_RECOVERY
        return (
            "Nothing was sent. Inspect `machine_preset_writes` for the slot's "
            "write ID, outcomes and exact contents; do not resend automatically, "
            "and ask the user before acknowledging an incomplete write."
        )
    slot_part = f" for slot {slot}" if slot else ""
    write_part = f" write {write_id}" if write_id else ""
    ack_part = (
        f"`acknowledge_incomplete={write_id}`"
        if write_id
        else "`acknowledge_incomplete`"
    )
    if state == "superseded":
        return (
            f"Nothing was sent. Historical{write_part}{slot_part} was already "
            "superseded by a batch, so its `acknowledge_incomplete` value is "
            "obsolete. Inspect `machine_preset_writes` (read-only) and "
            "`machine_state`, do not resend automatically, and omit "
            "`acknowledge_incomplete` if the user explicitly directs a new write."
        )
    if state == "saved":
        return (
            f"Nothing was sent. A save event was already observed{slot_part}"
            f"{write_part} (not a readback); no further write is needed. "
            "Inspect `machine_preset_writes` and do not resend or replay "
            f"without explicit user direction and {ack_part}."
        )
    if state == "exhausted":
        attempts_text = f"All {max_attempts}" if max_attempts is not None else "All"
        return (
            f"Nothing was sent. {attempts_text} attempts{slot_part}{write_part} "
            "were used without an observed save: inspect `machine_preset_writes`, "
            "stop, do not resend automatically, and ask the user. Only after "
            f"their direction, start a new write with {ack_part}."
        )
    if state == "cancelled":
        return (
            f"Nothing was sent. Recorded write{slot_part}{write_part} was "
            "cancelled: inspect `machine_preset_writes`, stop, do not resend "
            "automatically, and ask the user. Only after their direction, start "
            f"a new write with {ack_part}."
        )
    if state == "in_progress":
        return (
            f"Nothing was sent. An attempt{slot_part}{write_part} is already in "
            "progress; inspect `machine_preset_writes`, wait for its result, "
            "and do not resend automatically."
        )
    if batch_refused:
        return (
            f"Nothing was sent. Inspect `machine_preset_writes`{slot_part}"
            f"{write_part}; do not resend automatically, and finish or ask the "
            f"user before acknowledging it ({ack_part}) with `write_preset` "
            "before running a batch."
        )
    if different_contents:
        return (
            f"Nothing was sent. Inspect `machine_preset_writes`{slot_part}"
            f"{write_part} and its exact contents; do not resend automatically, "
            f"and ask the user before passing {ack_part}."
        )
    return (
        f"Nothing was sent. Inspect `machine_preset_writes`{slot_part}"
        f"{write_part}, outcomes and exact contents; do not resend automatically, "
        f"and ask the user before passing {ack_part}."
    )


def _validate_preset_guidance_types(container: Mapping[str, Any]) -> None:
    if "next_step" in container and type(container["next_step"]) is not str:
        raise ValueError("Remote preset next_step must be a string")
    if "retry_allowed" in container and type(container["retry_allowed"]) is not bool:
        raise ValueError("Remote preset retry_allowed must be a bool")
    if "retryable" in container and type(container["retryable"]) is not bool:
        raise ValueError("Remote preset retryable must be a bool")


def _apply_preset_write_policy(
    target: dict[str, Any], policy: _PresetWritePolicy
) -> bool:
    changed = False
    if "next_step" in target:
        target["next_step"] = policy.next_step
        changed = True
    if "retry_allowed" in target:
        target["retry_allowed"] = policy.retry_allowed
        changed = True
    if "retryable" in target:
        target["retryable"] = False
        changed = True
    return changed


def _extract_preset_write_layers(
    root: Mapping[str, Any],
    *,
    prefer_top_attempt: bool = False,
) -> tuple[Mapping[str, Any] | None, tuple[Mapping[str, Any], ...]]:
    raw_details = root.get("details")
    details = (
        raw_details if prefer_top_attempt and isinstance(raw_details, Mapping) else None
    )
    top_att = root.get("attempt") if isinstance(root.get("attempt"), Mapping) else None
    top_pw = (
        root.get("preset_write")
        if isinstance(root.get("preset_write"), Mapping)
        else None
    )
    top_pw_att = (
        top_pw.get("attempt")
        if top_pw is not None and isinstance(top_pw.get("attempt"), Mapping)
        else None
    )
    det_att = (
        details.get("attempt")
        if details is not None and isinstance(details.get("attempt"), Mapping)
        else None
    )
    det_pw = (
        details.get("preset_write")
        if details is not None and isinstance(details.get("preset_write"), Mapping)
        else None
    )
    det_pw_att = (
        det_pw.get("attempt")
        if det_pw is not None and isinstance(det_pw.get("attempt"), Mapping)
        else None
    )
    if prefer_top_attempt:
        priority: tuple[Mapping[str, Any] | None, ...] = (
            top_att,
            det_pw_att,
            det_att,
            top_pw_att,
            det_pw,
            top_pw,
            details,
            root,
        )
        enclosing_order: tuple[Mapping[str, Any] | None, ...] = (
            root,
            details,
            top_pw,
            top_pw_att,
            top_att,
            det_pw,
            det_pw_att,
            det_att,
        )
    else:
        priority = (top_pw_att, top_att, top_pw, root)
        enclosing_order = (root, top_pw, top_pw_att, top_att)

    record = next((item for item in priority if item is not None), None)
    enclosing: list[Mapping[str, Any]] = []
    seen_ids: set[int] = {id(record)} if record is not None else set()
    for item in enclosing_order:
        if item is not None and id(item) not in seen_ids:
            seen_ids.add(id(item))
            enclosing.append(item)
    return record, tuple(enclosing)


def _sanitize_preset_write_mapping(
    mapping: Mapping[str, Any],
    policy: _PresetWritePolicy,
    *,
    strict: bool = False,
    include_details: bool = False,
) -> tuple[dict[str, Any], bool]:
    if strict:
        _validate_preset_guidance_types(mapping)
    copy = dict(mapping)
    changed = _apply_preset_write_policy(copy, policy)

    raw_att = copy.get("attempt")
    if isinstance(raw_att, Mapping):
        if strict:
            _validate_preset_guidance_types(raw_att)
        att_copy = dict(raw_att)
        if _apply_preset_write_policy(att_copy, policy):
            copy["attempt"] = att_copy
            changed = True

    raw_pw = copy.get("preset_write")
    if isinstance(raw_pw, Mapping):
        pw_copy, pw_changed = _sanitize_preset_write_mapping(
            raw_pw, policy, strict=strict, include_details=False
        )
        if pw_changed:
            copy["preset_write"] = pw_copy
            changed = True

    if include_details:
        raw_details = copy.get("details")
        if isinstance(raw_details, Mapping):
            det_copy, det_changed = _sanitize_preset_write_mapping(
                raw_details, policy, strict=strict, include_details=False
            )
            if det_changed:
                copy["details"] = det_copy
                changed = True

    return copy, changed


def _untrusted_preset_write_cause(policy: _PresetWritePolicy, *, refused: bool) -> str:
    if policy.saved_cleanup_failed:
        return (
            "Preset write observed a save event, but post-save cleanup failed; "
            "session recovery is required."
        )
    if policy.conflict:
        subject = "refusal" if refused else "reply"
        if policy.recovery_required:
            return (
                f"Remote preset write {subject} contained conflicting or "
                "unverified evidence; stored contents are unverified and "
                "session recovery is required."
            )
        return (
            f"Remote preset write {subject} contained conflicting or "
            "unverified evidence; stored contents are unverified."
        )
    if not policy.valid:
        return CATALOG[
            "XB_PRESET_WRITE_REFUSED" if refused else "XB_PRESET_WRITE_INCOMPLETE"
        ].default_cause
    slot_label = f" for slot {policy.display_slot}" if policy.display_slot else ""
    if refused:
        wid = policy.write_id
        if policy.batch_refused and wid and policy.display_slot and policy.state:
            return (
                f"Write {wid} for slot {policy.display_slot} is {policy.state}; "
                "a batch would overwrite it. Nothing was sent."
            )
        if (
            policy.different_contents
            and wid
            and policy.display_slot
            and policy.state == "retryable"
        ):
            return (
                f"Incomplete write {wid} for slot {policy.display_slot} has "
                "different contents; nothing was sent. Ask the user before "
                "acknowledging it."
            )
        if policy.state == "superseded":
            lead = f"Write {wid}{slot_label}" if wid else f"Preset write{slot_label}"
            return (
                f"{lead} was already superseded by a batch; the "
                "`acknowledge_incomplete` value is stale and nothing was sent."
            )
        if policy.state == "saved":
            lead = f"Write {wid}{slot_label}" if wid else f"Preset write{slot_label}"
            return f"{lead} already observed a save; nothing was sent."
        if policy.state == "retryable":
            if wid:
                return (
                    f"Preset write{slot_label} was refused while incomplete "
                    f"write {wid} remains recorded; nothing was sent."
                )
            return (
                f"Preset write{slot_label} was refused while an incomplete "
                "retryable write remains recorded; nothing was sent."
            )
        if policy.state == "exhausted":
            lead = f"Write {wid}{slot_label}" if wid else f"Preset write{slot_label}"
            return (
                f"{lead} is exhausted ({policy.max_attempts} attempts used); "
                "nothing was sent. Stop and ask the user."
            )
        if policy.state == "cancelled":
            lead = f"Write {wid}{slot_label}" if wid else f"Preset write{slot_label}"
            return f"{lead} is cancelled; nothing was sent. Stop and ask the user."
        if policy.state == "in_progress":
            lead = f"Write {wid}{slot_label}" if wid else f"Preset write{slot_label}"
            return f"{lead} is in_progress; nothing was sent."
        if policy.state == "not_sent" and wid:
            return (
                f"Preset write{slot_label} (write {wid}) was refused; nothing was sent."
            )
        return CATALOG["XB_PRESET_WRITE_REFUSED"].default_cause
    if policy.state == "retryable":
        detail = f" ({policy.outcome})" if policy.outcome else ""
        return (
            f"Preset write{slot_label} did not confirm completion{detail}; "
            "stored contents are unverified."
        )
    if policy.state == "exhausted":
        return (
            f"Preset write{slot_label} did not confirm completion after "
            f"{policy.attempts_used}/{policy.max_attempts} attempts; stored "
            "contents are unverified."
        )
    if policy.state == "cancelled":
        return (
            f"Preset write{slot_label} was cancelled; stored contents are unverified."
        )
    if policy.state == "saved":
        return f"Preset write{slot_label} observed a save event (not a readback)."
    return CATALOG["XB_PRESET_WRITE_INCOMPLETE"].default_cause


@dataclass(frozen=True)
class _LayerEvidence:
    enclosing_valid: bool
    eff_recovery: bool
    single_slot: str | None
    display_slot: str | None
    seen_presets: frozenset[tuple[str, str, int]]
    record_preset: tuple[str, str, int] | None
    seen_write_ids: frozenset[str]


@dataclass(frozen=True)
class _RequestProvenanceCheck:
    enclosing_valid: bool
    display_slot: str | None
    different_contents: bool
    batch_refused: bool
    request_allows_resend: bool
    req_cmd: str | None
    req_preset: tuple[str, str, int] | None
    req_ack: str | None


@dataclass(frozen=True)
class _OutcomeEvidence:
    enclosing_valid: bool
    outcome_valid: bool
    rec_outcome: str | None
    record_saved_observed: bool
    attempts_outcomes: list[str] | None
    live_saved: bool
    enclosing_outcomes: tuple[str, ...]
    write_id: str | None
    state: _PresetWriteState | None
    any_attempt_saved: bool
    any_saved: bool
    any_layer_claims_retry: bool
    has_record_fields: bool
    resolved_outcome: str | None
    saved_cleanup_failed: bool


def _inspect_layer_evidence(
    all_layers: tuple[Mapping[str, Any], ...],
    *,
    recovery_required: bool | None,
) -> _LayerEvidence:
    enclosing_valid = True
    for layer in all_layers:
        for subkey in ("attempt", "preset_write"):
            if (
                subkey in layer
                and layer[subkey] is not None
                and not isinstance(layer[subkey], Mapping)
            ):
                enclosing_valid = False

    rec_bools: set[bool] = set()
    if recovery_required is not None:
        if type(recovery_required) is bool:
            rec_bools.add(recovery_required)
        else:
            enclosing_valid = False
    for layer in all_layers:
        if "recovery_required" in layer and layer["recovery_required"] is not None:
            if type(layer["recovery_required"]) is bool:
                rec_bools.add(layer["recovery_required"])
            else:
                enclosing_valid = False
    if len(rec_bools) > 1:
        enclosing_valid = False
    eff_recovery = True in rec_bools

    slot_consistent, single_slot, display_slot = _inspect_slots(all_layers)
    if not slot_consistent:
        enclosing_valid = False

    seen_presets: set[tuple[str, str, int]] = set()
    preset_malformed = False
    seen_write_ids: set[str] = set()
    for layer in all_layers:
        if "preset" in layer:
            norm = _normalize_preset(layer["preset"])
            if norm is None:
                preset_malformed = True
            else:
                seen_presets.add(norm)
        if "write_id" in layer:
            wid = safe_identifier(layer["write_id"])
            if wid is None:
                enclosing_valid = False
            else:
                seen_write_ids.add(wid)
    if len(seen_presets) > 1 or len(seen_write_ids) > 1:
        enclosing_valid = False
    record_preset = (
        next(iter(seen_presets))
        if len(seen_presets) == 1 and not preset_malformed
        else None
    )
    return _LayerEvidence(
        enclosing_valid=enclosing_valid,
        eff_recovery=eff_recovery,
        single_slot=single_slot,
        display_slot=display_slot,
        seen_presets=frozenset(seen_presets),
        record_preset=record_preset,
        seen_write_ids=frozenset(seen_write_ids),
    )


def _check_request_provenance(
    context: DiagnosticContext | None,
    evidence: _LayerEvidence,
    *,
    refused: bool,
    require_request_match: bool,
) -> _RequestProvenanceCheck:
    req_method = context.method if context is not None else None
    req_cmd = context.requested_command if context is not None else None
    req_preset = context.requested_preset if context is not None else None
    req_slot = context.requested_slot if context is not None else None
    req_slots = context.requested_slots if context is not None else None
    req_ack = context.requested_acknowledge if context is not None else None

    enclosing_valid = evidence.enclosing_valid
    display_slot = evidence.display_slot
    single_slot = evidence.single_slot
    record_preset = evidence.record_preset
    request_mismatch = False
    different_contents = False
    batch_refused = False

    if refused:
        if req_method is not None and req_method not in (
            "machine_command",
            "machine_preset_writes",
        ):
            request_mismatch = True
        elif req_method == "machine_preset_writes" and (
            display_slot is not None
            or bool(evidence.seen_write_ids)
            or bool(evidence.seen_presets)
        ):
            request_mismatch = True
        elif req_cmd is not None and req_cmd not in ("write_preset", "write_presets"):
            request_mismatch = True
        elif req_preset is not None:
            if display_slot is not None and display_slot != req_preset[0]:
                request_mismatch = True
            elif (
                display_slot == req_preset[0]
                and record_preset is not None
                and record_preset != req_preset
            ):
                different_contents = True
        elif req_cmd == "write_presets":
            if display_slot is not None:
                if req_slots is not None and (
                    single_slot is None or single_slot not in req_slots
                ):
                    request_mismatch = True
                elif single_slot is not None and (
                    req_slots is None or single_slot in req_slots
                ):
                    batch_refused = True
        elif req_slot is not None:
            if display_slot is not None and display_slot != req_slot:
                request_mismatch = True
    else:
        if req_method is not None and req_method not in (
            "machine_command",
            "machine_preset_writes",
        ):
            request_mismatch = True
        elif req_preset is not None:
            if (record_preset is not None and record_preset != req_preset) or (
                display_slot is not None and display_slot != req_preset[0]
            ):
                request_mismatch = True
        elif req_slot is not None:
            if display_slot is not None and display_slot != req_slot:
                request_mismatch = True

    if request_mismatch:
        enclosing_valid = False
        display_slot = None
        different_contents = False
        batch_refused = False

    request_allows_resend = not refused
    if not refused:
        if require_request_match:
            request_allows_resend = (
                req_cmd == "write_preset"
                and req_preset is not None
                and record_preset == req_preset
                and single_slot == req_preset[0]
            )
        elif req_slot is not None:
            request_allows_resend = single_slot == req_slot

    return _RequestProvenanceCheck(
        enclosing_valid=enclosing_valid,
        display_slot=display_slot,
        different_contents=different_contents,
        batch_refused=batch_refused,
        request_allows_resend=request_allows_resend,
        req_cmd=req_cmd,
        req_preset=req_preset,
        req_ack=req_ack,
    )


def _collect_outcome_evidence(
    record: Mapping[str, Any] | None,
    enclosing: tuple[Mapping[str, Any], ...],
    all_layers: tuple[Mapping[str, Any], ...],
    *,
    eff_recovery: bool,
    initial_valid: bool,
) -> _OutcomeEvidence:
    enclosing_valid = initial_valid
    rec_outcome: str | None = None
    outcome_valid = True
    record_saved_observed = False
    observed_bools: set[bool] = set()

    if isinstance(record, Mapping):
        raw_outcome = record.get("outcome")
        outcome_valid = "outcome" not in record or (
            type(raw_outcome) is str and raw_outcome in _VALID_OUTCOMES
        )
        rec_outcome = (
            raw_outcome if type(raw_outcome) is str and outcome_valid else None
        )
        if "saved_observed" in record:
            if type(record["saved_observed"]) is bool:
                record_saved_observed = record["saved_observed"]
                observed_bools.add(record_saved_observed)
            else:
                enclosing_valid = False

    attempts_outcomes = (
        _parse_attempt_outcomes(record.get("attempts"), _VALID_OUTCOMES)
        if isinstance(record, Mapping)
        else None
    )
    all_attempt_lists: list[list[str]] = []
    if attempts_outcomes is not None:
        all_attempt_lists.append(attempts_outcomes)
    elif isinstance(record, Mapping) and "attempts" in record:
        enclosing_valid = False

    live_saved = False
    enclosing_outcomes: list[str] = []
    for layer in enclosing:
        if "attempts" in layer:
            layer_attempts = _parse_attempt_outcomes(layer["attempts"], _VALID_OUTCOMES)
            if layer_attempts is None:
                enclosing_valid = False
            else:
                all_attempt_lists.append(layer_attempts)
                if SAVED & set(layer_attempts):
                    live_saved = True
                if "saved_cleanup_failed" in layer_attempts:
                    enclosing_outcomes.append("saved_cleanup_failed")
                if (
                    attempts_outcomes is not None
                    and layer_attempts != attempts_outcomes
                ):
                    enclosing_valid = False
        if "saved_observed" in layer:
            if type(layer["saved_observed"]) is bool:
                observed_bools.add(layer["saved_observed"])
                if layer["saved_observed"]:
                    live_saved = True
            else:
                enclosing_valid = False
        if "outcome" in layer:
            p_out = layer["outcome"]
            if type(p_out) is str and p_out in _VALID_OUTCOMES:
                enclosing_outcomes.append(p_out)
                if p_out in SAVED:
                    live_saved = True
            else:
                enclosing_valid = False
        if "state" in layer:
            p_state = layer["state"]
            if type(p_state) is not str or p_state not in NEXT_STEPS:
                enclosing_valid = False
            else:
                if p_state == "saved":
                    live_saved = True
                if isinstance(record, Mapping) and record.get("state") != p_state:
                    enclosing_valid = False
        if "retry_allowed" in layer:
            if type(layer["retry_allowed"]) is not bool or (
                isinstance(record, Mapping)
                and record.get("retry_allowed") != layer["retry_allowed"]
            ):
                enclosing_valid = False
        for count_key in ("attempts_used", "max_attempts", "remaining_attempts"):
            if count_key in layer:
                val = layer[count_key]
                if type(val) is not int or (
                    isinstance(record, Mapping)
                    and count_key in record
                    and record.get(count_key) != val
                ):
                    enclosing_valid = False

    if (
        len(observed_bools) > 1
        or len(set(enclosing_outcomes)) > 1
        or (True in observed_bools and any(o not in SAVED for o in enclosing_outcomes))
        or (False in observed_bools and any(o in SAVED for o in enclosing_outcomes))
    ):
        enclosing_valid = False

    write_id = (
        safe_identifier(record.get("write_id")) if isinstance(record, Mapping) else None
    )
    raw_state = record.get("state") if isinstance(record, Mapping) else None
    state: _PresetWriteState | None = (
        _PRESET_WRITE_STATES.get(raw_state)
        if type(raw_state) is str and raw_state in NEXT_STEPS
        else None
    )
    any_attempt_saved = any(bool(SAVED & set(items)) for items in all_attempt_lists)
    live_attempt_saved = any_attempt_saved and state != "superseded"
    any_saved = (
        rec_outcome in SAVED
        or live_saved
        or record_saved_observed
        or live_attempt_saved
    )
    any_layer_claims_retry = any(
        layer.get("retry_allowed") is True
        or layer.get("state") in ("retryable", "not_sent")
        for layer in all_layers
    )
    has_record_fields = any(
        key in layer for layer in all_layers for key in _PRESET_RECORD_FIELDS
    )
    resolved_outcome = (
        "saved_cleanup_failed"
        if rec_outcome == "saved_cleanup_failed"
        or "saved_cleanup_failed" in enclosing_outcomes
        else rec_outcome or (enclosing_outcomes[0] if enclosing_outcomes else None)
    )
    saved_cleanup_failed = (
        any_saved
        and eff_recovery
        and enclosing_valid
        and outcome_valid
        and not any_layer_claims_retry
    )
    return _OutcomeEvidence(
        enclosing_valid=enclosing_valid,
        outcome_valid=outcome_valid,
        rec_outcome=rec_outcome,
        record_saved_observed=record_saved_observed,
        attempts_outcomes=attempts_outcomes,
        live_saved=live_saved,
        enclosing_outcomes=tuple(enclosing_outcomes),
        write_id=write_id,
        state=state,
        any_attempt_saved=any_attempt_saved,
        any_saved=any_saved,
        any_layer_claims_retry=any_layer_claims_retry,
        has_record_fields=has_record_fields,
        resolved_outcome=resolved_outcome,
        saved_cleanup_failed=saved_cleanup_failed,
    )


def _build_fallback_policy(
    record: Mapping[str, Any] | None,
    provenance: _RequestProvenanceCheck,
    outcome_ev: _OutcomeEvidence,
    *,
    eff_recovery: bool,
    refused: bool,
) -> _PresetWritePolicy:
    saved_cleanup = outcome_ev.saved_cleanup_failed
    is_conflict = not saved_cleanup and (
        not outcome_ev.enclosing_valid
        or not outcome_ev.outcome_valid
        or outcome_ev.any_layer_claims_retry
        or outcome_ev.has_record_fields
    )
    step = (
        _SAFE_PRESET_DISCONNECT_RECOVERY
        if eff_recovery
        else _SAFE_PRESET_READONLY_RECOVERY
    )
    if saved_cleanup:
        step = _SAVED_CLEANUP_FAILED_FIX
    elif (
        outcome_ev.any_saved
        and outcome_ev.enclosing_valid
        and outcome_ev.outcome_valid
        and not outcome_ev.any_layer_claims_retry
    ):
        step = (
            "Nothing was sent. A save event was already observed (not a "
            "readback); no further write is needed. Inspect "
            "`machine_preset_writes` and do not resend or replay without "
            "explicit user direction and `acknowledge_incomplete`."
            if refused
            else "A save event was observed (not a readback); do not replay this "
            "preset write. Inspect `machine_preset_writes` and `machine_state` "
            "before any further control."
        )
    elif refused and not eff_recovery and not is_conflict:
        step = _refusal_next_step(
            valid=False,
            conflict=False,
            saved_cleanup_failed=False,
            recovery_required=False,
        )
    has_cleanup_counts = (
        saved_cleanup
        and isinstance(record, Mapping)
        and type(record.get("attempts_used")) is int
        and type(record.get("max_attempts")) is int
    )
    return _PresetWritePolicy(
        valid=False,
        next_step=step,
        retry_allowed=False,
        display_slot=provenance.display_slot,
        recovery_required=eff_recovery,
        saved=outcome_ev.any_saved,
        saved_cleanup_failed=saved_cleanup,
        conflict=is_conflict,
        write_id=outcome_ev.write_id if saved_cleanup else None,
        state=outcome_ev.state if saved_cleanup else None,
        outcome=outcome_ev.resolved_outcome if saved_cleanup else None,
        attempts_used=record["attempts_used"]
        if has_cleanup_counts and isinstance(record, Mapping)
        else None,
        max_attempts=record["max_attempts"]
        if has_cleanup_counts and isinstance(record, Mapping)
        else None,
    )


def _valid_preset_policy(
    provenance: _RequestProvenanceCheck,
    outcome_ev: _OutcomeEvidence,
    *,
    write_id: str,
    state: _PresetWriteState,
    eff_recovery: bool,
    attempts_used: int,
    max_attempts: int,
    step: str,
    allow_retry: bool,
) -> _PresetWritePolicy:
    return _PresetWritePolicy(
        valid=True,
        next_step=step,
        retry_allowed=allow_retry,
        display_slot=provenance.display_slot,
        recovery_required=eff_recovery,
        saved=outcome_ev.any_saved,
        saved_cleanup_failed=False,
        conflict=False,
        write_id=write_id,
        state=state,
        outcome=outcome_ev.resolved_outcome,
        attempts_used=attempts_used,
        max_attempts=max_attempts,
        different_contents=provenance.different_contents,
        batch_refused=provenance.batch_refused,
    )


def _valid_refusal_policy(
    provenance: _RequestProvenanceCheck,
    outcome_ev: _OutcomeEvidence,
    *,
    write_id: str,
    state: _PresetWriteState,
    eff_recovery: bool,
    attempts_used: int,
    max_attempts: int,
) -> _PresetWritePolicy:
    return _valid_preset_policy(
        provenance,
        outcome_ev,
        write_id=write_id,
        state=state,
        eff_recovery=eff_recovery,
        attempts_used=attempts_used,
        max_attempts=max_attempts,
        step=_refusal_next_step(
            valid=True,
            conflict=False,
            saved_cleanup_failed=False,
            recovery_required=eff_recovery,
            slot=provenance.display_slot,
            write_id=write_id,
            state=state,
            max_attempts=max_attempts,
            different_contents=provenance.different_contents,
            batch_refused=provenance.batch_refused,
        ),
        allow_retry=False,
    )


def _resolve_state_policy(
    record: Mapping[str, Any],
    enclosing: tuple[Mapping[str, Any], ...],
    all_layers: tuple[Mapping[str, Any], ...],
    provenance: _RequestProvenanceCheck,
    outcome_ev: _OutcomeEvidence,
    *,
    write_id: str,
    state: _PresetWriteState,
    attempts_outcomes: list[str],
    eff_recovery: bool,
    refused: bool,
    require_failed_status: bool,
) -> _PresetWritePolicy | None:
    retry_allowed = record.get("retry_allowed")
    attempts_used = record.get("attempts_used")
    max_attempts = record.get("max_attempts")
    remaining = record.get("remaining_attempts")
    if (
        type(retry_allowed) is not bool
        or type(attempts_used) is not int
        or type(max_attempts) is not int
        or type(remaining) is not int
        or max_attempts < 1
        or max_attempts > MAX_ATTEMPTS
        or attempts_used < 0
        or remaining < 0
    ):
        return None

    counted = [out for out in attempts_outcomes if out != "refused"]
    if len(counted) != attempts_used:
        return None

    if state == "superseded":
        if (
            eff_recovery
            or outcome_ev.live_saved
            or outcome_ev.record_saved_observed
            or (not refused and bool(enclosing))
            or (refused and provenance.batch_refused)
            or (
                refused
                and provenance.req_cmd == "write_preset"
                and provenance.req_preset is not None
                and provenance.req_ack is None
            )
            or outcome_ev.rec_outcome is not None
            or retry_allowed is not False
            or remaining != 0
        ):
            return None
        if refused:
            return _valid_refusal_policy(
                provenance,
                outcome_ev,
                write_id=write_id,
                state=state,
                eff_recovery=eff_recovery,
                attempts_used=attempts_used,
                max_attempts=max_attempts,
            )
        return _valid_preset_policy(
            provenance,
            outcome_ev,
            write_id=write_id,
            state=state,
            eff_recovery=eff_recovery,
            attempts_used=attempts_used,
            max_attempts=max_attempts,
            step=NEXT_STEPS["superseded"],
            allow_retry=False,
        )

    if state == "saved":
        if (
            retry_allowed is not False
            or remaining != 0
            or not outcome_ev.any_attempt_saved
        ):
            return None
        if refused:
            return _valid_refusal_policy(
                provenance,
                outcome_ev,
                write_id=write_id,
                state=state,
                eff_recovery=eff_recovery,
                attempts_used=attempts_used,
                max_attempts=max_attempts,
            )
        return _valid_preset_policy(
            provenance,
            outcome_ev,
            write_id=write_id,
            state=state,
            eff_recovery=eff_recovery,
            attempts_used=attempts_used,
            max_attempts=max_attempts,
            step=NEXT_STEPS["saved"].format(write_id=write_id),
            allow_retry=False,
        )

    if outcome_ev.any_saved:
        return None

    if state == "cancelled":
        if retry_allowed is not False or remaining != 0 or "cancelled" not in counted:
            return None
        if refused:
            return _valid_refusal_policy(
                provenance,
                outcome_ev,
                write_id=write_id,
                state=state,
                eff_recovery=eff_recovery,
                attempts_used=attempts_used,
                max_attempts=max_attempts,
            )
        return _valid_preset_policy(
            provenance,
            outcome_ev,
            write_id=write_id,
            state=state,
            eff_recovery=eff_recovery,
            attempts_used=attempts_used,
            max_attempts=max_attempts,
            step=NEXT_STEPS["cancelled"].format(write_id=write_id),
            allow_retry=False,
        )

    if state == "in_progress":
        if (
            retry_allowed is not False
            or remaining != 0
            or not counted
            or counted[-1] != "in_progress"
            or "cancelled" in counted
        ):
            return None
        if refused:
            return _valid_refusal_policy(
                provenance,
                outcome_ev,
                write_id=write_id,
                state=state,
                eff_recovery=eff_recovery,
                attempts_used=attempts_used,
                max_attempts=max_attempts,
            )
        return _valid_preset_policy(
            provenance,
            outcome_ev,
            write_id=write_id,
            state=state,
            eff_recovery=eff_recovery,
            attempts_used=attempts_used,
            max_attempts=max_attempts,
            step=NEXT_STEPS["in_progress"],
            allow_retry=False,
        )

    if state == "exhausted":
        if (
            retry_allowed is not False
            or remaining != 0
            or attempts_used < max_attempts
            or not all(out in RETRYABLE for out in counted)
        ):
            return None
        if refused:
            return _valid_refusal_policy(
                provenance,
                outcome_ev,
                write_id=write_id,
                state=state,
                eff_recovery=eff_recovery,
                attempts_used=attempts_used,
                max_attempts=max_attempts,
            )
        step = NEXT_STEPS["exhausted"].format(write_id=write_id, max=max_attempts)
        if counted and all(out == "no_preset_frame" for out in counted):
            step += NO_PRESET_FRAME
        return _valid_preset_policy(
            provenance,
            outcome_ev,
            write_id=write_id,
            state=state,
            eff_recovery=eff_recovery,
            attempts_used=attempts_used,
            max_attempts=max_attempts,
            step=step,
            allow_retry=False,
        )

    non_failed_status = any(
        "status" in layer and layer["status"] != "failed" for layer in all_layers
    )
    missing_required_failed = require_failed_status and (
        not enclosing or enclosing[0].get("status") != "failed"
    )

    if state == "retryable":
        if (
            (
                not refused
                and (not provenance.request_allows_resend or retry_allowed is not True)
            )
            or attempts_used < 1
            or attempts_used >= max_attempts
            or remaining != max_attempts - attempts_used
            or not all(out in RETRYABLE for out in counted)
            or (
                outcome_ev.rec_outcome is not None
                and outcome_ev.rec_outcome not in RETRYABLE
                and outcome_ev.rec_outcome != "refused"
            )
            or any(out not in RETRYABLE for out in outcome_ev.enclosing_outcomes)
            or non_failed_status
            or missing_required_failed
        ):
            return None
        if refused:
            return _valid_refusal_policy(
                provenance,
                outcome_ev,
                write_id=write_id,
                state=state,
                eff_recovery=eff_recovery,
                attempts_used=attempts_used,
                max_attempts=max_attempts,
            )
        step = NEXT_STEPS["retryable"].format(
            write_id=write_id, max=max_attempts, remaining=remaining
        )
        if all(out == "no_preset_frame" for out in counted):
            step += NO_PRESET_FRAME
        return _valid_preset_policy(
            provenance,
            outcome_ev,
            write_id=write_id,
            state=state,
            eff_recovery=eff_recovery,
            attempts_used=attempts_used,
            max_attempts=max_attempts,
            step=step,
            allow_retry=True,
        )

    if state == "not_sent":
        if (
            (
                not refused
                and (not provenance.request_allows_resend or retry_allowed is not True)
            )
            or eff_recovery
            or attempts_used != 0
            or remaining != max_attempts
            or (
                outcome_ev.rec_outcome is not None
                and outcome_ev.rec_outcome != "refused"
            )
            or bool(outcome_ev.enclosing_outcomes)
            or non_failed_status
            or missing_required_failed
        ):
            return None
        if refused:
            return _valid_refusal_policy(
                provenance,
                outcome_ev,
                write_id=write_id,
                state=state,
                eff_recovery=eff_recovery,
                attempts_used=attempts_used,
                max_attempts=max_attempts,
            )
        return _valid_preset_policy(
            provenance,
            outcome_ev,
            write_id=write_id,
            state=state,
            eff_recovery=eff_recovery,
            attempts_used=attempts_used,
            max_attempts=max_attempts,
            step=NEXT_STEPS["not_sent"],
            allow_retry=True,
        )

    return None


def _evaluate_preset_write(
    record: Mapping[str, Any] | None,
    *,
    enclosing_layers: Sequence[Mapping[str, Any]] = (),
    recovery_required: bool | None = None,
    refused: bool = False,
    require_failed_status: bool = False,
    require_request_match: bool = False,
    context: DiagnosticContext | None = None,
) -> _PresetWritePolicy:
    enclosing = tuple(layer for layer in enclosing_layers if isinstance(layer, Mapping))
    all_layers = ((record,) if isinstance(record, Mapping) else ()) + enclosing
    layer_ev = _inspect_layer_evidence(all_layers, recovery_required=recovery_required)
    provenance = _check_request_provenance(
        context,
        layer_ev,
        refused=refused,
        require_request_match=require_request_match,
    )
    outcome_ev = _collect_outcome_evidence(
        record,
        enclosing,
        all_layers,
        eff_recovery=layer_ev.eff_recovery,
        initial_valid=provenance.enclosing_valid,
    )
    if (
        outcome_ev.saved_cleanup_failed
        or not isinstance(record, Mapping)
        or outcome_ev.write_id is None
        or outcome_ev.state is None
        or not outcome_ev.outcome_valid
        or not outcome_ev.enclosing_valid
        or layer_ev.single_slot is None
        or outcome_ev.attempts_outcomes is None
    ):
        return _build_fallback_policy(
            record,
            provenance,
            outcome_ev,
            eff_recovery=layer_ev.eff_recovery,
            refused=refused,
        )
    resolved = _resolve_state_policy(
        record,
        enclosing,
        all_layers,
        provenance,
        outcome_ev,
        write_id=outcome_ev.write_id,
        state=outcome_ev.state,
        attempts_outcomes=outcome_ev.attempts_outcomes,
        eff_recovery=layer_ev.eff_recovery,
        refused=refused,
        require_failed_status=require_failed_status,
    )
    if resolved is not None:
        return resolved
    return _build_fallback_policy(
        record,
        provenance,
        outcome_ev,
        eff_recovery=layer_ev.eff_recovery,
        refused=refused,
    )


def render_preset_write_next_step(
    record: Mapping[str, Any],
    *,
    payload: Mapping[str, Any] | None = None,
    enclosing: Mapping[str, Any] | None = None,
    recovery_required: bool | None = None,
) -> str:
    """Render preset-write ``next_step`` strictly from local policy and evidence.

    Never forwards peer-supplied prose. Unknown or inconsistent evidence never
    authorizes a resend and falls back to safe read-only or disconnect recovery.
    """
    layers = tuple(
        layer for layer in (enclosing, payload) if isinstance(layer, Mapping)
    )
    return _evaluate_preset_write(
        record,
        enclosing_layers=layers,
        recovery_required=recovery_required,
        require_failed_status=isinstance(enclosing, Mapping),
    ).next_step


def _preset_write_fix(
    *,
    refused: bool,
    outcome: str | None,
    state: str | None,
    retry_allowed: bool | None,
    recovery_required: bool | None,
    saved_observed: bool,
    local_next_step: str | None,
) -> str | None:
    saved = outcome in SAVED or state == "saved" or saved_observed
    if not refused and (
        outcome == "saved_cleanup_failed" or (saved and recovery_required is True)
    ):
        return _SAVED_CLEANUP_FAILED_FIX
    if not refused and recovery_required is True:
        if local_next_step and retry_allowed is True:
            return local_next_step
        return _SAFE_PRESET_DISCONNECT_RECOVERY
    if not refused and saved:
        return (
            "A save event was observed (not a readback); do not replay this preset "
            "write. Inspect `machine_preset_writes` and `machine_state` before any "
            "further control."
        )
    if refused and state == "saved":
        return (
            "Nothing was sent. A save event was already observed (not a readback); "
            "inspect `machine_preset_writes` and do not replay the write without "
            "explicit user direction and `acknowledge_incomplete`."
        )
    if local_next_step:
        return local_next_step
    return None


def _recipes_mode_not_restored(payload: Mapping[str, Any] | None) -> bool:
    """A save was observed and the last acknowledged mode is still pro.

    Only evidence selects this code; its cause and fix are always local.
    """
    return (
        isinstance(payload, Mapping)
        and payload.get("saved_observed") is True
        and payload.get("acknowledged_mode") == "pro"
    )


def preset_write_diagnostic(
    *,
    refused: bool,
    cause: str,
    payload: Mapping[str, Any] | None = None,
    attempt: Mapping[str, Any] | None = None,
    enclosing: Mapping[str, Any] | None = None,
    recovery_required: bool | None = None,
    context: DiagnosticContext | None = None,
    trusted: bool = True,
    policy: _PresetWritePolicy | None = None,
) -> dict[str, Any]:
    """Build a diagnostic for incomplete or policy-refused preset writes."""
    code = "XB_PRESET_WRITE_REFUSED" if refused else "XB_PRESET_WRITE_INCOMPLETE"
    location: dict[str, Any] = {}
    if context is not None:
        if context.command:
            location["command"] = context.command
        if context.method:
            location["method"] = context.method
        if context.tool:
            location["tool"] = context.tool

    if not trusted or policy is not None:
        if policy is None:
            if isinstance(enclosing, Mapping):
                record, base_layers = _extract_preset_write_layers(
                    enclosing, prefer_top_attempt=True
                )
                layers = list(base_layers)
                seen_ids = {id(layer) for layer in layers}
                if record is not None:
                    seen_ids.add(id(record))
                for extra in (payload, attempt):
                    if isinstance(extra, Mapping) and id(extra) not in seen_ids:
                        seen_ids.add(id(extra))
                        layers.append(extra)
                policy = _evaluate_preset_write(
                    record,
                    enclosing_layers=layers,
                    recovery_required=recovery_required,
                    refused=refused,
                    require_failed_status=True,
                    require_request_match=True,
                    context=context,
                )
            else:
                record = attempt if isinstance(attempt, Mapping) else payload
                layers = []
                if isinstance(payload, Mapping) and payload is not record:
                    layers.append(payload)
                policy = _evaluate_preset_write(
                    record,
                    enclosing_layers=layers,
                    recovery_required=recovery_required,
                    refused=refused,
                    require_failed_status=False,
                    require_request_match=True,
                    context=context,
                )
        if policy.display_slot:
            location["kind"] = "slot"
            location["slot"] = policy.display_slot
        summary_bits: list[str] = []
        if policy.valid or policy.saved_cleanup_failed:
            if policy.write_id:
                summary_bits.append(f"write_id={policy.write_id}")
            if policy.state:
                summary_bits.append(f"state={policy.state}")
            if policy.outcome:
                summary_bits.append(f"outcome={policy.outcome}")
            if policy.attempts_used is not None and policy.max_attempts is not None:
                summary_bits.append(
                    f"attempts={policy.attempts_used}/{policy.max_attempts}"
                )
            summary_bits.append(f"retry_allowed={str(policy.retry_allowed).lower()}")
        if policy.recovery_required:
            summary_bits.append("recovery_required=true")

        fix = policy.next_step
        if policy.saved_cleanup_failed:
            fix = _SAVED_CLEANUP_FAILED_FIX
        elif policy.valid and not policy.retry_allowed and policy.recovery_required:
            fix = _SAFE_PRESET_DISCONNECT_RECOVERY

        resolved_cause = _untrusted_preset_write_cause(policy, refused=refused)
        if not (refused or policy.conflict) and _recipes_mode_not_restored(payload):
            code = "XB_PRESET_MODE_NOT_RESTORED"
            resolved_cause = CATALOG[code].default_cause
            fix = CATALOG[code].default_fix
        return build_diagnostic(
            code,
            cause=resolved_cause,
            fix=fix,
            location=location or None,
            context={"summary": "; ".join(summary_bits)} if summary_bits else None,
        )

    summary_bits = []
    source = attempt if isinstance(attempt, Mapping) else payload
    slot_layers = tuple(
        layer for layer in (source, payload, enclosing) if isinstance(layer, Mapping)
    )
    _, _, slot = _inspect_slots(slot_layers)
    if slot:
        location["kind"] = "slot"
        location["slot"] = slot

    write_id: str | None = None
    outcome: str | None = None
    state: str | None = None
    retry_allowed: bool | None = None
    max_attempts_val: int | None = None
    local_next_step: str | None = None
    payload_outcome = (
        payload["outcome"]
        if isinstance(payload, Mapping)
        and isinstance(payload.get("outcome"), str)
        and payload["outcome"].strip()
        else None
    )
    saved_observed = bool(
        isinstance(payload, Mapping)
        and (payload.get("saved_observed") is True or payload_outcome in SAVED)
    )

    if isinstance(source, Mapping):
        if isinstance(source.get("write_id"), str) and source["write_id"].strip():
            write_id = source["write_id"].strip()
            summary_bits.append(f"write_id={write_id}")
        if isinstance(source.get("state"), str) and source["state"].strip():
            state = source["state"]
            summary_bits.append(f"state={state}")
        if isinstance(source.get("outcome"), str) and source["outcome"].strip():
            outcome = source["outcome"]
            summary_bits.append(f"outcome={outcome}")
        if (
            type(source.get("attempts_used")) is int
            and type(source.get("max_attempts")) is int
        ):
            max_attempts_val = source["max_attempts"]
            summary_bits.append(
                f"attempts={source['attempts_used']}/{max_attempts_val}"
            )
        if type(source.get("retry_allowed")) is bool:
            retry_allowed = False if refused else source["retry_allowed"]
            summary_bits.append(f"retry_allowed={str(retry_allowed).lower()}")
        if isinstance(source.get("next_step"), str) and source["next_step"].strip():
            local_next_step = source["next_step"]
    if outcome is None and payload_outcome is not None:
        outcome = payload_outcome
        summary_bits.append(f"outcome={outcome}")
    elif payload_outcome == "saved_cleanup_failed":
        outcome = payload_outcome
    if recovery_required is True:
        summary_bits.append("recovery_required=true")

    if refused:
        src_preset = (
            _normalize_preset(source.get("preset"))
            if isinstance(source, Mapping)
            else None
        )
        req_preset = context.requested_preset if context is not None else None
        req_cmd = context.requested_command if context is not None else None
        diff_contents = (
            req_preset is not None
            and src_preset is not None
            and slot == req_preset[0]
            and src_preset != req_preset
        ) or ("different contents" in cause.lower())
        is_batch = req_cmd == "write_presets" or (
            "batch would overwrite" in cause.lower()
        )
        saved_cleanup = outcome == "saved_cleanup_failed" or (
            recovery_required is True
            and (outcome in SAVED or state == "saved" or saved_observed)
        )
        local_fix: str | None = _refusal_next_step(
            valid=isinstance(source, Mapping)
            and (write_id is not None or state is not None),
            conflict=False,
            saved_cleanup_failed=saved_cleanup,
            recovery_required=recovery_required is True,
            slot=slot,
            write_id=write_id,
            state=state,
            max_attempts=max_attempts_val,
            different_contents=diff_contents,
            batch_refused=is_batch,
        )
    else:
        local_fix = _preset_write_fix(
            refused=False,
            outcome=outcome,
            state=state,
            retry_allowed=retry_allowed,
            recovery_required=recovery_required,
            saved_observed=saved_observed,
            local_next_step=local_next_step,
        )
    resolved_cause = cause
    if (
        not refused
        and (
            outcome == "saved_cleanup_failed"
            or (
                recovery_required is True
                and (outcome in SAVED or state == "saved" or saved_observed)
            )
        )
        and "save" not in cause.lower()
    ):
        resolved_cause = (
            f"{cause} (save event observed, but post-save cleanup failed; "
            "session recovery required)"
        )
    if not refused and _recipes_mode_not_restored(payload):
        code = "XB_PRESET_MODE_NOT_RESTORED"
        resolved_cause = CATALOG[code].default_cause
        local_fix = CATALOG[code].default_fix
    return build_diagnostic(
        code,
        cause=resolved_cause,
        fix=local_fix,
        location=location or None,
        context={"summary": "; ".join(summary_bits)} if summary_bits else None,
    )
