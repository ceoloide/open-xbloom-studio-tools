import unittest
from copy import deepcopy

from tests.test_recipe_encoding import coffee, tea
from xbloom_studio import (
    EncodedRecipe,
    RecipeValidationError,
    check_recipe,
    grind_profile,
    recipe_constraints,
    validate_tea_recipe,
)
from xbloom_studio.recipes import keys, number, validate_recipe, validate_step


def native(**changes):
    content = {"payload_hex": "ab", "dose_g": 10}
    content.update(changes)
    return {"name": "N", "mode": "native", "native": content}


def paths(document):
    return {e["path"] for e in check_recipe(document)["errors"]}


class HostRecipeTest(unittest.TestCase):
    def test_host_steps_defaults_and_errors(self):
        report = check_recipe(
            {
                "name": "  Steps ",
                "description": {"any": ["json", 1, None]},
                "steps": [
                    {"kind": "water", "volume_ml": 100, "temperature_c": 20},
                    {"kind": "grind", "size": 20.0, "rpm": 90, "duration_s": 3},
                    {"kind": "wait", "duration_s": 0},
                    {"kind": "user", "instruction": "Swirl"},
                    {"kind": "tare"},
                ],
            }
        )
        self.assertTrue(report["valid"], report["errors"])
        normalized = report["normalized"]
        self.assertEqual(normalized["name"], "Steps")
        self.assertEqual(
            (normalized["mode"], normalized["schema_version"]), ("host", 1)
        )
        self.assertEqual(normalized["steps"][0]["pattern"], "center")
        self.assertEqual(report["validation_scope"], "recipe_definition")
        cases = [
            ({"name": "x", "steps": []}, "/steps"),
            ({"name": "x", "steps": "no"}, "/steps"),
            ({"name": "x", "steps": [1]}, "/steps/0"),
            ({"name": "x", "steps": [{}]}, "/steps/0/kind"),
            ({"name": "x", "steps": [{"kind": "boil"}]}, "/steps/0/kind"),
            ({"name": "x", "steps": [{"kind": "grind", "size": 1.5}]}, "/steps/0/size"),
            ({"name": "x", "steps": [{"kind": "tare", "extra": 1}]}, "/steps/0/extra"),
            ({"name": " ", "steps": [{"kind": "tare"}]}, "/name"),
            ({"steps": [{"kind": "tare"}]}, "/name"),
            ({"name": "x"}, "/steps"),
            ({"name": "x", "mode": "other", "steps": []}, "/mode"),
            (
                {"name": "x", "schema_version": 2, "steps": [{"kind": "tare"}]},
                "/schema_version",
            ),
            ({"name": "x", "steps": [{"kind": "tare"}], "native": {}}, "/native"),
        ]
        for document, path in cases:
            with self.subTest(document=document):
                self.assertIn(path, paths(document))
                self.assertIsNone(check_recipe(document)["normalized"])
        self.assertEqual(paths([]), {""})

    def test_slow_water_step_requires_an_explicit_timeout(self):
        step = {
            "kind": "water",
            "volume_ml": 500,
            "temperature_c": 90,
            "flow_ml_s": 0.1,
        }
        report = check_recipe({"name": "Slow", "steps": [step]})
        self.assertEqual(
            [(e["path"], e["code"]) for e in report["errors"]],
            [("/steps/0/timeout", "required_timeout")],
        )
        self.assertEqual(validate_step({**step, "timeout": 3600})["timeout"], 3600)

    def test_json_metadata_must_serialize(self):
        cyclic = []
        cyclic.append(cyclic)
        deep = current = {}
        for _ in range(101):
            current["x"] = current = {}
        for value, code in (
            (float("nan"), "non_finite"),
            ({1: "x"}, "json_key"),
            (object(), "json"),
            (cyclic, "json"),
            (deep, "json"),
        ):
            with self.subTest(code=code):
                document = {
                    "name": "x",
                    "steps": [{"kind": "tare"}],
                    "description": value,
                }
                codes = {e["code"] for e in check_recipe(document)["errors"]}
                self.assertIn(code, codes)
        huge = {"name": "x", "steps": [{"kind": "tare"}], "description": 10**5000}
        report = check_recipe(huge)
        self.assertIn("json_integer", {e["code"] for e in report["errors"]})
        self.assertEqual(report["errors"][0]["value"], "<integer with 16610 bits>")

    def test_helpers(self):
        self.assertEqual(
            validate_step({"kind": "water", "volume_ml": 5, "temperature_c": 20})[
                "flow_ml_s"
            ],
            3,
        )
        with self.assertRaises(RecipeValidationError) as caught:
            validate_recipe({"name": "x"})
        self.assertIn("/steps", str(caught.exception))
        self.assertEqual(caught.exception.report, check_recipe({"name": "x"}))
        self.assertEqual(number(5, "n", 1, 10, True), 5)
        for value in (True, "5", float("inf"), 11, 5.5):
            with self.assertRaises(ValueError):
                number(value, "n", 1, 10, True)
        keys({"a": 1}, ("a",), "x")
        for value in ([], {"b": 1}):
            with self.assertRaises(ValueError):
                keys(value, ("a",), "x")
        profile = grind_profile("Fine", 10, 90, 2.5)
        self.assertEqual(
            profile["steps"][0],
            {"kind": "grind", "size": 10, "rpm": 90, "duration_s": 2.5},
        )
        with self.assertRaises(RecipeValidationError):
            grind_profile("Fine", 0, 90, 2.5)


