"""The one hardware-tested tea content, and everything it excludes.

Only a direct start of one exact encoded tea value drops the experimental
opt-in. Any other tea content, and any tea upload, keeps it. Expected bytes
are written out from the documented payload layout.
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.machine import Machine, interaction
from xbloom_studio import EncodedTeaRecipe, encode_tea_recipe
from xbloom_studio.application import MachineService
from xbloom_studio.control import Interaction
from xbloom_studio.machine_controls import (
    VALIDATED_TEA,
    ExperimentalOptInError,
    UntestedOperationError,
)
from xbloom_studio.recipe_validation import RATIO_WRAP, TEA_REFERENCE, check_recipe
from xbloom_studio.recipes import (
    HistoryService,
    _compile_recipe,
    compile_recipe,
    upload_requires_untested,
    validate_retained_plan,
)
from xbloom_studio.storage import Store

FAST = {
    "settle": 0,
    "subscription_settle": 0,
    "transition_delay": 0,
    "ack_timeout": 0.2,
    "write_timeout": 3,
}
TEA_V1 = "studio-tea-android-2.2.3-v1"
TEA_V2 = "studio-tea-android-2.2.3-v2"
# 08            one steep * 8
# 5a 60 02 00   90 ml, BP code 96, spiral, no vibration
# e2 00 78 1e   -30 s, 0 min, RPM 120 (first steep), flow*10 = 30
# 32 96         grind 50, ratio*10 = 150
TESTED_PAYLOAD = bytes.fromhex("085a600200e200781e3296")
TESTED_CUP_LIMITS = struct.pack("<ff", 80, 40)
# What every tea plan recorded before any tea content was hardware-tested.
PRE_CHANGE_LIMITATIONS = [
    "Tea encoding follows the reference recipe format; physical execution remains "
    "unvalidated.",
    "Dose is manual preparation metadata; load the tea and prepare the appropriate "
    "brewer and cup.",
    "Soak volumes do not predict cup yield; automatic siphon top-up is firmware "
    "controlled.",
    "Physical prompts must be advanced on the machine; no remote advance is inferred.",
    "The watchdog adds 600 seconds for heating, filling and draining; it is not a "
    "duration prediction.",
]


def tested_tea(**steep_changes):
    """4 g, one 90 ml BP spiral steep of 30 s, every other setting default."""
    steep = {
        "volume_ml": 90,
        "temperature": "BP",
        "pattern": "spiral",
        "steep_seconds": 30,
    }
    steep.update(steep_changes)
    return {
        "name": "Tested tea",
        "mode": "tea",
        "tea": {"dose_g": 4, "steeps": [steep]},
    }


def two_steeps():
    document = tested_tea()
    document["tea"]["steeps"].append(dict(document["tea"]["steeps"][0]))
    return document


def pre_change(plan, *extra):
    """``plan`` as recorded before any tea content was hardware-tested."""
    return {
        **plan,
        "requires_untested": True,
        "limitations": [*PRE_CHANGE_LIMITATIONS, *extra],
    }


def request(document):
    return {"kind": "recipe", "document": document}


class TestedContentTests(unittest.TestCase):
    def test_reference_hardware_tested_steep_encodes_to_the_tested_value(self):
        tested = TEA_REFERENCE["hardware_tested"]
        self.assertIs(tested["requires_untested"], False)
        document = {
            "name": "Reference",
            "mode": "tea",
            "tea": {"dose_g": 4, "steeps": [tested["steep"]], **tested["metadata"]},
        }
        self.assertEqual(encode_tea_recipe(document), VALIDATED_TEA)
        self.assertEqual(VALIDATED_TEA, EncodedTeaRecipe(TESTED_PAYLOAD, (40, 80)))

    def test_unvalidated_warning_is_absent_only_for_the_tested_content(self):
        invalid = tested_tea(steep_seconds=361)
        for name, document, warned in (
            ("tested", tested_tea(), False),
            ("one field", tested_tea(pattern="center"), True),
            ("two steeps", two_steeps(), True),
            ("invalid", invalid, True),
        ):
            with self.subTest(name):
                codes = [w["code"] for w in check_recipe(document)["warnings"]]
                self.assertIs("tea_hardware_unvalidated" in codes, warned)

    def test_only_the_tested_content_compiles_without_opt_in(self):
        tested = compile_recipe(tested_tea())
        self.assertIs(tested["requires_untested"], False)
        self.assertIn("hardware-tested for a direct start", tested["limitations"][0])
        for document in (tested_tea(steep_seconds=31), two_steeps()):
            other = compile_recipe(document)
            self.assertIs(other["requires_untested"], True)
            self.assertIn("remains unvalidated", other["limitations"][0])

    def test_tea_upload_needs_opt_in_even_for_the_tested_content(self):
        self.assertIs(upload_requires_untested(compile_recipe(tested_tea())), True)


class RetainedPlanTests(unittest.TestCase):
    def test_limitations_recorded_before_the_change_still_validate(self):
        wrapped = tested_tea()
        wrapped["tea"]["ratio_metadata"] = 45
        for name, document, version, extra in (
            ("tested v2", tested_tea(), TEA_V2, ()),
            ("other v2", tested_tea(steep_seconds=31), TEA_V2, ()),
            ("wrapped v2", wrapped, TEA_V2, (RATIO_WRAP,)),
            ("tested v1", tested_tea(), TEA_V1, ()),
            ("other v1", tested_tea(steep_seconds=31), TEA_V1, ()),
        ):
            plan = _compile_recipe(document, tea_encoder_version=version)
            with self.subTest(name):
                validate_retained_plan(pre_change(plan, *extra))

    def test_repeat_keeps_stored_content_and_takes_todays_opt_in(self):
        stored = pre_change(compile_recipe(tested_tea()))
        history = HistoryService(SimpleNamespace(operation=lambda _: {"plan": stored}))
        repeated = history.repeat_plan("old")
        self.assertIs(repeated["requires_untested"], False)
        self.assertEqual({**repeated, "requires_untested": True}, stored)

    def test_other_changes_to_a_retained_plan_still_refuse(self):
        changed_timeout = pre_change(compile_recipe(tested_tea()))
        changed_timeout["execution"] = [
            {**changed_timeout["execution"][0], "timeout": 631}
        ]
        untested_marked_tested = {
            **compile_recipe(tested_tea(steep_seconds=31)),
            "requires_untested": False,
        }
        plain_native = compile_recipe(
            {
                "name": "N",
                "mode": "native",
                "native": {"payload_hex": "01", "dose_g": 9},
            }
        )
        for name, plan in {
            "changed timeout": changed_timeout,
            "untested tea marked tested": untested_marked_tested,
            "native marked untested": {**plain_native, "requires_untested": True},
        }.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, "modified"):
                validate_retained_plan(plan)


class LibraryGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_tea_sends_the_tested_content_without_opt_in(self):
        m = Machine(auto_complete=True)
        async with interaction(m) as session:
            result = await (await session.start_tea(VALIDATED_TEA)).wait()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(m.payloads(8104), [TESTED_CUP_LIMITS])
        self.assertEqual(m.payloads(4513), [TESTED_PAYLOAD])
        self.assertEqual(m.codes().count(4512), 1)

    async def test_start_tea_refuses_other_content_without_opt_in(self):
        for name, recipe in {
            "one byte": EncodedTeaRecipe(TESTED_PAYLOAD[:-1] + b"\x97", (40, 80)),
            "other cup limits": EncodedTeaRecipe(TESTED_PAYLOAD, (40, 90)),
            "no cup limits": EncodedTeaRecipe(TESTED_PAYLOAD),
        }.items():
            m = Machine()
            async with interaction(m) as session:
                with self.subTest(name), self.assertRaises(UntestedOperationError):
                    await session.start_tea(recipe)
            self.assertFalse({4512, 4513, 8104} & set(m.codes()))

    def test_capabilities_name_start_tea_conditionally_untested(self):
        info = Interaction.capabilities()
        self.assertNotIn("start_tea", info["untested_operations"])
        self.assertIn("upload_tea", info["untested_operations"])
        self.assertIn("start_tea", info["conditionally_untested_operations"])


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "xbloom-studio.sqlite3")
        self.machine = Machine("00:00:5E:00:53:01", auto_complete=True)
        self.service = MachineService(
            self.store, interaction_options=dict(FAST), connector=self.machine
        )

    async def asyncTearDown(self):
        await self.service.close()
        self.store.close()
        self.tmp.cleanup()

    async def finish(self):
        await self.service.task
        return self.store.operation(self.service.operation_id)

    async def test_tested_tea_starts_without_opt_in(self):
        await self.service.start(request(tested_tea()), "tea", True)
        done = await self.finish()
        self.assertEqual(done["status"], "completed")
        self.assertFalse(self.service.session.allow_untested)
        self.assertEqual(self.machine.payloads(4513), [TESTED_PAYLOAD])
        self.assertEqual(self.machine.codes().count(4512), 1)

    async def test_other_tea_needs_opt_in_and_sends_nothing(self):
        for name, document in {
            "one field": tested_tea(steep_seconds=31),
            "two steeps": two_steeps(),
        }.items():
            with self.subTest(name), self.assertRaises(ExperimentalOptInError):
                await self.service.start(request(document), name, True)
        self.assertEqual(self.machine.calls, [])

    async def test_tested_tea_upload_needs_opt_in_and_sends_nothing(self):
        preview = self.service.prepare(request(tested_tea()))
        self.assertIs(preview["requires_untested"], False)
        self.assertIs(preview["upload_requires_untested"], True)
        with self.assertRaises(ExperimentalOptInError):
            await self.service.upload(
                request(tested_tea()),
                True,
                expected_recipe_sha256=preview["recipe_sha256"],
            )
        self.assertEqual(self.machine.calls, [])

    async def test_exact_repeat_of_a_pre_change_plan_needs_no_opt_in(self):
        plan = pre_change(compile_recipe(tested_tea()))
        op, _ = self.store.create_operation(
            "00:00:5E:00:53:01", {"kind": "recipe"}, plan, "before"
        )
        await self.service.start(
            {"kind": "repeat", "operation_id": op["id"]}, "again", True
        )
        done = await self.finish()
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.machine.payloads(4513), [TESTED_PAYLOAD])
        self.assertEqual(done["plan"]["execution"], plan["execution"])
