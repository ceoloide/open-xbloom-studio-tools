"""Named settings, presets, tea and calibration on a simulated peripheral."""

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tests.frames import f32, frame, reply, u32
from tests.machine import Machine, interaction, transmitted, until
from xbloom_studio import (
    BusyError,
    EncodedTeaRecipe,
    PresetWriteError,
    SavedPreset,
    SessionError,
    UnsupportedStageError,
    UntestedOperationError,
)

HOME, MODE = 8022, 11511
PREPARE = [HOME, MODE]
NO_WATER = 0x0C
# V12.0D.500's refusal of 11510 in the no-water state.
NO_WATER_REFUSAL = bytes.fromhex("0000000000000000000000000040000c")


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name="backup.json"):
        return Path(self.tmp.name) / name


class SettingTests(Base):
    async def test_display_settings_encode_and_leave_machine_home(self):
        m = Machine()
        async with interaction(m) as session:
            result = await session.set_brightness(8)
            await session.set_weight_unit("oz")
            await session.set_temperature_unit("F")
        self.assertEqual(
            result,
            {
                "command": 8103,
                "acknowledged": True,
                "protocol_evidence": "tested",
                "readback_verified": False,
                "response_payload_hex": "",
            },
        )
        self.assertEqual(
            m.codes()[1:],
            PREPARE + [8103, HOME] + PREPARE + [8005, HOME] + PREPARE + [8010, HOME],
        )
        self.assertEqual(
            (m.payloads(8103), m.payloads(8005), m.payloads(8010)),
            ([u32(8)], [u32(2)], [u32(0)]),
        )
        self.assertEqual(m.payloads(MODE), [b"\0" * 4] * 3)

    async def test_pattern_and_manual_pages_configure_without_start(self):
        m = Machine()
        async with interaction(m) as session:
            await session.set_pour_pattern("ring", temperature_c=93)
            await session.configure_brewer(temperature_c=90.5, pattern="spiral")
            await session.configure_grinder(size=30, rpm=100)
        self.assertEqual(
            m.codes()[1:],
            PREPARE
            + [8007, 8016, 8013, HOME]
            + PREPARE
            + [8007, 4510, 8013, HOME]
            + PREPARE
            + [8006, 8012, HOME],
        )
        self.assertEqual(m.payloads(8016), [u32(1)])
        self.assertEqual(m.payloads(8007), [u32(0) + f32(930), u32(2) + f32(905)])
        self.assertEqual(m.payloads(4510), [u32(905)])
        self.assertEqual(m.payloads(8006), [u32(30) + u32(100)])
        self.assertNotIn(4506, m.codes())
        self.assertNotIn(3500, m.codes())

    async def test_mode_home_and_wake_use_existing_connection(self):
        m = Machine()
        async with interaction(m) as session:
            await session.set_mode("recipes")
            await session.back_home()
            m.emit(8011)
            woke = await session.wake()
        self.assertTrue(woke["awake_observed"])
        self.assertEqual(m.codes()[1:], [HOME, MODE, HOME, HOME])
        self.assertEqual(m.payloads(MODE), [bytes.fromhex("91327856")])
        self.assertEqual(m.calls.count("connect"), 1)
        m = Machine()
        async with interaction(m) as session:
            self.assertFalse((await session.wake())["awake_observed"])

    async def test_setting_after_owned_scale_exits_scale_first(self):
        m = Machine()
        async with interaction(m) as session:
            await session.enter_scale()
            m.state(4)
            start = len(m.sent)
            await session.set_mode("pro")
            self.assertEqual(m.codes()[start:], [8014, HOME, MODE])
            self.assertFalse(session._scale_mode)

    async def test_settings_cannot_interrupt_operation(self):
        m = Machine()
        async with interaction(m) as session:
            await session.start_water(volume_ml=10)
            with self.assertRaises(BusyError):
                await session.set_brightness(3)
            with self.assertRaises(BusyError):
                await session.back_home()
            with self.assertRaises(BusyError):
                await session.wake()

    async def test_home_and_wake_need_quiescence_not_a_state_report(self):
        m = Machine()
        # An awake reconnect on V12.0D.500 reports its identity but no state.
        m.overrides[8100] = lambda p: [reply(8100), reply(40521, m.studio.info)]
        m.overrides[HOME] = lambda p: [reply(HOME)]
        async with interaction(m) as session:
            await session.back_home()
            self.assertFalse((await session.wake())["awake_observed"])
            m.state(0x22)
            for call in (session.back_home, session.wake):
                with self.subTest(call.__name__), self.assertRaises(BusyError):
                    await call()
        self.assertEqual(m.codes(), [8100, HOME, HOME])

    async def test_wake_waits_once_so_its_state_report_stays_current(self):
        async with interaction(transition_delay=0.25, stale_after=0.4) as session:
            woke = await session.wake()
            state = session.snapshot()["fields"]["machine_state_code"]
        self.assertFalse(woke["awake_observed"])
        self.assertEqual((state["value"], state["available"]), (1, True))

    async def test_wake_returns_when_the_machine_reports_awake(self):
        m = Machine()
        async with interaction(m, transition_delay=30) as session:
            waking = asyncio.create_task(session.wake())
            await transmitted(m, HOME)
            m.emit(8011)
            woke = await asyncio.wait_for(waking, 5)
        self.assertTrue(woke["awake_observed"])

    async def test_wake_does_not_wait_when_already_awake(self):
        m = Machine()
        async with interaction(m, transition_delay=30) as session:
            m.emit(8011)
            woke = await asyncio.wait_for(session.wake(), 5)
        self.assertTrue(woke["awake_observed"])

    async def test_wake_returns_when_the_link_drops(self):
        m = Machine()
        async with interaction(m, transition_delay=30) as session:
            waking = asyncio.create_task(session.wake())
            await transmitted(m, HOME)
            m.drop()
            woke = await asyncio.wait_for(waking, 5)
        self.assertFalse(woke["awake_observed"])

    async def test_setting_validation_happens_before_writes(self):
        m = Machine()
        async with interaction(m, allow_untested=True) as session:
            for call in (
                lambda: session.set_brightness(0),
                lambda: session.set_brightness(16),
                lambda: session.set_brightness(True),
                lambda: session.set_weight_unit("kg"),
                lambda: session.set_temperature_unit("K"),
                lambda: session.set_pour_pattern("zigzag"),
                lambda: session.set_pour_pattern("ring", temperature_c=10),
                lambda: session.configure_grinder(size=0, rpm=60),
                lambda: session.configure_grinder(size=10, rpm=59),
                lambda: session.configure_brewer(pattern="none"),
                lambda: session.configure_brewer(temperature_c=float("inf")),
                lambda: session.set_mode("expert"),
                lambda: session.set_water_source("well"),
                lambda: session.initialize_pour_radius(-1),
                lambda: session.set_pour_radius(2**31),
                lambda: session.set_pour_radius_level(5, baseline=700),
                lambda: session.set_vibration_level(6),
                lambda: session.initialize_vibration(1.5),
                lambda: session.set_cup_limits(10, 5),
                lambda: session.set_preset_count(4),
                lambda: session.configure_recipe(dose_g=10.5),
                lambda: session.configure_recipe(dose_g=10, bypass_ml=-1),
                lambda: session.configure_recipe(dose_g=10, bypass_temperature_c=100),
                lambda: session.configure_recipe(dose_g=10, cup_limits=(5, 1)),
                lambda: session.set_preset_order(
                    "ABA", backup_order="ABC", backup_path=self.path()
                ),
                lambda: session.set_preset_order(
                    "ABC", backup_order=["A"], backup_path=self.path()
                ),
            ):
                with self.assertRaises(ValueError):
                    await call()
        self.assertEqual(m.codes(), [8100])
        self.assertFalse(self.path().exists())

    async def test_cancelled_setting_is_uncertain(self):
        m = Machine()
        m.block_code = 8103
        async with interaction(m) as session:
            task = asyncio.create_task(session.set_brightness(5))
            await until(lambda: m.blocked.is_set())
            task.cancel()
            m.release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(session.get_state()["recovery_required"])
            self.assertEqual(m.codes()[-1], 8103)  # no cleanup after uncertainty


