"""Optional MCP contracts; the core application never imports Pydantic.

Authoring validators deliberately accept arbitrary documents. These contracts
validate tool envelopes and describe successful outputs, not firmware
semantics. Requires the ``mcp`` extra.
"""

from __future__ import annotations

import functools
import inspect
import operator
from collections.abc import Callable
from copy import deepcopy
from typing import Annotated, Any, Literal, cast, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from .pagination import MAX_CURSOR_LENGTH
from .recipe_validation import HOST_STEPS, check_recipe

Model = type[BaseModel]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Output(BaseModel):
    # Keep future fields visible to clients. Required named fields make each
    # result kind useful while preserving complete historical data.
    model_config = ConfigDict(extra="allow", strict=True, allow_inf_nan=False)


def model(model_name: str, **fields: tuple[Any, Any, str]) -> Model:
    definitions: dict[str, Any] = {
        key: (typ, Field(default, description=description))
        for key, (typ, default, description) in fields.items()
    }
    return create_model(model_name, __base__=Output, **definitions)


def list_of(item: Any) -> Any:
    return list.__class_getitem__(item)


def optional(item: Any) -> Any:
    return item | None


def one_of(*items: Any) -> Any:
    return functools.reduce(operator.or_, items)


class RecipeRequest(Input):
    kind: Literal["recipe"] = Field(
        description="Compile a saved local revision or inline document."
    )
    reference: str | None = Field(
        None,
        min_length=1,
        description="Local recipe ID, alias or unambiguous name; not a machine slot "
        "readback.",
    )
    revision: int | None = Field(
        None,
        ge=1,
        description="Immutable saved revision; omit to use the current revision.",
    )
    document: dict[str, Any] | None = Field(
        None,
        description="Inline recipe document; validated by the shared authoring "
        "validator.",
    )

    @model_validator(mode="after")
    def one_source(self) -> RecipeRequest:
        if (self.reference is None) == (self.document is None):
            raise ValueError("Provide exactly one recipe reference or inline document")
        if self.document is not None and self.revision is not None:
            raise ValueError("An inline document cannot select a saved revision")
        return self


class RepeatRequest(Input):
    kind: Literal["repeat"] = Field(
        description="Use an immutable retained historical plan."
    )
    operation_id: str = Field(
        min_length=1,
        description="Historical operation ID whose exact plan should be repeated.",
    )


STEP_DESCRIPTIONS = {
    "water": {
        "kind": "One bounded manual water operation.",
        "volume_ml": "Requested water dose in ml; not measured cup yield.",
        "temperature_c": "Requested Celsius target; 20 is not a measured "
        "room-temperature guarantee.",
        "flow_ml_s": "Requested flow in ml/s.",
        "pattern": "Manual water pour pattern.",
        "timeout": "Optional operation deadline in seconds; required when "
        "volume_ml/flow_ml_s+45 exceeds the operation limit.",
    },
    "grind": {
        "kind": "One bounded manual grinder pulse.",
        "size": "Known grinder setting, whole number 1..80; do not infer an "
        "espresso profile.",
        "rpm": "Configured grinder RPM, whole number 60..120.",
        "duration_s": "Bounded grinder pulse duration in seconds.",
    },
}


class HostStepRequest(Input):
    """A bounded step checked by the shared recipe rules, never a copy of them."""

    @model_validator(mode="after")
    def shared_rules(self) -> HostStepRequest:
        step = self.model_dump(exclude_unset=True)
        name = step.pop("name", "request")
        report = check_recipe({"name": name, "steps": [step]})
        if not report["valid"]:
            raise ValueError(
                "; ".join(
                    (e["path"].removeprefix("/steps/0") or "/") + ": " + e["message"]
                    for e in report["errors"]
                )
            )
        return self


def _step_field(rule: dict[str, Any], description: str) -> tuple[Any, Any]:
    """One host step rule as a strict field with the same bounds and default."""
    options: dict[str, Any] = {}
    typ: Any
    if "enum" in rule:
        typ = Literal[tuple(rule["enum"])]
    else:
        # A number field accepts integers; whole numbers also accept 20.0.
        typ = float
        options.update(ge=rule["minimum"], le=rule["maximum"])
        if rule.get("whole_number"):
            options["multiple_of"] = 1
    if rule.get("required"):
        default = ...
    elif "default" in rule:
        default = rule["default"]
    else:
        return typ, Field(
            default_factory=lambda: None, description=description, **options
        )
    return typ, Field(default, description=description, **options)


