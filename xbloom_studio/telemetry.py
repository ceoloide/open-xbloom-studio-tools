"""Conservative decoding: retain unknowns, signed scale values and raw payloads."""

from __future__ import annotations

import math
import re
import struct
import time
from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .events import DECODED, decode_event, protocol_error
from .protocol import Packet

Value = tuple[str, Any, "str | None", str]

RAW_COUNTER = "raw_counter"
RAW_CODE = "raw_code_only"
RAW_PAYLOAD = "raw_payload"
UNCALIBRATED = "uncalibrated_conversion"
WEIGHT_UNITS = {0: "ml", 1: "g", 2: "oz"}
TEMPERATURE_UNITS = {0: "F", 1: "C"}
WATER_SOURCES = {0: "tank", 1: "tap"}
RECIPES_MODE = bytes.fromhex("91327856")
CONFIGURED_MODES = {b"\0" * 4: "pro", RECIPES_MODE: "recipes"}
# Identity block layout: serial at 0, model at 13..19, firmware at 19..29.
SERIAL = re.compile(rb"J15[A-Z0-9]{10}")
FIRMWARE = re.compile(rb"V\d+\.\d+[A-Z]?\.\d+")
INFO_BASE_SIZE = 41


@dataclass(frozen=True)
class Observation:
    value: Any
    observed_at: str
    monotonic_at: float
    command: int
    unit: str | None = None
    evidence: str = DECODED

    def to_dict(self, now: float, stale_after: float) -> dict[str, Any]:
        result = asdict(self)
        result.pop("monotonic_at")
        result["age_seconds"] = max(0, now - self.monotonic_at)
        result["stale"] = result["age_seconds"] > stale_after
        return result


def _events() -> deque[dict[str, Any]]:
    return deque(maxlen=128)


@dataclass
class DeviceState:
    connected: bool = False
    session_ready: bool = False
    revision: int = 0
    observations: dict[str, Observation] = field(default_factory=dict)
    notifications: int = 0
    decode_errors: int = 0
    last_error: str | None = None
    events: deque[dict[str, Any]] = field(default_factory=_events)

    def snapshot(self, stale_after: float = 5.0) -> dict[str, Any]:
        now = time.monotonic()
        fields = {
            key: value.to_dict(now, float("inf") if key == "asleep" else stale_after)
            for key, value in self.observations.items()
        }
        # Transport availability is independent of whether a measurement arrived.
        for item in fields.values():
            item["available"] = (
                self.connected and self.session_ready and not item["stale"]
            )
        sleep = self.observations.get("asleep")
        weight = self.observations.get("scale_weight")
        if (
            sleep
            and weight
            and (sleep.value or weight.monotonic_at <= sleep.monotonic_at)
        ):
            fields["scale_weight"]["available"] = False
            fields["scale_weight"]["unavailable_reason"] = (
                "asleep" if sleep.value else "awaiting_awake_measurement"
            )
        return {
            "connected": self.connected,
            "session_ready": self.session_ready,
            "revision": self.revision,
            "fields": fields,
            "notifications": self.notifications,
            "decode_errors": self.decode_errors,
            "last_error": self.last_error,
            "events": deepcopy(list(self.events)),
        }

    def apply(self, packet: Packet) -> None:
        self.notifications += 1
        self.revision += 1
        values = decode_values(packet)
        event = decode_event(packet)
        wall = datetime.now(UTC).isoformat()
        mono = time.monotonic()
        if event is not None:
            self.events.append(
                {**event, "revision": self.revision, "observed_at": wall}
            )
        for name, value, unit, evidence in values:
            self.observations[name] = Observation(
                value, wall, mono, packet.command, unit, evidence
            )


def _float32(payload: bytes, what: str) -> float:
    if len(payload) < 4:
        raise ValueError(f"{what} payload must contain at least one float32")
    value: float = struct.unpack("<f", payload[:4])[0]
    if not math.isfinite(value):
        raise ValueError("Non-finite measurement")
    return value


def _uint32(payload: bytes, what: str) -> int:
    if len(payload) < 4:
        raise ValueError(f"{what} payload must contain at least one uint32")
    return int.from_bytes(payload[:4], "little")


def _event_values(packet: Packet, data: dict[str, Any]) -> list[Value]:
    values: list[Value] = [
        (key, value, "rpm" if key == "grinder_rpm_setting" else None, DECODED)
        for key, value in data.items()
    ]
    if packet.command == 8105 and not 1 <= data["grind_setting"] <= 80:
        return []
    if packet.command == 8015:
        for key, mapping in (
            ("weight_unit", WEIGHT_UNITS),
            ("temperature_unit", TEMPERATURE_UNITS),
            ("water_source", WATER_SOURCES),
        ):
            label = mapping.get(data[key + "_code"])
            if label is not None:
                values.append((key, label, None, DECODED))
    return values