class UntestedTests(Base):
    async def test_all_experimental_entrypoints_are_gated_before_writes(self):
        m = Machine()
        preset = SavedPreset("A", b"x", 0)
        async with interaction(m) as session:
            for call in (
                lambda: session.write_preset(
                    preset, backup=preset, backup_path=self.path()
                ),
                lambda: session.set_water_source("tap"),
                lambda: session.initialize_pour_radius(700),
                lambda: session.set_pour_radius(700),
                lambda: session.set_pour_radius_level(2, baseline=700),
                lambda: session.initialize_vibration(1200),
                lambda: session.set_cup_limits(5, 100),
                lambda: session.configure_recipe(dose_g=10, bypass_ml=5),
                lambda: session.configure_recipe(dose_g=10, cup_limits=(5, 100)),
                lambda: session.start_tea(EncodedTeaRecipe(b"t")),
                lambda: session.upload_tea(EncodedTeaRecipe(b"t")),
                lambda: session.set_preset_count(2),
                lambda: session.set_preset_order(
                    "CAB", backup_order="ABC", backup_path=self.path()
                ),
                session.quit_recipe_preparation,
                session.back_to_normal,
            ):
                with self.assertRaises(UntestedOperationError):
                    await call()
        self.assertEqual(m.codes(), [8100])
        self.assertFalse(self.path().exists())

    async def test_parameter_queries_accept_actual_response_not_request_echo(self):
        m = Machine()
        async with interaction(m) as session:
            radius = await session.read_pour_radius()
            vibration = await session.read_vibration()
        self.assertEqual((radius["value"], vibration["value"]), (700, 1300))
        self.assertTrue(radius["readback_verified"])
        self.assertTrue(vibration["readback_verified"])
        self.assertEqual(radius["protocol_evidence"], "tested")
        self.assertEqual(vibration["protocol_evidence"], "tested")
        self.assertEqual(radius["units"], "raw_units")
        self.assertEqual(m.payloads(11506), [b""])
        self.assertEqual(m.payloads(11508), [b""])
        self.assertIn(frame(11506, function=2), b"".join(m.writes))

    async def test_vibration_level_uses_control_for_each_supported_level(self):
        m = Machine()
        async with interaction(m) as session:
            for level in range(6):
                result = await session.set_vibration_level(level)
                self.assertTrue(result["acknowledged"])
                self.assertEqual(result["protocol_evidence"], "tested")
                self.assertFalse(result["readback_verified"])
        self.assertEqual(
            m.payloads(11509), [u32(value) for value in range(1000, 1501, 100)]
        )

    async def test_malformed_parameter_reply_is_not_a_value(self):
        m = Machine()
        m.overrides[11506] = lambda p: [reply(11506, b"\x01\x02", 0xC2)]
        async with interaction(m) as session:
            with self.assertRaisesRegex(SessionError, "uint32"):
                await session.read_pour_radius()
            self.assertTrue(session.get_state()["recovery_required"])

    async def test_initialization_and_setting_use_distinct_payload_forms(self):
        m = Machine()
        async with interaction(m, allow_untested=True) as session:
            await session.initialize_pour_radius(650)
            await session.set_pour_radius(720)
            await session.initialize_vibration(1100)
            await session.set_vibration_level(3)
        self.assertEqual(m.payloads(11506), [u32(650)])
        self.assertEqual(m.payloads(11507), [u32(720)])
        self.assertEqual(m.payloads(11508), [u32(1100)])
        self.assertEqual(m.payloads(11509), [u32(1300)])
        self.assertIn(frame(11509, u32(1300), function=2), b"".join(m.writes))

    async def test_radius_levels_clamp_baseline_on_wire(self):
        m = Machine()
        async with interaction(m, allow_untested=True) as session:
            for level, baseline in ((0, 500), (2, 700), (4, 900)):
                await session.set_pour_radius_level(level, baseline=baseline)
        self.assertEqual(m.payloads(11507), [u32(400), u32(700), u32(1000)])

    async def test_new_source_is_preserved_by_the_next_pour(self):
        m = Machine()
        async with interaction(m, allow_untested=True) as session:
            result = await session.set_water_source("tap")
            self.assertEqual(result["requested_source"], "tap")
            self.assertEqual(session.get_state()["last_acknowledged_water_source"], 1)
            await session.start_water(volume_ml=10)
            await transmitted(m, 4506)
            await session.stop()
        self.assertEqual(m.payloads(4508), [u32(1)])
        self.assertEqual(m.payloads(4506)[0][12:16], u32(1))
        self.assertEqual(m.codes().count(4508), 1)

    async def test_recipe_configuration_bytes_without_executing(self):
        m = Machine()
        async with interaction(m, allow_untested=True) as session:
            plain = await session.configure_recipe(dose_g=15)
            full = await session.configure_recipe(
                dose_g=18, bypass_ml=40, bypass_temperature_c=85, cup_limits=(100, 300)
            )
            limits = await session.set_cup_limits(50, 250)
        self.assertEqual(plain["acknowledged_commands"], [8102])
        self.assertEqual(plain["protocol_evidence"], "tested")
        self.assertEqual(full["acknowledged_commands"], [8102, 8104])
        self.assertEqual(full["protocol_evidence"], "untested")
        self.assertEqual(
            m.payloads(8102),
            [f32(0) + f32(0) + u32(15), f32(40) + f32(850) + u32(18)],
        )
        # Cup limits travel maximum first.
        self.assertEqual(m.payloads(8104), [f32(300) + f32(100), f32(250) + f32(50)])
        self.assertEqual(limits["command"], 8104)
        self.assertNotIn(8002, m.codes())
        self.assertEqual(
            m.codes()[1:],
            PREPARE
            + [8102, HOME]
            + PREPARE
            + [8102, 8104, HOME]
            + PREPARE
            + [8104, HOME],
        )

    async def test_preset_count_and_order(self):
        m = Machine()
        async with interaction(m, allow_untested=True) as session:
            await session.set_preset_count(2)
            result = await session.set_preset_order(
                ["C", "A", "B"], backup_order="ABC", backup_path=self.path()
            )
        self.assertEqual(m.payloads(40525), [u32(2)])
        self.assertEqual(m.payloads(11512), [bytes([3, 2, 0, 1])])
        self.assertEqual(result["requested_order"], ("C", "A", "B"))
        self.assertFalse(result["readback_verified"])
        saved = json.loads(self.path().read_text())
        self.assertEqual(saved["order"], ["A", "B", "C"])
        self.assertFalse(saved["machine_readback_verified"])
        self.assertEqual(stat.S_IMODE(os.stat(self.path()).st_mode), 0o600)

    async def test_failed_order_write_is_uncertain_and_keeps_backup(self):
        m = Machine()
        m.studio.silent.add(11512)
        async with interaction(m, allow_untested=True) as session:
            with self.assertRaises(TimeoutError):
                await session.set_preset_order(
                    "BAC", backup_order="ABC", backup_path=self.path()
                )
            self.assertTrue(session.get_state()["recovery_required"])
        self.assertTrue(self.path().exists())

    async def test_housekeeping_requires_an_empty_acknowledgment(self):
        m = Machine()
        m.overrides[40526] = lambda p: [reply(40526, u32(85))]
        async with interaction(m, allow_untested=True, ack_timeout=0.02) as session:
            result = await session.quit_recipe_preparation()
            self.assertEqual(result["command"], 8017)
            with self.assertRaises(TimeoutError):
                await session.back_to_normal()
        self.assertEqual(m.codes()[1:], [8017, 40526])

    async def test_housekeeping_rejects_motion_but_not_unknown_state(self):
        m = Machine()
        async with interaction(m, allow_untested=True) as session:
            m.state(0x23)
            with self.assertRaisesRegex(SessionError, "motion"):
                await session.back_to_normal()
            m.state(0x77)
            await session.back_to_normal()
        self.assertEqual(m.codes()[1:], [40526])