class NativeRecipeTest(unittest.TestCase):
    def test_native_envelope_matches_encoded_recipe_contract(self):
        for key, value, valid in [
            ("dose_g", 0, False),
            ("dose_g", 101, False),
            ("dose_g", 10.0, True),
            ("dose_g", 10.5, False),
            ("bypass_ml", 1001, False),
            ("bypass_temperature_c", 19, False),
            ("grinder_enabled", 1, False),
            ("cup_limits", [10, 5], False),
            ("cup_limits", [0, 5000], True),
            ("cup_limits", [-1, 5], False),
            ("cup_limits", [0], False),
            ("cup_limits", None, True),
        ]:
            with self.subTest(key=key, value=value):
                document = native(**{key: value})
                self.assertEqual(check_recipe(document)["valid"], valid)
                content = document["native"]
                options = {
                    k: v
                    for k, v in content.items()
                    if k not in ("payload_hex", "dose_g")
                }
                if options.get("cup_limits") is not None:
                    options["cup_limits"] = tuple(options["cup_limits"])
                try:
                    EncodedRecipe(b"\xab", content["dose_g"], **options)
                    accepted = True
                except (TypeError, ValueError):
                    accepted = False
                self.assertEqual(accepted, valid)
        report = check_recipe(native())
        self.assertEqual(report["validation_scope"], "opaque_envelope_and_settings")
        self.assertEqual(report["warnings"][0]["code"], "opaque_native")
        for value, valid in [
            ("", False),
            ("zz", False),
            ("a", False),
            ("00" * 512, True),
            ("00" * 513, False),
            (42, False),
        ]:
            self.assertEqual(check_recipe(native(payload_hex=value))["valid"], valid)
        self.assertIn("/native", paths({"name": "N", "mode": "native"}))

    def test_upload_aliases_must_agree(self):
        for document, profile in ((native(), "native"), (coffee(), "coffee_recipe")):
            content = document[profile]
            content.update(grinder_enabled=True, set_grinder_size=False)
            self.assertIn(f"/{profile}/set_grinder_size", paths(document))
            content.update(set_grinder_size=True)
            self.assertTrue(check_recipe(document)["valid"])
            del content["grinder_enabled"]
            normalized = check_recipe(document)["normalized"][profile]
            self.assertTrue(normalized["grinder_enabled"])