def _info_values(payload: bytes) -> list[Value]:
    values: list[Value] = [("device_info_raw", payload.hex(), None, RAW_PAYLOAD)]
    serial = SERIAL.match(payload)
    fixed_firmware = payload[19:29].rstrip(b"\0")
    fixed_version = FIRMWARE.fullmatch(fixed_firmware) if len(payload) >= 29 else None
    # A version-shaped model or extension must not shadow the actual slot.
    firmware = fixed_version if serial and fixed_version else FIRMWARE.search(payload)
    if serial:
        values.append(("serial_number", serial.group().decode("ascii"), None, DECODED))
    if firmware:
        values.append(("firmware", firmware.group().decode("ascii"), None, DECODED))
    # Offsets apply only to the recognized identity layout with a fixed firmware
    # slot; other layouts stay raw and absent fields are never defaulted.
    if not (serial and fixed_version and len(payload) >= INFO_BASE_SIZE):
        return values
    for key, value in (
        ("model_raw_hex", payload[13:19].hex()),
        ("system_status_code", payload[34]),
        ("user_count_raw", payload[35]),
        ("water_source_code", payload[36]),
        ("voltage_raw", payload[39]),
    ):
        values.append((key, value, None, DECODED))
    model = payload[13:19].rstrip(b"\0 ")
    if model and all(32 <= byte <= 126 for byte in model):
        values.append(("model", model.decode("ascii"), None, DECODED))
    for key, start, end in (
        ("pour_radius_baseline", 55, 59),
        ("vibration_baseline", 59, 63),
    ):
        if len(payload) >= end:
            values.append(
                (key, int.from_bytes(payload[start:end], "little"), None, DECODED)
            )
    area = struct.unpack("<f", payload[29:33])[0]
    if math.isfinite(area):
        values.append(("area_ap_raw", area, None, DECODED))
    if payload[36] in WATER_SOURCES:
        values.append(("water_source", WATER_SOURCES[payload[36]], None, DECODED))
    if payload[33] in (0, 1):
        values.append(("water_sufficient", bool(payload[33]), None, DECODED))
    if 1 <= payload[38] <= 15:
        values.append(("led_brightness", payload[38], None, DECODED))
    if payload[40] in TEMPERATURE_UNITS:
        values.append(
            ("temperature_unit", TEMPERATURE_UNITS[payload[40]], None, DECODED)
        )
    if len(payload) >= 42 and payload[41] in WEIGHT_UNITS:
        values.append(("weight_unit", WEIGHT_UNITS[payload[41]], None, DECODED))
    mode = CONFIGURED_MODES.get(payload[51:55]) if len(payload) >= 55 else None
    if mode is not None:
        values.append(("configured_mode", mode, None, DECODED))
    if 31 <= payload[37] <= 110:
        values.append(
            ("grind_setting", payload[37] - 30, None, DECODED)  # same offset as 8105
        )
    return values


def decode_values(packet: Packet) -> list[Value]:
    """Return (name, value, unit, evidence); no speculative state/mode inference."""
    payload = packet.payload
    error = protocol_error(packet)
    if error is not None:
        return [("last_protocol_error", error, None, DECODED)]
    if (packet.device, packet.function, packet.marker) != (2, 7, 0xC1):
        return []
    event = decode_event(packet)
    if event is not None:
        return _event_values(packet, event["data"])
    if packet.command in (10507, 20501):
        return [("scale_weight", _float32(payload, "Weight"), "g", DECODED)]
    if packet.command == 40523:
        value = _float32(payload, "Weight")
        return [
            ("water_counter", value, None, RAW_COUNTER),
            ("dispensed_water_estimate", value / 1000.0, "ml", UNCALIBRATED),
        ]
    if packet.command in (8009, 8011):
        return [("asleep", packet.command == 8009, None, DECODED)]
    if packet.command == 40522:
        code = _uint32(payload, "Water fault")
        values: list[Value] = [("water_fault_code", code, None, DECODED)]
        if code in (0, 1):
            values.append(("water_sufficient", code == 1, None, DECODED))
        return values
    if packet.command == 8023:
        return [("machine_state_code", _uint32(payload, "State"), None, RAW_CODE)]
    if packet.command == 40521:
        return _info_values(payload)
    return []