class TeaTests(Base):
    async def test_tea_upload_start_phases_pause_resume_and_completion(self):
        m = Machine()
        async with interaction(m, allow_untested=True) as session:
            op = await session.start_tea(
                EncodedTeaRecipe(b"tea", cup_limits=(100, 200))
            )
            await transmitted(m, 4512)
            self.assertEqual(m.codes()[1:], [*PREPARE, 8104, 4513, 4512])
            self.assertEqual(m.payloads(4513), [b"tea"])
            m.emit(9012)
            self.assertEqual(op.phase, "soaking")
            m.emit(40515)
            self.assertEqual(op.phase, "tea_pause_observed")
            await session.resume()
            m.emit(9011)
            self.assertEqual(op.phase, "tea_resume_observed")
            await session.pause()
            m.state(0x24)
            result = await op.wait()
        self.assertEqual(result["status"], "completed")
        self.assertIn(40524, m.codes())
        self.assertIn(40518, m.codes())
        self.assertNotIn(40519, m.codes())

    async def test_tea_timeout_stops_instead_of_claiming_completion(self):
        m = Machine()
        async with interaction(m, allow_untested=True) as session:
            op = await session.start_tea(EncodedTeaRecipe(b"tea"), timeout=0.1)
            result = await op.wait()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["stop_acknowledged"])
        self.assertEqual(m.codes()[-2:], [40519, HOME])

    async def test_tea_commit_cancellation_finishes_frame_then_stops(self):
        m = Machine()
        m.block_code = 4512
        async with interaction(m, allow_untested=True) as session:
            op = await session.start_tea(EncodedTeaRecipe(b"t" * 30))
            await until(lambda: m.blocked.is_set())
            stopping = asyncio.create_task(session.stop())
            await asyncio.sleep(0)
            m.release.set()
            result = await stopping
        self.assertEqual(m.codes()[-3:], [4512, 40519, HOME])
        self.assertTrue(result["stop_acknowledged"])
        self.assertEqual(op.status, "stopped")

    async def test_tea_upload_without_execution(self):
        m = Machine()
        async with interaction(m, allow_untested=True) as session:
            prepared = await session.upload_tea(
                EncodedTeaRecipe(b"tea", cup_limits=(1, 2))
            )
            self.assertEqual(prepared["kind"], "tea")
            self.assertEqual(m.codes()[1:], [*PREPARE, 8104, 4513])
            op = await session.start_prepared(prepared["token"])
            await transmitted(m, 4512)
            m.state(0x24)
            await op.wait()
            with self.assertRaises(TypeError):
                await session.upload_tea(b"tea")
            with self.assertRaises(TypeError):
                await session.start_tea(b"tea")

    def test_tea_content_validation(self):
        for kwargs in (
            {"payload": b""},
            {"payload": "t"},
            {"payload": b"t", "cup_limits": (2, 1)},
        ):
            with self.assertRaises(ValueError):
                EncodedTeaRecipe(**kwargs)


