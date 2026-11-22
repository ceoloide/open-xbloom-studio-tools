"""Pure authoring constraints and exhaustive diagnostics; no storage or machine I/O."""

from __future__ import annotations

import math
from copy import deepcopy
from decimal import Decimal
from typing import Any

from .command_schema import MAX_OPERATION_TIMEOUT, water_timeout
from .diagnostics import validation_diagnostic

Rule = dict[str, Any]


def numeric(
    low: float, high: float, *, whole: bool = False, strict: bool = False, **extra: Any
) -> Rule:
    return {
        "type": "integer" if strict else "number",
        "minimum": low,
        "maximum": high,
        **({"whole_number": True} if whole else {}),
        **extra,
    }


def choice(*values: Any, **extra: Any) -> Rule:
    return {"enum": list(values), **extra}


UPLOAD_ALIAS = (
    "Legacy alias for set_grinder_size upload selection, not proof of a grinding stage."
)
UPLOAD_SELECTION = (
    "Select upload 8001 instead of 8004; does not establish actual grinding behavior."
)
PATTERNS = ("center", "ring", "spiral")

COMMON: dict[str, Rule] = {
    "schema_version": {"type": "integer", "enum": [1], "default": 1},
    "name": {"type": "string", "nonblank": True, "required": True},
    "coffee": {"type": "object"},
    "description": {"type": "json"},
    "source": {"type": "object"},
    "mode": choice("host", "native", "tea", "coffee", default="host"),
}
HOST_STEPS: dict[str, dict[str, Rule]] = {
    "water": {
        "volume_ml": numeric(1, 1000, required=True),
        "temperature_c": numeric(20, 99, required=True),
        "flow_ml_s": numeric(0.1, 10, default=3),
        "pattern": choice(*PATTERNS, default="center"),
        "timeout": numeric(0.1, MAX_OPERATION_TIMEOUT),
    },
    "grind": {
        "size": numeric(1, 80, whole=True, required=True),
        "rpm": numeric(60, 120, whole=True, required=True),
        "duration_s": numeric(0.1, 120, required=True),
    },
    "wait": {"duration_s": numeric(0, 3600, required=True)},
    "user": {"instruction": {"type": "string", "nonblank": True, "required": True}},
    "tare": {},
}
HOST_STEP_LIST: Rule = {
    "type": "array",
    "list_only": True,
    "min_items": 1,
    "max_items": 100,
}
NATIVE: dict[str, Rule] = {
    "payload_hex": {
        "type": "hex",
        "min_bytes": 1,
        "max_bytes": 512,
        "required": True,
    },
    "dose_g": numeric(1, 100, whole=True, required=True),
    "grinder_enabled": {
        "type": "boolean",
        "default": False,
        "description": UPLOAD_ALIAS,
    },
    "set_grinder_size": {"type": "boolean", "description": UPLOAD_SELECTION},
    "cup_limits": {
        "type": "array",
        "min_items": 2,
        "max_items": 2,
        "items": numeric(0, 5000),
        "nullable": True,
        "ordering": "minimum, maximum; maximum must be >= minimum",
    },
    "bypass_ml": numeric(0, 1000, default=0),
    "bypass_temperature_c": numeric(20, 99, default=20),
}
EDITOR_TEMPERATURE: Rule = {
    "type": "tea_temperature",
    "minimum": 40,
    "maximum": 95,
    "enum_alternatives": ["RT", "BP"],
    "required": True,
}
FLOWS = [3, 3.1, 3.2, 3.3, 3.4, 3.5]
TEA_STEEP: dict[str, Rule] = {
    "volume_ml": numeric(1, 90, strict=True, required=True),
    "steep_seconds": numeric(5, 360, strict=True, required=True),
    "temperature": {**EDITOR_TEMPERATURE, "maximum": 99, "numeric_exclusions": [96]},
    "flow_ml_s": numeric(3, 3.5, enum=FLOWS),
    "vibration_before": {"type": "boolean"},
    "vibration_after": {"type": "boolean"},
    "pattern": choice(*PATTERNS, default="center"),
}
TEA: dict[str, Rule] = {
    "dose_g": numeric(1, 5, required=True),
    "steeps": {
        "type": "array",
        "list_only": True,
        "min_items": 1,
        "max_items": 3,
        "properties": TEA_STEEP,
        "required": True,
    },
    "siphon_trigger": choice("auto", default="auto"),
    "grinder_size_metadata": numeric(1, 80, strict=True),
    "rpm_metadata": numeric(60, 120, strict=True, enum=list(range(60, 121, 10))),
    "ratio_metadata": numeric(1, 100, multiple_of=0.1),
}

