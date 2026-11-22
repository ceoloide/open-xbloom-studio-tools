"""Canonical diagnostics catalog, safe excerpts and plain human stderr display.

Every diagnostic carries a stable bracketed code, a plain cause, an optional
truthful location, a concrete fix, a canonical per-code documentation link and
an agent repair guide link. No fix or repair link ever suggests automatically
retrying an uncertain physical machine action.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .provider_exports import credential_key

DOCS_LOCAL_PATH = "docs/diagnostics.md"
DOCS_BASE_URL = (
    "https://github.com/ceoloide/open-xbloom-studio-tools/blob/main/" + DOCS_LOCAL_PATH
)

MAX_JSON_LOCATE_BYTES = 256 * 1024
MAX_JSON_LOCATE_DEPTH = 100
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
_JSON_TOKEN = re.compile(
    r"""
    (?P<ws>[ \t\r\n]+)
    |(?P<string>"(?:\\.|[^"\\\x00-\x1f])*")
    |(?P<punct>[{}\[\]:,])
    |(?P<atom>true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)
    """,
    re.VERBOSE,
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
_VALID_SLOTS = frozenset({"A", "B", "C"})
_SAVED_OUTCOMES = frozenset(
    {"saved", "saved_cleanup_failed", "saved_interrupted", "saved_late"}
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
_SAFE_PREPARED_INVALIDATED_RECOVERY = (
    "Disconnect and explicitly upload again; retained machine content is not verified"
)
_SAFE_PREPARED_FALLBACK_RECOVERY = (
    "Inspect `machine_state` and disconnect before uploading again; retained "
    "machine content is not verified."
)


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


def _is_sensitive_pointer(pointer: str | None) -> bool:
    if not isinstance(pointer, str):
        return False
    segments = _pointer_segments(pointer)
    return segments is not None and any(_is_sensitive_key(seg) for seg in segments)


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


def _pointer_segments(pointer: str) -> list[str] | None:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        return None
    return [
        part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")
    ]


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


def _valid_slot_value(value: Any) -> str | None:
    if type(value) is not str:
        return None
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if parts and all(part in _VALID_SLOTS for part in parts):
        return ",".join(parts)
    return None


def _valid_pointer_value(value: Any) -> str | None:
    if type(value) is not str:
        return None
    if value == "" or value.startswith("/"):
        return _truncate(value, MAX_SANITISED_TEXT)
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
    return value if type(value) is str and value in _VALID_SLOTS else None


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
    single = display if single_ok and display in _VALID_SLOTS else None
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
    from .preset_writes import (
        MAX_ATTEMPTS,
        NEXT_STEPS,
        NO_PRESET_FRAME,
        OUTCOMES,
        RETRYABLE,
        SAVED,
    )

    valid_outcomes = frozenset(
        OUTCOMES | {"interrupted", "saved_interrupted", "saved_late", "in_progress"}
    )
    enclosing = tuple(layer for layer in enclosing_layers if isinstance(layer, Mapping))
    all_layers = ((record,) if isinstance(record, Mapping) else ()) + enclosing

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

    req_method = context.method if context is not None else None
    req_cmd = context.requested_command if context is not None else None
    req_preset = context.requested_preset if context is not None else None
    req_slot = context.requested_slot if context is not None else None
    req_slots = context.requested_slots if context is not None else None
    req_ack = context.requested_acknowledge if context is not None else None

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
            display_slot is not None or bool(seen_write_ids) or bool(seen_presets)
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

    rec_outcome: str | None = None
    outcome_valid = True
    record_saved_observed = False
    observed_bools: set[bool] = set()
    if isinstance(record, Mapping):
        raw_outcome = record.get("outcome")
        outcome_valid = "outcome" not in record or (
            type(raw_outcome) is str and raw_outcome in valid_outcomes
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
        _parse_attempt_outcomes(record.get("attempts"), valid_outcomes)
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
            layer_attempts = _parse_attempt_outcomes(layer["attempts"], valid_outcomes)
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
            if type(p_out) is str and p_out in valid_outcomes:
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
    fallback = (
        _SAFE_PRESET_DISCONNECT_RECOVERY
        if eff_recovery
        else _SAFE_PRESET_READONLY_RECOVERY
    )

    def make_fallback() -> _PresetWritePolicy:
        is_conflict = not saved_cleanup_failed and (
            not enclosing_valid
            or not outcome_valid
            or any_layer_claims_retry
            or has_record_fields
        )
        step = fallback
        if saved_cleanup_failed:
            step = _SAVED_CLEANUP_FAILED_FIX
        elif (
            any_saved
            and enclosing_valid
            and outcome_valid
            and not any_layer_claims_retry
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
        return _PresetWritePolicy(
            valid=False,
            next_step=step,
            retry_allowed=False,
            display_slot=display_slot,
            recovery_required=eff_recovery,
            saved=any_saved,
            saved_cleanup_failed=saved_cleanup_failed,
            conflict=is_conflict,
            write_id=write_id if saved_cleanup_failed else None,
            state=state if saved_cleanup_failed else None,
            outcome=resolved_outcome if saved_cleanup_failed else None,
            attempts_used=(
                record["attempts_used"]
                if saved_cleanup_failed
                and isinstance(record, Mapping)
                and type(record.get("attempts_used")) is int
                and type(record.get("max_attempts")) is int
                else None
            ),
            max_attempts=(
                record["max_attempts"]
                if saved_cleanup_failed
                and isinstance(record, Mapping)
                and type(record.get("attempts_used")) is int
                and type(record.get("max_attempts")) is int
                else None
            ),
        )

    if saved_cleanup_failed:
        return make_fallback()

    if (
        not isinstance(record, Mapping)
        or write_id is None
        or state is None
        or not outcome_valid
        or not enclosing_valid
        or single_slot is None
        or attempts_outcomes is None
    ):
        return make_fallback()

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
        return make_fallback()

    counted = [out for out in attempts_outcomes if out != "refused"]
    if len(counted) != attempts_used:
        return make_fallback()

    def make_valid(step: str, allow_retry: bool) -> _PresetWritePolicy:
        return _PresetWritePolicy(
            valid=True,
            next_step=step,
            retry_allowed=allow_retry,
            display_slot=display_slot,
            recovery_required=eff_recovery,
            saved=any_saved,
            saved_cleanup_failed=False,
            conflict=False,
            write_id=write_id,
            state=state,
            outcome=resolved_outcome,
            attempts_used=attempts_used,
            max_attempts=max_attempts,
            different_contents=different_contents,
            batch_refused=batch_refused,
        )

    def make_valid_refusal() -> _PresetWritePolicy:
        return make_valid(
            _refusal_next_step(
                valid=True,
                conflict=False,
                saved_cleanup_failed=False,
                recovery_required=eff_recovery,
                slot=display_slot,
                write_id=write_id,
                state=state,
                max_attempts=max_attempts,
                different_contents=different_contents,
                batch_refused=batch_refused,
            ),
            False,
        )

    if state == "superseded":
        if (
            eff_recovery
            or live_saved
            or record_saved_observed
            or (not refused and bool(enclosing))
            or (refused and batch_refused)
            or (
                refused
                and req_cmd == "write_preset"
                and req_preset is not None
                and req_ack is None
            )
            or rec_outcome is not None
            or retry_allowed is not False
            or remaining != 0
        ):
            return make_fallback()
        if refused:
            return make_valid_refusal()
        return make_valid(NEXT_STEPS["superseded"], False)

    if state == "saved":
        if retry_allowed is not False or remaining != 0 or not any_attempt_saved:
            return make_fallback()
        if refused:
            return make_valid_refusal()
        return make_valid(NEXT_STEPS["saved"].format(write_id=write_id), False)

    if any_saved:
        return make_fallback()

    if state == "cancelled":
        if retry_allowed is not False or remaining != 0 or "cancelled" not in counted:
            return make_fallback()
        if refused:
            return make_valid_refusal()
        return make_valid(NEXT_STEPS["cancelled"].format(write_id=write_id), False)

    if state == "in_progress":
        if (
            retry_allowed is not False
            or remaining != 0
            or not counted
            or counted[-1] != "in_progress"
            or "cancelled" in counted
        ):
            return make_fallback()
        if refused:
            return make_valid_refusal()
        return make_valid(NEXT_STEPS["in_progress"], False)

    if state == "exhausted":
        if (
            retry_allowed is not False
            or remaining != 0
            or attempts_used < max_attempts
            or not all(out in RETRYABLE for out in counted)
        ):
            return make_fallback()
        if refused:
            return make_valid_refusal()
        step = NEXT_STEPS["exhausted"].format(write_id=write_id, max=max_attempts)
        if counted and all(out == "no_preset_frame" for out in counted):
            step += NO_PRESET_FRAME
        return make_valid(step, False)

    non_failed_status = any(
        "status" in layer and layer["status"] != "failed" for layer in all_layers
    )
    missing_required_failed = require_failed_status and (
        not enclosing or enclosing[0].get("status") != "failed"
    )

    if state == "retryable":
        if (
            (not refused and (not request_allows_resend or retry_allowed is not True))
            or attempts_used < 1
            or attempts_used >= max_attempts
            or remaining != max_attempts - attempts_used
            or not all(out in RETRYABLE for out in counted)
            or (
                rec_outcome is not None
                and rec_outcome not in RETRYABLE
                and rec_outcome != "refused"
            )
            or any(out not in RETRYABLE for out in enclosing_outcomes)
            or non_failed_status
            or missing_required_failed
        ):
            return make_fallback()
        if refused:
            return make_valid_refusal()
        step = NEXT_STEPS["retryable"].format(
            write_id=write_id, max=max_attempts, remaining=remaining
        )
        if all(out == "no_preset_frame" for out in counted):
            step += NO_PRESET_FRAME
        return make_valid(step, True)

    if state == "not_sent":
        if (
            (not refused and (not request_allows_resend or retry_allowed is not True))
            or eff_recovery
            or attempts_used != 0
            or remaining != max_attempts
            or (rec_outcome is not None and rec_outcome != "refused")
            or bool(enclosing_outcomes)
            or non_failed_status
            or missing_required_failed
        ):
            return make_fallback()
        if refused:
            return make_valid_refusal()
        return make_valid(NEXT_STEPS["not_sent"], True)

    return make_fallback()


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
    saved = outcome in _SAVED_OUTCOMES or state == "saved" or saved_observed
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
        and (
            payload.get("saved_observed") is True or payload_outcome in _SAVED_OUTCOMES
        )
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
            and (outcome in _SAVED_OUTCOMES or state == "saved" or saved_observed)
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
                and (outcome in _SAVED_OUTCOMES or state == "saved" or saved_observed)
            )
        )
        and "save" not in cause.lower()
    ):
        resolved_cause = (
            f"{cause} (save event observed, but post-save cleanup failed; "
            "session recovery required)"
        )
    return build_diagnostic(
        code,
        cause=resolved_cause,
        fix=local_fix,
        location=location or None,
        context={"summary": "; ".join(summary_bits)} if summary_bits else None,
    )


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


def _local_fix_for_remote(
    diag_code: str,
    details: Mapping[str, Any] | None,
    *,
    error_type: str | None = None,
    message: str = "",
) -> str | None:
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
            diag_code, details, error_type=error_type, message=message
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
        if error_type == "LeaseBusyError":
            return "XB_OWNER_LEASE_BUSY"
        return "XB_MACHINE_BUSY"
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
    if error_code == "connection_unavailable" and error_type == "SessionError":
        return "XB_SESSION_UNAVAILABLE"
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