def _step_request(kind: str) -> Any:
    descriptions = STEP_DESCRIPTIONS[kind]
    fields: dict[str, Any] = {
        "kind": (Literal[kind], Field(description=descriptions["kind"])),
        **{
            name: _step_field(rule, descriptions[name])
            for name, rule in HOST_STEPS[kind].items()
        },
        "name": (
            str,
            Field(
                default_factory=lambda: None,
                min_length=1,
                description="Optional name for this temporary operation.",
            ),
        ),
    }
    return create_model(
        kind.capitalize() + "Request", __base__=HostStepRequest, **fields
    )


WaterRequest = _step_request("water")
GrindRequest = _step_request("grind")

Request: Any = Annotated[
    one_of(RecipeRequest, RepeatRequest, WaterRequest, GrindRequest),
    Field(discriminator="kind"),
]

EXAMPLE_FIELDS = (
    "name",
    "roaster",
    "origin",
    "region",
    "type",
    "variety",
    "process",
    "flavor",
    "roast",
)
CoffeeExamplesInput = create_model(
    "CoffeeExamplesInput",
    __base__=Input,
    **{
        key: (
            str | list[str] | None,
            Field(
                None,
                description="Bean "
                + key
                + " as text or explicitly supplied categorical values.",
            ),
        )
        for key in EXAMPLE_FIELDS
    },
)  # type: ignore[call-overload]
CoffeeMatchInput = create_model(
    "CoffeeMatchInput",
    __base__=Input,
    **{
        key: (
            str | None,
            Field(None, description="Local matching " + key + " supplied by the user."),
        )
        for key in ("name", "roaster", "origin", "variety")
    },
)  # type: ignore[call-overload]

DESCRIPTIONS = {
    "target": "Explicit import conversion: host adaptation, structured coffee or "
    "structured tea. Refresh omission retains the original conversion target.",
    "coffee": "Bean metadata for matching: name, roaster, origin, region, type, "
    "variety, process, flavor or roast; strings or lists of strings.",
    "water_ml": "Optional requested reference water volume in ml; examples are not "
    "silently scaled.",
    "limit": "Maximum returned items; follow next_cursor while has_more is true.",
    "document": "Recipe document. Authoring diagnostics use the shared validator; no "
    "save or machine operation is implied.",
    "family": "Tea family; omit to inspect every supported family.",
    "expected_output_ml": "Optional nominal tea output target 120/240/360 ml; "
    "invalid types are returned as authoring diagnostics, not coerced.",
    "temporary": "Store an explicitly temporary local recipe, hidden from normal "
    "saved lists.",
    "reference": "Local recipe ID, alias or unambiguous name; B is a local alias, "
    "not machine preset readback.",
    "revision": "Immutable saved revision, integer>=1; omit for the current revision.",
    "query": "Case-insensitive local search text; each whitespace-separated word must "
    "match. Empty returns all eligible records.",
    "include_temporary": "Include temporary local recipes in addition to normal "
    "saved recipes.",
    "before": "Earlier immutable recipe revision, integer>=1.",
    "after": "Later revision for comparisons, or last durable event ID for event "
    "traversal.",
    "ident": "Exact retained operation or source-import ID, depending on this tool.",
    "expected_revision": "Current saved revision required for optimistic "
    "concurrency; reload after a conflict.",
    "alias": "Explicit local alias such as B; does not alter physical presets.",
    "url": "Public recipe URL from a supported hub; never an instruction to execute "
    "it.",
    "flow_ml_s": "Explicit host-adaptation flow in ml/s; omit when no host "
    "adaptation is intended.",
    "page": "Public provider page number, integer>=1.",
    "liked": "Restrict history to attempts with at least one feedback rating>=4; "
    "does not imply completion.",
    "status": "Optional exact historical operation status; exclude failed attempts "
    "explicitly when ranking completed brews.",
    "min_rating": "Optional minimum feedback rating 1..5; status filtering is "
    "independent.",
    "operation_id": "Exact durable operation ID; never infer it from a recipe name.",
    "note": "Actual user feedback for this exact operation; nonblank text.",
    "rating": "Optional user rating, integer 1..5; do not invent feedback.",
    "name": "User-provided local recipe name; nonblank text.",
    "command": "One command from machine_capabilities; calibration and operation "
    "starters are excluded.",
    "params": "Parameters for the selected command, validated against its shared "
    "command schema before connection.",
    "allow_control": "Explicit authorization to perform this requested control "
    "action; must be a JSON boolean.",
    "allow_untested": "Explicit experimental-feature opt-in; does not upgrade "
    "hardware validation evidence.",
    "request": "Discriminated request: saved/inline recipe, historical repeat, "
    "bounded water or bounded grind. Preview first.",
    "expected_recipe_sha256": "Optional 64 lowercase-hex digest from the reviewed "
    "preview; reject changed content before execution.",
    "token": "Opaque preparation/gate token returned by this daemon. Session-local "
    "tokens must never be fabricated.",
    "idempotency_key": "Nonblank unique key for one intended attempt. Reuse only for "
    "the identical attempt; never change it after uncertainty.",
    "resume": "False requests pause; true requests resume of the owned active "
    "operation.",
    "cursor": "Opaque next_cursor returned for these exact filters; do not construct "
    "or reuse across collections.",
    "detail": "compact returns an inspection summary; full retains complete "
    "documents/plans/event data.",
    "slot": "Physical preset slot A, B or C; omit for every recorded slot.",
}
EXPORT_DOCUMENT = (
    "Owner-supplied Firestore REST document envelope with name and typed fields; "
    "saves a sanitized import and, when valid, a local recipe. No file path or "
    "credentials."
)
PROVIDER_QUERY = (
    "Search keyword sent to the selected public provider; matching semantics belong "
    "to that provider."
)
TEXT_FIELDS = (
    "reference",
    "ident",
    "alias",
    "operation_id",
    "note",
    "name",
    "token",
    "idempotency_key",
)