COFFEE_POUR: dict[str, Rule] = {
    "volume_ml": numeric(0, 240, strict=True, required=True),
    "temperature": {**EDITOR_TEMPERATURE},
    "flow_ml_s": numeric(3, 3.5, enum=FLOWS, default=3),
    "pause_seconds": numeric(0, 59, strict=True, default=0),
    "pattern": choice(*PATTERNS, default="center"),
    "vibration_before": {"type": "boolean", "default": False},
    "vibration_after": {"type": "boolean", "default": False},
}
COFFEE: dict[str, Rule] = {
    "cup_type": choice("xpod", "xdripper", "other", required=True),
    "dose_g": numeric(5, 25, strict=True, required=True),
    "grind_size": numeric(1, 80, strict=True, required=True),
    "rpm": numeric(60, 120, strict=True, enum=list(range(60, 121, 10)), default=120),
    "grinder_enabled": {
        "type": "boolean",
        "default": False,
        "description": UPLOAD_ALIAS,
    },
    "set_grinder_size": {"type": "boolean", "description": UPLOAD_SELECTION},
    "ratio": numeric(5, 25, enum=[5 + i / 2 for i in range(41)], required=True),
    "pours": {
        "type": "array",
        "list_only": True,
        "min_items": 1,
        "max_items": 9,
        "properties": COFFEE_POUR,
        "required": True,
    },
    "bypass": {
        "type": "object",
        "nullable": True,
        "default": None,
        "properties": {
            "volume_ml": numeric(5, 100, strict=True, required=True),
            "temperature": {**EDITOR_TEMPERATURE},
        },
    },
}
# ``android_app`` names the app version whose recipe encoding is reproduced;
# it is a compatibility label, not an affiliation.
COFFEE_REFERENCE: dict[str, Any] = {
    "android_app": "2.2.3",
    "conversion": "Celsius editor model temperatures and explicit decimal JSON flow",
    "encoder_version": "studio-coffee-android-2.2.3-v2",
    "cup_limits": {"xpod": [40, 70], "xdripper": [90, 110], "other": [80, 200]},
    "cup_limits_evidence": (
        "Reference cup limits sent with each coffee recipe; their physical "
        "meaning and any dependence on the recipe are unverified."
    ),
    "pour_temperature_codes": {"RT": 20, "BP": 98},
    "bypass_temperature_codes": {"RT": 20, "BP": 96},
    "temperature_evidence": (
        "RT and BP are encoded as fixed codes; the bypass BP code differs from "
        "the pour BP code."
    ),
    "dose_limits": {"xpod": [15, 15], "xdripper": [5, 18], "other": [5, 25]},
    "pour_volume_total": (
        "sum(pour.volume_ml) must equal floor(dose_g * ratio); bypass excluded"
    ),
    "cached_flow_note": (
        "Flow is encoded from its exact decimal value, so 3.1 and 3.3 ml/s "
        "encode as 31 and 33."
    ),
    "execution_supported": True,
    "requires_untested": True,
}

TEA_REFERENCE: dict[str, Any] = {
    "android_app": "2.2.3",
    "fixed_new_recipe_values": {
        "flow_ml_s": 3,
        "vibration_before": False,
        "vibration_after": False,
        "grinder_size_metadata": 50,
        "rpm_metadata": 120,
        "ratio_metadata": 15,
    },
    "estimated_ml_per_steep": 120,
    "dose_note": (
        "Manual preparation metadata: finite 1–5 g. Fractional doses are allowed "
        "and are not sent to the machine."
    ),
    "temperature_note": (
        "RT/BP encode the codes 39/96; numeric 96 is rejected. Numeric 40..99 are "
        "supported encoding inputs, not measured outlet temperatures."
    ),
    "preserved_metadata_note": (
        "Optional metadata is serialized as given, without established physical "
        "bounds or grind/yield semantics. Ratio*10 is narrowed to one byte, with "
        "an explicit warning above 25.5."
    ),
    "execution_supported": True,
    "requires_untested": True,
    "cached_cup_limits": [40, 80],
    "temperature_sentinels": {"RT": 39, "BP": 96},
    "pattern_codes": {"center": 0, "ring": 1, "spiral": 2},
}

