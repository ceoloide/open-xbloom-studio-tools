"""Guarded transports above a byte-link connector, and the optional Bleak link.

:class:`BleakTransport` is the read-only transport: its only write is the fixed
session handshake. :class:`ControlTransport` additionally accepts encoded
control frames, each checked against the control command gate. Both validate
and segment frames above the :class:`~xbloom_studio.connector.Connector`, so
any connector runs behind the same checks, hooks and byte transcript.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Protocol, runtime_checkable

from .connector import (
    DEFAULT_WRITE_SIZE,
    Connector,
    check_information_field,
    check_write_size,
    normalize_identity,
)
from .protocol import Packet, decode

SERVICE = "0000e0ff-3c17-d293-8e48-14fe2e4da212"
COMMAND = "0000ffe1-0000-1000-8000-00805f9b34fb"
STATUS = "0000ffe2-0000-1000-8000-00805f9b34fb"
HANDSHAKE = bytes.fromhex("580101a41f1400000001b900000001000000bdd1")
STANDARD_INFO = {
    "manufacturer": "2a29",
    "model": "2a24",
    "serial": "2a25",
    "firmware": "2a26",
    "hardware": "2a27",
    "software": "2a28",
}
HANDSHAKE_COMMAND = 8100

# Function 2 parameter, mode and preset commands; all others use function 1.
FUNCTION_2_COMMANDS = frozenset({11506, 11507, 11508, 11509, 11510, 11511, 11512})
CONTROL_COMMANDS = FUNCTION_2_COMMANDS | frozenset(
    {
        3500, 3502, 3505, 4506, 4507, 4508, 4510, 4512, 4513,
        8001, 8002, 8003, 8004, 8005, 8006, 8007, 8010, 8012, 8013, 8014,
        8016, 8017, 8018, 8019, 8020, 8021, 8022, 8102, 8103, 8104, 8500,
        40518, 40519, 40524, 40525, 40526,
    }
)  # fmt: skip
# Stop commands may be sent after a hook failure: a failing journal must not
# prevent a bounded attempt to stop the machine.
STOP_COMMANDS = frozenset({3505, 4507, 40519})
STOP_HOOK_TIMEOUT = 0.05
# Synchronous observers cannot receive a task cancellation, so a
# CancelledError they raise is their own and is suppressed like any failure.
OBSERVER_ERRORS = (Exception, asyncio.CancelledError)

EventSink = Callable[[str, dict[str, Any]], None]


def validate_handshake(frame: bytes) -> Packet:
    """Return the decoded handshake; any other frame is rejected."""
    packet = decode(frame)
    if frame != HANDSHAKE or (
        packet.device,
        packet.function,
        packet.command,
        packet.marker,
    ) != (1, 1, HANDSHAKE_COMMAND, 1):
        raise ValueError("Only the fixed session handshake may open a session")
    return packet


def validate_control_frame(frame: bytes) -> Packet:
    """Return the decoded control frame, or reject it before any write."""
    packet = decode(frame)
    if (packet.device, packet.marker) != (1, 1) or (
        packet.command not in CONTROL_COMMANDS
    ):
        raise ValueError("Frame outside the control API allowlist")
    if packet.function != (2 if packet.command in FUNCTION_2_COMMANDS else 1):
        raise ValueError("Unexpected control function")
    return packet


class Transport(Protocol):
    """Session-level transport used by the read-only client."""

    async def connect(self, on_disconnect: Callable[[], None]) -> None: ...

    async def disconnect(self) -> None: ...

    async def subscribe(self, callback: Callable[[bytes], None]) -> None: ...

    async def unsubscribe(self) -> None: ...

    async def open_session(self) -> None: ...

    async def read_information(self, name: str) -> str | None: ...


class FrameTransport(Transport, Protocol):
    """Transport that also accepts encoded control frames."""

    async def _write_frame(self, frame: bytes) -> None: ...


@runtime_checkable
class GuardedTransport(Transport, Protocol):
    """Transport supporting per-call handshake timing hooks and byte transcripts."""

    event_sink: EventSink | None

    async def open_session_guarded(
        self, *, on_transmit: Callable[[], None] | None = None
    ) -> None: ...


@runtime_checkable
class GuardedFrameTransport(GuardedTransport, FrameTransport, Protocol):
    """Frame transport supporting per-call guards and chunk-level transmit hooks."""

    async def _write_frame_guarded(
        self,
        frame: bytes,
        *,
        command_guard: Callable[[], None] | None = None,
        on_transmit: Callable[[], None] | None = None,
        on_chunk: Callable[[bool], None] | None = None,
    ) -> None: ...


class CommandHooks(Protocol):
    """Observer around each outgoing frame, for example a durable journal.

    ``before_write`` runs after the frame passed its gate and before the
    command guard and the first byte; raising prevents the write, except for
    stop commands, which wait at most ``STOP_HOOK_TIMEOUT`` and then proceed
    without waiting for the cancelled hook to finish. Exactly one of
    ``write_completed`` and ``write_failed`` follows a write attempt, including
    when ``before_write`` raises before any bytes are written. Neither
    may block; their exceptions, an explicit ``CancelledError`` included, are
    suppressed so an observer can never turn a sent frame into a failure or
    hide the original write error.
    """

    async def before_write(self, packet: Packet, frame: bytes) -> None: ...

    def write_completed(self, packet: Packet) -> None: ...

    def write_failed(self, packet: Packet, error: BaseException) -> None: ...


def _bleak() -> Any:
    try:
        return importlib.import_module("bleak")
    except ImportError as exc:
        raise RuntimeError("Install xbloom-studio[ble] for Bluetooth support") from exc


async def discover(timeout: float = 10.0) -> list[dict[str, Any]]:
    """Discover Studio advertisements without connecting or pairing."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    found = await _bleak().BleakScanner.discover(timeout=timeout, return_adv=True)
    return [
        {
            "address": device.address,
            "name": adv.local_name or device.name,
            "rssi": adv.rssi,
            "service_uuids": adv.service_uuids,
        }
        for device, adv in found.values()
        if (adv.local_name or device.name or "").upper().startswith("XBLOOM")
        or SERVICE in [uuid.lower() for uuid in adv.service_uuids]
    ]


