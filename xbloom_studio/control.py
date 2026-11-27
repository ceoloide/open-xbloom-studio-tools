"""Opt-in, connection-owning controls built from tested command sequences.

No recipe store, parser or guessed next-step command. EncodedRecipe accepts
machine recipe content produced by an encoder, never a complete frame. Every
frame passes the control command gate before any transport sees it.
"""

from __future__ import annotations

import asyncio
import struct
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal

from .client import ProtocolCommandError, ReadOnlyClient, SessionError
from .command_schema import MAX_OPERATION_TIMEOUT, water_timeout
from .connector import Connector
from .events import decode_event, protocol_error
from .machine_controls import (
    ACTIVE_STATES,
    TEA_SOAKING_STATE,
    MachineControls,
    UntestedOperationError,
    _number,
    cup_limits_payload,
    validate_cup_limits,
)
from .protocol import Packet, encode
from .telemetry import WATER_SOURCES, decode_values
from .transport import (
    STOP_COMMANDS,
    STOP_HOOK_TIMEOUT,
    CommandHooks,
    ControlTransport,
    FrameTransport,
    GuardedFrameTransport,
    Transport,
    validate_control_frame,
)

IDLE_STATES = (1, 0x0A, 0x41, 0x24)
NO_WATER_STATE = 0x0C
MOTION_STATES = (0x22, 0x23)
SCALE_STATES = (4, 5)
CONTEXT_STATES = (1, 0x0A, 0x41, 0x24, 4, 5)
FAULT_EVENTS = (40517, 40522, 8203, 8204)
# Evidence derived from a code's report, never one of its decoded values.
SeenTag = Literal["water_fault", "any_state", "at_rest"]
SeenKey = tuple[int, "int | SeenTag | None"]
FAULT_KEYS: tuple[SeenKey, ...] = tuple((code, None) for code in FAULT_EVENTS)
LOW_WATER: SeenKey = (40522, 0)
WATER_FAULT: SeenKey = (40522, "water_fault")
# The latest decoded state code, whatever its value, and the latest one that
# is not a motion state.
STATE_REPORT: SeenKey = (8023, "any_state")
STATE_AT_REST: SeenKey = (8023, "at_rest")
# A calibration report at the calibrated position.
CALIBRATED: SeenKey = (40526, 85)
# Faults that always require recovery, before any water or source check.
MACHINE_FAULT_KEYS: tuple[SeenKey, ...] = tuple(
    key for key in FAULT_KEYS if key[0] != 40522
) + (WATER_FAULT,)
# Motion and stop evidence per actuator. One actuator's stop never ends the
# other's motion; only a machine-wide state report ends both.
GRINDER_MOTION: tuple[SeenKey, ...] = ((8023, MOTION_STATES[0]), (9003, None))
BREWER_MOTION: tuple[SeenKey, ...] = (
    (8023, MOTION_STATES[1]),
    (8023, TEA_SOAKING_STATE),
    (9005, None),
    (40502, None),
    (40520, None),  # bypass started
)
MOTION_KEYS = GRINDER_MOTION + BREWER_MOTION
ACTUATORS: dict[str, tuple[tuple[SeenKey, ...], tuple[SeenKey, ...]]] = {
    "grinder": (GRINDER_MOTION, ((40507, None),)),
    "brewer": (BREWER_MOTION, ((40511, None),)),
}
OPERATION_ACTUATORS = {
    "water": ("brewer",),
    "grinder": ("grinder",),
    "calibration": ("grinder",),
    "recipe": ("grinder", "brewer"),
    "tea": ("grinder", "brewer"),
}
# Machine-wide rest for admission: the machine reports one state code, so a
# newer non-motion state ends both actuators' reported motion. Starts still
# require a recognized idle state; housekeeping may follow an unknown one.
MACHINE_REST: tuple[SeenKey, ...] = (STATE_AT_REST,)
SOURCE_CODES = {label: code for code, label in WATER_SOURCES.items()}
WATER_KINDS = ("water", "recipe", "tea")
MEASUREMENTS = (20501, 40523, 40521)
STOP_CODES = {
    "water": 4507,
    "grinder": 3505,
    "calibration": 3505,
    "recipe": 40519,
    "tea": 40519,
}
EXIT_CODES = {"water": 8013, "grinder": 8012, "calibration": 8012}
PAUSE_RESUME_CODES = {
    "water": (8019, 8021),
    "grinder": (8018, 8020),
    "recipe": (40518, 40524),
    "tea": (40518, 40524),
}
PATTERNS = {"center": 0, "ring": 1, "spiral": 2}
# Function 2 commands the machine refuses with a non-empty report (marker
# 0xC1) instead of its 0xC2 acknowledgment, as V12.0D.500 refuses 11510.
REFUSING_COMMANDS = frozenset({11510})
CALIBRATED_POSITION = struct.pack("<I", 85)
# The xDripper reference cup limits of the one hardware-observed upload profile.
VALIDATED_UPLOAD_CUP_LIMITS = (90, 110)


class BusyError(RuntimeError):
    pass


class InsufficientWaterError(BusyError):
    pass


class IdleStateRequiredError(BusyError):
    pass


class UnsupportedStageError(NotImplementedError):
    pass


class _Omitted:
    pass


_OMITTED = _Omitted()


@dataclass(frozen=True, init=False)
class EncodedRecipe:
    """Opaque machine content plus the settings sent separately.

    No built-in recipes. Cup limits are explicit optional (min, max) values;
    omitting them preserves the machine configuration.
    """

    payload: bytes
    dose_g: float
    grinder_enabled: bool = False
    cup_limits: tuple[float, float] | None = None
    bypass_ml: float = 0
    bypass_temperature_c: float = 20

    def __init__(
        self,
        payload: bytes,
        dose_g: float,
        grinder_enabled: bool | _Omitted = _OMITTED,
        cup_limits: tuple[float, float] | None = None,
        bypass_ml: float = 0,
        bypass_temperature_c: float = 20,
        *,
        set_grinder_size: bool | _Omitted = _OMITTED,
    ) -> None:
        """``set_grinder_size`` selects the grind-size upload; the alias is kept.

        Neither option establishes whether opaque content runs a grinder stage.
        """
        for name, value in (
            ("grinder_enabled", grinder_enabled),
            ("set_grinder_size", set_grinder_size),
        ):
            if value is not _OMITTED and type(value) is not bool:
                raise ValueError(name + " must be boolean")
        if (
            isinstance(grinder_enabled, bool)
            and isinstance(set_grinder_size, bool)
            and grinder_enabled != set_grinder_size
        ):
            raise ValueError(
                "Conflicting grinder_enabled and set_grinder_size upload options"
            )
        selected = (
            set_grinder_size
            if isinstance(set_grinder_size, bool)
            else grinder_enabled
            if isinstance(grinder_enabled, bool)
            else False
        )
        for name, item in (
            ("payload", payload),
            ("dose_g", dose_g),
            ("grinder_enabled", selected),
            ("cup_limits", cup_limits),
            ("bypass_ml", bypass_ml),
            ("bypass_temperature_c", bypass_temperature_c),
        ):
            object.__setattr__(self, name, item)
        self._validate()

    @property
    def set_grinder_size(self) -> bool:
        return self.grinder_enabled

    def upload_options(self) -> dict[str, Any]:
        return {
            "set_grinder_size": self.set_grinder_size,
            "upload_command": 8001 if self.set_grinder_size else 8004,
            "actual_grinding": "not_established_by_upload_option",
        }

    def _validate(self) -> None:
        if not isinstance(self.payload, bytes) or not 1 <= len(self.payload) <= 512:
            raise ValueError(
                "Recipe content must be 1–512 bytes, without a BLE envelope"
            )
        _number("dose_g", self.dose_g, 1, 100)
        if self.dose_g != int(self.dose_g):
            raise ValueError("Validated dose command requires whole grams")
        validate_cup_limits(self.cup_limits)
        _number("bypass_ml", self.bypass_ml, 0, 1000)
        _number("bypass_temperature_c", self.bypass_temperature_c, 20, 99)

    def settings_payload(self) -> bytes:
        # Bypass volume is an unscaled float32 and its temperature is in tenths
        # of a degree. Disabled bypass sends two zero bit patterns, not 20 C.
        temperature = self.bypass_temperature_c * 10 if self.bypass_ml else 0
        return struct.pack("<ffI", self.bypass_ml, temperature, int(self.dose_g))


