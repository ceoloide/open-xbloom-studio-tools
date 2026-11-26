"""Coffee and tea encoders, plans and retained-plan repeat.

Wire expectations are written out byte by byte from the documented payload
layout for synthetic recipes, independently of the encoder code.
"""

import asyncio
import struct
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.machine import interaction, transmitted
from xbloom_studio import (
    EncodedRecipe,
    EncodedTeaRecipe,
    RecipeValidationError,
    encode_coffee_recipe,
    encode_tea_recipe,
)
from xbloom_studio import recipes as recipes_module
from xbloom_studio.coffee import _encode_coffee_recipe_v1
from xbloom_studio.recipes import (
    HistoryService,
    _compile_recipe,
    compile_recipe,
    validate_retained_plan,
)
from xbloom_studio.serialization import digest, dumps
from xbloom_studio.storage import Store
from xbloom_studio.tea import _encode_tea_recipe_v1

COFFEE_V1 = "studio-coffee-android-2.2.3-v1"
COFFEE_V2 = "studio-coffee-android-2.2.3-v2"
TEA_V1 = "studio-tea-android-2.2.3-v1"
TEA_V2 = "studio-tea-android-2.2.3-v2"


def coffee(**changes):
    recipe = {
        "cup_type": "xdripper",
        "dose_g": 15,
        "grind_size": 60,
        "ratio": 15,
        "pours": [{"volume_ml": 225, "temperature": 95}],
    }
    recipe.update(changes)
    return {
        "name": "Synthetic coffee",
        "mode": "coffee",
        "coffee": {"origin": "Synthetic"},
        "coffee_recipe": recipe,
    }


def tea(**changes):
    content = {
        "dose_g": 3,
        "steeps": [{"volume_ml": 90, "steep_seconds": 20, "temperature": 92}],
    }
    content.update(changes)
    return {"name": "Synthetic tea", "mode": "tea", "tea": content}


# One pour of 225 ml at 95 C, center, flow 3.0, no pause, RPM 120:
#   0c            body length (12)
#   7f 5f 00 00   127 ml fragment, 95 C, center, no vibration
#   62 5f 00 00   98 ml fragment, same settings
#   00 00 78 1e   pause 0, reserved, RPM 120, flow*10 = 30
#   3c 96         grind 60, ratio*10 = 150
SINGLE_POUR = "0c7f5f0000625f00000000781e3c96"

# Steep: 90 ml, 92 C, center, no vibration, -20 s, 0 min, RPM 120, flow 30;
# trailer grind 50, ratio*10 = 150.
SINGLE_STEEP = "085a5c0000ec00781e3296"