class CoffeeRecipeTest(unittest.TestCase):
    def test_every_invalid_setting_reports_its_path(self):
        cases = {
            "dose_g": [0, 4, 26, 15.0, True],
            "grind_size": [0, 81, 1.5],
            "ratio": [4.5, 25.5, 15.1, True],
            "rpm": [50, 130, 65, 60.0],
            "cup_type": ["tea", None],
            "grinder_enabled": [1, "yes"],
        }
        pours = {
            "volume_ml": [-1, 241, 1.0],
            "temperature": [39, 96, 99, 40.0, "rt"],
            "flow_ml_s": [2.9, 3.6, 3.01, float("inf")],
            "pause_seconds": [-1, 60, 5.0],
            "pattern": ["circular"],
            "vibration_before": [1],
            "vibration_after": [None],
        }
        for schema, prefix in (
            (cases, "/coffee_recipe/"),
            (pours, "/coffee_recipe/pours/0/"),
        ):
            for field, values in schema.items():
                for value in values:
                    with self.subTest(field=field, value=value):
                        document = coffee()
                        target = document["coffee_recipe"]
                        if schema is pours:
                            target = target["pours"][0]
                        target[field] = value
                        report = check_recipe(document)
                        self.assertFalse(report["execution_supported"])
                        self.assertIn(
                            prefix + field, {e["path"] for e in report["errors"]}
                        )
        for bypass in (
            [],
            {"volume_ml": 0, "temperature": 39},
            {"volume_ml": 101},
            {"volume_ml": 5, "temperature": "RT", "x": 1},
        ):
            document = coffee(bypass=bypass)
            self.assertTrue(
                any(p.startswith("/coffee_recipe/bypass") for p in paths(document))
            )

    def test_cross_field_checks_do_not_cascade(self):
        error = next(
            e
            for e in check_recipe(coffee(cup_type="xpod", dose_g=10))["errors"]
            if e["code"] == "cup_dose"
        )
        self.assertEqual(
            (error["expected"]["minimum"], error["expected"]["maximum"]), (15, 15)
        )
        report = check_recipe(coffee(pours=[{"volume_ml": 224, "temperature": 95}]))
        self.assertEqual(report["errors"][0]["code"], "volume_total")
        self.assertEqual(report["errors"][0]["expected"], {"total_ml": 225})
        for pours in (
            [None],
            [{"temperature": 95}],
            [{"volume_ml": True, "temperature": 95}],
            {},
        ):
            codes = {e["code"] for e in check_recipe(coffee(pours=pours))["errors"]}
            self.assertNotIn("volume_total", codes)
        for value in (None, [], 42):
            document = coffee()
            document["coffee_recipe"] = value
            self.assertEqual(paths(document), {"/coffee_recipe"})
        document = coffee()
        document["tea"] = {}
        self.assertEqual(check_recipe(document)["errors"][0]["code"], "conflict")
        normalized = check_recipe(coffee())["normalized"]["coffee_recipe"]
        self.assertEqual(
            (normalized["rpm"], normalized["bypass"], normalized["grinder_enabled"]),
            (120, None, False),
        )
        self.assertEqual(normalized["pours"][0]["flow_ml_s"], 3)
        self.assertEqual(
            check_recipe(coffee())["warnings"][0]["code"], "coffee_hardware_unvalidated"
        )


