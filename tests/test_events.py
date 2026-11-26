import math
import struct
import time
import unittest

from tests.frames import identity_block, reply, u32
from xbloom_studio.events import EVENT_FIELDS, decode_event, protocol_error
from xbloom_studio.protocol import Packet, decode
from xbloom_studio.telemetry import DeviceState, decode_values


def packet(code, payload=b"", marker=0xC1, function=7, device=2):
    return Packet(device, function, code, marker, payload)


def apply(state, code, payload=b"", marker=0xC1):
    state.apply(decode(reply(code, payload, marker)))


class ErrorEnvelopeTests(unittest.TestCase):
    def test_kinds_values_and_markers(self):
        machine = protocol_error(packet(65534, u32(4361), marker=0xCD))
        self.assertEqual(machine["kind"], "machine_error")
        self.assertEqual(machine["value"], 4361)
        self.assertFalse(machine["malformed"])
        self.assertEqual(
            set(machine),
            {
                "kind",
                "command",
                "function",
                "marker",
                "payload_hex",
                "value",
                "malformed",
                "evidence",
            },
        )
        malformed = protocol_error(packet(65534, b"\x01", marker=0xCD))
        self.assertTrue(malformed["malformed"])
        self.assertIsNone(malformed["value"])
        self.assertEqual(
            protocol_error(packet(65534, u32(5), function=3))["kind"], "protocol_error"
        )
        # On function 7 the same code falls in the response-error range.
        self.assertEqual(
            protocol_error(packet(65534, u32(5)))["kind"], "command_response_error"
        )
        self.assertEqual(
            protocol_error(packet(65533, marker=0xCD))["kind"], "transport_error"
        )
        self.assertIsNone(protocol_error(packet(65533, function=3)))
        self.assertEqual(
            protocol_error(packet(50002))["kind"], "command_response_error"
        )
        for code in (50001, 50038, 50039):
            self.assertIsNone(protocol_error(packet(code)))
        self.assertIsNone(protocol_error(packet(65534, marker=0xCD, device=1)))

    def test_error_takes_priority_over_event(self):
        event = decode_event(packet(65534, u32(9), marker=0xCD))
        self.assertEqual(event["name"], "protocol_error")
        self.assertEqual(event["data"]["value"], 9)
        self.assertEqual(
            decode_values(packet(65534, u32(9), marker=0xCD))[0][0],
            "last_protocol_error",
        )


class EventTests(unittest.TestCase):
    def test_scalars_signals_and_offsets(self):
        self.assertEqual(
            decode_event(packet(8105, u32(52)))["data"], {"grind_setting": 22}
        )
        self.assertEqual(
            decode_event(packet(8111, b"\x02"))["data"], {"selected_preset_index": 2}
        )
        self.assertEqual(
            decode_event(packet(40510, u32(3) + b"\xff"))["data"], {"pour_index": 3}
        )
        self.assertEqual(decode_event(packet(40511))["name"], "brewer_stopped")
        self.assertIsNone(decode_event(packet(40526)))  # empty housekeeping ACK
        self.assertEqual(
            decode_event(packet(40526, u32(85)))["data"], {"grinder_position": 85}
        )

    def test_layouts(self):
        units = decode_event(packet(8015, u32(2) + u32(0) + u32(1)))
        self.assertEqual(
            units["data"],
            {"weight_unit_code": 2, "temperature_unit_code": 0, "water_source_code": 1},
        )
        grinder = decode_event(packet(9003, u32(1000) + u32(90)))
        self.assertEqual(grinder["name"], "grinder_started")
        self.assertEqual(grinder["data"]["grinder_rpm_setting"], 90)
        brewer = decode_event(packet(9001, u32(1500) + u32(930) + u32(2)))
        self.assertEqual(brewer["name"], "brewer_entered")
        self.assertNotIn("brewer_temperature_f_setting_raw", brewer["data"])
        extended = decode_event(packet(9005, u32(1) + u32(2) + u32(0) + u32(2000)))
        self.assertEqual(extended["data"]["brewer_temperature_f_setting_raw"], 2000)
        self.assertEqual(
            decode_event(packet(40501, b"ABC123extra"))["data"], {"pod_id": "ABC123"}
        )
        self.assertEqual(
            decode_event(packet(11518, b"\x01\x02\x03\x04\x05"))["data"],
            {"preset_state_hex": "01020304"},
        )

    def test_malformed_lengths_and_encoding(self):
        for code, payload in (
            (8105, b"\x01"),
            (8111, b""),
            (8015, u32(1)),
            (9000, u32(1)),
            (9001, u32(1)),
            (40501, b"ABC"),
            (11518, b"\x01"),
        ):
            with self.subTest(code=code), self.assertRaises(ValueError):
                decode_event(packet(code, payload))
        with self.assertRaisesRegex(ValueError, "ASCII"):
            decode_event(packet(40501, b"\xff" * 6))

    def test_unmodeled_or_outgoing_frames(self):
        self.assertIsNone(decode_event(packet(9999)))
        self.assertIsNone(decode_event(packet(40511, marker=0xC2)))
        self.assertIsNone(decode_event(packet(40511, device=1, function=1, marker=1)))

    def test_event_fields_are_unique(self):
        self.assertEqual(len(EVENT_FIELDS), len(set(EVENT_FIELDS)))
        self.assertIn("pour_radius_baseline", EVENT_FIELDS)