def _options(tool: str, name: str) -> dict[str, Any]:
    options: dict[str, Any] = {"description": DESCRIPTIONS[name]}
    if name == "document" and tool == "recipe_import_export":
        options["description"] = EXPORT_DOCUMENT
    if name == "query" and tool == "provider_search":
        options["description"] = PROVIDER_QUERY
    if name in ("revision", "before", "expected_revision", "page"):
        options["ge"] = 1
    if name == "page":
        options["le"] = 1000
    if name == "after":
        options["ge"] = 1 if tool == "recipe_compare" else 0
    if name == "limit":
        maximum = {"recipe_examples": 20, "recipe_match": 25}.get(tool, 1000)
        options.update(ge=1, le=maximum)
    if name in ("rating", "min_rating"):
        options.update(ge=1, le=5)
    if name == "water_ml":
        options.update(ge=1, le=1000)
    if name == "flow_ml_s":
        options.update(ge=0.1, le=10)
    if name in TEXT_FIELDS:
        options.update(min_length=1, pattern=r"\S")
    if name == "expected_recipe_sha256":
        options["pattern"] = "^[0-9a-f]{64}$"
    if name == "cursor":
        options["max_length"] = MAX_CURSOR_LENGTH
    return options


def input_model(fn: Callable[..., Any]) -> Model:
    """Build a strict, closed input model from a tool function's signature."""
    fields: dict[str, Any] = {}
    hints = get_type_hints(fn)
    tool = fn.__name__
    for name, parameter in inspect.signature(fn).parameters.items():
        typ = hints[name]
        if name == "request":
            typ = Request
        if name == "coffee":
            typ = CoffeeMatchInput if tool == "recipe_match" else CoffeeExamplesInput
        empty = parameter.default is inspect.Parameter.empty
        default = ... if empty else parameter.default
        fields[name] = (typ, Field(default, **_options(tool, name)))
    return create_model(tool + "Input", __base__=Input, **fields)