class TeaRecipeTest(unittest.TestCase):
    def test_tea_limit_boundaries(self):
        published = recipe_constraints()["tea"]["dose_g"]
        self.assertEqual((published["minimum"], published["maximum"]), (1, 5))
        # dose_g is a non-strict number rule, so fractions just outside count.
        for dose, expected in [
            (1, set()),
            (5, set()),
            (0.99, {"/tea/dose_g"}),
            (5.01, {"/tea/dose_g"}),
        ]:
            with self.subTest(dose_g=dose):
                self.assertEqual(paths(tea(dose_g=dose)), expected)
        # Steep temperature is an integer, so 39 is the nearest lower value.
        for temperature, expected in [
            (40, set()),
            (39, {"/tea/steeps/0/temperature"}),
        ]:
            document = tea()
            document["tea"]["steeps"][0]["temperature"] = temperature
            with self.subTest(temperature=temperature):
                self.assertEqual(paths(document), expected)

    def test_tea_steep_limit_boundaries(self):
        for key, value, valid in [
            ("volume_ml", 0, False),
            ("volume_ml", 1, True),
            ("volume_ml", 90, True),
            ("volume_ml", 91, False),
            ("steep_seconds", 360, True),
            ("steep_seconds", 361, False),
        ]:
            document = tea()
            document["tea"]["steeps"][0][key] = value
            expected = set() if valid else {"/tea/steeps/0/" + key}
            with self.subTest(key=key, value=value):
                self.assertEqual(paths(document), expected)
        flow = recipe_constraints()["tea"]["steeps"]["properties"]["flow_ml_s"]
        self.assertEqual(flow["enum"], [3, 3.1, 3.2, 3.3, 3.4, 3.5])

    def test_tea_steep_count_boundaries(self):
        for count, expected in [
            (0, {"/tea/steeps"}),
            (1, set()),
            (3, set()),
            (4, {"/tea/steeps"}),
        ]:
            document = tea()
            document["tea"]["steeps"] *= count
            with self.subTest(count=count):
                self.assertEqual(paths(document), expected)

    def test_tea_bounds_and_warnings(self):
        for key, value in [
            ("dose_g", 0),
            ("dose_g", 6),
            ("siphon_trigger", "manual"),
            ("ratio_metadata", 1.05),
            ("ratio_metadata", float("inf")),
            ("ratio_metadata", True),
            ("rpm_metadata", 61),
            ("grinder_size_metadata", 81),
        ]:
            with self.subTest(key=key, value=value):
                self.assertIn("/tea/" + key, paths(tea(**{key: value})))
        for key, value in [
            ("flow_ml_s", 3.15),
            ("vibration_before", 1),
            ("temperature", 96),
            ("temperature", 100),
            ("temperature", 39),
            ("steep_seconds", 4),
            ("volume_ml", 91.0),
        ]:
            document = tea()
            document["tea"]["steeps"][0][key] = value
            self.assertFalse(check_recipe(document)["valid"], (key, value))
        report = check_recipe(tea(dose_g=3.33, ratio_metadata=45))
        codes = [w["code"] for w in report["warnings"]]
        self.assertEqual(
            codes,
            [
                "tea_hardware_unvalidated",
                "manual_fractional_dose",
                "tea_ratio_byte_wrap",
            ],
        )
        self.assertEqual(
            (report["warnings"][2]["value"], report["warnings"][2]["encoded_byte"]),
            (45, 194),
        )
        self.assertEqual(report["normalized"]["tea"]["steeps"][0]["pattern"], "center")
        self.assertEqual(report["normalized"]["tea"]["siphon_trigger"], "auto")

    def test_tea_validation_reports_repairs_and_nominal_output(self):
        report = validate_tea_recipe(tea(), 120)
        self.assertTrue(report["valid"])
        self.assertEqual(report["estimated_output_ml"], 120)
        self.assertFalse(report["yield_verified"])
        self.assertEqual(set(report["constraints"]), {"tea", "tea_reference"})
        self.assertEqual(report["warnings"][-1]["code"], "nominal_output_only")
        mismatch = validate_tea_recipe(tea(), 240)
        self.assertEqual(mismatch["errors"][0]["code"], "nominal_output")
        self.assertIsNone(mismatch["normalized"])
        self.assertIn("steep count", mismatch["errors"][0]["repair_hint"])
        hints = {}
        document = tea(dose_g=9, extra=1)
        document["tea"]["steeps"][0]["temperature"] = 96
        document["tea"]["steeps"][0]["volume_ml"] = 0
        for error in validate_tea_recipe(document, 100)["errors"]:
            hints[error["path"]] = error["repair_hint"]
        self.assertIn("1 through 5", hints["/tea/dose_g"])
        self.assertIn("Allowed fields here", hints["/tea/extra"])
        self.assertIn("BP", hints["/tea/steeps/0/temperature"])
        self.assertIn("expected schema", hints["/tea/steeps/0/volume_ml"])
        self.assertIn("120, 240 or 360", hints["/expected_output_ml"])
        wrong = validate_tea_recipe(coffee())
        self.assertIn("relabelling", wrong["errors"][-1]["repair_hint"])
        self.assertIsNone(validate_tea_recipe("text")["estimated_output_ml"])
        empty = tea()
        empty["tea"]["steeps"] = []
        self.assertIsNone(validate_tea_recipe(empty)["estimated_output_ml"])

    def test_constraints_are_detached(self):
        constraints = recipe_constraints()
        constraints["tea"]["dose_g"]["maximum"] = 999
        constraints["coffee_reference"]["cup_limits"]["xpod"][1] = 0
        self.assertEqual(recipe_constraints()["tea"]["dose_g"]["maximum"], 5)
        self.assertEqual(
            recipe_constraints()["coffee_reference"]["cup_limits"]["xpod"], [40, 70]
        )
        self.assertFalse(check_recipe(tea(dose_g=6))["valid"])
        report = validate_tea_recipe(tea())
        report["constraints"]["tea"]["dose_g"]["maximum"] = 999
        self.assertEqual(
            validate_tea_recipe(tea())["constraints"]["tea"]["dose_g"]["maximum"], 5
        )
        original = tea()
        snapshot = deepcopy(original)
        check_recipe(original)
        self.assertEqual(original, snapshot)