class PresetTests(Base):
    def test_preset_frames_are_validated_before_becoming_slot_contents(self):
        good = frame(11510, bytes([1, 4]) + b"recipe", function=2)
        self.assertEqual(SavedPreset.from_frame(good), SavedPreset("B", b"recipe", 4))
        self.assertEqual(
            SavedPreset("B", b"recipe", 4).body(), bytes([1, 4]) + b"recipe"
        )
        for bad in (
            frame(11510, bytes([3, 4]) + b"r", function=2),
            frame(11510, bytes([0, 4]), function=2),
            frame(11510, bytes([0, 4]) + b"r"),
            frame(11511, bytes([0, 4]) + b"r", function=2),
        ):
            with self.assertRaises(ValueError):
                SavedPreset.from_frame(bad)
        for kwargs in (
            {"slot": "D"},
            {"payload": b""},
            {"flags": 256},
            {"flags": True},
        ):
            values = {"slot": "A", "payload": b"x", "flags": 0, **kwargs}
            with self.assertRaises(ValueError):
                SavedPreset(**values)

    async def test_batch_write_requires_experimental_session(self):
        m = Machine()
        presets = [SavedPreset(slot, b"new", 0) for slot in "ABC"]
        backup = [SavedPreset(slot, b"old", 0) for slot in "ABC"]
        async with interaction(m) as session:
            with self.assertRaises(UntestedOperationError):
                await session.write_presets(
                    presets, backup=backup, backup_path=self.path()
                )
        self.assertEqual(m.codes(), [8100])
        self.assertFalse(self.path().exists())

    async def test_backup_precedes_batch_write_and_save_event_is_required(self):
        m = Machine()
        presets = [SavedPreset(slot, slot.encode() * 3, 4) for slot in "CAB"]
        backup = [SavedPreset(slot, b"old", 1) for slot in "ABC"]
        async with interaction(m, allow_untested=True) as session:
            result = await session.write_presets(
                presets, backup=backup, backup_path=self.path()
            )
        saved = json.loads(self.path().read_text())
        self.assertEqual(saved["kind"], "caller_supplied_preset_backup")
        self.assertEqual([p["slot"] for p in saved["presets"]], ["A", "B", "C"])
        self.assertEqual(result["backup"]["path"], str(self.path()))
        self.assertTrue(result["saved_observed"])
        self.assertEqual(result["save_event"]["correlation"], "temporal_only")
        self.assertFalse(result["readback_verified"])
        self.assertEqual(result["acknowledged_slots"], ["A", "B", "C"])
        self.assertEqual(
            m.payloads(11510),
            [b"\0\x04AAA", b"\x01\x04BBB", b"\x02\x04CCC"],
        )
        self.assertEqual(m.codes()[1:], [*PREPARE, 11510, 11510, 11510, HOME])

    async def test_single_slot_write_sends_three_planned_messages(self):
        m = Machine()
        async with interaction(m, allow_untested=True) as session:
            result = await session.write_preset(
                SavedPreset("B", b"new", 2),
                backup=SavedPreset("B", b"old", 2),
                backup_path=self.path(),
            )
            self.assertEqual(result["requested_slots"], ["B"])
            self.assertEqual(result["planned_slot_writes"], ["B", "B", "B"])
            self.assertEqual(result["acknowledged_write_count"], 3)
            with self.assertRaisesRegex(SessionError, "fresh interaction"):
                await session.write_preset(
                    SavedPreset("B", b"new", 2),
                    backup=SavedPreset("B", b"old", 2),
                    backup_path=self.path("second.json"),
                )
        self.assertEqual(m.payloads(11510), [b"\x01\x02new"] * 3)
        self.assertFalse(self.path("second.json").exists())

    async def test_early_completion_stops_further_single_slot_writes(self):
        m = Machine()
        m.overrides[11510] = lambda p: [reply(11510, b"", 0xC2), reply(8023, u32(0x25))]
        async with interaction(m, allow_untested=True) as session:
            with self.assertRaises(PresetWriteError) as caught:
                await session.write_preset(
                    SavedPreset("A", b"n", 0),
                    backup=SavedPreset("A", b"o", 0),
                    backup_path=self.path(),
                )
        self.assertEqual(m.codes().count(11510), 1)
        self.assertTrue(caught.exception.result["saved_observed"])
        self.assertIn("before the final", caught.exception.result["error"])

    async def test_ack_without_save_is_not_success(self):
        m = Machine()
        m.overrides[11510] = lambda p: [reply(11510, b"", 0xC2)]
        async with interaction(m, allow_untested=True) as session:
            with self.assertRaises(PresetWriteError) as caught:
                await session.write_presets(
                    [SavedPreset(s, b"x", 0) for s in "ABC"],
                    backup=[SavedPreset(s, b"y", 0) for s in "ABC"],
                    backup_path=self.path(),
                    save_timeout=0.02,
                )
            self.assertTrue(session.get_state()["recovery_required"])
        result = caught.exception.result
        self.assertEqual(result["acknowledged_write_count"], 3)
        self.assertFalse(result["saved_observed"])
        self.assertFalse(result["restoration_attempted"])
        self.assertNotIn(HOME, m.codes()[-1:])

    async def test_missing_ack_does_not_retry_or_roll_back(self):
        m = Machine()
        m.studio.silent.add(11510)
        async with interaction(m, allow_untested=True) as session:
            with self.assertRaises(PresetWriteError) as caught:
                await session.write_presets(
                    [SavedPreset(s, b"x", 0) for s in "ABC"],
                    backup=[SavedPreset(s, b"y", 0) for s in "ABC"],
                    backup_path=self.path(),
                )
        self.assertEqual(m.codes().count(11510), 1)
        self.assertEqual(caught.exception.result["acknowledged_slots"], [])
        self.assertTrue(self.path().exists())

    async def test_save_is_recorded_when_a_later_step_fails(self):
        m = Machine()
        homes = []

        def home(packet):
            homes.append(packet)
            return m.studio(packet) if len(homes) == 1 else []

        m.overrides[HOME] = home
        async with interaction(m, allow_untested=True) as session:
            with self.assertRaises(PresetWriteError) as caught:
                await session.write_presets(
                    [SavedPreset(s, b"x", 0) for s in "ABC"],
                    backup=[SavedPreset(s, b"y", 0) for s in "ABC"],
                    backup_path=self.path(),
                )
        result = caught.exception.result
        self.assertTrue(result["saved_observed"])
        self.assertIn("TimeoutError", result["error"])
        self.assertEqual(len(homes), 2)

    async def test_cancelled_write_keeps_backup_and_propagates(self):
        m = Machine()
        m.block_code = 11510
        async with interaction(m, allow_untested=True) as session:
            task = asyncio.create_task(
                session.write_presets(
                    [SavedPreset(s, b"x", 0) for s in "ABC"],
                    backup=[SavedPreset(s, b"y", 0) for s in "ABC"],
                    backup_path=self.path(),
                )
            )
            await until(lambda: m.blocked.is_set())
            task.cancel()
            m.release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIn(
                "CancelledError", session.get_state()["last_preset_write"]["error"]
            )
        self.assertEqual(m.codes().count(11510), 1)
        self.assertTrue(self.path().exists())

    async def test_validation_and_existing_backup_cannot_write(self):
        m = Machine()
        a = SavedPreset("A", b"x", 0)
        async with interaction(m, allow_untested=True) as session:
            for call in (
                lambda: session.write_preset(
                    a, backup=SavedPreset("B", b"x", 0), backup_path=self.path()
                ),
                lambda: session.write_preset(
                    a, backup={"slot": "A"}, backup_path=self.path()
                ),
                lambda: session.write_presets(
                    [a, a, a], backup=[a, a, a], backup_path=self.path()
                ),
                lambda: session.write_presets([a], backup=[a], backup_path=self.path()),
                lambda: session.write_presets(
                    [SavedPreset(s, b"x", 0) for s in "ABC"],
                    backup=[SavedPreset(s, b"x", 0) for s in "ABC"],
                    backup_path=self.path(),
                    save_timeout=0,
                ),
            ):
                with self.assertRaises(ValueError):
                    await call()
            self.path().write_text("existing")
            with self.assertRaises(FileExistsError):
                await session.write_preset(a, backup=a, backup_path=self.path())
        self.assertEqual(m.codes(), [8100])
        self.assertEqual(self.path().read_text(), "existing")

    async def test_preset_writes_wait_for_water(self):
        m = Machine()
        async with interaction(m, allow_untested=True) as session:
            m.state(NO_WATER)
            with self.assertRaises(BusyError):
                await session.write_preset(SavedPreset("A", b"new", 4))
            with self.assertRaises(BusyError):
                await session.write_presets(
                    [SavedPreset(s, b"x", 0) for s in "ABC"],
                    backup=[SavedPreset(s, b"y", 0) for s in "ABC"],
                    backup_path=self.path(),
                )
            self.assertEqual(m.codes(), [8100])
            self.assertFalse(self.path().exists())
            m.state(1)
            result = await session.write_preset(SavedPreset("A", b"new", 4))
            self.assertTrue(result["saved_observed"])

    async def test_a_low_water_report_refuses_preset_writes_while_idle(self):
        m = Machine()
        async with interaction(m, allow_untested=True) as session:
            m.emit(40522, u32(0))
            m.state(1)
            with self.assertRaises(BusyError):
                await session.write_preset(SavedPreset("A", b"new", 4))
            self.assertEqual(m.codes(), [8100])
            m.emit(40522, u32(1))
            result = await session.write_preset(SavedPreset("A", b"new", 4))
            self.assertTrue(result["saved_observed"])