RecipeRecord = model(
    "RecipeRecord",
    id=(str, ..., "Stable local recipe ID."),
    name=(str, ..., "Local display name."),
    revision=(int, ..., "Immutable document revision."),
    document=(dict[str, Any], ..., "Complete normalized authoring document."),
    sha256=(str, ..., "Canonical document digest."),
)
RecipeSummary = model(
    "RecipeSummary",
    id=(str, ..., "Stable local recipe ID."),
    name=(str, ..., "Display name."),
    revision=(int, ..., "Current revision."),
    sha256=(str, ..., "Canonical document digest."),
    temporary=(int, ..., "SQLite temporary flag 0/1."),
    head=(int, ..., "Current immutable revision pointer."),
    created_at=(str, ..., "UTC recipe creation timestamp."),
)
Plan = model(
    "RecipePlan",
    schema_version=(int, ..., "Plan schema version."),
    encoder_version=(str, ..., "Pinned encoder version."),
    recipe_sha256=(str, ..., "Digest of the pinned recipe snapshot."),
    recipe_snapshot=(dict[str, Any], ..., "Complete immutable recipe snapshot."),
    execution=(
        list[dict[str, Any]],
        ...,
        "Ordered host/native execution steps; not evidence of physical execution.",
    ),
    requires_untested=(
        bool,
        ...,
        "Whether physical execution requires experimental opt-in; governs a direct "
        "operation_start.",
    ),
    upload_requires_untested=(
        optional(bool),
        None,
        "machine_prepare only: the experimental opt-in check for a plan "
        "recipe_upload otherwise accepts; not a promise that it can be uploaded. "
        "When false for an experimental plan, upload without allow_untested must "
        "pass this preview's recipe_sha256 as expected_recipe_sha256. Absent from "
        "older daemons and recipe_preview; then follow requires_untested.",
    ),
    requires_user=(list[str], ..., "Known host preparation instructions."),
    limitations=(list[str], ..., "Evidence and physical workflow limits."),
)
Feedback = model(
    "Feedback",
    id=(str, ..., "Feedback ID."),
    operation_id=(str, ..., "Exact rated operation."),
    note=(str, ..., "User feedback."),
    rating=(int | None, ..., "Optional integer rating 1..5."),
)
OperationIdentity = model(
    "OperationIdentity",
    id=(str, ..., "Persisted attempt ID."),
    device=(str | None, ..., "Configured device identity."),
    idempotency_key=(str, ..., "Original attempt key."),
    status=(str, ..., "Persisted status; no implicit recovery."),
    created_at=(str, ..., "UTC acceptance time."),
    updated_at=(str, ..., "UTC last update."),
)
OperationLookup = model(
    "OperationLookup",
    operation=(
        optional(OperationIdentity),
        ...,
        "Compact persisted identity/status or null. Read-only; absence does not "
        "exclude an in-flight start.",
    ),
)
Operation = model(
    "OperationRecord",
    id=(str, ..., "Durable operation ID; use this to inspect/stop the owned attempt."),
    status=(str, ..., "Recorded status, never inferred from an ACK alone."),
    plan=(Plan, ..., "Pinned retained execution plan."),
    request=(dict[str, Any], ..., "Original accepted request."),
    result=(Any, None, "Latest recorded outcome and diagnostics."),
    feedback=(list_of(Feedback), ..., "User feedback for this attempt."),
)
HistorySummary = model(
    "HistorySummary",
    id=(str, ..., "Durable operation ID."),
    status=(str, ..., "Recorded attempt status."),
    recipe_name=(str | None, None, "Name from the immutable snapshot."),
    recipe_sha256=(str | None, None, "Retained snapshot digest."),
    feedback=(
        list_of(Feedback),
        ...,
        "Most recent bounded feedback; use history_get for all entries.",
    ),
    recipe_id=(str | None, ..., "Source local recipe ID, if retained."),
    recipe_revision=(int | None, ..., "Pinned source revision, if retained."),
    rating=(
        int | None,
        ...,
        "Maximum retained feedback rating; not a completion signal.",
    ),
    created_at=(str, ..., "UTC operation acceptance timestamp."),
    updated_at=(str, ..., "Latest durable transition timestamp."),
    feedback_count=(int, ..., "Total feedback entry count."),
    feedback_truncated=(bool, ..., "Whether this summary omits older feedback."),
)
Event = model(
    "HistoryEvent",
    id=(int, ..., "Monotonic durable event ID."),
    kind=(str, ..., "Event kind."),
    data=(Any, None, "Full event data when detail=full."),
    operation_id=(str | None, None, "Related operation if any."),
)
Diagnostic = model(
    "ValidationDiagnostic",
    path=(str, ..., "JSON Pointer to the rejected field; empty string means root."),
    code=(str, ..., "Stable validation rule identifier."),
    message=(str, ..., "Human-readable reason for rejection."),
    expected=(Any, None, "Allowed bounds, enumerated values or expected shape."),
    allowed=(Any, None, "Allowed command schema when supplied."),
    value=(Any, None, "Rejected JSON-safe value."),
    actual_type=(str | None, None, "Original value type."),
    repair_hint=(
        str | None,
        None,
        "How to correct this specific tea field without silent clamping.",
    ),
)
Warning = model(
    "ValidationWarning",
    code=(str, ..., "Stable evidence/compatibility warning code."),
    message=(str, ..., "Evidence limitation or preparation warning."),
)


class DiagnosticRepairGuide(Input):
    local: str = Field(
        description=(
            "Repository checkout-relative path and anchor to the agent repair guide."
        )
    )
    url: str = Field(
        description="Canonical documentation URL for the agent repair guide."
    )
    skill: str | None = Field(
        None,
        description="Optional repository checkout-relative skill path and anchor.",
    )