TEA_UNVALIDATED = (
    "Tea encoding follows the reference recipe format; physical execution "
    "remains unvalidated and requires allow_untested."
)
COFFEE_UNVALIDATED = (
    "Coffee encoding follows the reference recipe format; physical workflow "
    "remains unvalidated and requires allow_untested. Native prompts may need "
    "physical input."
)
RATIO_WRAP = (
    "Ratio metadata*10 is narrowed to one byte on the wire. The original "
    "metadata is retained; its firmware meaning is unverified. This is not a "
    "dose/output ratio guarantee."
)


def recipe_constraints() -> dict[str, Any]:
    """Detached, versioned constraints for recipe authors, including fixed values."""
    return deepcopy(
        {
            "version": 1,
            "scope": "recipe_definitions",
            "common": COMMON,
            "host": {"steps": HOST_STEP_LIST, "step_kinds": HOST_STEPS},
            "native": NATIVE,
            "tea": TEA,
            "coffee_recipe": COFFEE,
            "coffee_reference": COFFEE_REFERENCE,
            "tea_reference": TEA_REFERENCE,
            "notes": [
                "Bounds describe supported recipe definitions, not machine "
                "readiness or physical accuracy.",
                "Host/native whole_number fields accept whole floats for "
                "compatibility; tea volumes, durations and numeric temperatures "
                "are strict integers.",
                "Native payload bytes are opaque; validity covers envelope/settings "
                "only.",
                "Tea and coffee hardware execution remains unvalidated and requires "
                "allow_untested.",
                "Unknown fields are rejected; coffee/source objects and description "
                "accept JSON metadata.",
            ],
        }
    )


def _pointer(path: str, key: object) -> str:
    try:
        text = str(key)
    except TypeError, ValueError:
        text = f"<invalid {type(key).__name__} key>"
    return path + "/" + text.replace("~", "~0").replace("/", "~1")


def _actual(value: object) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, int) and value.bit_length() > 4096:
        return f"<integer with {value.bit_length()} bits>"
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return f"<{type(value).__name__}>"


PROFILES = {
    "host": "steps",
    "native": "native",
    "tea": "tea",
    "coffee": "coffee_recipe",
}