class CoffeeEncodingTest(unittest.TestCase):
    def test_single_pour_layout(self):
        doc = coffee()
        original = deepcopy(doc)
        encoded = encode_coffee_recipe(doc)
        self.assertEqual(encoded.payload.hex(), SINGLE_POUR)
        self.assertEqual(_encode_coffee_recipe_v1(doc).payload.hex(), SINGLE_POUR)
        self.assertEqual(doc, original)
        self.assertEqual(encoded.dose_g, 15)
        self.assertFalse(encoded.grinder_enabled)
        self.assertEqual(encoded.cup_limits, (90, 110))
        self.assertEqual((encoded.bypass_ml, encoded.bypass_temperature_c), (0, 20))

    def test_symbolic_temperatures_flags_flow_and_pauses(self):
        doc = coffee(
            cup_type="other",
            dose_g=10,
            ratio=12,
            rpm=90,
            grinder_enabled=True,
            pours=[
                {
                    "volume_ml": 40,
                    "temperature": "RT",
                    "flow_ml_s": 3.3,
                    "pause_seconds": 30,
                    "pattern": "ring",
                    "vibration_before": True,
                },
                {
                    "volume_ml": 80,
                    "temperature": "BP",
                    "flow_ml_s": 3.1,
                    "pattern": "spiral",
                    "vibration_after": True,
                },
            ],
            bypass={"volume_ml": 20, "temperature": "BP"},
        )
        current = (
            "10"  # body length 16
            "28140101"  # 40 ml, RT code 20, ring, vibration before
            "e2005a21"  # pause -30, reserved, RPM 90, flow 33
            "50620202"  # 80 ml, BP code 98, spiral, vibration after
            "0000001f"  # no pause, no RPM after the first pour, flow 31
            "3c78"  # grind 60, ratio 120
        )
        legacy = current.replace("28140101", "28270101").replace("50620202", "50600202")
        encoded = encode_coffee_recipe(doc)
        self.assertEqual(encoded.payload.hex(), current)
        self.assertEqual(encoded.cup_limits, (80, 200))
        self.assertEqual(encoded.bypass_temperature_c, 96)
        self.assertTrue(encoded.grinder_enabled)
        old = _encode_coffee_recipe_v1(doc)
        self.assertEqual(old.payload.hex(), legacy)
        self.assertEqual(old.cup_limits, (40, 90))
        self.assertEqual(old.bypass_temperature_c, 96)
        doc["coffee_recipe"]["bypass"]["temperature"] = "RT"
        self.assertEqual(encode_coffee_recipe(doc).bypass_temperature_c, 20)
        self.assertEqual(_encode_coffee_recipe_v1(doc).bypass_temperature_c, 39)

    def test_volume_splitting_and_zero_pours(self):
        doc = coffee(
            cup_type="other",
            dose_g=25,
            ratio=25,
            pours=[
                {"volume_ml": v, "temperature": 90}
                for v in (240, 240, 145, 0, 0, 0, 0, 0, 0)
            ],
        )
        payload = encode_coffee_recipe(doc).payload
        # 240 = 127 + 113; 145 = 127 + 18; a zero pour keeps one record.
        self.assertEqual(payload[0], 2 * 12 + 12 + 6 * 8)
        self.assertEqual(payload[1:5], bytes((127, 90, 0, 0)))
        self.assertEqual(payload[5:9], bytes((113, 90, 0, 0)))
        self.assertEqual(payload[-10:-2], bytes((0, 90, 0, 0, 0, 0, 0, 30)))
        self.assertEqual(payload[-2:], bytes((60, 250)))

    def test_rejects_invalid_and_other_modes(self):
        doc = coffee(grind_size=81)
        with self.assertRaises(RecipeValidationError) as caught:
            encode_coffee_recipe(doc)
        self.assertEqual(
            caught.exception.issues[0]["path"], "/coffee_recipe/grind_size"
        )
        with self.assertRaisesRegex(ValueError, "mode=coffee"):
            encode_coffee_recipe({"name": "Host", "steps": [{"kind": "tare"}]})


class TeaEncodingTest(unittest.TestCase):
    def test_single_steep_layout(self):
        doc = tea()
        result = encode_tea_recipe(doc)
        self.assertEqual(result.payload.hex(), SINGLE_STEEP)
        self.assertEqual(result.cup_limits, (40, 80))
        self.assertEqual(_encode_tea_recipe_v1(doc).payload.hex(), SINGLE_STEEP)

    def test_extended_settings_sentinels_and_ratio_wrap(self):
        doc = tea(
            dose_g=4.5,
            grinder_size_metadata=20,
            rpm_metadata=80,
            ratio_metadata=45,
            steeps=[
                {
                    "volume_ml": 60,
                    "steep_seconds": 125,
                    "temperature": "RT",
                    "pattern": "ring",
                    "flow_ml_s": 3.5,
                    "vibration_before": True,
                    "vibration_after": True,
                },
                {"volume_ml": 70, "steep_seconds": 60, "temperature": "BP"},
                {"volume_ml": 80, "steep_seconds": 5, "temperature": 99},
            ],
        )
        expected = (
            "18"  # three steeps * 8
            # 60 ml, RT code 39, ring, both vibrations, -5 s, 2 min, RPM 80, 35
            "3c270103fb405023"
            # 70 ml, BP code 96, center, none, 0 s, 1 min, no RPM, flow 30
            "466000000020001e"
            # 80 ml, 99 C, center, none, -5 s, 0 min, no RPM, flow 30
            "50630000fb00001e"
            "14c2"  # grind 20, ratio 450 narrowed to one byte (194)
        )
        self.assertEqual(encode_tea_recipe(doc).payload.hex(), expected)
        with self.assertRaisesRegex(ValueError, "v1"):
            _encode_tea_recipe_v1(doc)

    def test_dose_is_metadata_and_invalid_documents_are_refused(self):
        a = compile_recipe(tea())
        b = compile_recipe(tea(dose_g=5))
        self.assertEqual(a["execution"], b["execution"])
        self.assertNotEqual(a["recipe_sha256"], b["recipe_sha256"])
        bad = tea()
        bad["tea"]["steeps"][0]["steep_seconds"] = 361
        with self.assertRaises(RecipeValidationError):
            encode_tea_recipe(bad)
        with self.assertRaisesRegex(ValueError, "mode=tea"):
            encode_tea_recipe({"name": "Water", "steps": [{"kind": "tare"}]})