class DiagnosticLocation(Input):
    kind: str | None = None
    command: str | None = None
    method: str | None = None
    tool: str | None = None
    parameter: str | None = None
    pointer: str | None = None
    path: str | None = None
    line: int | None = Field(None, ge=1)
    column: int | None = Field(None, ge=1)
    slot: str | None = None
    operation_id: str | None = None


class DiagnosticExcerptLine(Input):
    line: int = Field(ge=1)
    text: str
    highlight: bool
    column: int | None = Field(None, ge=1)


class DiagnosticContextInfo(Input):
    summary: str | None = None
    excerpt: list[DiagnosticExcerptLine] | None = None


class CanonicalDiagnostic(Input):
    code: str = Field(description="Stable canonical XB_* diagnostic identifier.")
    cause: str = Field(description="Plain-language cause.")
    fix: str = Field(description="Concrete, safe next action.")
    docs_url: str = Field(description="Canonical per-code documentation URL.")
    repair_guide: DiagnosticRepairGuide = Field(
        description="Canonical and checkout-relative agent repair links."
    )
    location: DiagnosticLocation | None = None
    context: DiagnosticContextInfo | None = None


Validation = model(
    "RecipeValidation",
    valid=(bool, ..., "Whether the document passes all applicable checks."),
    errors=(
        list_of(Diagnostic),
        ...,
        "Every error with JSON Pointer, code, rejected value and allowed values.",
    ),
    warnings=(list_of(Warning), ..., "Evidence/compatibility warnings."),
    normalized=(Any, ..., "Validated normalized document, or null when invalid."),
    execution_supported=(
        bool,
        ...,
        "Whether execution compilation is supported; not hardware verification.",
    ),
    diagnostic=(
        optional(CanonicalDiagnostic),
        None,
        "Canonical diagnostic with stable code, location, fix and documentation "
        "links when validation fails.",
    ),
)


class Observation(Output):
    value: Any = Field(
        description="Observed value, not a presumed physical measurement."
    )
    observed_at: str = Field(description="UTC observation timestamp.")
    age_seconds: float = Field(description="Age at snapshot time.")
    stale: bool = Field(
        description="Whether the observation exceeds its freshness policy."
    )
    available: bool = Field(description="Transport/session/freshness availability.")
    command: int = Field(description="Incoming numeric source command.")
    evidence: str = Field(description="Protocol or hardware evidence label.")
    unit: str | None = Field(
        description="Unit where established; null means raw/unknown."
    )


class TelemetrySnapshot(Output):
    connected: bool = Field(description="Current link state.")
    session_ready: bool = Field(description="Whether session registration completed.")
    revision: int = Field(description="Monotonic cache revision.")
    fields: dict[str, Observation] = Field(
        description="Named observations with source, age, evidence and availability."
    )
    decode_errors: int = Field(
        description="Number of rejected/malformed notifications."
    )
    last_error: str | None = Field(description="Most recent decode error, if any.")


Snapshot = TelemetrySnapshot
InteractionState = model(
    "InteractionState",
    machine=(Snapshot, ..., "Cached telemetry snapshot."),
    recovery_required=(
        bool,
        ...,
        "Uncertain command effects require explicit recovery.",
    ),
    query_source=(
        str,
        ...,
        "notifications_cache; no active status-query opcode is sent.",
    ),
)
State = model(
    "MachineState",
    device=(str | None, ..., "Configured machine identity, or null."),
    machine=(
        optional(InteractionState),
        ...,
        "Cached transport/observations/owned low-level state; null before connection.",
    ),
    operation=(
        dict[str, Any] | None,
        ...,
        "Current durable operation projection.",
    ),
    prepared=(
        dict[str, Any] | None,
        None,
        "Session-local preparation token/status and recovery information; null "
        "when absent.",
    ),
    gate=(dict[str, Any] | None, ..., "Current host-only user gate."),
    recording=(dict[str, Any] | None, ..., "Recorder health; null before connection."),
)
Prepared = model(
    "PreparedUpload",
    token=(str, ..., "Session-local single-use preparation token."),
    kind=(str, ..., "recipe or tea."),
    status=(str, ..., "Preparation status."),
    recipe_sha256=(str, ..., "Pinned recipe digest."),
    readback_verified=(
        bool,
        ...,
        "False: upload does not establish recipe readback.",
    ),
)