def check_recipe(document: Any) -> dict[str, Any]:
    """Return every independent error with allowed values; never save or connect."""
    errors: list[dict[str, Any]] = []
    seen_errors: set[tuple[str, str]] = set()

    def issue(
        path: str,
        code: str,
        message: str,
        expected: Any,
        value: Any = None,
        missing: bool = False,
    ) -> None:
        key = (path, code)
        if key not in seen_errors:
            seen_errors.add(key)
            errors.append(
                {
                    "path": path,
                    "code": code,
                    "message": message,
                    "expected": deepcopy(expected),
                    "actual_type": "missing" if missing else type(value).__name__,
                    "value": "<missing>" if missing else _actual(value),
                }
            )

    def field(value: Any, rule: Rule, path: str) -> bool:
        if value is None and rule.get("nullable"):
            return True
        kind = rule.get("type")
        numeric_type = isinstance(value, (int, float)) and not isinstance(value, bool)
        typed = {
            "integer": type(value) is int,
            "number": numeric_type,
            "string": isinstance(value, str),
            "hex": isinstance(value, str),
            "boolean": type(value) is bool,
            "object": isinstance(value, dict),
            "array": isinstance(value, list)
            if rule.get("list_only")
            else isinstance(value, (list, tuple)),
            "json": True,
            None: True,
            "tea_temperature": type(value) is int or isinstance(value, str),
        }
        if not typed[kind]:
            issue(path, "type", f"Expected {kind}", rule, value)
            return False
        if kind == "tea_temperature":
            valid = (
                type(value) is int
                and rule["minimum"] <= value <= rule["maximum"]
                and value not in rule.get("numeric_exclusions", [])
            ) or value in rule["enum_alternatives"]
            if not valid:
                issue(
                    path,
                    "choice",
                    "Use an integer Celsius value within bounds, RT, or BP "
                    "(not numeric sentinels)",
                    rule,
                    value,
                )
            return valid
        if kind in ("integer", "number"):
            if isinstance(value, float) and not math.isfinite(value):
                issue(path, "non_finite", "Use a finite number", rule, value)
                return False
            if (
                "minimum" in rule and not rule["minimum"] <= value <= rule["maximum"]
            ) or (rule.get("whole_number") and int(value) != value):
                issue(
                    path,
                    "range",
                    "Value must be within the inclusive bounds"
                    + (" and a whole number" if rule.get("whole_number") else ""),
                    rule,
                    value,
                )
                return False
            if "multiple_of" in rule and Decimal(str(value)) % Decimal(
                str(rule["multiple_of"])
            ):
                issue(
                    path,
                    "precision",
                    "Use a multiple of the supported increment",
                    rule,
                    value,
                )
                return False
        if "enum" in rule and (isinstance(value, bool) or value not in rule["enum"]):
            issue(path, "choice", "Choose one of the allowed values", rule, value)
            return False
        if rule.get("nonblank") and not value.strip():
            issue(path, "blank", "A nonblank string is required", rule, value)
            return False
        if kind == "hex":
            try:
                payload = bytes.fromhex(value)
            except ValueError:
                issue(
                    path,
                    "hex",
                    "Use pairs of hexadecimal digits (whitespace is allowed)",
                    rule,
                    value,
                )
                return False
            if not rule["min_bytes"] <= len(payload) <= rule["max_bytes"]:
                issue(
                    path,
                    "length",
                    "Encoded byte length is outside the inclusive bounds",
                    rule,
                    value,
                )
                return False
        if kind == "array":
            if not rule["min_items"] <= len(value) <= rule["max_items"]:
                issue(
                    path,
                    "length",
                    "Array length is outside the inclusive bounds",
                    rule,
                    value,
                )
            for i, item in enumerate(value):
                if "properties" in rule:
                    obj(item, rule["properties"], _pointer(path, i))
                elif "items" in rule:
                    field(item, rule["items"], _pointer(path, i))
        if kind == "object" and "properties" in rule:
            obj(value, rule["properties"], path)
        return True

    def obj(value: Any, properties: dict[str, Rule], path: str) -> None:
        if not isinstance(value, dict):
            issue(
                path,
                "type",
                "Expected an object",
                {"type": "object", "allowed_fields": list(properties)},
                value,
            )
            return
        for key in value:
            if key not in properties:
                issue(
                    _pointer(path, key),
                    "unknown_field",
                    "Unsupported field",
                    {"allowed_fields": list(properties)},
                    value[key],
                )
        for name, rule in properties.items():
            p = _pointer(path, name)
            if name in value:
                field(value[name], rule, p)
            elif rule.get("required"):
                issue(p, "missing", "Required field is missing", rule, missing=True)

    # JSON metadata must not defeat report serialization or persistence later.
    def json_value(
        value: Any, path: str = "", ancestors: set[int] | None = None
    ) -> None:
        ancestors = set() if ancestors is None else ancestors
        if value is None or isinstance(value, (str, bool)):
            return
        if isinstance(value, int):
            try:
                str(value)
            except ValueError:
                issue(
                    path,
                    "json_integer",
                    "Integer exceeds the JSON runtime digit limit",
                    {"type": "JSON-serializable integer"},
                    value,
                )
        elif isinstance(value, float):
            if not math.isfinite(value):
                issue(
                    path,
                    "non_finite",
                    "JSON numbers must be finite",
                    {"type": "finite JSON number"},
                    value,
                )
        elif isinstance(value, (dict, list, tuple)):
            if id(value) in ancestors or len(ancestors) >= 100:
                issue(
                    path,
                    "json",
                    "JSON must be acyclic and at most 100 containers deep",
                    {"type": "JSON"},
                    value,
                )
                return
            ancestors.add(id(value))
            items = value.items() if isinstance(value, dict) else enumerate(value)
            for key, child in items:
                p = _pointer(path, key)
                if isinstance(value, dict) and not isinstance(key, str):
                    issue(
                        p,
                        "json_key",
                        "JSON object keys must be strings",
                        {"type": "string key"},
                        key,
                    )
                json_value(child, p, ancestors)
            ancestors.remove(id(value))
        else:
            issue(
                path, "json", "Value is not JSON serializable", {"type": "JSON"}, value
            )

    mode = document.get("mode", "host") if isinstance(document, dict) else None
    profile = PROFILES.get(mode) if isinstance(mode, str) else None
    properties = {**COMMON, **{k: {"type": "json"} for k in PROFILES.values()}}
    obj(document, properties, "")
    if isinstance(document, dict) and profile:
        for key in PROFILES.values():
            if key != profile and key in document:
                issue(
                    "/" + key,
                    "conflict",
                    f"{mode} recipes cannot contain {key}",
                    {"mode": mode, "content_field": profile},
                    document[key],
                )
        if profile not in document:
            issue(
                "/" + profile,
                "missing",
                "Recipe content is required",
                {"type": "array" if mode == "host" else "object"},
                missing=True,
            )
        elif mode == "host":
            _check_host(document["steps"], field, obj, issue)
        elif mode == "native":
            _check_native(document["native"], obj, issue)
        elif mode == "tea":
            obj(document["tea"], TEA, "/tea")
        else:
            content = document["coffee_recipe"]
            obj(content, COFFEE, "/coffee_recipe")
            if isinstance(content, dict):
                _check_coffee(content, errors, issue)
    if mode in ("native", "coffee") and isinstance(document, dict) and profile:
        content = document.get(profile)
        if isinstance(content, dict) and all(
            type(content.get(k)) is bool
            for k in ("grinder_enabled", "set_grinder_size")
        ):
            if content["grinder_enabled"] != content["set_grinder_size"]:
                issue(
                    "/" + profile + "/set_grinder_size",
                    "conflict",
                    "Upload aliases must agree",
                    {"same_as": "grinder_enabled"},
                    content["set_grinder_size"],
                )
    json_value(document)
    warnings = _warnings(document, mode, bool(errors))
    normalized = None if errors else _normalize(document, mode, profile)
    report: dict[str, Any] = {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "normalized": normalized,
        "validation_scope": "opaque_envelope_and_settings"
        if mode == "native"
        else "recipe_definition",
        "execution_supported": not errors
        and mode in ("host", "native", "tea", "coffee"),
    }
    if errors:
        report["diagnostic"] = validation_diagnostic(errors)
    return report