class PresetAttemptTests(Base):
    """One single-slot attempt: evidence stops it early and classifies it."""

    async def attempt(self, m, preset=None, **kwargs):
        async with interaction(m, allow_untested=True, **kwargs) as session:
            with self.assertRaises(PresetWriteError) as caught:
                await asyncio.wait_for(
                    session.write_preset(
                        preset or SavedPreset("A", b"new", 4), save_timeout=30
                    ),
                    2,
                )
        return caught.exception.result

    async def test_observed_rejection_ends_the_attempt_without_a_save_wait(self):
        m = Machine()
        m.studio.reject_preset_attempts = 1
        result = await self.attempt(m)
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["transmitted_write_count"], 3)
        self.assertEqual(result["acknowledged_write_count"], 2)
        self.assertFalse(result["saved_observed"])
        self.assertIsNone(result["backup"])
        self.assertEqual(
            [(e["command"], e["payload_hex"]) for e in result["rejection_evidence"]],
            [(11510, "00000000000000000000000000400044"), (8023, "44000000")],
        )
        self.assertEqual(m.codes()[1:], [*PREPARE, 11510, 11510, 11510])

    async def test_a_refusal_report_ends_the_attempt_without_an_ack_wait(self):
        m = Machine()
        m.overrides[11510] = lambda p: [reply(11510, NO_WATER_REFUSAL, 0xC1)]
        result = await self.attempt(m, ack_timeout=30)
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["transmitted_write_count"], 1)
        self.assertEqual(result["acknowledged_write_count"], 0)
        self.assertEqual(
            [(e["command"], e["payload_hex"]) for e in result["rejection_evidence"]],
            [(11510, NO_WATER_REFUSAL.hex())],
        )
        self.assertEqual(m.codes().count(11510), 1)

    async def test_an_empty_refusal_report_is_no_acknowledgment(self):
        m = Machine()
        m.overrides[11510] = lambda p: [reply(11510, b"", 0xC1)]
        result = await self.attempt(m)
        self.assertEqual(result["outcome"], "unconfirmed")
        self.assertEqual(result["acknowledged_write_count"], 0)
        self.assertEqual(result["rejection_evidence"], [])

    async def test_a_non_empty_response_stops_a_batch(self):
        for marker in (0xC1, 0xC2):
            with self.subTest(marker=marker):
                m = Machine()
                m.overrides[11510] = lambda p, marker=marker: [
                    reply(11510, NO_WATER_REFUSAL, marker)
                ]
                async with interaction(
                    m, allow_untested=True, ack_timeout=30
                ) as session:
                    with self.assertRaises(PresetWriteError) as caught:
                        await asyncio.wait_for(
                            session.write_presets(
                                [SavedPreset(s, b"x", 0) for s in "ABC"],
                                backup=[SavedPreset(s, b"y", 0) for s in "ABC"],
                                backup_path=self.path(f"{marker}.json"),
                            ),
                            2,
                        )
                result = caught.exception.result
                self.assertEqual(result["outcome"], "rejected")
                self.assertEqual(
                    (result["acknowledged_slots"], result["acknowledged_write_count"]),
                    ([], 0),
                )
                self.assertEqual(
                    [e["payload_hex"] for e in result["rejection_evidence"]],
                    [NO_WATER_REFUSAL.hex()],
                )
                self.assertEqual(m.codes().count(11510), 1)

    async def test_low_water_while_preparing_stops_before_any_preset_frame(self):
        for report in (reply(40522, u32(0)), reply(8023, u32(NO_WATER))):
            with self.subTest(report=report.hex()):
                m = Machine()
                m.overrides[MODE] = lambda p, m=m, report=report: [
                    *m.studio(p),
                    report,
                ]
                result = await self.attempt(m)
                self.assertEqual(result["outcome"], "no_preset_frame")
                self.assertEqual(m.codes()[1:], PREPARE)

    async def test_rejection_state_is_checked_before_the_next_frame(self):
        m = Machine()
        m.overrides[11510] = lambda p: [reply(11510, b"", 0xC2), reply(8023, u32(0x44))]
        result = await self.attempt(m)
        self.assertEqual(m.codes().count(11510), 1)
        self.assertEqual(result["outcome"], "rejected")
        self.assertIn("rejection state", result["error"])

    async def test_rejection_state_while_awaiting_the_save(self):
        m = Machine()
        sent = []

        def respond(packet):
            sent.append(packet)
            late = [reply(8023, u32(0x44))] if len(sent) == 3 else []
            return [reply(11510, b"", 0xC2), *late]

        m.overrides[11510] = respond
        result = await self.attempt(m)
        self.assertEqual(result["acknowledged_write_count"], 3)
        self.assertEqual(result["outcome"], "rejected")
        self.assertIn("not a save", result["error"])

    async def evidence_during_second_journal(self, evidence, flood=0):
        m = Machine()
        intents = []

        class Hooks:
            async def before_write(self, packet, frame):
                if packet.command == 11510:
                    intents.append(packet)
                    if len(intents) == 2:
                        # After the cursor the old code captured, before any
                        # byte of the second frame; a flood of reports then
                        # rolls the bounded history past it.
                        m.state(evidence)
                        for _ in range(flood):
                            m.state(1)
                        await asyncio.sleep(0.01)

            def write_completed(self, packet):
                pass

            def write_failed(self, packet, error):
                pass

        result = await self.attempt(m, command_hooks=Hooks())
        self.assertEqual(len(intents), 2)
        return m, result

    async def test_evidence_while_the_next_frame_journal_waits_stops_it(self):
        for evidence, outcome in ((0x44, "rejected"), (0x25, "saved_cleanup_failed")):
            with self.subTest(outcome=outcome):
                m, result = await self.evidence_during_second_journal(evidence)
                self.assertEqual(m.codes().count(11510), 1)
                self.assertEqual(result["transmitted_write_count"], 1)
                self.assertFalse(result["partial_frame"])
                self.assertEqual(result["outcome"], outcome)

    async def test_a_save_before_the_first_frame_is_not_correlated(self):
        m = Machine()

        class Hooks:
            async def before_write(self, packet, frame):
                if packet.command == 11510 and 11510 not in m.codes():
                    m.state(0x25)  # Stale: before this attempt's first frame.

            def write_completed(self, packet):
                pass

            def write_failed(self, packet, error):
                pass

        async with interaction(m, allow_untested=True, command_hooks=Hooks()) as s:
            result = await s.write_preset(SavedPreset("A", b"new", 4))
        # The stale save neither stopped the planned frames nor completed them.
        self.assertEqual(m.codes().count(11510), 3)
        self.assertEqual(result["outcome"], "saved")
        stale = [r for r, p in s._received_events if p.payload == u32(0x25)]
        self.assertEqual(len(stale), 2)
        self.assertGreaterEqual(result["save_event"]["after_event_revision"], stale[0])

    async def test_cancellation_before_any_preset_frame_is_cancelled(self):
        m = Machine()
        m.block_code = MODE
        async with interaction(m, allow_untested=True) as session:
            task = asyncio.create_task(session.write_preset(SavedPreset("A", b"n", 4)))
            await until(lambda: m.blocked.is_set())
            task.cancel()
            m.release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            result = session._last_preset_write
        self.assertEqual(result["transmitted_write_count"], 0)
        self.assertEqual(result["outcome"], "cancelled")
        self.assertNotIn(11510, m.codes())

    async def test_prepare_failure_sends_no_preset_frame(self):
        m = Machine()
        m.fail_commands[MODE] = ConnectionError("gone")
        result = await self.attempt(m)
        self.assertEqual(
            (result["outcome"], result["transmitted_write_count"]),
            ("no_preset_frame", 0),
        )
        self.assertNotIn(11510, m.codes())

    async def test_partial_frame_counts_as_an_unconfirmed_attempt(self):
        m = Machine(write_size=20)
        m.block_code = 11510
        result = await self.attempt(
            m, SavedPreset("A", bytes(40), 0), write_timeout=0.1
        )
        self.assertEqual(result["transmitted_write_count"], 0)
        self.assertTrue(result["partial_frame"])
        self.assertEqual(result["outcome"], "unconfirmed")

    async def test_backup_is_optional_for_one_slot(self):
        m = Machine()
        async with interaction(m, allow_untested=True) as session:
            with self.assertRaisesRegex(ValueError, "go together"):
                await session.write_preset(
                    SavedPreset("C", b"new", 1), backup_path=self.path()
                )
            result = await session.write_preset(SavedPreset("C", b"new", 1))
        self.assertIsNone(result["backup"])
        self.assertEqual(
            (result["outcome"], result["transmitted_write_count"]), ("saved", 3)
        )
        self.assertEqual(result["rejection_evidence"], [])
        self.assertFalse(self.path().exists())

    async def test_cancelled_single_slot_attempt_is_classified(self):
        m = Machine()
        m.block_code = 11510
        async with interaction(m, allow_untested=True) as session:
            task = asyncio.create_task(session.write_preset(SavedPreset("A", b"x", 0)))
            await until(lambda: m.blocked.is_set())
            task.cancel()
            m.release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            result = session.get_state()["last_preset_write"]
        self.assertEqual(
            (result["outcome"], result["transmitted_write_count"]), ("cancelled", 1)
        )

    async def test_cancellation_survives_a_write_that_then_fails(self):
        m = Machine()
        m.block_code = 11510
        async with interaction(m, allow_untested=True) as session:
            task = asyncio.create_task(session.write_preset(SavedPreset("A", b"x", 0)))
            await until(lambda: m.blocked.is_set())
            m.fail_commands[11510] = ConnectionError("link lost while draining")
            task.cancel()
            m.release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            result = session._last_preset_write
            self.assertEqual(m.codes().count(11510), 1)  # Sent, then failed.
            # Only the attempt keeps its cancellation; later commands do not.
            self.assertFalse(session._keep_cancellation)
            self.assertIsNone(session._event_latch)
        self.assertEqual(
            (result["outcome"], result["transmitted_write_count"]), ("cancelled", 1)
        )

    async def test_terminal_evidence_outlives_bounded_event_history(self):
        for evidence, outcome in ((0x44, "rejected"), (0x25, "saved_cleanup_failed")):
            with self.subTest(outcome=outcome):
                m, result = await self.evidence_during_second_journal(evidence, 300)
                self.assertEqual(m.codes().count(11510), 1)
                self.assertEqual(result["outcome"], outcome)
        flood = [reply(8023, u32(1))] * 300
        # The save after the final frame also survives the flood.
        m = Machine()
        sent = []

        def respond(packet):
            sent.append(packet)
            if len(sent) < 3:
                return [reply(11510, b"", 0xC2)]
            return [reply(11510, b"", 0xC2), reply(8023, u32(0x25)), *flood]

        m.overrides[11510] = respond
        async with interaction(m, allow_untested=True) as session:
            result = await asyncio.wait_for(
                session.write_preset(SavedPreset("A", b"new", 4), save_timeout=1), 2
            )
        self.assertEqual(result["outcome"], "saved")


