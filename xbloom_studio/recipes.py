"""Editable recipe documents and deterministic, offline execution plans.

A plan records the encoder version, the recipe snapshot and the exact bytes
to send. Retained plans are repeated exactly as stored, after checking that
their recorded encoder still reproduces them; they are never re-encoded with
a newer encoder. Only whether a repeat needs experimental opt-in follows
current hardware evidence.
"""

from __future__ import annotations

import difflib
import heapq
import math
from collections.abc import Iterator
from copy import deepcopy
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .coffee import (
    COFFEE_ENCODER_VERSION,
    LEGACY_COFFEE_ENCODER_VERSION,
    MAX_POUR_FRAGMENT_ML,
    _encode_coffee_recipe_v1,
    encode_coffee_recipe,
)
from .control import VALIDATED_UPLOAD_CUP_LIMITS, EncodedRecipe
from .machine_controls import VALIDATED_TEA, EncodedTeaRecipe
from .pagination import integer
from .recipe_validation import (
    RATIO_WRAP,
    RecipeValidationError,
    check_recipe,
    recipe_constraints,
)
from .serialization import digest, dumps
from .tea import (
    LEGACY_TEA_ENCODER_VERSION,
    TEA_ENCODER_VERSION,
    TEA_OPERATION_OVERHEAD_SECONDS,
    _encode_tea_recipe_v1,
    encode_tea_recipe,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .storage import Store

__all__ = [
    "ENCODER_VERSION",
    "HistoryService",
    "RecipeService",
    "RecipeValidationError",
    "check_recipe",
    "compile_recipe",
    "recipe_constraints",
    "upload_requires_untested",
    "validate_recipe",
    "validate_retained_plan",
    "validate_step",
]

ENCODER_VERSION = "studio-plan-1"
COFFEE_ENCODERS: dict[str, Callable[[dict[str, Any]], EncodedRecipe]] = {
    COFFEE_ENCODER_VERSION: encode_coffee_recipe,
    LEGACY_COFFEE_ENCODER_VERSION: _encode_coffee_recipe_v1,
}
# The coffee encoder identifier of the hardware-observed prepared upload. It
# is a stable recorded name, deliberately not COFFEE_ENCODER_VERSION.
VALIDATED_COFFEE_ENCODER_VERSION = "studio-coffee-android-2.2.3-v2"
TEA_ENCODERS: dict[str, Callable[[dict[str, Any]], EncodedTeaRecipe]] = {
    TEA_ENCODER_VERSION: encode_tea_recipe,
    LEGACY_TEA_ENCODER_VERSION: _encode_tea_recipe_v1,
}
STEP_LIMITATIONS = [
    "Host steps use separate manual operations and are not a native machine recipe.",
    "20 C is a requested target, not verified room-temperature outlet water.",
    "Native physical prompts must be advanced on the machine; no remote advance is "
    "inferred.",
]
COFFEE_LIMITATIONS = {
    COFFEE_ENCODER_VERSION: "Coffee encoding uses the editor temperature codes and "
    "reference cup limits; physical workflow remains experimental.",
    LEGACY_COFFEE_ENCODER_VERSION: "Coffee encoding uses the legacy temperature "
    "codes and cup limits, kept for exact repeat; physical workflow remains "
    "unvalidated.",
}
COFFEE_COMMON_LIMITATIONS = [
    "Flow is encoded from its exact decimal value.",
    "Native physical prompts must be advanced on the machine; no remote advance is "
    "inferred.",
    "The watchdog budgets filling and pauses plus 600 seconds; it is not a duration "
    "prediction.",
]
TEA_TESTED_LIMITATION = (
    "This exact content is hardware-tested for a direct start only: one 90 ml BP "
    "spiral steep at 3 ml/s for 30 s, from one completed run on firmware "
    "V12.0D.500, judged from telemetry. Uploading it first still needs "
    "allow_untested."
)
TEA_UNTESTED_LIMITATION = (
    "This content differs from the one hardware-tested single steep, so physical "
    "execution remains unvalidated."
)
TEA_COMMON_LIMITATIONS = [
    "Dose is manual preparation metadata; load the tea and prepare the appropriate "
    "brewer and cup.",
    "Soak volumes do not predict cup yield; automatic siphon top-up is firmware "
    "controlled.",
    "Physical prompts must be advanced on the machine; no remote advance is inferred.",
    "The watchdog adds 600 seconds for heating, filling and draining; it is not a "
    "duration prediction.",
]
# Plans written before the current wording carry earlier advisory text. It is
# recognized by the SHA-256 of its canonical JSON per (mode, encoder version),
# so a retained plan still validates and repeats unchanged. Everything that
# reaches the machine is compared exactly.
LEGACY_LIMITATIONS: dict[tuple[str, str], frozenset[str]] = {
    ("coffee", COFFEE_ENCODER_VERSION): frozenset(
        {"bed6ffc5bb0d46fe447156b225a8b0c80d3e31e6708ab956176cfca2f02a2cdd"}
    ),
    ("coffee", LEGACY_COFFEE_ENCODER_VERSION): frozenset(
        {"05ec22607fe5bf4113109f5479b8d8387e708a82427ec71f44a7ecbfed27a171"}
    ),
    ("tea", TEA_ENCODER_VERSION): frozenset(
        {
            "6b19e8777809910281876b857c67bf4f9c8c6799b89ad6f9725cad745b675e4c",
            "c4221afc0f162d0c7d07162f4f2fd27cebba135e9370b801ac73335137e4da60",
            # Before any tea content was hardware-tested, with and without
            # the ratio wrap note.
            "b07ee2c548c1371c65f9538cf8e6ffdcdc90b79290e9ba2046d682c2f8dcf1b4",
            "9701cf9dd0ec96141edc4e889c85de07796bb6071b91c8fd05f4faeb9660ef5e",
        }
    ),
    ("tea", LEGACY_TEA_ENCODER_VERSION): frozenset(
        {
            "6b19e8777809910281876b857c67bf4f9c8c6799b89ad6f9725cad745b675e4c",
            # Before any tea content was hardware-tested; v1 cannot wrap.
            "b07ee2c548c1371c65f9538cf8e6ffdcdc90b79290e9ba2046d682c2f8dcf1b4",
        }
    ),
}


def number(
    value: object, name: str, low: float, high: float, integer: bool = False
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
        or not low <= value <= high
        or (integer and int(value) != value)
    ):
        kind = "an integer" if integer else "a number"
        raise ValueError(f"{name} must be {kind} in {low}..{high}")
    return value


def keys(value: object, allowed: tuple[str, ...], name: str) -> None:
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"{name} has unsupported fields or is not an object")