class TelemetryTests(unittest.TestCase):
    def test_signed_weight_and_alternate_weight(self):
        state = DeviceState()
        apply(state, 20501, struct.pack("<f", -0.25))
        self.assertEqual(state.observations["scale_weight"].value, -0.25)
        self.assertEqual(state.observations["scale_weight"].unit, "g")
        apply(state, 10507, struct.pack("<f", 3.5))
        self.assertEqual(state.observations["scale_weight"].value, 3.5)

    def test_water_counter_is_raw_and_estimate_uncalibrated(self):
        state = DeviceState()
        apply(state, 40523, struct.pack("<f", 14000))
        self.assertEqual(state.observations["water_counter"].value, 14000)
        self.assertIsNone(state.observations["water_counter"].unit)
        estimate = state.observations["dispensed_water_estimate"]
        self.assertEqual((estimate.value, estimate.unit), (14, "ml"))
        self.assertEqual(estimate.evidence, "uncalibrated_conversion")

    def test_nonfinite_and_short_measurements_rejected(self):
        for code, payload in (
            (20501, struct.pack("<f", math.nan)),
            (40523, struct.pack("<f", math.inf)),
            (20501, b"\x00"),
            (40522, b"\x00"),
            (8023, b"\x01"),
        ):
            with self.subTest(code=code), self.assertRaises(ValueError):
                DeviceState().apply(decode(reply(code, payload)))

    def test_state_and_water_fault(self):
        state = DeviceState()
        apply(state, 8023, u32(0xDEAD))
        self.assertEqual(state.observations["machine_state_code"].value, 0xDEAD)
        self.assertEqual(
            state.observations["machine_state_code"].evidence, "raw_code_only"
        )
        apply(state, 40522, u32(3))
        self.assertNotIn("water_sufficient", state.observations)
        apply(state, 40522, u32(0))
        self.assertIs(state.observations["water_sufficient"].value, False)

    def test_water_ok_code_reports_sufficient_water(self):
        state = DeviceState()
        apply(state, 40522, u32(1))
        self.assertIs(state.observations["water_sufficient"].value, True)
        self.assertEqual(state.observations["water_fault_code"].value, 1)

    def test_settings_events_become_fields(self):
        state = DeviceState()
        apply(state, 8015, u32(2) + u32(1) + u32(1))
        values = {k: v.value for k, v in state.observations.items()}
        self.assertEqual(values["weight_unit"], "oz")
        self.assertEqual(values["temperature_unit"], "C")
        self.assertEqual(values["water_source"], "tap")
        apply(state, 8015, u32(9) + u32(9) + u32(9))
        self.assertEqual(state.observations["weight_unit_code"].value, 9)
        self.assertEqual(state.observations["weight_unit"].value, "oz")  # unchanged
        apply(state, 9003, u32(1000) + u32(90))
        self.assertEqual(state.observations["grinder_rpm_setting"].unit, "rpm")
        apply(state, 8105, u32(200))  # out-of-range setting is ignored
        self.assertNotIn("grind_setting", state.observations)

    def test_sleep_hides_weight_until_new_awake_sample(self):
        state = DeviceState(connected=True, session_ready=True)
        apply(state, 20501, struct.pack("<f", 5))
        self.assertTrue(state.snapshot()["fields"]["scale_weight"]["available"])
        apply(state, 8009)
        weight = state.snapshot()["fields"]["scale_weight"]
        self.assertEqual(
            (weight["available"], weight["unavailable_reason"]), (False, "asleep")
        )
        apply(state, 8011)
        weight = state.snapshot()["fields"]["scale_weight"]
        self.assertEqual(weight["unavailable_reason"], "awaiting_awake_measurement")
        time.sleep(0.001)
        apply(state, 20501, struct.pack("<f", 6))
        self.assertTrue(state.snapshot()["fields"]["scale_weight"]["available"])
        self.assertFalse(state.snapshot(stale_after=0)["fields"]["asleep"]["stale"])

    def test_identity_outlives_stale_after_but_identity_settings_expire(self):
        identity = {"serial_number", "firmware", "model", "model_raw_hex"}
        state = DeviceState(connected=True, session_ready=True)
        apply(state, 40521, identity_block())
        time.sleep(0.002)
        fields = state.snapshot(stale_after=0)["fields"]
        self.assertLessEqual(
            {
                "configured_mode",
                "water_sufficient",
                "grind_setting",
                "water_source",
                "pour_radius_baseline",
                "vibration_baseline",
                "device_info_raw",
            },
            set(fields) - identity,
        )
        self.assertEqual({k for k, v in fields.items() if not v["stale"]}, identity)
        self.assertEqual({k for k, v in fields.items() if v["available"]}, identity)
        self.assertGreaterEqual(fields["serial_number"]["age_seconds"], 0.002)
        state.connected = False
        fields = state.snapshot(stale_after=0)["fields"]
        self.assertFalse(any(item["available"] for item in fields.values()))

    def test_events_are_bounded_and_detached(self):
        state = DeviceState()
        for index in range(200):
            apply(state, 40510, u32(index))
        snapshot = state.snapshot()
        self.assertEqual(len(snapshot["events"]), 128)
        self.assertEqual(snapshot["events"][-1]["data"], {"pour_index": 199})
        snapshot["events"][-1]["data"]["pour_index"] = -1
        self.assertEqual(state.snapshot()["events"][-1]["data"]["pour_index"], 199)

    def test_ignored_frames(self):
        self.assertEqual(decode_values(packet(20501, b"", marker=0xC2)), [])
        self.assertEqual(decode_values(packet(9999)), [])