PresetWrite = model(
    "PresetWrite",
    write_id=(
        str,
        ...,
        "Write ID; acknowledge_incomplete names it after user direction.",
    ),
    preset=(dict[str, Any], ..., "Exact slot, payload_hex and flags to resubmit."),
    state=(
        str,
        ...,
        "retryable, exhausted, cancelled, saved, not_sent, in_progress or "
        "superseded (a batch may have changed the slot since).",
    ),
    attempts_used=(
        int,
        ...,
        "Attempts that reached preparation, including no_preset_frame ones.",
    ),
    max_attempts=(int, ..., "Attempt limit per machine and slot."),
    remaining_attempts=(int, ..., "Identical attempts still allowed."),
    retry_allowed=(bool, ..., "Whether the identical write may be resubmitted."),
    attempts=(
        list[dict[str, Any]],
        ...,
        "Attempt IDs, start times, outcomes and frames sent.",
    ),
    next_step=(str, ..., "What a caller may do next."),
    readback_verified=(bool, ..., "False: nothing read the slot back."),
)
PresetWrites = model(
    "PresetWrites",
    device=(str, ..., "Machine identity the writes belong to."),
    max_attempts=(int, ..., "Attempt limit per machine and slot."),
    writes=(list_of(PresetWrite), ..., "Latest recorded write per slot."),
)


class CommandOutcome(Output):
    model_config = ConfigDict(
        extra="allow",
        strict=True,
        allow_inf_nan=False,
        json_schema_extra={
            "anyOf": [
                {"required": ["command", "acknowledged"]},
                {"required": ["machine", "query_source"]},
                {
                    "required": [
                        "requested_slots",
                        "acknowledged_write_count",
                        "saved_observed",
                    ]
                },
                {"required": ["status", "details", "recovery_required"]},
            ]
        },
    )
    command: int | None = Field(None, description="Numeric command when applicable.")
    acknowledged: bool | None = Field(
        None, description="ACK observation, not a physical-effect guarantee."
    )
    machine: TelemetrySnapshot | None = Field(
        None, description="Owned standalone scale telemetry snapshot."
    )
    query_source: str | None = Field(
        None, description="Cache provenance for scale operations."
    )
    status: str | None = Field(None, description="Failure status for partial writes.")
    details: dict[str, Any] | None = Field(
        None, description="Partial-write backup/ACK recovery details when present."
    )
    recovery_required: bool | None = Field(
        None, description="Whether explicit recovery is required."
    )
    backup: dict[str, Any] | None = Field(
        None,
        description="Intentional caller-backup identity and service-owned path; "
        "null when no backup was supplied.",
    )
    attempt: dict[str, Any] | None = Field(
        None,
        description="Single-slot preset attempt: write ID, outcome, attempts used "
        "and remaining, retry_allowed and next_step.",
    )
    requested_slots: list[str] | None = Field(
        None, description="Requested physical preset slots."
    )
    acknowledged_write_count: int | None = Field(
        None, description="Number of individually ACKed preset writes."
    )
    saved_observed: bool | None = Field(
        None,
        description="Temporally correlated save event; not stored contents readback.",
    )
    diagnostic: CanonicalDiagnostic | None = Field(
        None,
        description="Canonical diagnostic with stable code, location, fix and "
        "documentation links when a command reports failure.",
    )

    @model_validator(mode="after")
    def recognized_result(self) -> CommandOutcome:
        if (
            (self.command is not None and self.acknowledged is not None)
            or (self.machine is not None and self.query_source is not None)
            or (
                self.requested_slots is not None
                and self.acknowledged_write_count is not None
                and self.saved_observed is not None
            )
            or (
                self.status == "failed"
                and self.details is not None
                and self.recovery_required is not None
            )
        ):
            return self
        raise ValueError(
            "Expected command ACK, owned scale state or preset-write outcome"
        )


