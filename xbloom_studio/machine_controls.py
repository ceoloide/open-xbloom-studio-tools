"""Named machine commands; firmware update is deliberately absent.

Mixed into Interaction so commands share its session, locks and stop path.
Untested commands require allow_untested=True, not a new connection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import struct
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .client import SessionError
from .command_schema import recipe_configuration_untested
from .protocol import Packet, decode

if TYPE_CHECKING:
    from collections import deque

    from .control import Operation
    from .telemetry import DeviceState

PRESET_SAVED = b"\x25\0\0\0"
# Observed instead of a save when the machine refused a single-slot write.
PRESET_REJECTED = b"\x44\0\0\0"
RECIPES_MODE = bytes.fromhex("91327856")
PRO_MODE = b"\0" * 4
PATTERNS = {"center": 0, "ring": 1, "spiral": 2}
SLOTS = ("A", "B", "C")


class ControlPermissionError(ValueError):
    pass


class ExperimentalOptInError(ValueError):
    pass


class UntestedOperationError(SessionError):
    pass


class PresetWriteError(SessionError):
    """A persistent write did not finish; result identifies its saved backup."""

    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


def preset_outcome(result: dict[str, Any], exc: BaseException | None) -> str:
    """Classify one write attempt from its evidence; never a content readback."""
    if result["saved_observed"]:
        return "saved" if exc is None else "saved_cleanup_failed"
    # A cancellation ends automatic repetition even before the first frame.
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    # Preparation frames may have been sent, but provably no preset frame.
    if not result["transmitted_write_count"] and not result["partial_frame"]:
        return "no_preset_frame"
    return "rejected" if result["rejection_evidence"] else "unconfirmed"


def _number(name: str, value: object, low: float, high: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be finite and between {low} and {high}")
    return value


def _uint(name: str, value: object, maximum: int = 0xFFFFFFFF) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        raise ValueError(f"{name} must be an integer from 0 to {maximum}")
    return value


def validate_cup_limits(limits: object) -> None:
    if limits is not None:
        if not isinstance(limits, tuple) or len(limits) != 2:
            raise ValueError("cup_limits must be (minimum, maximum)")
        low, high = limits
        _number("cup minimum", low, 0, 5000)
        _number("cup maximum", high, low, 5000)


def cup_limits_payload(limits: tuple[float, float]) -> bytes:
    """Cup limits travel as float32 maximum, then minimum."""
    low, high = limits
    return struct.pack("<ff", high, low)


@dataclass(frozen=True)
class SavedPreset:
    """Encoded contents and flags for a Studio slot, not an entire frame."""

    slot: str
    payload: bytes
    flags: int

    def __post_init__(self) -> None:
        if self.slot not in SLOTS:
            raise ValueError("slot must be A, B or C")
        if not isinstance(self.payload, bytes) or not 1 <= len(self.payload) <= 512:
            raise ValueError("Preset recipe payload must be 1–512 bytes")
        _uint("preset flags", self.flags, 255)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "payload_hex": self.payload.hex(),
            "flags": self.flags,
        }

    def body(self) -> bytes:
        """Write payload: slot index, flags, then recipe content."""
        return bytes([SLOTS.index(self.slot), self.flags]) + self.payload

    @classmethod
    def from_frame(cls, frame: bytes) -> SavedPreset:
        packet = decode(frame)
        if (
            (packet.device, packet.function, packet.command, packet.marker)
            != (1, 2, 11510, 1)
            or len(packet.payload) < 3
            or packet.payload[0] > 2
        ):
            raise ValueError(
                "Expected an outgoing Studio preset frame (11510, slot 0–2)"
            )
        return cls(SLOTS[packet.payload[0]], packet.payload[2:], packet.payload[1])


@dataclass(frozen=True)
class EncodedTeaRecipe:
    """Dedicated tea content, including encoded steep and soak data."""

    payload: bytes
    cup_limits: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.payload, bytes) or not 1 <= len(self.payload) <= 512:
            raise ValueError("Tea payload must be 1–512 bytes, without a BLE frame")
        validate_cup_limits(self.cup_limits)


def _batch(presets: object) -> tuple[SavedPreset, ...]:
    if not isinstance(presets, (list, tuple)) or len(presets) != 3:
        raise ValueError("Provide all three SavedPreset objects, A/B/C")
    if not all(isinstance(preset, SavedPreset) for preset in presets) or {
        preset.slot for preset in presets
    } != set(SLOTS):
        raise ValueError("Provide exactly one SavedPreset for each of A/B/C")
    return tuple(sorted(presets, key=lambda preset: preset.slot))


def _save_backup(path: str | os.PathLike[str], contents: Any) -> dict[str, str]:
    """Persist before writes, private permissions; never overwrite a backup."""
    target = Path(path).expanduser().absolute()
    body = json.dumps(contents, indent=2) + "\n"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    return {"path": str(target), "sha256": hashlib.sha256(body.encode()).hexdigest()}


def _order(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, (tuple, list, str))
        or len(value) != 3
        or set(value) != set(SLOTS)
    ):
        raise ValueError("order must be a permutation of A/B/C")
    return tuple(value)


class MachineControls:
    METHODS = (
        "set_brightness",
        "set_weight_unit",
        "set_temperature_unit",
        "set_pour_pattern",
        "back_home",
        "wake",
        "set_mode",
        "write_preset",
        "write_presets",
        "start_grinder_calibration",
        "calibrate_grinder",
        "set_water_source",
        "read_pour_radius",
        "read_vibration",
        "initialize_pour_radius",
        "set_pour_radius",
        "set_pour_radius_level",
        "initialize_vibration",
        "set_vibration_level",
        "set_cup_limits",
        "configure_recipe",
        "start_tea",
        "set_preset_count",
        "set_preset_order",
        "quit_recipe_preparation",
        "back_to_normal",
        "configure_grinder",
        "configure_brewer",
    )
    # Awaited with each slot before a batch's first frame for that slot, so a
    # caller can commit that the slot may change; never set by a caller's
    # parameters.
    _before_slot_write: Callable[[str], Awaitable[None]] | None = None
    UNTESTED_METHODS = (
        "write_preset",
        "write_presets",
        "set_water_source",
        "initialize_pour_radius",
        "set_pour_radius",
        "set_pour_radius_level",
        "initialize_vibration",
        "set_cup_limits",
        "start_tea",
        "set_preset_count",
        "set_preset_order",
        "quit_recipe_preparation",
        "back_to_normal",
    )

    if TYPE_CHECKING:
        # Provided by Interaction and ReadOnlyClient.
        allow_untested: bool
        transition_delay: float
        state: DeviceState
        _lifecycle: asyncio.Lock
        _scale_mode: bool
        _uncertain: bool
        _partial_frame: bool
        _keep_cancellation: bool
        _event_latch: Callable[[int, Packet], None] | None
        _event_revision: int
        _preset_write_used: bool
        _last_preset_write: dict[str, Any] | None
        _selected_water_source: int | None
        _received_events: deque[tuple[int, Packet]]

        async def _command(
            self,
            code: int,
            payload: bytes = b"",
            function: int = 1,
            *,
            active_operation: Operation | None = None,
            ack_payload: bytes | None = None,
            before_write: Callable[[], None] | None = None,
            on_transmit: Callable[[], None] | None = None,
            ack_budget: float | None = None,
        ) -> Packet: ...

        def _require_idle(self, *, allow_no_water: bool = False) -> None: ...

        def _require_quiescent(self, *, allow_prepared: bool = False) -> None: ...

        async def _prepare(self) -> None: ...

        def _water_source(self) -> int: ...

        def snapshot(self) -> dict[str, Any]: ...

        async def stop(self) -> dict[str, Any] | None: ...

        async def _wait_event(
            self,
            after_revision: int,
            predicate: Callable[[Packet], bool],
            timeout: float,
            events: Iterable[tuple[int, Packet]] | None = None,
        ) -> Packet: ...

        def _launch(
            self,
            kind: str,
            configure: Callable[[], Awaitable[None]],
            start_code: int,
            payload: bytes,
            timeout: float,
            *,
            prepare: bool = True,
            before_start: Callable[[], None] | None = None,
        ) -> Operation: ...

        async def _complete(self, starting: Awaitable[Operation]) -> dict[str, Any]: ...

    def _require_untested(self, feature: str) -> None:
        if not self.allow_untested:
            raise UntestedOperationError(
                f"{feature} is not hardware-tested; set allow_untested=True to use it"
            )

    async def _setting(
        self,
        code: int,
        payload: bytes = b"",
        *,
        function: int = 1,
        prepare: bool = True,
        finish: bool = True,
        before: Sequence[tuple[int, bytes]] = (),
        after: Sequence[tuple[int, bytes]] = (),
        untested: bool = False,
        idle: bool = True,
        ack_payload: bytes | None = None,
    ) -> dict[str, Any]:
        if untested:
            self._require_untested(f"Command {code}")
        async with self._lifecycle:
            if idle:
                self._require_idle()
            else:
                self._require_quiescent()
            try:
                if prepare:
                    await self._prepare()
                elif self._scale_mode:
                    await self._command(8014)
                    self._scale_mode = False
                    await asyncio.sleep(self.transition_delay)
                for command, body in before:
                    await self._command(command, body)
                    await asyncio.sleep(self.transition_delay)
                packet = await self._command(
                    code, payload, function, ack_payload=ack_payload
                )
                if not finish:
                    await asyncio.sleep(self.transition_delay)
                return {
                    "command": code,
                    "acknowledged": True,
                    "protocol_evidence": "untested" if untested else "tested",
                    "readback_verified": False,
                    "response_payload_hex": packet.payload.hex(),
                }
            except asyncio.CancelledError:
                self._uncertain = True
                raise
            finally:
                if finish and self.state.session_ready and not self._uncertain:
                    for command, body in after:
                        await self._command(command, body)
                        await asyncio.sleep(self.transition_delay)
                    await self._command(8022)
                    await asyncio.sleep(self.transition_delay)

    async def set_brightness(self, level: int) -> dict[str, Any]:
        _uint("brightness", level, 15)
        if level < 1:
            raise ValueError("brightness must be 1–15")
        return await self._setting(8103, struct.pack("<I", level))

    async def set_weight_unit(self, unit: str) -> dict[str, Any]:
        units = {"ml": 0, "g": 1, "oz": 2}
        if unit not in units:
            raise ValueError("weight unit must be ml, g or oz")
        return await self._setting(8005, struct.pack("<I", units[unit]))

    async def set_temperature_unit(self, unit: str) -> dict[str, Any]:
        units = {"F": 0, "C": 1}
        if unit not in units:
            raise ValueError("temperature unit must be C or F")
        return await self._setting(8010, struct.pack("<I", units[unit]))

    async def set_pour_pattern(
        self, pattern: str, *, temperature_c: float = 20
    ) -> dict[str, Any]:
        if pattern not in PATTERNS:
            raise ValueError("pattern must be center, ring or spiral")
        _number("temperature_c", temperature_c, 20, 99)
        return await self._setting(
            8016,
            struct.pack("<I", PATTERNS[pattern]),
            before=((8007, struct.pack("<If", 0, temperature_c * 10)),),
            after=((8013, b""),),
        )

    async def configure_grinder(self, *, size: int, rpm: int) -> dict[str, Any]:
        """Set the manual grinder controls and leave its page, without rotation."""
        _uint("size", size, 80)
        _uint("rpm", rpm, 120)
        if size < 1 or rpm < 60:
            raise ValueError("size must be 1–80 and rpm must be 60–120")
        return await self._setting(
            8006, struct.pack("<II", size, rpm), after=((8012, b""),)
        )

    async def configure_brewer(
        self, *, temperature_c: float = 20, pattern: str = "center"
    ) -> dict[str, Any]:
        """Set manual water target/pattern and leave its page, without dispensing."""
        _number("temperature_c", temperature_c, 20, 99)
        if pattern not in PATTERNS:
            raise ValueError("pattern must be center, ring or spiral")
        return await self._setting(
            4510,
            struct.pack("<I", round(temperature_c * 10)),
            before=((8007, struct.pack("<If", PATTERNS[pattern], temperature_c * 10)),),
            after=((8013, b""),),
        )

    async def back_home(self) -> dict[str, Any]:
        return await self._setting(8022, prepare=False, finish=False)

    async def wake(self) -> dict[str, Any]:
        result = await self.back_home()
        await asyncio.sleep(self.transition_delay)
        asleep = self.snapshot()["fields"].get("asleep")
        return {
            **result,
            "awake_observed": asleep is not None
            and asleep["available"]
            and asleep["value"] is False,
        }

    async def set_mode(self, mode: str) -> dict[str, Any]:
        if mode not in ("pro", "recipes"):
            raise ValueError("mode must be pro or recipes")
        return await self._setting(
            11511,
            PRO_MODE if mode == "pro" else RECIPES_MODE,
            function=2,
            prepare=False,
            finish=False,
            before=((8022, b""),),
        )

    async def write_preset(
        self,
        preset: SavedPreset,
        *,
        backup: SavedPreset | None = None,
        backup_path: str | os.PathLike[str] | None = None,
        save_timeout: float = 10,
    ) -> dict[str, Any]:
        """Send one slot's replacement as one attempt of three planned messages.

        The messages are planned writes, never retries after a failure: an
        early save, a non-empty response or a rejection state stops the rest.
        An optional backup holds the same slot's previous contents as the
        caller knows them, not a readback. Success means ACK plus a fresh,
        temporally correlated save notification; it does not establish stored
        contents or persistence across restart.
        """
        self._require_untested("Single-slot preset writes")
        if not isinstance(preset, SavedPreset) or not isinstance(
            backup, (SavedPreset, type(None))
        ):
            raise ValueError("Provide SavedPreset objects for preset and backup")
        if backup is not None and preset.slot != backup.slot:
            raise ValueError("Preset and backup must address the same slot")
        if (backup is None) != (backup_path is None):
            raise ValueError("A backup and its backup_path go together")
        return await self._write_preset_selection(
            (preset,) * 3,
            None if backup is None else (backup,),
            backup_path,
            save_timeout,
        )

    async def write_presets(
        self,
        presets: Sequence[SavedPreset],
        *,
        backup: Sequence[SavedPreset],
        backup_path: str | os.PathLike[str],
        save_timeout: float = 10,
    ) -> dict[str, Any]:
        """One complete A/B/C batch per connection; supplied backup is not readback.

        The caller-supplied previous presets persist before the first write.
        On failure partial progress and the file remain; no same-session rollback.
        """
        self._require_untested("Batch preset writes")
        return await self._write_preset_selection(
            _batch(presets), _batch(backup), backup_path, save_timeout
        )

    async def _write_preset_selection(
        self,
        presets: tuple[SavedPreset, ...],
        backup: tuple[SavedPreset, ...] | None,
        backup_path: str | os.PathLike[str] | None,
        save_timeout: float,
    ) -> dict[str, Any]:
        _number("save_timeout", save_timeout, 0.001, 60)
        async with self._lifecycle:
            self._require_idle(allow_no_water=True)
            if self._preset_write_used:
                raise SessionError(
                    "Each preset write requires a fresh interaction; "
                    "do not restore on this session"
                )
            saved = None
            if backup is not None and backup_path is not None:
                saved = _save_backup(
                    backup_path,
                    {
                        "kind": "caller_supplied_preset_backup",
                        "machine_readback_verified": False,
                        "presets": [preset.to_dict() for preset in backup],
                    },
                )
            slots = list(dict.fromkeys(preset.slot for preset in presets))
            # Only a single-slot attempt stops early on evidence; batches keep
            # their planned sequence.
            single = len(slots) == 1
            acknowledged: list[str] = []
            evidence: list[dict[str, Any]] = []
            result: dict[str, Any] = {
                "backup": saved,
                "requested_slots": slots,
                "planned_slot_writes": [preset.slot for preset in presets],
                "transmitted_write_count": 0,
                # A begun frame without its final chunk counts as an attempt.
                "partial_frame": False,
                "acknowledged_write_count": 0,
                "acknowledged_slots": acknowledged,
                "saved_observed": False,
                "save_event": None,
                "rejection_evidence": evidence,
                "outcome": None,
                "readback_verified": False,
                "restoration_attempted": False,
            }
            self._last_preset_write = result
            self._preset_write_used = True
            start: int | None = None
            cursor: int | None = None
            hook = self._before_slot_write
            announced: set[str] = set()

            def is_save(packet: Packet) -> bool:
                return packet.command == 8023 and packet.payload == PRESET_SAVED

            def is_rejection(packet: Packet) -> bool:
                return packet.command == 8023 and packet.payload == PRESET_REJECTED

            # Terminal evidence is latched for the attempt's lifetime, from
            # before its first frame: no burst of other reports evicts it.
            latched: list[tuple[int, Packet]] = []

            def latch(revision: int, packet: Packet) -> None:
                if is_save(packet) or is_rejection(packet):
                    latched.append((revision, packet))

            def since(after: int, predicate: Callable[[Packet], bool]) -> Packet | None:
                return next(
                    (
                        packet
                        for revision, packet in latched
                        if revision > after and predicate(packet)
                    ),
                    None,
                )

            def record_save(packet: Packet, after: int) -> None:
                result["saved_observed"] = True
                result["save_event"] = {
                    "command": packet.command,
                    "payload_hex": packet.payload.hex(),
                    "after_event_revision": after,
                    "correlation": "temporal_only",
                }

            def transmitted() -> None:
                # Correlation starts when a frame can reach the machine, not
                # when its journal hook began waiting.
                nonlocal start, cursor
                cursor = self._event_revision
                if start is None:
                    start = cursor
                result["transmitted_write_count"] += 1

            def terminal() -> None:
                """Stop before any byte of the next frame on terminal evidence.

                Also the frame's command guard, so it is rechecked after the
                frame's journal hook and immediately before its first byte:
                the machine must still be quiescent. Other state reports,
                such as the attempt's own 0x43, are not refused; motion is.
                """
                self._require_quiescent()
                if not single or start is None:
                    return
                if since(start, is_save) is not None:
                    raise SessionError(
                        "Preset completion arrived before the final planned "
                        "write; no further writes sent"
                    )
                if since(start, is_rejection) is not None:
                    raise SessionError(
                        "Machine reported a rejection state; no further writes sent"
                    )

            self._event_latch = latch
            # A cancellation stays a cancellation even if the frame's write
            # then fails; it ends automatic repetition.
            self._keep_cancellation = True
            try:
                await self._prepare()
                for index, preset in enumerate(presets):
                    terminal()
                    if hook is not None and preset.slot not in announced:
                        announced.add(preset.slot)
                        await hook(preset.slot)
                    packet = await self._command(
                        11510,
                        preset.body(),
                        2,
                        before_write=terminal,
                        on_transmit=transmitted,
                    )
                    if packet.payload:
                        evidence.append(
                            {
                                "command": 11510,
                                "payload_hex": packet.payload.hex(),
                                "write_index": index,
                                "correlation": "response_to_this_write",
                            }
                        )
                        if single:
                            raise SessionError(
                                "Preset write response was not an empty "
                                "acknowledgment; no further writes sent"
                            )
                    if preset.slot not in acknowledged:
                        acknowledged.append(preset.slot)
                    result["acknowledged_write_count"] += 1
                    await asyncio.sleep(0.1 if self.transition_delay else 0)
                assert cursor is not None  # Every planned frame was transmitted.
                packet = await self._wait_event(
                    cursor,
                    lambda packet: is_save(packet) or (single and is_rejection(packet)),
                    save_timeout,
                    latched,
                )
                if not is_save(packet):
                    raise SessionError("Machine reported a rejection state, not a save")
                record_save(packet, cursor)
                await self._command(8022)
                await asyncio.sleep(self.transition_delay)
                result["outcome"] = preset_outcome(result, None)
                return {**result, "acknowledged_slots": acknowledged.copy()}
            except BaseException as exc:
                # Any save after a single-slot attempt's first transmission
                # wins; a batch correlates only with its last frame.
                if start is not None and cursor is not None:
                    after = start if single else cursor
                    late = since(after, is_save)
                    if late is not None:
                        record_save(late, after)
                    state = since(start, is_rejection)
                    if state is not None:
                        evidence.append(
                            {
                                "command": state.command,
                                "payload_hex": state.payload.hex(),
                                "after_event_revision": start,
                                "correlation": "temporal_only",
                            }
                        )
                self._uncertain = True
                result["partial_frame"] = self._partial_frame
                result["error"] = f"{type(exc).__name__}: {exc}"
                result["outcome"] = preset_outcome(result, exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise PresetWriteError(
                    "Preset write incomplete; inspect result and backup before "
                    "recovery in a fresh interaction",
                    result,
                ) from exc
            finally:
                self._event_latch = None
                self._keep_cancellation = False

    async def start_grinder_calibration(self, *, timeout: float = 180) -> Operation:
        async with self._lifecycle:
            self._require_idle()

            async def configure() -> None:
                pass

            return self._launch(
                "calibration", configure, 3502, struct.pack("<I", 1000), timeout
            )

    async def calibrate_grinder(self, **kwargs: Any) -> dict[str, Any]:
        return await self._complete(self.start_grinder_calibration(**kwargs))

    async def set_water_source(self, source: str) -> dict[str, Any]:
        sources = {"tank": 0, "tap": 1}
        if source not in sources:
            raise ValueError("source must be tank or tap")
        result = await self._setting(
            4508, struct.pack("<I", sources[source]), untested=True
        )
        # Keep the acknowledged selection for later pours without fabricating
        # a fresh identity observation from this setting ACK.
        self._selected_water_source = sources[source]
        return {**result, "requested_source": source}

    async def read_pour_radius(self) -> dict[str, Any]:
        return await self._read_parameter(11506)

    async def read_vibration(self) -> dict[str, Any]:
        return await self._read_parameter(11508)

    async def _read_parameter(self, code: int) -> dict[str, Any]:
        result = await self._setting(code, function=2)
        payload = bytes.fromhex(result["response_payload_hex"])
        if len(payload) != 4:
            self._uncertain = True
            raise SessionError(
                f"Command {code} did not return the expected uint32 value"
            )
        return {
            **result,
            "value": struct.unpack("<I", payload)[0],
            "readback_verified": True,
            "units": "raw_units",
        }

    async def initialize_pour_radius(self, value: int) -> dict[str, Any]:
        _uint("initial radius", value, 0x7FFFFFFF)
        return await self._setting(
            11506, struct.pack("<I", value), function=2, untested=True
        )

    async def set_pour_radius(self, value: int) -> dict[str, Any]:
        _uint("radius", value, 0x7FFFFFFF)
        return await self._setting(
            11507, struct.pack("<I", value), function=2, untested=True
        )

    async def set_pour_radius_level(
        self, level: int, *, baseline: int
    ) -> dict[str, Any]:
        _uint("radius level", level, 4)
        _uint("baseline radius", baseline, 0x7FFFFFFF)
        # Clamp the reference to 560..840, then offset 80 per level from 2.
        baseline = max(560, min(840, baseline))
        return await self.set_pour_radius(baseline + (level - 2) * 80)

    async def initialize_vibration(self, value: int) -> dict[str, Any]:
        _uint("initial vibration", value, 0x7FFFFFFF)
        return await self._setting(
            11508, struct.pack("<I", value), function=2, untested=True
        )

    async def set_vibration_level(self, level: int) -> dict[str, Any]:
        _uint("vibration level", level, 5)
        return await self._setting(
            11509, struct.pack("<I", 1000 + level * 100), function=2
        )

    async def set_cup_limits(self, minimum: float, maximum: float) -> dict[str, Any]:
        validate_cup_limits((minimum, maximum))
        return await self._setting(
            8104, cup_limits_payload((minimum, maximum)), untested=True
        )

    async def configure_recipe(
        self,
        *,
        dose_g: float,
        bypass_ml: float = 0,
        bypass_temperature_c: float = 20,
        cup_limits: tuple[float, float] | None = None,
    ) -> dict[str, Any]:
        _number("dose_g", dose_g, 1, 100)
        if dose_g != int(dose_g):
            raise ValueError("dose_g must be whole grams")
        _number("bypass_ml", bypass_ml, 0, 1000)
        _number("bypass_temperature_c", bypass_temperature_c, 20, 99)
        validate_cup_limits(cup_limits)
        untested = recipe_configuration_untested(bypass_ml, cup_limits)
        if untested:
            self._require_untested("Bypass/cup-limit behavior")
        after = () if cup_limits is None else ((8104, cup_limits_payload(cup_limits)),)
        temperature = bypass_temperature_c * 10 if bypass_ml else 0
        body = struct.pack("<ffI", bypass_ml, temperature, int(dose_g))
        result = await self._setting(8102, body, after=after, untested=untested)
        commands = [8102] + ([8104] if cup_limits is not None else [])
        return {**result, "acknowledged_commands": commands}

    async def start_tea(
        self, recipe: EncodedTeaRecipe, *, timeout: float = 600
    ) -> Operation:
        if not isinstance(recipe, EncodedTeaRecipe):
            raise TypeError("Provide EncodedTeaRecipe with complete tea content")
        self._require_untested("Dedicated tea recipe execution")
        async with self._lifecycle:
            self._require_idle()
            self._water_source()

            async def configure() -> None:
                if recipe.cup_limits is not None:
                    await self._command(8104, cup_limits_payload(recipe.cup_limits))
                await self._command(4513, recipe.payload)
                await asyncio.sleep(1 if self.transition_delay else 0)

            return self._launch("tea", configure, 4512, b"", timeout)

    async def set_preset_count(self, count: int) -> dict[str, Any]:
        _uint("Studio preset count", count, 3)
        return await self._setting(40525, struct.pack("<I", count), untested=True)

    async def set_preset_order(
        self,
        order: Sequence[str] | str,
        *,
        backup_order: Sequence[str] | str,
        backup_path: str | os.PathLike[str],
    ) -> dict[str, Any]:
        requested, previous = _order(order), _order(backup_order)
        self._require_untested("Preset-order write")
        async with self._lifecycle:
            self._require_idle()
            saved = _save_backup(
                backup_path,
                {
                    "kind": "caller_supplied_preset_order_backup",
                    "order": previous,
                    "machine_readback_verified": False,
                },
            )
            try:
                await self._prepare()
                payload = bytes([3] + [SLOTS.index(slot) for slot in requested])
                packet = await self._command(11512, payload, 2)
                await self._command(8022)
                await asyncio.sleep(self.transition_delay)
                return {
                    "command": 11512,
                    "acknowledged": True,
                    "readback_verified": False,
                    "backup": saved,
                    "requested_order": requested,
                    "response_payload_hex": packet.payload.hex(),
                    "protocol_evidence": "untested",
                }
            except BaseException:
                self._uncertain = True
                raise

    async def quit_recipe_preparation(self) -> dict[str, Any]:
        return await self._housekeeping(8017)

    async def back_to_normal(self) -> dict[str, Any]:
        return await self._housekeeping(40526)

    async def _housekeeping(self, code: int) -> dict[str, Any]:
        self._require_untested(f"Housekeeping command {code}")
        activity = self.state.observations.get("machine_state_code")
        if activity and activity.value in (0x22, 0x23):
            raise SessionError("Stop active motion before housekeeping commands")
        return await self._setting(
            code,
            prepare=False,
            finish=False,
            untested=True,
            idle=False,
            ack_payload=b"",
        )