def validate_step(step: dict[str, Any]) -> dict[str, Any]:
    step_document = validate_recipe({"name": "Step validation", "steps": [step]})
    result: dict[str, Any] = step_document["steps"][0]
    return result


def validate_recipe(document: Any) -> dict[str, Any]:
    """Strict normalizer; check_recipe returns the full report instead."""
    report = check_recipe(document)
    if not report["valid"]:
        raise RecipeValidationError(report)
    normalized: dict[str, Any] = report["normalized"]
    return normalized


def compile_recipe(document: Any) -> dict[str, Any]:
    """Compile a new plan with the current encoder; retained plans are separate."""
    return _compile_recipe(document)


def validate_retained_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Check that a retained plan is reproduced by its recorded encoder.

    Returns the recompiled plan. Nothing is rewritten: the stored plan is what
    repeats. Unknown encoder versions, malformed plans and any changed field
    refuse the repeat, except advisory text recognized by digest and a tea
    plan's requires_untested recorded true before its content was
    hardware-tested.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("recipe_snapshot"), dict):
        raise ValueError("Retained plan is malformed; inspect before recompiling")
    document = plan["recipe_snapshot"]
    mode = document.get("mode")
    version = plan.get("encoder_version")
    expected = _compile_recipe(
        document,
        coffee_encoder_version=version if mode == "coffee" else COFFEE_ENCODER_VERSION,
        tea_encoder_version=version if mode == "tea" else TEA_ENCODER_VERSION,
    )
    for key, value in expected.items():
        if key in plan and dumps(plan[key]) == dumps(value):
            continue
        if key == "limitations" and _recognized_limitations(plan, mode, version):
            continue
        if (
            key == "requires_untested"
            and mode == "tea"
            and plan.get(key) is True
            and value is False
        ):
            continue
        raise ValueError(
            "Historical plan is incompatible or has been modified; "
            "inspect before recompiling"
        )
    return expected


def upload_requires_untested(plan: object) -> bool:
    """Whether uploading a resolved plan still needs experimental opt-in.

    This is only the opt-in check for a plan that recipe_upload otherwise
    accepts (exactly one native coffee or tea step); it does not say whether a
    plan can be uploaded. Pure and never persisted; a plan's own
    ``requires_untested`` keeps governing a direct start.

    One upload profile was observed on hardware: a structured coffee plan
    from the ``VALIDATED_COFFEE_ENCODER_VERSION`` encoder, still reproduced by
    it, for the xDripper reference cup limits, with the grind-size upload
    selected (command 8001), no bypass, no vibration, numeric pour
    temperatures and pours of 1..127 ml, each a single payload record. A tea
    plan always requires opt-in here, even when its ``requires_untested`` is
    false: tea was observed only as a direct start, never uploaded first.
    Every other plan keeps its recorded requirement, and a malformed plan
    requires opt-in.
    """
    if not isinstance(plan, dict):
        return True
    execution = plan.get("execution")
    if not isinstance(execution, list) or any(
        not isinstance(step, dict) or step.get("kind") == "tea" for step in execution
    ):
        return True
    if plan.get("requires_untested") is False:
        return False
    return not _validated_prepared_coffee(plan)