def _check_host(steps: Any, field: Any, obj: Any, issue: Any) -> None:
    if not field(steps, HOST_STEP_LIST, "/steps"):
        return
    for i, step in enumerate(steps):
        path = "/steps/" + str(i)
        if not isinstance(step, dict):
            obj(step, {"kind": choice(*HOST_STEPS, required=True)}, path)
            continue
        kind = step.get("kind")
        rules = HOST_STEPS.get(kind) if isinstance(kind, str) else None
        if rules is None:
            if "kind" not in step:
                issue(
                    path + "/kind",
                    "missing",
                    "Required step kind is missing",
                    choice(*HOST_STEPS),
                    missing=True,
                )
            else:
                field(kind, choice(*HOST_STEPS), path + "/kind")
        else:
            obj(step, {"kind": choice(*HOST_STEPS, required=True), **rules}, path)
            if kind == "water" and "timeout" not in step:
                volume = step.get("volume_ml")
                flow = step.get("flow_ml_s", rules["flow_ml_s"]["default"])
                if (
                    isinstance(volume, (int, float))
                    and not isinstance(volume, bool)
                    and isinstance(flow, (int, float))
                    and not isinstance(flow, bool)
                    and 1 <= volume <= 1000
                    and 0.1 <= flow <= 10
                    and water_timeout(volume, flow) > MAX_OPERATION_TIMEOUT
                ):
                    issue(
                        path + "/timeout",
                        "required_timeout",
                        "Specify a timeout when the default exceeds the limit",
                        rules["timeout"],
                        missing=True,
                    )


def _check_native(native: Any, obj: Any, issue: Any) -> None:
    obj(native, NATIVE, "/native")
    limits = native.get("cup_limits") if isinstance(native, dict) else None
    rule = NATIVE["cup_limits"]["items"]
    if (
        isinstance(limits, (list, tuple))
        and len(limits) == 2
        and all(
            type(x) in (int, float) and rule["minimum"] <= x <= rule["maximum"]
            for x in limits
        )
        and limits[1] < limits[0]
    ):
        issue(
            "/native/cup_limits/1",
            "ordering",
            "Maximum cup limit must be >= minimum",
            numeric(limits[0], rule["maximum"]),
            limits[1],
        )


