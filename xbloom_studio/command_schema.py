"""Platform-neutral JSON contracts for named machine settings and scale actions.

No schema-library dependency. Callers validate before connecting; adapters
can publish these same rules. Interaction keeps its own physical guards.
"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from typing import Any

Schema = dict[str, Any]
MAX_OPERATION_TIMEOUT = 3600


def recipe_configuration_untested(bypass_ml: float, cup_limits: object) -> bool:
    return bool(bypass_ml or cup_limits is not None)


def water_timeout(volume_ml: float, flow_ml_s: float) -> float:
    return volume_ml / flow_ml_s + 45


def number(
    description: str,
    minimum: float,
    maximum: float,
    *,
    integer: bool = False,
    default: float | None = None,
) -> Schema:
    result: Schema = {
        "type": "integer" if integer else "number",
        "description": description,
        "minimum": minimum,
        "maximum": maximum,
    }
    if default is not None:
        result["default"] = default
    return result


def enum(description: str, *values: str, default: str | None = None) -> Schema:
    result: Schema = {
        "type": "string",
        "enum": list(values),
        "description": description,
    }
    if default is not None:
        result["default"] = default
    return result


def obj(properties: Schema | None = None, required: tuple[str, ...] = ()) -> Schema:
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }


TEMP = number(
    "Requested temperature in Celsius; not a measured outlet temperature.",
    20,
    99,
    default=20,
)
PATTERN = enum("Pour geometry.", "center", "ring", "spiral", default="center")
RAW = number(
    "Raw parameter; encoding bound, not a physically validated range.",
    0,
    2147483647,
    integer=True,
)
PRESET = obj(
    {
        "slot": enum("Physical preset slot.", "A", "B", "C"),
        "payload_hex": {
            "type": "string",
            "description": "Complete encoded recipe content, without a frame.",
            "minLength": 2,
            "maxLength": 1024,
            "pattern": r"^(?:[0-9a-fA-F]{2})+$",
        },
        "flags": number(
            "Preset flags byte; preserve the value known for this preset.",
            0,
            255,
            integer=True,
        ),
    },
    ("slot", "payload_hex", "flags"),
)
ORDER = {
    "description": "Permutation of A/B/C, as an array or three-character string.",
    "anyOf": [
        {"type": "string", "enum": ["ABC", "ACB", "BAC", "BCA", "CAB", "CBA"]},
        {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "uniqueItems": True,
            "items": enum("Slot.", "A", "B", "C"),
        },
    ],
}
CUP = {
    "type": "array",
    "description": "Cup reference minimum and maximum; not temperatures.",
    "minItems": 2,
    "maxItems": 2,
    "items": number("Cup reference value.", 0, 5000),
}
MAX_SAVE_TIMEOUT = 60
SAVE_TIMEOUT = number(
    "Wait for a temporally correlated preset-save event, in seconds.",
    0.001,
    MAX_SAVE_TIMEOUT,
    default=10,
)

COMMAND_SCHEMAS: dict[str, Schema] = {
    "set_brightness": obj(
        {"level": number("Display brightness level.", 1, 15, integer=True)},
        ("level",),
    ),
    "set_weight_unit": obj(
        {"unit": enum("Display weight/volume unit.", "ml", "g", "oz")}, ("unit",)
    ),
    "set_temperature_unit": obj(
        {"unit": enum("Display temperature unit.", "C", "F")}, ("unit",)
    ),
    "set_pour_pattern": obj({"pattern": PATTERN, "temperature_c": TEMP}, ("pattern",)),
    "back_home": obj(),
    "wake": obj(),
    "set_mode": obj(
        {"mode": enum("Machine user interface mode.", "pro", "recipes")}, ("mode",)
    ),
    "write_preset": obj(
        {
            "preset": {
                **PRESET,
                "description": "Intended replacement; resubmit it unchanged to "
                "repeat an incomplete write.",
            },
            "backup": {
                **PRESET,
                "description": (
                    "Optional caller-supplied previous contents for the same slot; "
                    "not readback."
                ),
            },
            "save_timeout": SAVE_TIMEOUT,
            "acknowledge_incomplete": {
                "type": "string",
                "description": "Only after the user's direction: the slot's latest "
                "write ID, acknowledged so a new write may start.",
                "pattern": "^[0-9a-f]{32}$",
            },
        },
        ("preset",),
    ),
    "write_presets": obj(
        {
            "presets": {
                "type": "array",
                "description": "Exactly one intended replacement for each A/B/C slot.",
                "minItems": 3,
                "maxItems": 3,
                "items": PRESET,
            },
            "backup": {
                "type": "array",
                "description": "Known previous A/B/C contents; not machine readback.",
                "minItems": 3,
                "maxItems": 3,
                "items": PRESET,
            },
            "save_timeout": SAVE_TIMEOUT,
        },
        ("presets", "backup"),
    ),
    "set_water_source": obj(
        {
            "source": enum(
                "Water inlet source; switching is experimental.", "tank", "tap"
            )
        },
        ("source",),
    ),
    "read_pour_radius": obj(),
    "read_vibration": obj(),
    "initialize_pour_radius": obj({"value": RAW}, ("value",)),
    "set_pour_radius": obj({"value": RAW}, ("value",)),
    "set_pour_radius_level": obj(
        {
            "level": number(
                "Pour-radius level; 2 is the baseline.", 0, 4, integer=True
            ),
            "baseline": {
                **RAW,
                "description": "Reference baseline radius, clamped to 560..840.",
            },
        },
        ("level", "baseline"),
    ),
    "initialize_vibration": obj({"value": RAW}, ("value",)),
    "set_vibration_level": obj(
        {
            "level": number(
                "Vibration level; encoded as 1000 + 100*level.", 0, 5, integer=True
            )
        },
        ("level",),
    ),
    "set_cup_limits": obj(
        {
            "minimum": number("Minimum cup reference.", 0, 5000),
            "maximum": number("Maximum cup reference; must be >= minimum.", 0, 5000),
        },
        ("minimum", "maximum"),
    ),
    "configure_recipe": obj(
        {
            "dose_g": number("Recipe dose, in whole grams.", 1, 100, integer=True),
            "bypass_ml": number(
                "Bypass water amount in millilitres.", 0, 1000, default=0
            ),
            "bypass_temperature_c": TEMP,
            "cup_limits": {
                "anyOf": [CUP, {"type": "null"}],
                "description": (
                    "Optional [minimum, maximum] cup references; requires "
                    "experimental permission."
                ),
                "default": None,
            },
        },
        ("dose_g",),
    ),
    "set_preset_count": obj(
        {"count": number("Experimental preset count.", 0, 3, integer=True)},
        ("count",),
    ),
    "set_preset_order": obj(
        {
            "order": ORDER,
            "backup_order": {
                **ORDER,
                "description": "Known previous order, not readback.",
            },
        },
        ("order", "backup_order"),
    ),
    "quit_recipe_preparation": obj(),
    "back_to_normal": obj(),
    "configure_grinder": obj(
        {
            "size": number(
                "User grind size; configuring can reposition the mechanism.",
                1,
                80,
                integer=True,
            ),
            "rpm": number(
                "Configured grinder RPM; no rotation-start command.",
                60,
                120,
                integer=True,
            ),
        },
        ("size", "rpm"),
    ),
    "configure_brewer": obj({"temperature_c": TEMP, "pattern": PATTERN}),
    "enter_scale": obj(),
    "tare": obj(),
    "exit_scale": obj(),
}


class CommandValidationError(ValueError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.report = {"valid": False, "errors": errors}
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors))


def command_schema(name: str | None = None) -> Schema:
    if name is not None:
        if name not in COMMAND_SCHEMAS:
            raise ValueError("Unknown machine command; inspect machine_capabilities")
        return deepcopy(COMMAND_SCHEMAS[name])
    flags = {
        "allow_control": {
            "type": "boolean",
            "description": "Explicitly authorize this machine command.",
            "default": False,
        },
        "allow_untested": {
            "type": "boolean",
            "description": "Opt in to behavior that is not hardware-tested.",
            "default": False,
        },
    }
    branches = []
    for command, schema in COMMAND_SCHEMAS.items():
        params = deepcopy(schema)
        if not schema["required"]:
            params = {
                "anyOf": [params, {"type": "null"}],
                "description": "Parameters for this command; omit for defaults.",
            }
        else:
            params["description"] = "Required parameters for " + command + "."
        branches.append(
            obj(
                {
                    "command": {
                        "const": command,
                        "type": "string",
                        "description": "Named machine command.",
                    },
                    "params": params,
                    **flags,
                },
                ("command", "params") if schema["required"] else ("command",),
            )
        )
    return {
        "oneOf": branches,
        "description": (
            "Exactly one named command with its matching parameter contract."
        ),
    }


def _issues(value: Any, schema: Schema, path: str) -> list[dict[str, Any]]:
    """Validate the small closed schema vocabulary above, not arbitrary schemas."""

    def bad(message: str) -> list[dict[str, Any]]:
        return [{"path": path or "/", "message": message, "allowed": deepcopy(schema)}]

    if "anyOf" in schema:
        if any(not _issues(value, option, path) for option in schema["anyOf"]):
            return []
        return bad("Value does not match any allowed shape")
    kind = schema["type"]
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "null": value is None,
        "number": type(value) in (int, float),
        "integer": type(value) is int,
    }[kind]
    if not valid:
        return bad("Expected " + kind)
    if kind in ("number", "integer"):
        if (
            not math.isfinite(value)
            or not schema["minimum"] <= value <= schema["maximum"]
        ):
            return bad("Value must be finite and within the inclusive bounds")
    elif kind == "string":
        if "enum" in schema and value not in schema["enum"]:
            return bad("Choose an allowed value")
        if (
            not schema.get("minLength", 0)
            <= len(value)
            <= schema.get("maxLength", math.inf)
        ):
            return bad("Invalid string length")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            return bad("Invalid string format")
    elif kind == "array":
        if not schema["minItems"] <= len(value) <= schema["maxItems"]:
            return bad("Invalid item count")
        if schema.get("uniqueItems") and len({str(x) for x in value}) != len(value):
            return bad("Items must be unique")
        return [
            issue
            for index, item in enumerate(value)
            for issue in _issues(item, schema["items"], f"{path}/{index}")
        ]
    elif kind == "object":
        issues = []
        for key in schema["required"]:
            if key not in value:
                issues.append(
                    {
                        "path": f"{path}/{key}",
                        "message": "Required field is missing",
                        "allowed": schema["properties"][key],
                    }
                )
        for key, item in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            if key not in schema["properties"]:
                issues.append(
                    {
                        "path": f"{path}/{escaped}",
                        "message": "Unknown parameter",
                        "allowed": {"properties": list(schema["properties"])},
                    }
                )
            else:
                issues.extend(
                    _issues(item, schema["properties"][key], f"{path}/{escaped}")
                )
        return issues
    return []


def validate_command_params(command: str, params: Any) -> None:
    schema = command_schema(command)
    errors = _issues(params, schema, "/params")
    if errors:
        raise CommandValidationError(errors)

    def invalid(path: str, message: str) -> None:
        errors.append(
            {"path": path, "message": message, "allowed": {"constraint": message}}
        )

    if (
        command == "write_preset"
        and "backup" in params
        and params["preset"]["slot"] != params["backup"]["slot"]
    ):
        invalid("/params/backup/slot", "Backup must address the replacement slot")
    if command == "write_presets":
        for key in ("presets", "backup"):
            if {p["slot"] for p in params[key]} != {"A", "B", "C"}:
                invalid("/params/" + key, "Provide one preset per A/B/C slot")
    if command == "set_preset_order":
        for key in ("order", "backup_order"):
            if set(params[key]) != {"A", "B", "C"}:
                invalid("/params/" + key, "Order must be a permutation of A/B/C")
    if command == "set_cup_limits" and params["minimum"] > params["maximum"]:
        invalid("/params/maximum", "Maximum must be at least minimum")
    if (
        command == "configure_recipe"
        and params.get("cup_limits") is not None
        and params["cup_limits"][0] > params["cup_limits"][1]
    ):
        invalid("/params/cup_limits", "Cup limits must be [minimum, maximum]")
    if errors:
        raise CommandValidationError(errors)