CommandCapability = model(
    "CommandCapability",
    description=(str, ..., "Meaning and physical limitations of this command."),
    input_schema=(
        dict[str, Any],
        ...,
        "Platform-neutral JSON schema for params; shared with application validation.",
    ),
    requires_allow_control=(bool, ..., "Explicit operation authorization is required."),
    requires_allow_untested=(
        bool,
        ...,
        "Whether this command always requires experimental opt-in.",
    ),
    conditional_experimental=(
        list[str],
        ...,
        "Additional parameter-dependent experimental requirements.",
    ),
    parameters=(
        str,
        ...,
        "Legacy Python signature for compatibility; use input_schema for new callers.",
    ),
)
Capabilities = model(
    "MachineCapabilities",
    commands=(
        dict.__class_getitem__((str, CommandCapability)),
        ...,
        "Command descriptions, JSON schemas and experimental flags.",
    ),
    preset_backup=(
        str,
        ...,
        "Caller-supplied backup provenance and service path policy.",
    ),
    prepared_recipe=(str, ..., "Preparation lifetime and execution rules."),
    native_stage_advance=(bool, ..., "False: no native remote prompt advance."),
    calibration_exposed=(
        bool,
        ...,
        "False for this application command interface.",
    ),
)
Constraints = model(
    "RecipeConstraints",
    version=(int, ..., "Constraint document version."),
    tea=(dict[str, Any], ..., "Tea authoring field bounds."),
    modes=(Any, None, "Supported authoring modes when published."),
)
Examples = model(
    "CoffeeExamples",
    corpus_version=(int, ..., "Bundled example collection version."),
    snapshot_date=(str | None, ..., "Collection capture date, if any."),
    examples=(
        list[dict[str, Any]],
        ...,
        "Matches, scores and conflicting/unknown metadata.",
    ),
    limitations=(
        list[str],
        ...,
        "No taste or independent-source confidence guarantee.",
    ),
)
TeaReferences = model(
    "TeaReferences",
    version=(int, ..., "Reference collection version."),
    available_families=(list[str], ..., "Supported canonical tea families."),
    families=(
        list[dict[str, Any]],
        ...,
        "Range summaries and coherent starting profiles.",
    ),
    nominal_output_ml=(
        int,
        ...,
        "Catalogue estimate by steep count, not a physical yield measurement.",
    ),
    yield_verified=(bool, ..., "False: not a measured cup yield."),
)
Template = model(
    "RecipeTemplate",
    id=(str, ..., "Stable bundled template ID."),
    document=(dict[str, Any], ..., "Draft document; never a physical preset readback."),
)
RecipeMatch = model(
    "RecipeMatch",
    id=(str, ..., "Matched saved recipe ID."),
    match_score=(float, ..., "Metadata overlap score, not taste confidence."),
)
ImportRecord = model(
    "SourceImport",
    id=(str, ..., "Retained source-import ID."),
    source=(dict[str, Any], ..., "Attributed provider data."),
    document=(
        dict[str, Any] | None,
        ...,
        "Normalized document if conversion succeeded.",
    ),
    error=(str | None, ..., "Blocked-conversion explanation when applicable."),
)
Compare = model(
    "RecipeComparison",
    before=(dict[str, Any], ..., "Earlier normalized document."),
    after=(dict[str, Any], ..., "Later normalized document."),
    diff=(list[str], ..., "Textual unified diff."),
)
Refresh = model(
    "RecipeRefresh",
    recipe_id=(str, ..., "Current local recipe ID."),
    requires_explicit_update=(
        bool,
        ...,
        "True: refresh never overwrites local content.",
    ),
)
ProviderSearch = model(
    "ProviderSearch",
    provider=(str, ..., "Public provider name."),
    page=(int, ..., "Provider page index."),
    results=(
        list[dict[str, Any]],
        ...,
        "Public search results, not validated executable recipes.",
    ),
)
ProviderCapabilities = model(
    "ProviderCapabilities",
    collective=(dict[str, bool], ..., "Supported Collective operations."),
    xrecipe=(dict[str, bool], ..., "Supported unofficial hub operations."),
)


def page_model(name: str, item: Any) -> Model:
    return model(
        name,
        items=(
            list_of(item),
            ...,
            "Ordered page items; detail selects summary or full data.",
        ),
        scan_exhausted=(
            bool,
            ...,
            "Whether the bounded candidate scan ended before filling this page; "
            "continue even if items is empty.",
        ),
        has_more=(bool, ..., "Whether traversal can continue."),
        next_cursor=(str | None, ..., "Opaque cursor for these exact filters."),
        limit=(int, ..., "Requested maximum number of returned items."),
        count=(int, ..., "Number of returned items."),
        live_collection=(
            bool,
            ...,
            "True: concurrent changes can affect later pages.",
        ),
        ordering=(str, ..., "Stable traversal ordering."),
    )