def _check_coffee(
    content: dict[str, Any], errors: list[dict[str, Any]], issue: Any
) -> None:
    def usable(*fields: str) -> bool:
        return not any(
            e["path"] == "/coffee_recipe"
            or any(e["path"].startswith("/coffee_recipe/" + key) for key in fields)
            for e in errors
        )

    if usable("cup_type", "dose_g"):
        low, high = COFFEE_REFERENCE["dose_limits"][content["cup_type"]]
        if not low <= content["dose_g"] <= high:
            issue(
                "/coffee_recipe/dose_g",
                "cup_dose",
                "Dose must match the selected cup type",
                numeric(low, high, strict=True),
                content["dose_g"],
            )
    # The total depends on volumes, not on unrelated pattern or flow errors.
    pours = content.get("pours")
    invalid_paths = {e["path"] for e in errors}
    volumes_valid = isinstance(pours, list) and all(
        isinstance(p, dict)
        and "volume_ml" in p
        and f"/coffee_recipe/pours/{i}/volume_ml" not in invalid_paths
        for i, p in enumerate(pours)
    )
    if usable("dose_g", "ratio") and volumes_valid and isinstance(pours, list):
        expected_total = int(
            Decimal(str(content["dose_g"])) * Decimal(str(content["ratio"]))
        )
        actual_total = sum(p["volume_ml"] for p in pours)
        if expected_total != actual_total:
            issue(
                "/coffee_recipe/pours",
                "volume_total",
                "Pour volumes must equal floor(dose_g * ratio); bypass is separate",
                {"total_ml": expected_total},
                actual_total,
            )


def _warnings(document: Any, mode: Any, failed: bool) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if mode == "native":
        warnings.append(
            {
                "code": "opaque_native",
                "message": "Only the envelope/settings are validated; encoded "
                "recipe semantics are unknown.",
            }
        )
    elif mode == "tea":
        warnings.append(
            {"code": "tea_hardware_unvalidated", "message": TEA_UNVALIDATED}
        )
    elif mode == "coffee":
        warnings.append(
            {"code": "coffee_hardware_unvalidated", "message": COFFEE_UNVALIDATED}
        )
    if mode == "tea" and not failed:
        dose = document["tea"]["dose_g"]
        if dose != int(dose):
            warnings.append(
                {
                    "code": "manual_fractional_dose",
                    "message": TEA_REFERENCE["dose_note"],
                }
            )
        ratio = document["tea"].get("ratio_metadata", 15)
        if Decimal(str(ratio)) * 10 > 255:
            warnings.append(
                {
                    "code": "tea_ratio_byte_wrap",
                    "path": "/tea/ratio_metadata",
                    "value": ratio,
                    "encoded_byte": int(Decimal(str(ratio)) * 10) & 255,
                    "message": RATIO_WRAP,
                }
            )
    return warnings


def _normalize(document: Any, mode: Any, profile: Any) -> dict[str, Any]:
    normalized: dict[str, Any] = deepcopy(document)
    normalized.setdefault("schema_version", 1)
    normalized.setdefault("mode", "host")
    normalized["name"] = normalized["name"].strip()
    if mode in ("native", "coffee") and "set_grinder_size" in normalized[profile]:
        normalized[profile]["grinder_enabled"] = normalized[profile]["set_grinder_size"]
    if mode == "host":
        for step in normalized["steps"]:
            if step["kind"] == "water":
                step.setdefault("flow_ml_s", 3)
                step.setdefault("pattern", "center")
    elif mode == "native":
        native = normalized["native"]
        native["payload_hex"] = bytes.fromhex(native["payload_hex"]).hex()
    elif mode == "tea":
        normalized["tea"].setdefault("siphon_trigger", "auto")
        for steep in normalized["tea"]["steeps"]:
            steep.setdefault("pattern", "center")
    else:
        content = normalized["coffee_recipe"]
        for key, rule in COFFEE.items():
            if "default" in rule:
                content.setdefault(key, deepcopy(rule["default"]))
        for pour in content["pours"]:
            for key, rule in COFFEE_POUR.items():
                if "default" in rule:
                    pour.setdefault(key, rule["default"])
    return normalized


class RecipeValidationError(ValueError):
    """Strict persistence/compilation failure with the complete diagnostic report."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        self.issues: list[dict[str, Any]] = report["errors"]
        super().__init__(
            "; ".join(f"{e['path'] or '/'}: {e['message']}" for e in self.issues)
        )
