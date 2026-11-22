"""Pure tea authoring diagnostics, using the shared schema as the authority."""

from __future__ import annotations

from typing import Any

from .diagnostic_catalog import validation_diagnostic
from .recipe_validation import _actual, check_recipe, recipe_constraints

OUTPUT_TARGETS = (120, 240, 360)


def _repair(error: dict[str, Any]) -> str:
    path, expected = error["path"], error["expected"]
    if path == "/mode":
        return (
            'Use mode="tea" and a tea object; do not convert another mode by '
            "relabelling it."
        )
    if path == "/expected_output_ml":
        return (
            "Choose 120, 240 or 360 ml as a nominal serving target, or omit the "
            "target. Exact physical yield is not validated."
        )
    if error["code"] == "nominal_output":
        return (
            "Choose a coherent recipe with the required steep count, or change the "
            "nominal target. Do not just drop or duplicate steeps."
        )
    if error["code"] == "unknown_field":
        return (
            "Remove or relocate the unsupported field only after resolving its "
            f"intent. Allowed fields here: {expected}. Temperatures belong in "
            "tea.steeps[].temperature."
        )
    if path.startswith("/tea/steeps/") and path.endswith("/temperature"):
        return (
            'Use integer 40..99 except 96, or "RT"/"BP". Numeric 96 must use BP; '
            "99 is kept as given without interpreting it as BP. Targets are not "
            "measured outlet temperatures."
        )
    if path == "/tea/dose_g":
        return (
            "Use a finite number from 1 through 5 g, excluding booleans. Doses are "
            "manual preparation metadata and may be fractional."
        )
    return (
        f"Correct this field to the expected schema: {expected}. Preserve user "
        "intent; do not silently clamp or discard settings."
    )


def validate_tea_recipe(
    document: Any, expected_output_ml: object = None
) -> dict[str, Any]:
    """Validate all tea fields with repair hints; never save, encode or connect.

    An optional 120/240/360 ml target checks the nominal steep count only.
    Firmware siphon/top-up controls actual output; soak volumes do not
    determine cup yield. Invalid reports contain no normalized document.
    """
    report = check_recipe(document)
    errors: list[dict[str, Any]] = report["errors"]
    mode = document.get("mode") if isinstance(document, dict) else None
    if mode != "tea":
        errors.append(
            {
                "path": "/mode",
                "code": "tea_mode",
                "message": "Tea validation requires explicit mode=tea",
                "expected": {"enum": ["tea"]},
                "value": _actual(mode),
                "actual_type": type(mode).__name__,
            }
        )
    target_valid = expected_output_ml is None or (
        type(expected_output_ml) is int and expected_output_ml in OUTPUT_TARGETS
    )
    if not target_valid:
        errors.append(
            {
                "path": "/expected_output_ml",
                "code": "output_target",
                "message": "Unsupported nominal output target",
                "expected": {"enum": list(OUTPUT_TARGETS), "nullable": True},
                "value": _actual(expected_output_ml),
                "actual_type": type(expected_output_ml).__name__,
            }
        )
    content = document.get("tea") if isinstance(document, dict) else None
    steeps = content.get("steeps") if isinstance(content, dict) else None
    nominal = (
        len(steeps) * 120
        if isinstance(steeps, list) and 1 <= len(steeps) <= 3 and mode == "tea"
        else None
    )
    if (
        target_valid
        and isinstance(expected_output_ml, int)
        and isinstance(steeps, list)
        and nominal is not None
        and nominal != expected_output_ml
    ):
        errors.append(
            {
                "path": "/tea/steeps",
                "code": "nominal_output",
                "message": "Steep count does not match the nominal serving target",
                "expected": {
                    "steep_count": expected_output_ml // 120,
                    "nominal_output_ml": expected_output_ml,
                },
                "value": len(steeps),
                "actual_type": "int",
            }
        )
    for error in errors:
        error["repair_hint"] = _repair(error)
    constraints = recipe_constraints()
    report.update(
        valid=not errors,
        estimated_output_ml=nominal,
        output_validation_scope="nominal_steep_count_only",
        yield_verified=False,
        constraints={key: constraints[key] for key in ("tea", "tea_reference")},
    )
    report["warnings"].append(
        {
            "code": "nominal_output_only",
            "message": "Output is a nominal estimate of 120 ml per steep, not "
            "measured yield. Soak volumes do not predict firmware siphon/top-up "
            "output.",
        }
    )
    if errors:
        report.update(
            normalized=None,
            execution_supported=False,
            diagnostic=validation_diagnostic(errors, tea=True),
        )
    return report