OUTPUTS: dict[str, Model] = {
    "recipe_examples": Examples,
    "recipe_templates": model(
        "Templates",
        result=(list_of(Template), ..., "Bundled starter recipe templates."),
    ),
    "recipe_constraints": Constraints,
    "recipe_validate": Validation,
    "recipe_tea_validate": Validation,
    "recipe_tea_references": TeaReferences,
    "recipe_preview": Plan,
    "recipe_create": RecipeRecord,
    "recipe_get": RecipeRecord,
    "recipe_update": RecipeRecord,
    "recipe_list": page_model("RecipePage", one_of(RecipeRecord, RecipeSummary)),
    "recipe_compare": Compare,
    "recipe_import_get": ImportRecord,
    "recipe_compile": Plan,
    "recipe_import": ImportRecord,
    "recipe_import_export": ImportRecord,
    "recipe_archive": model(
        "ArchivedRecipe",
        id=(str, ..., "Local recipe ID."),
        archived=(bool, ..., "Archive result; history remains available."),
    ),
    "recipe_alias": model(
        "RecipeAlias",
        alias=(str, ..., "Local alias."),
        recipe_id=(str, ..., "Resolved local recipe ID."),
    ),
    "recipe_match": model(
        "RecipeMatches", result=(list_of(RecipeMatch), ..., "Ranked local candidates.")
    ),
    "recipe_refresh": Refresh,
    "provider_search": ProviderSearch,
    "provider_capabilities": ProviderCapabilities,
    "history_list": page_model("HistoryPage", one_of(Operation, HistorySummary)),
    "history_get": Operation,
    "history_events": page_model("EventPage", Event),
    "history_feedback": Feedback,
    "history_save_recipe": RecipeRecord,
    "machine_capabilities": Capabilities,
    "machine_command": CommandOutcome,
    "recipe_upload": Prepared,
    "recipe_start_uploaded": Operation,
    "recipe_cancel_uploaded": model(
        "CancelledUpload",
        token=(str, ..., "Invalidated preparation token."),
        cancelled=(
            bool,
            ...,
            "True when preparation-exit and home commands were acknowledged. "
            "False when preparation was already invalidated and the reservation "
            "was released without sending commands.",
        ),
    ),
    "machine_state": State,
    "machine_preset_writes": PresetWrites,
    "machine_prepare": Plan,
    "machine_connect": State,
    "machine_disconnect": model(
        "Disconnected",
        disconnected=(
            bool,
            ...,
            "Transport ownership released; not proof native preparation canceled.",
        ),
    ),
    "operation_lookup": OperationLookup,
    "operation_start": Operation,
    "operation_stop": Operation,
    "operation_pause_resume": State,
    "operation_acknowledge": model(
        "AcknowledgedGate",
        acknowledged=(
            bool,
            ...,
            "Host user gate acknowledged; not native stage advance.",
        ),
    ),
}


def normalized_input(value: BaseModel, raw: Any = None) -> dict[str, Any]:
    """Apply request defaults; omit nullable options with application semantics.

    Supplied request fields keep their raw JSON values, so the same request
    reaches the application identically from every adapter; only omitted
    defaults are added. Authoring documents stay untouched, including
    explicit nulls for diagnostics.
    """
    result = value.model_dump(exclude_unset=True)
    fields = type(value).model_fields
    if "request" in fields:
        request = cast(Any, value).request.model_dump(exclude_none=True)
        supplied = raw.get("request") if isinstance(raw, dict) else None
        if isinstance(supplied, dict):
            request = {
                key: supplied[key] if key in supplied else default
                for key, default in request.items()
            }
        result["request"] = request
    if "coffee" in fields:
        result["coffee"] = cast(Any, value).coffee.model_dump(
            exclude_none=True, exclude_unset=True
        )
    return result


def compact_state(state: dict[str, Any]) -> dict[str, Any]:
    """Project cached state for routine inspection without mutating journal data."""
    result = deepcopy(state)
    operation = result.get("operation")
    if isinstance(operation, dict):
        plan = operation.get("plan") or {}
        result["operation"] = {
            key: operation[key]
            for key in (
                "id",
                "status",
                "device",
                "idempotency_key",
                "created_at",
                "updated_at",
            )
            if key in operation
        }
        for key in ("recipe_id", "recipe_revision", "recipe_sha256"):
            if key in plan:
                result["operation"][key] = plan[key]
    machine = result.get("machine")
    if isinstance(machine, dict):
        # Interaction.get_state wraps the telemetry snapshot under machine.
        snapshot = machine.get("machine")
        if isinstance(snapshot, dict):
            snapshot.pop("events", None)
            snapshot["fields"] = {
                key: value
                for key, value in snapshot.get("fields", {}).items()
                if key not in ("device_info_raw", "model_raw_hex", "preset_state_hex")
            }
        low_operation = machine.get("operation")
        if isinstance(low_operation, dict):
            machine["operation"] = {
                key: value
                for key, value in low_operation.items()
                if key not in ("final_state", "state", "events", "last_machine_event")
            }
    return result