@dataclass
class Operation:
    """One operation. Waiting does not own a command lock or release Bluetooth."""

    kind: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    phase: str = "configuring"
    status: str = "running"
    start_observed: bool = False
    stop_acknowledged: bool | None = None
    stop_observed: bool = False
    cleanup_status: str = "pending"
    error: str | None = None
    protocol_error: dict[str, Any] | None = None
    fault_event: dict[str, Any] | None = None
    pending_user_action: str | None = None
    last_machine_event: dict[str, Any] | None = None
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _armed: bool = field(default=False, repr=False)
    _stop_requested: bool = field(default=False, repr=False)
    _paused: bool = field(default=False, repr=False)
    _fault_is_low_water: bool = field(default=False, repr=False)
    _final: dict[str, Any] | None = field(default=None, repr=False)
    # Event revision at launch: faults or motion after it block the start.
    _since: int = field(default=0, repr=False)
    # Water source encoded in the start command, when it carries one.
    _source: int | None = field(default=None, repr=False)
    # Event revision when the start could first reach the machine; natural
    # completion evidence must be newer.
    _armed_at: int = field(default=0, repr=False)
    # Event revision at the stop write's final chunk, once transmitted.
    _stop_boundary: int | None = field(default=None, repr=False)
    # Natural completion that is itself stop evidence, counted from launch.
    _natural_rest: bool = field(default=False, repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            key: getattr(self, key)
            for key in (
                "id",
                "kind",
                "phase",
                "status",
                "start_observed",
                "stop_acknowledged",
                "stop_observed",
                "cleanup_status",
                "error",
                "protocol_error",
                "fault_event",
                "pending_user_action",
                "last_machine_event",
            )
        }

    @property
    def done(self) -> bool:
        return self._task is not None and self._task.done()

    async def wait(self) -> dict[str, Any]:
        if self._task is None:
            raise RuntimeError("Operation has not started")
        # Cancelling a consumer does not silently cancel the machine operation,
        # and a worker cancelled during cleanup still leaves its final result.
        await asyncio.wait({self._task})
        if not self._task.cancelled():
            self._task.result()
        return {**self.snapshot(), "state": self._final}


class _Progress:
    """How far one command got: rejected by its guard, or transmitted."""

    rejected = False
    # The frame was started but its final chunk withheld.
    withheld = False
    # A chunk before the final one was handed to the connector, and so may
    # have reached the machine; ``tracked`` when the transport reports chunks.
    begun = False
    tracked = False
    transmitted_at: float | None = None
    # Latest monotonic time at which an acknowledgment is accepted.
    deadline: float | None = None
    expiry: asyncio.TimerHandle | None = None

    def accepts_ack(self) -> bool:
        return self.deadline is None or time.monotonic() <= self.deadline

    def unfinished(self) -> bool:
        """Bytes of the frame may have been sent, but not its final chunk."""
        if not self.tracked:
            return self.withheld
        return self.begun and (self.transmitted_at is None or self.withheld)


Waiter = tuple[tuple[int, int, "bytes | None"], "asyncio.Future[Packet]", _Progress]


def _confirmed(future: asyncio.Future[Packet]) -> bool:
    return future.done() and not future.cancelled() and future.exception() is None


def _known_low_water(payload: bytes) -> bool:
    return len(payload) >= 4 and int.from_bytes(payload[:4], "little") == 0


def _known_water_ok(payload: bytes) -> bool:
    return len(payload) >= 4 and int.from_bytes(payload[:4], "little") == 1


def _is_preparation_water_fault(payload: bytes, status: str) -> bool:
    if _known_water_ok(payload):
        return False
    return not (_known_low_water(payload) and status in ("uploading", "prepared"))


async def _drain(task: asyncio.Task[None]) -> bool:
    """Wait until ``task`` settles, never cancelling it.

    Returns whether the caller was cancelled meanwhile. Only the command's own
    deadline decision may cancel the write; a caller cannot release the link
    while the connector still runs.
    """
    interrupted = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            interrupted = True
    return interrupted