class CalibrationTests(Base):
    async def test_calibration_uses_position_not_timer_to_complete(self):
        m = Machine(auto_complete=True)
        async with interaction(m) as session:
            result = await session.calibrate_grinder(timeout=5)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["start_observed"])
        # Completion is the calibrated position; the stop is explicit cleanup.
        self.assertTrue(result["stop_acknowledged"])
        self.assertEqual(m.payloads(3502), [u32(1000)])
        self.assertEqual(m.codes()[1:], [*PREPARE, 3502, 3505, 8012, HOME])

    async def test_calibration_timeout_stops_and_is_not_success(self):
        m = Machine()
        async with interaction(m) as session:
            op = await session.start_grinder_calibration(timeout=0.1)
            result = await op.wait()
            with self.assertRaises(BusyError):
                await session.pause()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["stop_acknowledged"])

    async def test_calibration_cannot_pause_and_cancel_keeps_connection(self):
        m = Machine()
        async with interaction(m) as session:
            task = asyncio.create_task(session.calibrate_grinder())
            await transmitted(m, 3502)
            await until(lambda: session.operation._armed and session._waiter is None)
            with self.assertRaises(UnsupportedStageError):
                await session.pause()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIn(3505, m.codes())
            self.assertTrue(session.get_state()["machine"]["connected"])
        self.assertEqual(m.calls.count("connect"), 1)


if __name__ == "__main__":
    unittest.main()