class IdentityTests(unittest.TestCase):
    def values(self, payload):
        return {
            name: value for name, value, _, _ in decode_values(packet(40521, payload))
        }

    def test_complete_layout(self):
        info = bytearray(identity_block(source=1, grind_raw=52))
        info[29:33] = struct.pack("<f", 1.5)
        info[34], info[35], info[39] = 7, 2, 200
        info[51:55] = bytes.fromhex("91327856")
        info[55:59] = u32(700)
        info[59:63] = u32(1300)
        values = self.values(bytes(info))
        self.assertEqual(values["serial_number"], "J15TESTUNIT00")
        self.assertEqual(values["firmware"], "V9.9Z.99")
        self.assertEqual(values["model"], "TEST")
        self.assertEqual(values["model_raw_hex"], b"TEST\0\0".hex())
        self.assertEqual(values["water_source"], "tap")
        self.assertIs(values["water_sufficient"], True)
        self.assertEqual(values["grind_setting"], 22)
        self.assertEqual(values["led_brightness"], 15)
        self.assertEqual(
            (values["temperature_unit"], values["weight_unit"]), ("C", "g")
        )
        self.assertEqual(values["configured_mode"], "recipes")
        self.assertEqual(
            (values["pour_radius_baseline"], values["vibration_baseline"]), (700, 1300)
        )
        self.assertEqual(values["area_ap_raw"], 1.5)
        self.assertEqual(
            (
                values["system_status_code"],
                values["user_count_raw"],
                values["voltage_raw"],
            ),
            (7, 2, 200),
        )

    def test_unknown_values_are_omitted_not_defaulted(self):
        info = bytearray(identity_block(size=41))
        info[13:19] = b"\x01\x02\x03\x04\x05\x06"
        info[29:33] = struct.pack("<f", math.nan)
        info[33], info[36], info[37], info[38], info[40] = 9, 9, 0, 0, 9
        values = self.values(bytes(info))
        for name in (
            "model",
            "area_ap_raw",
            "water_sufficient",
            "water_source",
            "grind_setting",
            "led_brightness",
            "temperature_unit",
            "weight_unit",
            "configured_mode",
            "pour_radius_baseline",
        ):
            self.assertNotIn(name, values)
        self.assertEqual(values["water_source_code"], 9)

    def test_short_or_unknown_layout_stays_raw(self):
        short = identity_block(size=40)
        values = self.values(short)
        self.assertEqual(values["firmware"], "V9.9Z.99")
        self.assertNotIn("water_sufficient", values)
        unknown = b"no serial V1.2.3 tail"
        values = self.values(unknown)
        self.assertEqual(
            values, {"device_info_raw": unknown.hex(), "firmware": "V1.2.3"}
        )
        self.assertEqual(self.values(b"plain"), {"device_info_raw": b"plain".hex()})

    def test_version_shaped_model_does_not_shadow_firmware_slot(self):
        values = self.values(identity_block(model=b"V1.1.1"))
        self.assertEqual(values["firmware"], "V9.9Z.99")


if __name__ == "__main__":
    unittest.main()