def _validated_prepared_coffee(plan: dict[str, Any]) -> bool:
    # The observed identifier, not "the current encoder": a later encoder
    # never inherits this exemption without its own hardware evidence.
    if plan.get("encoder_version") != VALIDATED_COFFEE_ENCODER_VERSION:
        return False
    try:
        # A freshly compiled plan passes trivially; a retained one must still
        # be what its recorded encoder produces, byte for byte.
        validate_retained_plan(plan)
    except ValueError:
        return False
    document = plan["recipe_snapshot"]
    execution = plan.get("execution")
    if document.get("mode") != "coffee" or not (
        isinstance(execution, list) and len(execution) == 1
    ):
        return False
    content, step = document["coffee_recipe"], execution[0]
    return (
        content["cup_type"] == "xdripper"
        and content["bypass"] is None
        and step["kind"] == "native"
        and step["grinder_enabled"] is True
        and tuple(step["cup_limits"]) == VALIDATED_UPLOAD_CUP_LIMITS
        and step["bypass_ml"] == 0
        and all(
            not isinstance(pour["temperature"], str)
            and 1 <= pour["volume_ml"] <= MAX_POUR_FRAGMENT_ML
            and pour["vibration_before"] is False
            and pour["vibration_after"] is False
            for pour in content["pours"]
        )
    )


def _recognized_limitations(
    plan: dict[str, Any], mode: object, version: object
) -> bool:
    limitations = plan.get("limitations")
    if not isinstance(limitations, list) or not isinstance(version, str):
        return False
    known = LEGACY_LIMITATIONS.get((str(mode), version), frozenset())
    return digest(limitations) in known


def _compile_recipe(
    document: Any,
    *,
    coffee_encoder_version: object = COFFEE_ENCODER_VERSION,
    tea_encoder_version: object = TEA_ENCODER_VERSION,
) -> dict[str, Any]:
    doc = validate_recipe(document)
    if doc["mode"] == "coffee":
        return _coffee_plan(doc, coffee_encoder_version)
    if doc["mode"] == "tea":
        return _tea_plan(doc, tea_encoder_version)
    native = doc.get("native", {})
    experimental = doc["mode"] == "native" and bool(
        native.get("grinder_enabled")
        or native.get("cup_limits") is not None
        or native.get("bypass_ml")
    )
    execution = deepcopy(
        doc["steps"] if doc["mode"] == "host" else [{"kind": "native", **native}]
    )
    return {
        "schema_version": 1,
        "encoder_version": ENCODER_VERSION,
        "recipe_sha256": digest(doc),
        "recipe_snapshot": doc,
        "execution": execution,
        "requires_untested": experimental,
        "requires_user": [
            s["instruction"] for s in doc.get("steps", []) if s["kind"] == "user"
        ],
        "limitations": list(STEP_LIMITATIONS),
    }


def _coffee_plan(doc: dict[str, Any], version: object) -> dict[str, Any]:
    if not isinstance(version, str) or version not in COFFEE_ENCODERS:
        raise ValueError("Historical coffee encoder version is unknown or incompatible")
    encoded = COFFEE_ENCODERS[version](doc)
    content = doc["coffee_recipe"]
    filling = sum(
        Decimal(p["volume_ml"]) / Decimal(str(p["flow_ml_s"])) for p in content["pours"]
    )
    # Budget the bypass at the minimum supported flow; not a wire setting.
    timeout = (
        sum(p["pause_seconds"] for p in content["pours"])
        + math.ceil(filling + Decimal(encoded.bypass_ml) / 3)
        + 600
    )
    return {
        "schema_version": 1,
        "encoder_version": version,
        "recipe_sha256": digest(doc),
        "recipe_snapshot": doc,
        "execution": [
            {
                "kind": "native",
                "payload_hex": encoded.payload.hex(),
                "dose_g": encoded.dose_g,
                "grinder_enabled": encoded.grinder_enabled,
                "cup_limits": list(encoded.cup_limits or ()),
                "bypass_ml": encoded.bypass_ml,
                "bypass_temperature_c": encoded.bypass_temperature_c,
                "timeout": timeout,
            }
        ],
        "requires_untested": True,
        "requires_user": [],
        "limitations": [COFFEE_LIMITATIONS[version], *COFFEE_COMMON_LIMITATIONS],
    }