class PlanTest(unittest.TestCase):
    def test_current_plans_record_encoder_bytes_and_watchdog(self):
        plan = compile_recipe(coffee(bypass={"volume_ml": 20, "temperature": "RT"}))
        self.assertEqual(plan["encoder_version"], COFFEE_V2)
        step = plan["execution"][0]
        self.assertEqual(step["payload_hex"], SINGLE_POUR)
        self.assertEqual(step["cup_limits"], [90, 110])
        self.assertEqual((step["bypass_ml"], step["bypass_temperature_c"]), (20, 20))
        # pauses 0 + ceil(225 / 3 + 20 / 3) + 600
        self.assertEqual(step["timeout"], 682)
        self.assertTrue(plan["requires_untested"])
        self.assertEqual(plan["recipe_sha256"], digest(plan["recipe_snapshot"]))
        tea_plan = compile_recipe(tea())
        self.assertEqual(tea_plan["encoder_version"], TEA_V2)
        self.assertEqual(tea_plan["execution"][0]["timeout"], 620)
        wrapped = compile_recipe(tea(ratio_metadata=45))
        self.assertEqual(len(wrapped["limitations"]), len(tea_plan["limitations"]) + 1)

    def test_host_and_native_plans(self):
        host = compile_recipe(
            {
                "name": " Host ",
                "steps": [
                    {"kind": "user", "instruction": "Place cup"},
                    {"kind": "water", "volume_ml": 100, "temperature_c": 90},
                ],
            }
        )
        self.assertEqual(host["encoder_version"], "studio-plan-1")
        self.assertEqual(host["requires_user"], ["Place cup"])
        self.assertFalse(host["requires_untested"])
        self.assertEqual(host["recipe_snapshot"]["name"], "Host")
        self.assertEqual(host["execution"][1]["flow_ml_s"], 3)
        native = {"name": "N", "mode": "native", "native": {"payload_hex": "AB CD"}}
        native["native"]["dose_g"] = 10.0
        plan = compile_recipe(native)
        self.assertEqual(
            plan["execution"],
            [{"kind": "native", "payload_hex": "abcd", "dose_g": 10.0}],
        )
        self.assertFalse(plan["requires_untested"])
        native["native"]["cup_limits"] = [40, 80]
        self.assertTrue(compile_recipe(native)["requires_untested"])

    def test_legacy_encoder_plans_validate_and_repeat_exactly(self):
        doc = coffee(
            pours=[{"volume_ml": 225, "temperature": "BP"}],
            bypass={"volume_ml": 10, "temperature": "RT"},
        )
        old = _compile_recipe(doc, coffee_encoder_version=COFFEE_V1)
        self.assertEqual(old["encoder_version"], COFFEE_V1)
        # As SINGLE_POUR, with the legacy BP pour code 96 (0x60).
        self.assertEqual(
            old["execution"][0]["payload_hex"], "0c7f600000626000000000781e3c96"
        )
        self.assertEqual(old["execution"][0]["cup_limits"], [40, 90])
        self.assertEqual(old["execution"][0]["bypass_temperature_c"], 39)
        new = compile_recipe(doc)
        self.assertEqual(
            new["execution"][0]["payload_hex"], "0c7f620000626200000000781e3c96"
        )
        validate_retained_plan(old)
        validate_retained_plan(new)
        old_tea = _compile_recipe(tea(), tea_encoder_version=TEA_V1)
        validate_retained_plan(old_tea)
        self.assertEqual(old_tea["execution"][0]["payload_hex"], SINGLE_STEEP)
        for plan in (old, old_tea):
            with self.subTest(version=plan["encoder_version"]):
                tampered = deepcopy(plan)
                tampered["execution"][0]["payload_hex"] = (
                    "00" + plan["execution"][0]["payload_hex"][2:]
                )
                with self.assertRaisesRegex(ValueError, "modified"):
                    validate_retained_plan(tampered)
                unknown = deepcopy(plan)
                unknown["encoder_version"] = "unknown-encoder"
                with self.assertRaisesRegex(ValueError, "version"):
                    validate_retained_plan(unknown)
        for key, value in (("grinder_size_metadata", 50), ("ratio_metadata", 15)):
            with self.assertRaisesRegex(ValueError, "v1"):
                _compile_recipe(tea(**{key: value}), tea_encoder_version=TEA_V1)

    def test_malformed_retained_plans_are_refused(self):
        plan = compile_recipe(coffee())
        for bad in (None, [], {}, {"recipe_snapshot": "x"}, {"recipe_snapshot": None}):
            with (
                self.subTest(plan=bad),
                self.assertRaisesRegex(ValueError, "malformed"),
            ):
                validate_retained_plan(bad)
        broken = deepcopy(plan)
        broken["recipe_snapshot"]["coffee_recipe"]["grind_size"] = 0
        with self.assertRaises(RecipeValidationError):
            validate_retained_plan(broken)
        for key, value in (
            ("recipe_sha256", "0" * 64),
            ("requires_untested", False),
            ("schema_version", 2),
            ("limitations", "text"),
        ):
            changed = deepcopy(plan)
            changed[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "modified"):
                validate_retained_plan(changed)
        missing = deepcopy(plan)
        del missing["encoder_version"]
        with self.assertRaisesRegex(ValueError, "version"):
            validate_retained_plan(missing)

    def test_retained_execution_rejects_signed_zero_and_boolean_changes(self):
        plan = compile_recipe(
            {
                "name": "Native",
                "mode": "native",
                "native": {
                    "payload_hex": "ab",
                    "dose_g": 1,
                    "cup_limits": [0.0, 2.0],
                },
            }
        )
        validate_retained_plan(plan)
        for patch in ({"cup_limits": [-0.0, 2.0]}, {"dose_g": True}):
            changed = deepcopy(plan)
            changed["execution"][0].update(patch)
            with (
                self.subTest(patch=patch),
                self.assertRaisesRegex(ValueError, "modified"),
            ):
                validate_retained_plan(changed)

    def test_earlier_advisory_text_is_recognized_by_digest_only(self):
        """Synthetic stand-in for advisory text written by earlier versions."""
        plan = compile_recipe(tea())
        retained = deepcopy(plan)
        retained["limitations"] = ["Synthetic earlier advisory text."]
        with self.assertRaisesRegex(ValueError, "modified"):
            validate_retained_plan(retained)
        known = {("tea", TEA_V2): frozenset({digest(retained["limitations"])})}
        with mock.patch.object(recipes_module, "LEGACY_LIMITATIONS", known):
            validate_retained_plan(retained)
            other_mode = deepcopy(retained)
            other_mode["encoder_version"] = TEA_V1
            with self.assertRaisesRegex(ValueError, "modified"):
                validate_retained_plan(other_mode)
            retained["execution"][0]["timeout"] += 1
            with self.assertRaisesRegex(ValueError, "modified"):
                validate_retained_plan(retained)

    def test_recorded_digests_cover_each_retained_encoder(self):
        self.assertEqual(
            set(recipes_module.LEGACY_LIMITATIONS),
            {
                ("coffee", COFFEE_V1),
                ("coffee", COFFEE_V2),
                ("tea", TEA_V1),
                ("tea", TEA_V2),
            },
        )
        for digests in recipes_module.LEGACY_LIMITATIONS.values():
            self.assertTrue(all(len(value) == 64 for value in digests))
        tested_steep = {
            "volume_ml": 90,
            "steep_seconds": 30,
            "temperature": "BP",
            "pattern": "spiral",
        }
        current = {
            digest(compile_recipe(coffee())["limitations"]),
            digest(compile_recipe(tea())["limitations"]),
            digest(compile_recipe(tea(steeps=[tested_steep]))["limitations"]),
        }
        recorded = set().union(*recipes_module.LEGACY_LIMITATIONS.values())
        self.assertFalse(current & recorded)

    def test_repeat_returns_the_stored_plan(self):
        golden = compile_recipe(tea())
        history = HistoryService(SimpleNamespace(operation=lambda _: {"plan": golden}))
        repeated = history.repeat_plan("old")
        self.assertEqual(repeated, golden)
        self.assertIsNot(repeated, golden)