class Interaction(MachineControls, ReadOnlyClient):
    """Hold the link until context exit, including idle and user-input waits.

    Controls are opt-in independently of connecting. Pass an address, a
    platform device or ``connector=``. Reconnection is never automatic.
    State methods and stop remain responsive while operations run.
    """

    transport: FrameTransport

    def __init__(
        self,
        device: object = None,
        *,
        allow_control: bool = False,
        allow_untested: bool = False,
        transport: FrameTransport | None = None,
        connector: Connector | None = None,
        command_hooks: CommandHooks | None = None,
        ack_timeout: float = 5.0,
        write_timeout: float = 5.0,
        transition_delay: float = 3.0,
        start_timeout: float = 5.0,
        **kwargs: Any,
    ) -> None:
        if type(allow_control) is not bool or type(allow_untested) is not bool:
            raise ValueError("allow_control and allow_untested must be booleans")
        _number("ack_timeout", ack_timeout, 0.001, 60)
        _number("write_timeout", write_timeout, 0.001, 60)
        _number("transition_delay", transition_delay, 0, 30)
        _number("start_timeout", start_timeout, 0.001, 60)
        super().__init__(
            device,
            transport=transport,
            connector=connector,
            command_hooks=command_hooks,
            **kwargs,
        )
        self.allow_control = allow_control
        self.allow_untested = allow_untested
        self.ack_timeout = ack_timeout
        self.write_timeout = write_timeout
        self.transition_delay = transition_delay
        self.start_timeout = start_timeout
        self.operation: Operation | None = None
        self._commands = asyncio.Lock()
        self._lifecycle = asyncio.Lock()
        self._waiter: Waiter | None = None
        self._closing = False
        self._entered = False
        self._scale_mode = False
        self._uncertain = False
        # Commands whose delivery or effect stayed unconfirmed, as opposed to
        # refusals that sent nothing; never reset, compared across a step.
        self._unconfirmed_deliveries = 0
        self._event_revision = 0
        self._received_events: deque[tuple[int, Packet]] = deque(maxlen=256)
        # Latest revision per event (and per reported state), kept apart from
        # the bounded history so benign traffic can never evict a fault.
        self._seen: dict[SeenKey, int] = {}
        self._preset_write_used = False
        self._last_preset_write: dict[str, Any] | None = None
        self._selected_water_source: int | None = None
        # The mode the latest 11511 ACK selected on this link, kept apart from
        # telemetry; ``_mode_acks`` counts those ACKs and never resets.
        self._selected_mode: str | None = None
        self._mode_acks = 0
        # Latest decoded source from an identity block or units/source event.
        self._reported_source: int | None = None
        # Water sufficiency decoded from the latest identity block only; a
        # newer block without a recognized value never inherits an older one.
        self._identity_water: bool | None = None
        # A frame was begun but not finished: later bytes would be appended
        # to it, so nothing more is written on this link.
        self._partial_frame = False
        # While set, a caller cancelled during a frame's write sees that
        # cancellation even if the write then fails or its deadline passes.
        self._keep_cancellation = False
        # Sees every decoded event, so bounded history cannot evict it.
        self._event_latch: Callable[[int, Packet], None] | None = None
        self._prepared: dict[str, Any] | None = None
        self._last_preparation: dict[str, Any] | None = None

    @staticmethod
    def _default_transport(
        device: object, connector: Connector | None, hooks: CommandHooks | None
    ) -> Transport:
        return ControlTransport(device, connector=connector, hooks=hooks)

    async def __aenter__(self) -> Interaction:
        if self._entered or self._closing:
            raise BusyError("Interaction context is already in use")
        if not self.allow_control:
            raise SessionError("Controls require allow_control=True")
        self._entered = True
        try:
            await super().__aenter__()
        except BaseException:
            self._entered = False
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.disconnect()

    async def unsubscribe(self) -> None:
        # Check and unsubscribe under the lifecycle lock, so no operation or
        # preparation is admitted between the check and the call.
        async with self._lifecycle:
            if self._prepared is not None:
                raise BusyError("Cancel the prepared recipe before disabling telemetry")
            if self.operation is not None and not self.operation.done:
                raise BusyError("Stop the active operation before disabling telemetry")
            await super().unsubscribe()

    async def disconnect(self) -> None:
        self._closing = True
        self._invalidate_preparation("disconnect")

        async def finish() -> None:
            try:
                await self.stop()
                # Wait for an in-flight scale/mode command before disconnecting.
                async with self._lifecycle:
                    if self._scale_mode and self.state.session_ready:
                        await self._command(8014)
                        self._scale_mode = False
            finally:
                await ReadOnlyClient.disconnect(self)
                self._entered = False

        task = asyncio.create_task(finish())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _disconnected(self) -> None:
        self._invalidate_preparation("connection_lost")
        self._reported_source = self._identity_water = None
        self._selected_mode = None
        super()._disconnected()
        if self._waiter is not None:
            _, future, _ = self._waiter
            if not future.done():
                future.set_exception(
                    SessionError("Connection lost; machine state is unknown")
                )

    def _packet_received(self, packet: Packet) -> None:
        error = protocol_error(packet)
        if error is not None:
            self._protocol_error(error)
            return
        if (packet.device, packet.function) != (2, 7):
            return
        if self._waiter is not None:
            (code, marker, expected_payload), future, progress = self._waiter
            acknowledged = packet.marker == marker and (
                expected_payload is None or packet.payload == expected_payload
            )
            refused = (
                code in REFUSING_COMMANDS
                and packet.marker == 0xC1
                and packet.payload != b""
            )
            if (
                packet.command == code
                and (acknowledged or refused)
                and not future.done()
                # Judged by the clock at receipt: an ACK after its deadline is
                # never accepted, however late cleanup inspects the future.
                and progress.accepts_ack()
            ):
                future.set_result(packet)
        if packet.marker != 0xC1:
            return
        if packet.command in (40521, 8015):
            label = self._decoded(packet, "water_source")
            self._reported_source = (
                SOURCE_CODES.get(label) if isinstance(label, str) else None
            )
        if packet.command == 40521:
            water = self._decoded(packet, "water_sufficient")
            self._identity_water = water if isinstance(water, bool) else None
        if self._prepared is not None:
            self._check_preparation(packet)
        self._event_revision += 1
        water_ok = packet.command == 40522 and _known_water_ok(packet.payload)
        if not water_ok:
            self._seen[(packet.command, None)] = self._event_revision
        if packet.command == 8023 and len(packet.payload) >= 4:
            state = int.from_bytes(packet.payload[:4], "little")
            self._seen[(8023, state)] = self._event_revision
            self._seen[STATE_REPORT] = self._event_revision
            if state not in ACTIVE_STATES:
                self._seen[STATE_AT_REST] = self._event_revision
        if packet.command == 40521 and self._identity_water is False:
            self._seen[LOW_WATER] = self._event_revision
        if packet.command == 40522 and not water_ok:
            water = LOW_WATER if _known_low_water(packet.payload) else WATER_FAULT
            self._seen[water] = self._event_revision
        if packet.command == 40526 and packet.payload == CALIBRATED_POSITION:
            self._seen[CALIBRATED] = self._event_revision
        if packet.command not in MEASUREMENTS:
            self._received_events.append((self._event_revision, packet))
        if self._event_latch is not None:
            self._event_latch(self._event_revision, packet)
        op = self.operation
        if op is None or op._task is None or op._task.done():
            return
        if packet.command in FAULT_EVENTS and not water_ok:
            self._record_fault_event(op, packet)
        if not op._armed or packet.command in MEASUREMENTS:
            return
        self._observe(op, packet)
        self._signal()

    def _protocol_error(self, error: dict[str, Any]) -> None:
        self._invalidate_preparation("protocol_error")
        if self.state.session_ready:
            self._uncertain = True
        pending = self._waiter[0][0] if self._waiter is not None else None
        exc = ProtocolCommandError(error, pending)
        if self._waiter is not None and not self._waiter[1].done():
            self._waiter[1].set_exception(exc)
        op = self.operation
        if op is not None and not op.done and op.protocol_error is None:
            op.protocol_error = exc.details
        self._signal()

    @staticmethod
    def _decoded(packet: Packet, field: str) -> object:
        """The packet's last decoded ``field``; None when it has none."""
        try:
            values = decode_values(packet)
        except ValueError:
            return None
        found = [value for name, value, _, _ in values if name == field]
        return found[-1] if found else None

    def _check_preparation(self, packet: Packet) -> None:
        assert self._prepared is not None
        code, payload = packet.command, packet.payload
        motion = code in (9003, 9005, 40502, 40520)
        water_fault = code == 40522 and _is_preparation_water_fault(
            payload, self._prepared["status"]
        )
        fault = code in (40517, 8203, 8204) or water_fault
        context = code in (8111, 9000, 9001, 9002, 9004, 9006, 9008)
        if code == 8023 and len(payload) >= 4:
            state = int.from_bytes(payload[:4], "little")
            motion = state in ACTIVE_STATES
            context = state in CONTEXT_STATES
        if motion or fault or context:
            self._invalidate_preparation(
                "machine_fault"
                if fault
                else "machine_motion"
                if motion
                else "machine_context_changed"
            )
            if motion or fault:
                self._uncertain = True

    def _raw_event(self, packet: Packet) -> dict[str, Any]:
        return {
            "revision": self._event_revision,
            "at": datetime.now(UTC).isoformat(),
            "command": packet.command,
            "payload": packet.payload.hex(),
        }

    def _record_fault_event(self, op: Operation, packet: Packet) -> None:
        if op.status != "running":
            return
        low_water = packet.command == 40522 and _known_low_water(packet.payload)
        if op.fault_event is not None and (low_water or not op._fault_is_low_water):
            return
        event = self._raw_event(packet)
        decoded = decode_event(packet)
        op.fault_event = {**event, **decoded} if decoded is not None else event
        op._fault_is_low_water = low_water

    def _observe(self, op: Operation, packet: Packet) -> None:
        code, payload = packet.command, packet.payload
        event = self._raw_event(packet)
        op.last_machine_event = event
        try:
            decoded = decode_event(packet)
        except ValueError:
            return  # Recorded by telemetry as a decode error, never lifecycle data.
        if decoded is not None:
            op.last_machine_event = {**event, **decoded}
            if (code == 9003 and op.kind in ("grinder", "recipe")) or (
                code == 9005 and op.kind in ("water", "recipe", "tea")
            ):
                op.start_observed = True
                op.phase = "grinding" if code == 9003 else "pouring"
                op._paused = False
            if (op.kind, code) in (("grinder", 9009), ("water", 9010)):
                op._paused = True
                op.phase = "paused"
            if op.kind in ("recipe", "tea") and code in (40520, 40527):
                op.phase = "bypass" if code == 40520 else "before_vibration"
        if code == 8023 and len(payload) >= 4:
            self._observe_state(op, int.from_bytes(payload[:4], "little"))
        # A pod identifier is not evidence of motion.
        if code == 40502 and op.kind in ("water", "recipe", "tea"):
            op.start_observed = True
        if (
            op.kind == "calibration"
            and code == 40526
            and payload == CALIBRATED_POSITION
        ):
            op.start_observed = True
        if op.kind == "tea" and code in (9012, 9011, 40515):
            op.start_observed = True
            op.phase = {
                9012: "soaking",
                9011: "tea_resume_observed",
                40515: "tea_pause_observed",
            }[code]
            if code in (9011, 40515):
                op._paused = code == 40515

    @staticmethod
    def _observe_state(op: Operation, state: int) -> None:
        if state in MOTION_STATES:
            if op.kind in ("recipe", "tea") or (op.kind, state) in (
                ("grinder", 0x22),
                ("calibration", 0x22),
                ("water", 0x23),
            ):
                op.start_observed = True
            op.phase = "grinding" if state == 0x22 else "pouring"
            op._paused = False
        elif op.kind == "tea" and state == TEA_SOAKING_STATE:
            op.start_observed = True
            op.phase = "soaking"
            op._paused = False
        elif (op.kind, state) in (("grinder", 2), ("water", 3)):
            op._paused = True
            op.phase = "paused"
        elif op.kind in ("recipe", "tea") and state not in (1, 0x24):
            op.phase = "unknown_machine_stage"

    def get_state(self) -> dict[str, Any]:
        """Immediate cached observations; this does not send a status command."""
        return {
            "machine": self.snapshot(),
            "operation": self.operation.snapshot() if self.operation else None,
            "event_revision": self._event_revision,
            "recovery_required": self._uncertain,
            "last_preset_write": self._last_preset_write,
            "last_acknowledged_water_source": self._selected_water_source,
            "last_acknowledged_mode": self._selected_mode,
            "prepared": dict(self._prepared) if self._prepared else None,
            "last_preparation": dict(self._last_preparation)
            if self._last_preparation
            else None,
            "query_source": "notifications_cache",
        }

    @staticmethod
    def capabilities() -> dict[str, Any]:
        return {
            "read_only": False,
            "requires": ["allow_control"],
            "operations": [
                "start_water",
                "dispense_water",
                "start_grinder",
                "grind",
                "start_recipe",
                "upload_recipe",
                "upload_tea",
                "start_prepared",
                "cancel_prepared",
                "pause",
                "resume",
                "stop",
                "enter_scale",
                "tare",
                "exit_scale",
                "get_state",
                "wait_for_state",
            ]
            + list(MachineControls.METHODS),
            "recipe_input": "encoded_machine_content_with_explicit_settings",
            "remote_stage_advance": False,
            "active_state_query": False,
            "automatic_reconnect": False,
            "automatic_start_retry": False,
            "untested_requires": "allow_untested=True",
            "untested_operations": list(MachineControls.UNTESTED_METHODS),
            "untested_recipe_options": ["grinder_enabled", "bypass_ml>0", "cup_limits"],
            "active_setting_queries": ["read_pour_radius", "read_vibration"],
            "firmware_update": False,
            "address_lock_scope": "same OS user and host",
            "physical_recipe_controls": (
                "observed via notifications; unmapped stages stay unknown"
            ),
        }

    async def wait_for_state(
        self, after_revision: int, *, timeout: float = 5.0
    ) -> dict[str, Any]:
        _number("timeout", timeout, 0.001, MAX_OPERATION_TIMEOUT)
        await self.wait_for_update(after_revision, timeout)
        return self.get_state()

    def _require_session(self) -> None:
        if (
            not self.allow_control
            or not self._entered
            or self._closing
            or not self.state.session_ready
        ):
            raise SessionError("Enter an open control interaction first")
        if self._uncertain:
            raise SessionError(
                "Previous operation uncertain; inspect machine and open a new "
                "interaction"
            )

    def _require_idle(self, *, allow_no_water: bool = False) -> None:
        self._require_quiescent()
        observation = self.state.observations.get("machine_state_code")
        if (
            self._scale_mode
            and observation is not None
            and observation.value in SCALE_STATES
        ):
            return  # Owned standalone scale can be exited by _prepare.
        states = IDLE_STATES + ((NO_WATER_STATE,) if allow_no_water else ())
        if observation is None or observation.value not in states:
            raise IdleStateRequiredError(
                "Machine has not reported a recognized idle/completed state"
            )

    def _require_preset_water(self) -> None:
        """Refuse on observed low water; V12.0D.500 refuses 11510 without it."""
        water = self.state.observations.get("water_sufficient")
        state = self.state.observations.get("machine_state_code")
        if (water is not None and water.value is False) or (
            state is not None and state.value == NO_WATER_STATE
        ):
            raise InsufficientWaterError(
                "Machine reports insufficient water for a preset write"
            )

    def _require_quiescent(self, *, allow_prepared: bool = False) -> None:
        self._require_session()
        if self._prepared is not None and not allow_prepared:
            raise BusyError(
                "A prepared recipe owns the interaction; start or cancel it first"
            )
        if self.operation is not None and not self.operation.done:
            raise BusyError("An operation already owns this interaction")
        if self._motion_reported():
            raise BusyError("Machine reports motion outside an owned operation")

    def _motion_reported(self) -> bool:
        activity = self.state.observations.get("machine_state_code")
        if activity and activity.value in ACTIVE_STATES:
            return True
        # A motion event newer than its own actuator's stop and any rest state
        # is outstanding even when the last state code or identity looks idle.
        return self._outstanding(0, MACHINE_REST)

    async def _wait_event(
        self,
        after_revision: int,
        predicate: Callable[[Packet], bool],
        timeout: float,
        events: Iterable[tuple[int, Packet]] | None = None,
    ) -> Packet:
        deadline = time.monotonic() + timeout
        while True:
            for revision, packet in self._received_events if events is None else events:
                if revision > after_revision and predicate(packet):
                    return packet
            if not self.state.connected:
                raise SessionError("Disconnected while waiting for machine event")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Expected machine event was not observed")
            await self.wait_for_update(self.state.revision, min(0.1, remaining))

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
    ) -> Packet:
        """Serialize frame+ACK; cancellation finishes the bounded frame first.

        ``before_write`` guards the first byte, after any journal hook; when it
        rejects, nothing is sent. The ACK is armed, and ``on_transmit`` runs,
        immediately before the frame can reach the machine: before its final
        chunk, or before a legacy transport's call. ``ack_budget`` bounds the
        ACK wait from that boundary.
        """
        frame = encode(code, payload, function=function)
        # The gate runs above every transport, injected ones included, and a
        # rejected frame is never handed to a transport.
        validate_control_frame(frame)
        async with self._commands:
            self._require_frame_boundary()
            if active_operation is not None and (
                active_operation._stop_requested or active_operation.status != "running"
            ):
                raise BusyError("Operation is stopping or has ended")
            if not self.state.connected or not self.state.session_ready:
                raise SessionError("Control connection unavailable")
            future: asyncio.Future[Packet] = asyncio.get_running_loop().create_future()
            expected = payload if code == 11511 else ack_payload
            key = (code, 0xC2 if function == 2 else 0xC1, expected)
            progress = _Progress()
            progress.tracked = self._withholds_final_chunk()
            loop = asyncio.get_running_loop()
            guard: Callable[[], None] | None = None
            if before_write is not None:
                admit = before_write

                def guard() -> None:
                    try:
                        admit()
                    except BaseException:
                        progress.rejected = True
                        raise

            # One cancellation decision covers the write bound and the command
            # budget, whichever expires first: it cancels the one write task
            # at most once, and nothing else ever cancels that task.
            expired: list[str] = []

            def expire(reason: str) -> None:
                # Honored only if the connector honors cancellation; the
                # command, its lock and its hooks still wait for the write.
                if not expired and not write.done():
                    expired.append(reason)
                    write.cancel()

            def chunk(final: bool) -> None:
                # Once the deadline decision is made, the write task never
                # hands the connector another chunk, even if the connector
                # absorbed that cancellation and returned normally.
                if not expired and time.monotonic() >= write_deadline:
                    expired.append("write")
                if expired:
                    raise asyncio.CancelledError()
                if not final:
                    progress.begun = True

            def transmit() -> None:
                # A matching notification before this point is stale.
                progress.transmitted_at = now = time.monotonic()
                self._waiter = (key, future, progress)
                if ack_budget is not None:
                    progress.deadline = now + ack_budget
                    progress.expiry = loop.call_later(ack_budget, expire, "budget")
                if on_transmit is not None:
                    try:
                        on_transmit()
                    except BaseException:
                        # Raised before the final chunk, which is withheld;
                        # earlier chunks, if any, leave the frame unfinished.
                        progress.withheld = True
                        raise

            # A stop's bounded journal wait must not consume its write bound.
            bound = self.write_timeout + (
                STOP_HOOK_TIMEOUT if code in STOP_COMMANDS else 0
            )
            # The one task that owns the connector for this frame. _drain keeps
            # the link held until the inner write finishes even on cancellation.
            write_deadline = time.monotonic() + bound
            finished_at = write_deadline

            async def write_frame() -> None:
                nonlocal finished_at
                try:
                    await self._write(frame, guard, transmit, chunk)
                finally:
                    finished_at = time.monotonic()

            write = loop.create_task(write_frame())
            write_expiry = loop.call_later(bound, expire, "write")
            try:
                interrupted = await _drain(write)
                cancelled = write.cancelled()
                if interrupted and self._keep_cancellation:
                    raise asyncio.CancelledError() from (
                        None if cancelled else write.exception()
                    )
                if not cancelled:
                    write.result()
                if expired == ["write"] or finished_at >= write_deadline:
                    raise TimeoutError(
                        "Write did not finish within write_timeout; delivery unknown"
                    )
                budget_expired = expired == ["budget"] or (
                    progress.deadline is not None and finished_at >= progress.deadline
                )
                if (cancelled or budget_expired) and not _confirmed(future):
                    raise TimeoutError(
                        "Command budget expired during its write; delivery unconfirmed"
                    )
                if interrupted:
                    raise asyncio.CancelledError()
                if cancelled or budget_expired:
                    return future.result()
                wait, budgeted = self.ack_timeout, False
                now = time.monotonic()
                if progress.deadline is not None:
                    left = progress.deadline - now
                    if left < wait:
                        wait, budgeted = max(0.0, left), True
                progress.deadline = now + wait
                # asyncio.wait leaves cleanup below responsible for the shared
                # ACK future without cancelling it on timeout.
                done, _ = await asyncio.wait({future}, timeout=wait)
                if not done:
                    if budgeted:
                        raise TimeoutError(
                            "Acknowledgment not received within the command budget"
                        )
                    raise TimeoutError()
                return future.result()
            except BaseException as exc:
                # A missing ACK leaves command effects uncertain; never retry.
                # A guard rejection sent nothing. A cancellation is uncertain
                # once the frame could have reached the machine unacknowledged.
                if (
                    not progress.rejected
                    if isinstance(exc, Exception)
                    else progress.transmitted_at is not None and not _confirmed(future)
                ):
                    self._uncertain = True
                    if progress.transmitted_at is not None and not progress.withheld:
                        self._unconfirmed_deliveries += 1
                # A begun frame whose final chunk was never attempted would
                # absorb the next bytes; a final attempt stays merely unknown.
                if progress.unfinished() and self._withholds_final_chunk():
                    self._partial_frame = self._uncertain = True
                raise
            finally:
                write_expiry.cancel()
                if progress.expiry is not None:
                    progress.expiry.cancel()
                self._waiter = None
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()  # consume an error even if the write failed

    def _require_frame_boundary(self) -> None:
        """Precondition of every frame written on this link, handshake included.

        A begun but unfinished frame would absorb the next bytes, and nothing
        on this link resets the peripheral's parser: not unsubscribing, not
        a new subscription. The latch lasts for this interaction.
        """
        if self._partial_frame:
            raise SessionError(
                "A partially written frame holds this link; disconnect and "
                "open a new interaction"
            )

    async def _send_handshake(self) -> None:
        # Serialized with command writes: it waits for a write that still owns
        # the connector and shares their frame-boundary precondition.
        async with self._commands:
            self._require_frame_boundary()
            await super()._send_handshake()

    def _withholds_final_chunk(self) -> bool:
        return isinstance(self.transport, GuardedFrameTransport)

    async def _write(
        self,
        frame: bytes,
        guard: Callable[[], None] | None,
        transmit: Callable[[], None],
        chunk: Callable[[bool], None],
    ) -> None:
        transport = self.transport
        if isinstance(transport, GuardedFrameTransport):
            await transport._write_frame_guarded(
                frame,
                command_guard=guard,
                on_transmit=transmit,
                on_chunk=chunk,
            )
            return
        if guard is not None:
            guard()
        transmit()
        await transport._write_frame(frame)

    async def _prepare(self) -> None:
        if self._scale_mode:
            await self._command(8014)
            self._scale_mode = False
            await asyncio.sleep(self.transition_delay)
        await self._command(8022)
        await asyncio.sleep(self.transition_delay)
        await self._command(11511, b"\0" * 4, 2)
        self._record_mode_ack("pro")
        await asyncio.sleep(self.transition_delay)

    def _record_mode_ack(self, mode: str) -> None:
        self._selected_mode = mode
        self._mode_acks += 1

    def _water_source(self) -> int:
        info = self.state.observations.get("device_info_raw")
        sufficient = self.state.observations.get("water_sufficient")
        # Both the latest identity block and every later water report must
        # affirm sufficiency; unknown is never sufficient.
        if (
            self._identity_water is not True
            or sufficient is None
            or sufficient.value is not True
        ):
            raise SessionError("Water sufficiency not confirmed; no automatic retry")
        raw = bytes.fromhex(info.value) if info else b""
        if len(raw) < 63:
            raise SessionError("Current water-source configuration unknown")
        # An acknowledged selection overrides every report, including one
        # without a known source; it never overrides the checks above.
        if self._selected_water_source is not None:
            return self._selected_water_source
        # Otherwise the newest decoded report (identity block or 8015 event)
        # decides; a newer report without a known source makes it unknown.
        if self._reported_source is None:
            raise SessionError("Current water-source configuration unknown")
        return self._reported_source

    async def start_water(
        self,
        *,
        volume_ml: float,
        temperature_c: float = 20,
        flow_ml_s: float = 3.0,
        pattern: str = "center",
        timeout: float | None = None,
    ) -> Operation:
        _number("volume_ml", volume_ml, 1, 1000)
        _number("temperature_c", temperature_c, 20, 99)
        _number("flow_ml_s", flow_ml_s, 0.1, 10)
        if pattern not in PATTERNS:
            raise ValueError("pattern must be center, ring or spiral")
        pattern_code = PATTERNS[pattern]
        limit = water_timeout(volume_ml, flow_ml_s) if timeout is None else timeout
        async with self._lifecycle:
            self._require_idle()
            source = self._water_source()

            async def configure() -> None:
                await self._command(
                    8007, struct.pack("<If", pattern_code, temperature_c * 10)
                )
                await self._command(4510, struct.pack("<I", round(temperature_c * 10)))

            start = struct.pack(
                "<fffII",
                flow_ml_s * 10,
                volume_ml * 10,
                temperature_c * 10,
                source,
                pattern_code,
            )
            return self._launch("water", configure, 4506, start, limit, source=source)

    async def dispense_water(self, **kwargs: Any) -> dict[str, Any]:
        return await self._complete(self.start_water(**kwargs))

    async def _complete(self, starting: Awaitable[Operation]) -> dict[str, Any]:
        op = await starting
        try:
            return await op.wait()
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def start_grinder(
        self, *, size: int, rpm: int, duration_s: float
    ) -> Operation:
        _number("size", size, 1, 80)
        _number("rpm", rpm, 60, 120)
        _number("duration_s", duration_s, 0.1, 120)
        if size != int(size) or rpm != int(rpm):
            raise ValueError("size and rpm must be integers")
        async with self._lifecycle:
            self._require_idle()

            async def configure() -> None:
                await self._command(8006, struct.pack("<II", int(size), int(rpm)))

            start = struct.pack("<III", 1000, int(size), int(rpm))
            return self._launch("grinder", configure, 3500, start, duration_s)

    async def grind(self, **kwargs: Any) -> dict[str, Any]:
        return await self._complete(self.start_grinder(**kwargs))

    def _require_recipe_options(self, recipe: EncodedRecipe) -> None:
        if recipe.bypass_ml or recipe.cup_limits is not None or recipe.set_grinder_size:
            self._require_untested(
                "Grinder-setting upload, bypass or cup-limit behavior"
            )

    async def start_recipe(
        self, recipe: EncodedRecipe, *, timeout: float = 600
    ) -> Operation:
        if not isinstance(recipe, EncodedRecipe):
            raise TypeError(
                "Provide EncodedRecipe machine content, not a recipe name or URL"
            )
        self._require_recipe_options(recipe)
        async with self._lifecycle:
            self._require_idle()
            self._water_source()

            async def configure() -> None:
                await self._command(8102, recipe.settings_payload())
                if recipe.cup_limits is not None:
                    await self._command(8104, cup_limits_payload(recipe.cup_limits))
                await self._command(
                    8001 if recipe.set_grinder_size else 8004, recipe.payload
                )
                await asyncio.sleep(1 if self.transition_delay else 0)

            return self._launch("recipe", configure, 8002, b"", timeout)

    def _invalidate_preparation(self, reason: str) -> None:
        if self._prepared is not None:
            self._last_preparation = {
                **self._prepared,
                "status": "invalidated",
                "reason": reason,
            }
            self._prepared = None
            self._signal()

    def _preparation(self, token: object) -> dict[str, Any]:
        if (
            not isinstance(token, str)
            or self._prepared is None
            or token != self._prepared["token"]
            or self._prepared["status"] != "prepared"
        ):
            raise SessionError("Stale or invalid preparation token; no command sent")
        self._require_quiescent(allow_prepared=True)
        return self._prepared

    async def upload_recipe(self, recipe: EncodedRecipe) -> dict[str, Any]:
        """Upload coffee settings/content without executing or advancing a stage."""
        if not isinstance(recipe, EncodedRecipe):
            raise TypeError("Provide EncodedRecipe machine content")
        self._require_recipe_options(recipe)
        return await self._upload("recipe", self._recipe_upload(recipe))

    async def _upload_validated_coffee(self, recipe: EncodedRecipe) -> dict[str, Any]:
        """Upload the hardware-observed coffee options without experimental opt-in.

        Internal to the application, which admits only a validated structured
        coffee plan here. This path itself accepts nothing but the grind-size
        upload (8001) with the xDripper reference cup limits and no bypass;
        any other option keeps the upload_recipe opt-in.
        """
        if not isinstance(recipe, EncodedRecipe):
            raise TypeError("Provide EncodedRecipe machine content")
        if (
            recipe.set_grinder_size is not True
            or recipe.cup_limits != VALIDATED_UPLOAD_CUP_LIMITS
            or recipe.bypass_ml != 0
        ):
            raise UntestedOperationError(
                "Only the validated xDripper grind-size upload without bypass "
                "skips allow_untested"
            )
        return await self._upload("recipe", self._recipe_upload(recipe))

    def _recipe_upload(self, recipe: EncodedRecipe) -> Callable[[], Awaitable[None]]:
        async def configure() -> None:
            await self._upload_command(8102, recipe.settings_payload())
            if recipe.cup_limits is not None:
                await self._upload_command(8104, cup_limits_payload(recipe.cup_limits))
            await self._upload_command(
                8001 if recipe.set_grinder_size else 8004, recipe.payload
            )

        return configure

    async def upload_tea(self, recipe: Any) -> dict[str, Any]:
        """Upload dedicated tea content; never sends the tea execute command."""
        from .machine_controls import EncodedTeaRecipe

        if not isinstance(recipe, EncodedTeaRecipe):
            raise TypeError("Provide EncodedTeaRecipe machine content")
        self._require_untested("Dedicated tea recipe upload")

        async def configure() -> None:
            if recipe.cup_limits is not None:
                await self._upload_command(8104, cup_limits_payload(recipe.cup_limits))
            await self._upload_command(4513, recipe.payload)

        return await self._upload("tea", configure)

    async def _upload_command(self, code: int, payload: bytes) -> Packet:
        preparation = self._prepared

        def validate() -> None:
            self._require_session()
            if preparation is None or self._prepared is not preparation:
                raise SessionError("Machine context changed during recipe upload")

        validate()
        return await self._command(code, payload, before_write=validate)

    async def _upload(
        self, kind: str, configure: Callable[[], Awaitable[None]]
    ) -> dict[str, Any]:
        async with self._lifecycle:
            self._require_idle(allow_no_water=True)
            try:
                await self._prepare()
                self._require_session()
                self._prepared = {
                    "token": uuid.uuid4().hex,
                    "kind": kind,
                    "status": "uploading",
                    "retained_until": "start/cancel/disconnect",
                }
                await configure()
                self._require_session()
                prepared = self._prepared
                if prepared is None:
                    raise SessionError("Machine context changed during recipe upload")
                prepared["status"] = "prepared"
                self._signal()
                return dict(prepared)
            except BaseException:
                self._invalidate_preparation("upload_failed")
                self._uncertain = True
                raise

    async def start_prepared(self, token: str, *, timeout: float = 600) -> Operation:
        """Execute one owned upload without home/mode changes or another upload."""
        _number("timeout", timeout, 0.1, 3600)
        async with self._lifecycle:
            preparation = self._preparation(token)
            self._water_source()
            kind = preparation["kind"]
            preparation["status"] = "starting"

            def validate() -> None:
                self._require_session()
                if self._prepared is not preparation:
                    raise SessionError(
                        "Prepared machine context changed before execution"
                    )
                self._water_source()

            async def configure() -> None:
                validate()

            def claim() -> None:
                validate()
                self._last_preparation = {**preparation, "status": "consumed"}
                self._prepared = None

            return self._launch(
                kind,
                configure,
                8002 if kind == "recipe" else 4512,
                b"",
                timeout,
                prepare=False,
                before_start=claim,
            )

    async def cancel_prepared(self, token: str) -> dict[str, Any]:
        """Leave an owned preparation and return home; no experimental gate."""
        async with self._lifecycle:
            preparation = self._preparation(token)
            self._prepared = None
            last = {**preparation, "status": "cancelling"}
            self._last_preparation = last
            try:
                await self._command(8017, ack_payload=b"")
                await asyncio.sleep(self.transition_delay)
                await self._command(8022)
                await asyncio.sleep(self.transition_delay)
                last["status"] = "cancelled"
                return {**last, "cancelled": True}
            except BaseException:
                last.update(status="invalidated", reason="cancel_failed")
                self._uncertain = True
                raise
            finally:
                self._signal()

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
        source: int | None = None,
    ) -> Operation:
        _number("timeout", timeout, 0.1, 3600)
        op = Operation(kind, _since=self._event_revision, _source=source)
        self.operation = op
        op._task = asyncio.create_task(
            self._run(
                op,
                configure,
                start_code,
                payload,
                timeout,
                prepare=prepare,
                before_start=before_start,
            )
        )
        self._signal()
        return op

    def _start_guard(
        self, op: Operation, claim: Callable[[], None] | None
    ) -> Callable[[], None]:
        """Recheck admission after configuration and hooks, before byte one."""

        def guard() -> None:
            self._require_session()
            if not self._owns():
                raise SessionError("Machine ownership lost; start not sent")
            if op._stop_requested:
                raise BusyError("Operation was stopped before its start was sent")
            self._admit_start(op)
            if claim is not None:
                claim()

        return guard

    def _admit_start(self, op: Operation) -> None:
        """Start admission, at byte one and again before the final chunk.

        Faults (an actionable water report included) and motion come first:
        they always require recovery, even when the water check would also
        refuse. For a water operation, low water, an unknown source or a
        changed source then stay ordinary refusals; for any other operation
        low water reported since launch is a fault, as it is once running.
        """
        self._refuse_changed_machine(op)
        if op.kind not in WATER_KINDS:
            return
        # A selected source still overrides the reported one.
        source = self._water_source()
        if self._seen.get(LOW_WATER, 0) > op._since:
            raise SessionError(
                "Water sufficiency not confirmed since admission; no automatic retry"
            )
        if op._source is not None and source != op._source:
            raise SessionError("Water source changed after admission; start not sent")

    def _refuse_changed_machine(self, op: Operation) -> None:
        """Refuse a start after any fault or motion reported since launch.

        Uses the persistent per-event revisions, so neither history eviction,
        later benign telemetry nor the gap between frame chunks can hide one.
        """
        faults = MACHINE_FAULT_KEYS
        if op.kind not in WATER_KINDS:
            faults += (LOW_WATER,)
        if self._last(faults) > op._since:
            self._uncertain = True
            raise SessionError("Machine reported a fault before start; not sent")
        if self._last(MOTION_KEYS) > op._since:
            self._uncertain = True
            raise BusyError("Machine reported motion before start; not sent")

    def _completed(self, op: Operation) -> bool:
        if op.protocol_error is not None:
            raise ProtocolCommandError(
                op.protocol_error, op.protocol_error.get("pending_command")
            )
        if self._last(MACHINE_FAULT_KEYS) > op._since:
            raise SessionError("Machine reported a fault; inspect fault_event")
        if self._seen.get((40522, None), 0) > op._since:
            raise InsufficientWaterError(
                "Machine reported insufficient water during operation"
            )
        # Only the latest qualifying completion counts, and only while no
        # relevant motion is newer: superseded completion neither ends the
        # operation nor comes back with a later, unrelated stop or rest.
        completion = self._completion(op)
        return completion > max(op._armed_at, self._relevant_motion(op.kind))

    def _completion(self, op: Operation) -> int:
        """Revision of the latest natural completion evidence, or 0."""
        seen = self._seen.get
        if op.kind in ("recipe", "tea"):
            # The executed report plus idle qualifies only when both belong to
            # the current activity interval and idle is the latest state.
            idle = seen((8023, 1), 0)
            executed = min(seen((40513, None), 0), idle)
            if idle != seen(STATE_REPORT, 0):
                executed = 0
            return max(seen((8023, 0x24), 0), executed)
        if op.kind == "calibration":
            return seen(CALIBRATED, 0)
        return seen((40511 if op.kind == "water" else 40507, None), 0)

    def _relevant_motion(self, kind: str) -> int:
        return max(self._last(ACTUATORS[name][0]) for name in OPERATION_ACTUATORS[kind])

    async def _run(
        self,
        op: Operation,
        configure: Callable[[], Awaitable[None]],
        start_code: int,
        payload: bytes,
        timeout: float,
        *,
        prepare: bool = True,
        before_start: Callable[[], None] | None = None,
    ) -> None:
        success = False
        try:
            if prepare:
                await self._prepare()
            await configure()
            op.phase = "awaiting_machine"
            boundary: list[float] = []

            def transmitted() -> None:
                # Recheck immediately before the final chunk: a fault or motion
                # reported between chunks withholds it, so the machine never
                # receives a complete start. The begun frame then blocks every
                # later write on this link instead of corrupting the next one.
                # Water sufficiency and the effective source are rechecked too.
                self._admit_start(op)
                op._armed = True  # starts can take effect before their ACK
                op._armed_at = self._event_revision
                boundary.append(time.monotonic())

            grinder = op.kind == "grinder"
            await self._command(
                start_code,
                payload,
                before_write=self._start_guard(op, before_start),
                on_transmit=transmitted,
                # A grinder pulse is budgeted from its start frame, so a slow
                # ACK cannot extend it; expiry before the ACK requests stop.
                ack_budget=timeout if grinder else None,
            )
            started_at = time.monotonic()
            deadline = (boundary[0] if grinder else started_at) + timeout
            while True:
                if not self.state.connected:
                    raise SessionError(
                        "Connection lost during operation; machine state unknown"
                    )
                # Newer relevant motion than the completion means it is still
                # running: recipes and tea would otherwise skip their stop.
                success = self._completed(op)
                if success:
                    op._natural_rest = op.kind != "calibration"
                    op.stop_observed = op._natural_rest
                    break
                now = time.monotonic()
                if (
                    op.kind in ("water", "grinder")
                    and not op.start_observed
                    and now - started_at >= self.start_timeout
                ):
                    raise TimeoutError(
                        "Start acknowledged but activity not observed; requesting stop"
                    )
                if now >= deadline:
                    if op.kind == "grinder":
                        if not op.start_observed:
                            raise TimeoutError(
                                "Grinder activity not observed; requesting stop"
                            )
                        break  # planned bounded pulse; stop is sent below
                    raise TimeoutError("Operation deadline reached; requesting stop")
                # Never mistake ACK for start. Keep recipe prompts observable.
                await asyncio.sleep(0.05)
            op.status = "completed"
        except asyncio.CancelledError:
            op.status = "stopped"
        except Exception as exc:
            op.status = "failed"
            op.error = f"{type(exc).__name__}: {exc}"
        finally:
            await self._finish(op, success, before_start is not None)

    async def _finish(self, op: Operation, success: bool, claimed: bool) -> None:
        try:
            await self._cleanup_operation(op, success, claimed)
        finally:
            # Even an interrupted cleanup leaves a final result for waiters.
            if op.cleanup_status == "pending":
                op.cleanup_status = "skipped_uncertain"
                self._uncertain = True
            # Reconcile once more after cleanup, cancelled or not, from the same
            # boundary: rest first reported during cleanup confirms the stop,
            # and motion newer than every stop or completion voids it.
            self._reconcile_stop(op)
            if op._armed and self._moving(op):
                op.stop_observed = False
                op.status = "unknown"
                op.error = op.error or "Machine reports motion after the operation"
                self._uncertain = True
            if not self.state.connected:
                op.status = "unknown"
                self._uncertain = True
            op.phase = op.status
            op._final = self.snapshot()
            self._signal()

    async def _cleanup_operation(
        self, op: Operation, success: bool, claimed: bool
    ) -> None:
        if claimed and self._prepared is not None:
            self._invalidate_preparation("execution_not_transmitted")
            self._uncertain = True
        # Also stop after natural manual completion to make cleanup explicit.
        if op._armed and (not success or op.kind not in ("recipe", "tea")):
            await self._send_stop(op)
        if op._armed and not op.stop_observed and self._moving(op):
            # An unconfirmed operation is never terminal while motion is known.
            op.status = "unknown"
            op.error = op.error or "Stop not observed; machine reports motion"
            self._uncertain = True
        if not self.state.connected:
            op.status = "unknown"
            self._uncertain = True
        # A lost ACK may leave mode/actuator effects uncertain even after stop.
        if self.state.session_ready and not self._uncertain:
            try:
                exit_code = EXIT_CODES.get(op.kind)
                if exit_code is not None:
                    await self._command(exit_code)
                    await asyncio.sleep(self.transition_delay)
                if op.kind not in ("recipe", "tea") or not success:
                    await self._command(8022)
                op.cleanup_status = "completed"
            except Exception as exc:
                op.error = f"Cleanup unconfirmed: {type(exc).__name__}: {exc}"
                self._uncertain = True
                op.cleanup_status = "failed"
        else:
            op.cleanup_status = "skipped_uncertain"

    def _last(self, keys: tuple[SeenKey, ...]) -> int:
        return max(self._seen.get(key, 0) for key in keys)

    def _outstanding(self, since: int, idle: tuple[SeenKey, ...]) -> bool:
        """Motion of either actuator newer than ``since``, its own stop and
        every ``idle`` state; another actuator's stop never ends it."""
        return any(
            self._last(motion) > max(since, self._last(stop + idle))
            for motion, stop in ACTUATORS.values()
        )

    @staticmethod
    def _operation_idle(kind: str) -> tuple[SeenKey, ...]:
        idle: tuple[SeenKey, ...] = ((8023, 1),)
        return idle + ((8023, 0x24),) if kind in ("recipe", "tea") else idle

    def _stopped_since(self, op: Operation, revision: int, stopped: bool) -> bool:
        """Stop evidence for ``op`` after ``revision``.

        Outstanding motion since launch decides against it. Otherwise idle,
        or the rest of this single-actuator operation's own actuator, newer
        than ``revision`` confirms it; without any, ``stopped`` (prior
        completion) stands. A rest report retained from before ``revision``,
        or another actuator's stop, never proves this stop. A recipe or tea
        needs whole-operation rest: one actuator's stop may be an
        intermediate stage, such as the end of one pour.
        """
        idle = self._operation_idle(op.kind)
        if self._outstanding(op._since, idle):
            return False
        if op.kind in ("recipe", "tea"):
            motion = self._relevant_motion(op.kind)
            return self._last(idle) > max(revision, motion) or (
                stopped and self._completion(op) > max(op._armed_at, motion)
            )
        rest = idle + tuple(
            key for name in OPERATION_ACTUATORS[op.kind] for key in ACTUATORS[name][1]
        )
        return self._last(rest) > revision or stopped

    def _moving(self, op: Operation) -> bool:
        return self._outstanding(op._since, self._operation_idle(op.kind))

    def _reconcile_stop(self, op: Operation) -> None:
        """Completion observed before the stop (its own stop evidence) counts
        from launch; otherwise only evidence after the stop's write boundary
        counts. Either way newer motion voids it, even when the stop itself
        was only acknowledged."""
        since = op._since if op._natural_rest else op._stop_boundary
        if op._armed and since is not None:
            op.stop_observed = self._stopped_since(op, since, op._natural_rest)

    async def _send_stop(self, op: Operation) -> None:
        def transmitted() -> None:
            op._stop_boundary = self._event_revision

        try:
            await self._command(STOP_CODES[op.kind], on_transmit=transmitted)
            op.stop_acknowledged = True
            await asyncio.sleep(self.transition_delay)
        except BaseException as exc:
            if not op.stop_acknowledged:
                op.stop_acknowledged = False
                op.status = "unknown"
                op.error = f"Stop unconfirmed: {type(exc).__name__}: {exc}"
                self._uncertain = True
            if not isinstance(exc, Exception):
                raise
        finally:
            self._reconcile_stop(op)

    async def stop(self) -> dict[str, Any] | None:
        op = self.operation
        if op is None or op._task is None:
            return None
        if op.status == "running" and not op._task.done() and not op._stop_requested:
            op._stop_requested = True
            # Scheduled on the loop, not in this coroutine: the request survives
            # its caller's cancellation, and every later caller shares it. It
            # runs after a newly scheduled worker has entered its try/finally.
            asyncio.get_running_loop().call_soon(self._cancel_worker, op, op._task)
        return await op.wait()

    @staticmethod
    def _cancel_worker(op: Operation, task: asyncio.Task[None]) -> None:
        # Never interrupt cleanup: the worker leaves "running" before it.
        if not task.done() and op.status == "running":
            task.cancel()

    async def _pause_resume(self, resume: bool) -> dict[str, Any]:
        self._require_session()
        op = self.operation
        if op is None or op.status != "running" or not op._armed:
            raise BusyError("No active operation to pause or resume")
        if op.kind == "calibration":
            raise UnsupportedStageError(
                "Calibration supports stop; pausing its adjustment is not validated"
            )
        if resume != op._paused:
            raise BusyError(
                "Operation is not paused" if resume else "Operation already paused"
            )
        code = PAUSE_RESUME_CODES[op.kind][int(resume)]
        try:
            await self._command(code, active_operation=op)
        except Exception, asyncio.CancelledError:
            # An uncertain pause/resume must not leave an unattended operation.
            await self.stop()
            raise
        op._paused = not resume
        # ACK-confirmed request, not evidence that the physical action occurred.
        op.phase = "resume_acknowledged" if resume else "pause_acknowledged"
        self._signal()
        return op.snapshot()

    async def pause(self) -> dict[str, Any]:
        return await self._pause_resume(False)

    async def resume(self) -> dict[str, Any]:
        return await self._pause_resume(True)

    async def advance(self, *, expected_stage: str, revision: int) -> None:
        self._require_session()
        if revision != self._event_revision:
            raise BusyError("Machine events changed; read state before advancing")
        raise UnsupportedStageError(
            f"Remote advancement for {expected_stage!r} is not validated; "
            "use the machine controls"
        )

    async def enter_scale(self) -> dict[str, Any]:
        async with self._lifecycle:
            self._require_idle()
            await self._prepare()
            await self._command(8003)
            self._scale_mode = True
        return self.get_state()

    async def tare(self) -> dict[str, Any]:
        async with self._lifecycle:
            self._require_session()
            if not self._scale_mode or (self.operation and not self.operation.done):
                raise BusyError("Tare requires standalone scale mode")
            await self._command(8500)
        return self.get_state()

    async def exit_scale(self) -> dict[str, Any]:
        async with self._lifecycle:
            self._require_session()
            if self.operation and not self.operation.done:
                raise BusyError("Operation in progress")
            if self._scale_mode:
                await self._command(8014)
                self._scale_mode = False
                await asyncio.sleep(self.transition_delay)
                await self._command(8022)
        return self.get_state()
