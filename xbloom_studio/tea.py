"""Pure structured tea recipe encoding; no storage, network or machine I/O.

One encoded value, machine_controls.VALIDATED_TEA, is hardware-tested for a
direct start; physical brewing of any other tea content remains unvalidated.
Encoder version identifiers are persisted in plans: the legacy version
accepts only the original fields and temperatures, so plans it produced
repeat byte for byte.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .machine_controls import EncodedTeaRecipe
from .recipe_validation import TEA_REFERENCE, RecipeValidationError, check_recipe

TEA_ENCODER_VERSION = "studio-tea-android-2.2.3-v2"
LEGACY_TEA_ENCODER_VERSION = "studio-tea-android-2.2.3-v1"
# Library watchdog budget for heating, filling and draining; not a wire field.
TEA_OPERATION_OVERHEAD_SECONDS = 600
_LEGACY_TEA_FIELDS = {"dose_g", "steeps", "siphon_trigger"}
_LEGACY_STEEP_FIELDS = {"volume_ml", "temperature", "pattern", "steep_seconds"}


def encode_tea_recipe(document: dict[str, Any]) -> EncodedTeaRecipe:
    """Validate a full tea document and return its payload and cup limits.

    Dose is manual preparation metadata and does not affect the payload.
    RT/BP map to fixed codes, not measured outlet temperatures.
    """
    return _encode_tea_recipe(document)


def _encode_tea_recipe_v1(document: dict[str, Any]) -> EncodedTeaRecipe:
    return _encode_tea_recipe(document, legacy=True)


def _encode_tea_recipe(
    document: dict[str, Any], *, legacy: bool = False
) -> EncodedTeaRecipe:
    report = check_recipe(document)
    if not report["valid"]:
        raise RecipeValidationError(report)
    doc = report["normalized"]
    if doc["mode"] != "tea":
        raise ValueError("encode_tea_recipe requires mode=tea")
    content = doc["tea"]
    steeps = content["steeps"]
    if legacy and (
        set(content) - _LEGACY_TEA_FIELDS
        or any(
            set(steep) - _LEGACY_STEEP_FIELDS
            or (type(steep["temperature"]) is int and steep["temperature"] > 95)
            for steep in steeps
        )
    ):
        raise ValueError(
            "Historical tea v1 cannot contain extended settings or temperatures"
        )
    return encode_tea_content(content)


def encode_tea_content(content: dict[str, Any]) -> EncodedTeaRecipe:
    """Machine content for normalized, valid tea fields; validates nothing.

    Payload: steep count*8, then 8 bytes per steep, then grind and ratio*10.
    Each steep is volume, temperature code, pattern code, vibration flags,
    negated seconds remainder, minutes<<5, RPM on the first steep only and
    flow*10. Ratio*10 is narrowed to one byte.
    """
    steeps = content["steeps"]
    fixed = TEA_REFERENCE["fixed_new_recipe_values"]
    sentinels = TEA_REFERENCE["temperature_sentinels"]
    payload = bytearray([len(steeps) * 8])
    for index, steep in enumerate(steeps):
        temperature = sentinels.get(steep["temperature"], steep["temperature"])
        minutes, seconds = divmod(steep["steep_seconds"], 60)
        payload.extend(
            (
                steep["volume_ml"],
                temperature,
                TEA_REFERENCE["pattern_codes"][steep["pattern"]],
                int(steep.get("vibration_before", False))
                | (int(steep.get("vibration_after", False)) << 1),
                (-seconds) & 255,
                minutes << 5,
                content.get("rpm_metadata", fixed["rpm_metadata"]) if index == 0 else 0,
                int(Decimal(str(steep.get("flow_ml_s", fixed["flow_ml_s"]))) * 10),
            )
        )
    payload.extend(
        (
            content.get("grinder_size_metadata", fixed["grinder_size_metadata"]),
            int(
                Decimal(str(content.get("ratio_metadata", fixed["ratio_metadata"])))
                * 10
            )
            & 255,
        )
    )
    cup_limits = TEA_REFERENCE["cached_cup_limits"]
    return EncodedTeaRecipe(bytes(payload), (cup_limits[0], cup_limits[1]))