def _tea_plan(doc: dict[str, Any], version: object) -> dict[str, Any]:
    if not isinstance(version, str) or version not in TEA_ENCODERS:
        raise ValueError("Historical tea encoder version is unknown or incompatible")
    encoded = TEA_ENCODERS[version](doc)
    tested = encoded == VALIDATED_TEA
    wrap = [
        RATIO_WRAP
        for w in check_recipe(doc)["warnings"]
        if w["code"] == "tea_ratio_byte_wrap"
    ]
    timeout = (
        sum(s["steep_seconds"] for s in doc["tea"]["steeps"])
        + TEA_OPERATION_OVERHEAD_SECONDS
    )
    return {
        "schema_version": 1,
        "encoder_version": version,
        "recipe_sha256": digest(doc),
        "recipe_snapshot": doc,
        "execution": [
            {
                "kind": "tea",
                "payload_hex": encoded.payload.hex(),
                "cup_limits": list(encoded.cup_limits or ()),
                "timeout": timeout,
            }
        ],
        "requires_untested": not tested,
        "requires_user": [],
        "limitations": [
            TEA_TESTED_LIMITATION if tested else TEA_UNTESTED_LIMITATION,
            *TEA_COMMON_LIMITATIONS,
            *wrap,
        ],
    }


class RecipeService:
    """Recipe operations over a store: compile, compare, save from a brew, match."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def compile(self, reference: str, revision: int | None = None) -> dict[str, Any]:
        row = self.store.get_recipe(reference, revision)
        return {
            **compile_recipe(row["document"]),
            "recipe_id": row["id"],
            "recipe_revision": row["revision"],
        }

    def compare(self, reference: str, before: int, after: int) -> dict[str, Any]:
        a = self.store.get_recipe(reference, before)["document"]
        b = self.store.get_recipe(reference, after)["document"]
        diff = difflib.unified_diff(
            dumps(a).splitlines(),
            dumps(b).splitlines(),
            fromfile=str(before),
            tofile=str(after),
        )
        return {"before": a, "after": b, "diff": list(diff)}

    def from_brew(self, operation_id: str, name: str) -> dict[str, Any]:
        """Save a brew's recorded recipe snapshot as a new recipe."""
        op = self.store.operation(operation_id)
        doc = deepcopy(op["plan"]["recipe_snapshot"])
        doc["name"] = name
        doc["source"] = {**doc.get("source", {}), "brew_id": operation_id}
        return self.store.create_recipe(doc)

    def match(
        self, coffee: dict[str, Any], limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Rank every candidate; optional top-k bounds retained matches, not scans."""
        keys(coffee, ("name", "roaster", "origin", "variety"), "coffee search")
        if limit is not None:
            integer(limit, "limit", 1, 1000)

        def candidates() -> Iterator[dict[str, Any]]:
            cursor = None
            while True:
                page = self.store.recipe_page(
                    include_temporary=True, limit=1000, cursor=cursor, detail="full"
                )
                for row in page["items"]:
                    metadata = row["document"].get("coffee")
                    if not isinstance(metadata, dict):
                        metadata = {}
                    reasons = []
                    score = 0
                    for field, weight in (
                        ("name", 8),
                        ("roaster", 6),
                        ("variety", 2),
                        ("origin", 2),
                    ):
                        query, found = coffee.get(field), metadata.get(field)
                        if query is None or found is None:
                            continue
                        query = str(query).strip().casefold()
                        found = str(found).strip().casefold()
                        if query and found and (query == found or query in found):
                            score += weight
                            reasons.append(field)
                    if score:
                        yield {**row, "match_score": score, "match_reasons": reasons}
                if not page["has_more"]:
                    break
                cursor = page["next_cursor"]

        def rank(row: dict[str, Any]) -> tuple[int, str]:
            return (-row["match_score"], row["id"])

        if limit is None:
            return sorted(candidates(), key=rank)
        return heapq.nsmallest(limit, candidates(), key=rank)


class HistoryService:
    """Read retained brews; repeat returns the stored plan, never a re-encoding."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def repeat_plan(self, ident: str) -> dict[str, Any]:
        plan: dict[str, Any] = self.store.operation(ident)["plan"]
        current = validate_retained_plan(plan)
        # The stored content repeats; whether it needs opt-in follows today's
        # evidence, which may now cover a tea plan recorded as untested.
        return {**deepcopy(plan), "requires_untested": current["requires_untested"]}
