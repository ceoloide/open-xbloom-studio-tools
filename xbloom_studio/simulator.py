"""Deterministic in-memory connector and a scripted machine for offline use.

:class:`ScriptedConnector` implements the connector contract without any
radio: it records every written chunk, reassembles outgoing frames and passes
each complete frame to a responder whose replies are delivered synchronously,
in order, as notifications. :class:`SimulatedStudio` is a small responder that
acknowledges every command and reports plausible, clearly synthetic state.
Nothing here models physical timing or verifies machine behavior.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from .connector import DEFAULT_WRITE_SIZE, check_information_field, check_write_size
from .protocol import FrameBuffer, Packet, encode
from .transport import FUNCTION_2_COMMANDS, HANDSHAKE_COMMAND

Responder = Callable[[Packet], Iterable[bytes]]

IDLE = 0x01
GRINDING = 0x22
POURING = 0x23
RECIPE_COMPLETE = 0x24
PRESET_SAVED = 0x25
PRESET_REJECTED = 0x44
# The non-empty 11510 report (marker 0xC1) that refuses a single-slot write.
PRESET_REJECTION_RESPONSE = bytes.fromhex("00000000000000000000000000400044")


def notification(code: int, payload: bytes = b"", *, marker: int = 0xC1) -> bytes:
    """Encode one machine notification frame."""
    return encode(code, payload, function=7, device=2, marker=marker)


def state_notification(value: int) -> bytes:
    return notification(8023, struct.pack("<I", value))


def device_info(
    *,
    serial: bytes = b"J15SIMULATED0",
    model: bytes = b"SIM",
    firmware: bytes = b"V1.0A.1",
    water_sufficient: bool = True,
    water_source: int = 0,
    grind_setting: int = 20,
    brightness: int = 8,
    temperature_unit: int = 1,
    weight_unit: int = 1,
    mode: bytes = b"\0" * 4,
    pour_radius: int = 700,
    vibration: int = 1300,
) -> bytes:
    """Return a synthetic 63-byte identity and configuration block."""
    info = bytearray(63)
    info[0:13] = serial[:13].ljust(13, b"\0")
    info[13:19] = model[:6].ljust(6, b"\0")
    info[19:29] = firmware[:10].ljust(10, b"\0")
    info[33] = int(water_sufficient)
    info[35] = 1
    info[36] = water_source
    info[37] = grind_setting + 30
    info[38] = brightness
    info[40] = temperature_unit
    info[41] = weight_unit
    info[51:55] = mode
    info[55:59] = struct.pack("<I", pour_radius)
    info[59:63] = struct.pack("<I", vibration)
    return bytes(info)


@dataclass
class SimulatedStudio:
    """Scripted responder: acknowledge commands and emit synthetic state.

    With ``auto_complete`` pours, recipes, tea and calibration report
    completion right after starting; otherwise they stay active until stopped
    or until a test injects notifications. ``silent`` commands get no reply.
    Each third consecutive preset write saves, unless ``reject_preset_attempts``
    is positive: that attempt is refused as once observed on hardware (a
    rejection state, a non-empty response, then idle) and the count drops.
    """

    auto_complete: bool = True
    info: bytes = field(default_factory=device_info)
    weight: float = 0.0
    silent: set[int] = field(default_factory=set)
    parameters: dict[int, int] = field(
        default_factory=lambda: {11506: 700, 11508: 1300}
    )
    reject_preset_attempts: int = 0
    _preset_writes: int = field(default=0, init=False)

    def __call__(self, packet: Packet) -> list[bytes]:
        code = packet.command
        if code in self.silent:
            return []
        if code == HANDSHAKE_COMMAND:
            return [
                notification(code),
                notification(40521, self.info),
                state_notification(IDLE),
                notification(20501, struct.pack("<f", self.weight)),
            ]
        if code == 11510:
            return self._preset_write()
        if code in FUNCTION_2_COMMANDS:
            reply = packet.payload
            if code in self.parameters and not reply:
                reply = struct.pack("<I", self.parameters[code])
            replies = [notification(code, reply, marker=0xC2)]
        else:
            replies = [notification(code)]
        return replies + self._effects(code)

    def _effects(self, code: int) -> list[bytes]:
        done = self.auto_complete
        self._preset_writes = 0
        if code == 8022:
            return [state_notification(IDLE)]
        if code == 4506:
            finished = [notification(40511), state_notification(IDLE)]
            return self._started(finished if done else [])
        if code in (8002, 4512):
            return self._started([state_notification(RECIPE_COMPLETE)] if done else [])
        if code in (3500, 3502):
            started = [state_notification(GRINDING)]
            if code == 3502 and done:
                started.append(notification(40526, struct.pack("<I", 85)))
            return started
        if code in (4507, 3505, 40519):
            stopped = 40507 if code == 3505 else 40511
            return [notification(stopped), state_notification(IDLE)]
        return []

    def _preset_write(self) -> list[bytes]:
        self._preset_writes += 1
        acknowledgment = notification(11510, marker=0xC2)
        if self._preset_writes < 3:
            return [acknowledgment]
        self._preset_writes = 0
        if self.reject_preset_attempts > 0:
            self.reject_preset_attempts -= 1
            return [
                state_notification(PRESET_REJECTED),
                notification(11510, PRESET_REJECTION_RESPONSE),
                state_notification(IDLE),
            ]
        return [
            acknowledgment,
            state_notification(PRESET_SAVED),
            state_notification(IDLE),
        ]

    @staticmethod
    def _started(finished: list[bytes]) -> list[bytes]:
        return [notification(40502), state_notification(POURING), *finished]


class ScriptedConnector:
    """Deterministic connector for tests, examples and offline development.

    ``writes`` is the exact chunk transcript; ``sent`` holds the reassembled
    outgoing packets. ``calls`` records lifecycle calls in order. Set
    ``fail_commands`` to raise an exception from the write that completes a
    frame with that command, and ``connect_error`` to fail ``connect``.
    """

    def __init__(
        self,
        identity: str = "SIMULATED-STUDIO",
        *,
        responder: Responder | None = None,
        write_size: int = DEFAULT_WRITE_SIZE,
        information: Mapping[str, str] | None = None,
    ) -> None:
        self._identity = identity
        self.write_size = check_write_size(write_size)
        self.responder: Responder | None = (
            SimulatedStudio() if responder is None else responder
        )
        self.information = dict(information or {"manufacturer": "Simulator"})
        self.calls: list[str] = []
        self.writes: list[bytes] = []
        self.sent: list[Packet] = []
        self.dropped: list[bytes] = []
        self.fail_commands: dict[int, BaseException] = {}
        self.connect_error: BaseException | None = None
        self._outgoing = FrameBuffer()
        self._callback: Callable[[bytes], None] | None = None
        self._on_disconnect: Callable[[], None] | None = None
        self._connected = False

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _require_connected(self) -> None:
        if not self._connected:
            raise ConnectionError("Simulated link is not connected")

    async def connect(self, on_disconnect: Callable[[], None]) -> None:
        self.calls.append("connect")
        if self._connected:
            raise ConnectionError("Simulated link is already connected")
        if self.connect_error is not None:
            raise self.connect_error
        self._connected = True
        self._on_disconnect = on_disconnect
        self._outgoing = FrameBuffer()

    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        self.calls.append("start_notify")
        self._require_connected()
        self._callback = callback

    async def stop_notify(self) -> None:
        self.calls.append("stop_notify")
        self._callback = None

    async def write(self, data: bytes) -> None:
        self._require_connected()
        if not 1 <= len(data) <= self.write_size:
            raise ValueError("Write exceeds the connector write size")
        self.writes.append(bytes(data))
        for packet in self._outgoing.feed(data):
            self.sent.append(packet)
            error = self.fail_commands.get(packet.command)
            if error is not None:
                raise error
            if self.responder is not None:
                for reply in self.responder(packet):
                    self.notify(reply)

    def notify(self, data: bytes) -> None:
        """Deliver one notification now, or record it as dropped if unsubscribed."""
        if self._callback is None:
            self.dropped.append(bytes(data))
        else:
            self._callback(bytes(data))

    def drop(self) -> None:
        """Simulate an unexpected link loss."""
        self._close()

    def _close(self) -> None:
        was_connected, self._connected = self._connected, False
        self._callback = None
        callback, self._on_disconnect = self._on_disconnect, None
        if was_connected and callback is not None:
            callback()

    async def read_information(self, name: str) -> str | None:
        check_information_field(name)
        self._require_connected()
        return self.information.get(name)

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self._close()