class BleakConnector:
    """Local Bluetooth Low Energy link using the optional ``bleak`` package.

    Accepts an address or a platform device object from an existing scanner.
    Pairing, MTU overrides and adapter resets are deliberately absent. Writes
    are writes without response of at most 20 bytes.
    """

    write_size = DEFAULT_WRITE_SIZE

    def __init__(self, device: object, timeout: float = 15.0) -> None:
        self.device = device
        self.timeout = timeout
        self._identity = normalize_identity(device)
        self._client: Any = None

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def is_connected(self) -> bool:
        return self._client is not None and bool(self._client.is_connected)

    async def connect(self, on_disconnect: Callable[[], None]) -> None:
        bleak = _bleak()
        device = self.device
        if isinstance(device, str):
            device = await bleak.BleakScanner.find_device_by_address(
                device, timeout=self.timeout
            )
            if device is None:
                raise ConnectionError(
                    "xBloom not advertising; release other Bluetooth clients"
                )
        self._client = bleak.BleakClient(
            device,
            timeout=self.timeout,
            disconnected_callback=lambda _: on_disconnect(),
        )
        await self._client.connect()
        for uuid in (COMMAND, STATUS):
            if self._client.services.get_characteristic(uuid) is None:
                raise ConnectionError(f"Required Studio characteristic missing: {uuid}")

    def _require_client(self) -> Any:
        if self._client is None:
            raise ConnectionError("Not connected")
        return self._client

    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        await self._require_client().start_notify(
            STATUS, lambda _, data: callback(bytes(data))
        )

    async def stop_notify(self) -> None:
        if self.is_connected:
            await self._client.stop_notify(STATUS)

    async def write(self, data: bytes) -> None:
        if not 1 <= len(data) <= self.write_size:
            raise ValueError("Write exceeds the connector write size")
        # The machine rejects writes with response on this characteristic.
        await self._require_client().write_gatt_char(COMMAND, data, response=False)

    async def read_information(self, name: str) -> str | None:
        uuid = f"0000{STANDARD_INFO[check_information_field(name)]}"
        uuid += "-0000-1000-8000-00805f9b34fb"
        client = self._require_client()
        if client.services.get_characteristic(uuid) is None:
            return None
        value = bytes(await client.read_gatt_char(uuid))
        return value.decode("utf-8", errors="replace").strip("\x00") or None

    async def disconnect(self) -> None:
        try:
            if self._client is not None:
                await self._client.disconnect()
        finally:
            self._client = None