class RepeatOnMachineTest(unittest.IsolatedAsyncioTestCase):
    async def test_repeat_sends_byte_identical_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "history.db")
            try:
                plan = _compile_recipe(coffee(), coffee_encoder_version=COFFEE_V1)
                op, created = store.create_operation(
                    "sim-control", {"kind": "recipe"}, plan, "first"
                )
                self.assertTrue(created)
                again, created = store.create_operation(
                    "sim-control", {"kind": "recipe"}, plan, "first"
                )
                self.assertFalse(created)
                self.assertEqual(again["id"], op["id"])
                repeated = HistoryService(store).repeat_plan(op["id"])
            finally:
                store.close()
        self.assertEqual(repeated, plan)
        self.assertEqual(dumps(repeated), dumps(plan))
        sent = []
        for stored in (plan, repeated):
            step = stored["execution"][0]
            recipe = EncodedRecipe(
                bytes.fromhex(step["payload_hex"]),
                step["dose_g"],
                step["grinder_enabled"],
                tuple(step["cup_limits"]),
                step["bypass_ml"],
                step["bypass_temperature_c"],
            )
            async with interaction(allow_untested=True) as session:
                operation = await session.start_recipe(recipe, timeout=step["timeout"])
                machine = session.transport.connector
                await transmitted(machine, 8002)
                sent.append(
                    [
                        (packet.command, packet.payload)
                        for packet in machine.sent
                        if packet.command in (8001, 8004, 8102, 8104)
                    ]
                )
                await session.stop()
                await asyncio.wait({asyncio.ensure_future(operation.wait())}, timeout=1)
        self.assertEqual(sent[0], sent[1])
        self.assertIn((8004, bytes.fromhex(SINGLE_POUR)), sent[0])
        self.assertIn((8104, struct.pack("<ff", 90, 40)), sent[0])
        self.assertIn((8102, struct.pack("<ffI", 0, 0, 15)), sent[0])

    async def test_tea_plan_bytes_reach_the_tea_upload(self):
        step = compile_recipe(tea())["execution"][0]
        recipe = EncodedTeaRecipe(
            bytes.fromhex(step["payload_hex"]), tuple(step["cup_limits"])
        )
        async with interaction(allow_untested=True) as session:
            await session.start_tea(recipe, timeout=step["timeout"])
            machine = session.transport.connector
            await transmitted(machine, 4512)
            self.assertEqual(machine.payloads(4513), [bytes.fromhex(SINGLE_STEEP)])
            await session.stop()