def _retrieve(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()


class BleakTransport:
    """Read-only session transport; its only write is the fixed handshake.

    Pass an address or platform device to use the local Bleak connector, or
    ``connector=`` for any other link. A session may make the machine announce
    a pairing even though no operating-system bonding takes place.

    ``event_sink`` sees each chunk as ``ble.outgoing_chunk`` before its guard,
    ACK arming and write; a chunk then refused before its write is followed
    by ``ble.outgoing_chunk_withheld`` with the same bytes.
    """

    def __init__(
        self,
        device: object = None,
        timeout: float = 15.0,
        *,
        connector: Connector | None = None,
        hooks: CommandHooks | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        if connector is None:
            if device is None:
                raise ValueError("Provide a device address or a connector")
            connector = BleakConnector(device, timeout)
        elif not isinstance(connector, Connector):
            raise TypeError("connector must implement the Connector protocol")
        self._write_size = check_write_size(connector.write_size)
        self.connector = connector
        self.device = device if device is not None else connector.identity
        self.timeout = timeout
        self.hooks = hooks
        self.event_sink: EventSink | None = event_sink
        self._subscribed = False
        # True while an interrupted stop_notify leaves notifications unknown.
        self._notify_unknown = False
        self._journals: set[asyncio.Future[None]] = set()

    @property
    def identity(self) -> str:
        return self.connector.identity

    async def connect(self, on_disconnect: Callable[[], None]) -> None:
        self._subscribed = self._notify_unknown = False
        await self.connector.connect(on_disconnect)

    async def subscribe(self, callback: Callable[[bytes], None]) -> None:
        if not self._subscribed:
            if self._notify_unknown and self.connector.is_connected:
                # An interrupted unsubscribe may have left the old callback
                # registered; end it before registering the new one.
                with suppress(Exception):
                    await self.connector.stop_notify()
            await self.connector.start_notify(callback)
            self._subscribed, self._notify_unknown = True, False

    async def unsubscribe(self) -> None:
        # Writes stop being admitted before stop_notify is even attempted.
        subscribed, self._subscribed = self._subscribed, False
        if subscribed and self.connector.is_connected:
            self._notify_unknown = True
            await self.connector.stop_notify()
            self._notify_unknown = False

    async def open_session_guarded(
        self, *, on_transmit: Callable[[], None] | None = None
    ) -> None:
        if not self._subscribed:
            raise RuntimeError("Subscribe before opening session")
        await self._send(
            HANDSHAKE,
            validate_handshake(HANDSHAKE),
            on_transmit=on_transmit,
        )

    async def open_session(self) -> None:
        await self.open_session_guarded()

    async def read_information(self, name: str) -> str | None:
        return await self.connector.read_information(check_information_field(name))

    async def disconnect(self) -> None:
        try:
            await self.connector.disconnect()
        finally:
            self._subscribed = False

    async def _journal_stop(
        self, hooks: CommandHooks, packet: Packet, frame: bytes
    ) -> None:
        """Give a stop's journal a bounded head start; never wait past it.

        A hook that ignores or resists cancellation keeps running detached, so
        it can neither delay the stop beyond ``STOP_HOOK_TIMEOUT`` nor consume
        the bounded write that follows.
        """
        try:
            journal = asyncio.ensure_future(hooks.before_write(packet, frame))
        except Exception:
            return
        try:
            await asyncio.wait({journal}, timeout=STOP_HOOK_TIMEOUT)
        finally:
            if not journal.done():
                journal.cancel()
                self._journals.add(journal)
                journal.add_done_callback(self._journals.discard)
            journal.add_done_callback(_retrieve)

    async def _send(
        self,
        frame: bytes,
        packet: Packet,
        *,
        command_guard: Callable[[], None] | None = None,
        on_transmit: Callable[[], None] | None = None,
        on_chunk: Callable[[bool], None] | None = None,
    ) -> None:
        hooks = self.hooks
        try:
            if hooks is not None:
                if packet.command in STOP_COMMANDS:
                    await self._journal_stop(hooks, packet, frame)
                else:
                    await hooks.before_write(packet, frame)
            size = self._write_size
            for offset in range(0, len(frame), size):
                chunk = frame[offset : offset + size]
                # The observer runs first, so a notification it causes is seen
                # by the guard and can never satisfy a not-yet-armed ACK.
                self._observe("ble.outgoing_chunk", chunk)
                try:
                    if offset == 0:
                        if command_guard is not None:
                            command_guard()
                        if not self._subscribed or not self.connector.is_connected:
                            raise ConnectionError("No subscribed control connection")
                    final = offset + size >= len(frame)
                    if on_chunk is not None:
                        on_chunk(final)
                    if final and on_transmit is not None:
                        on_transmit()
                except BaseException as exc:
                    # Keep the transcript exact: the observed chunk was withheld.
                    self._observe("ble.outgoing_chunk_withheld", chunk, exc)
                    raise
                await self.connector.write(chunk)
        except BaseException as exc:
            if hooks is not None:
                with suppress(*OBSERVER_ERRORS):
                    hooks.write_failed(packet, exc)
            raise
        if hooks is not None:
            with suppress(*OBSERVER_ERRORS):
                hooks.write_completed(packet)

    def _observe(
        self, kind: str, data: bytes, error: BaseException | None = None
    ) -> None:
        if self.event_sink is not None:
            details = {"hex": bytes(data).hex()}
            if error is not None:
                details["error"] = f"{type(error).__name__}: {error}"
            # A transcript observer must never prevent or fail a write.
            with suppress(*OBSERVER_ERRORS):
                self.event_sink(kind, details)


class ControlTransport(BleakTransport):
    """Opt-in transport for the interaction API; read-only transport stays unchanged.

    Only encoded control frames that pass the command gate are written; ACKs
    are handled by the interaction, not this transport.
    """

    async def _write_frame_guarded(
        self,
        frame: bytes,
        *,
        command_guard: Callable[[], None] | None = None,
        on_transmit: Callable[[], None] | None = None,
        on_chunk: Callable[[bool], None] | None = None,
    ) -> None:
        await self._send(
            frame,
            validate_control_frame(frame),
            command_guard=command_guard,
            on_transmit=on_transmit,
            on_chunk=on_chunk,
        )

    async def _write_frame(self, frame: bytes) -> None:
        await self._write_frame_guarded(frame)
