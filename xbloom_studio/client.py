"""Atomic, read-only Studio session and latest-state cache."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from types import TracebackType
from typing import Any, Self

from .connector import Connector, normalize_identity
from .events import protocol_error
from .ownership import AddressLease
from .protocol import FrameBuffer, FrameError, Packet
from .telemetry import DeviceState, decode_values
from .transport import (
    OBSERVER_ERRORS,
    BleakTransport,
    CommandHooks,
    EventSink,
    Transport,
)

# One owner per machine identity in this process; other processes use leases.
_owners: dict[str, ReadOnlyClient] = {}
DECODER_VERSION = "0.0.1"


async def wait_set(event: asyncio.Event, timeout: float) -> bool:
    """Wait for an event; unlike wait_for, never swallow a racing cancellation.

    When ``event`` is already set, ``asyncio.wait_for`` completes synchronously
    before a pending cancellation arrives; ``asyncio.wait`` always yields once.
    """
    waiter = asyncio.ensure_future(event.wait())
    try:
        done, _ = await asyncio.wait({waiter}, timeout=timeout)
    finally:
        waiter.cancel()
    return bool(done)


async def _settle[T](task: asyncio.Future[T]) -> None:
    task.cancel()
    await asyncio.wait({task})
    if not task.cancelled():
        task.exception()  # retrieved; the caller reports its own outcome


async def bounded[T](awaitable: Awaitable[T], timeout: float) -> T:
    """Like ``wait_for``, but a cancellation always propagates.

    The inner operation is cancelled and finished before the caller's
    cancellation or timeout is raised, so cleanup never overlaps it.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        await _settle(task)
        raise
    if not done:
        await _settle(task)
        raise TimeoutError()
    return task.result()


class SessionError(ConnectionError):
    pass


class ProtocolCommandError(SessionError):
    """Machine error observed while a command was pending; association is temporal."""

    def __init__(
        self, details: dict[str, Any], pending_command: int | None = None
    ) -> None:
        self.details = {
            **details,
            "pending_command": pending_command,
            "correlation": "pending_at_receipt"
            if pending_command is not None
            else "unsolicited",
        }
        super().__init__(
            f"{details['kind']} {details['value']} "
            f"while waiting for command {pending_command}"
        )


def _identity(device: object, transport: object, connector: Connector | None) -> str:
    sources = [] if device is None else [normalize_identity(device)]
    if connector is not None:
        sources.append(normalize_identity(connector.identity))
    elif transport is not None:
        # Legacy transports without an identity fall back to the device.
        identity = getattr(transport, "identity", None)
        if identity is not None or device is None:
            sources.append(normalize_identity(identity))
    if not sources:
        raise ValueError("Provide a device address, a connector or a transport")
    if len(set(sources)) != 1:
        raise ValueError("Device address and connector identity differ")
    return sources[0]


class _Link:
    """One connection attempt; callbacks from an earlier link are ignored."""

    lost = False


class _Registration:
    """One notification subscription on a link.

    Invalidated before unsubscribing, on a failed subscribe and on link loss,
    so a callback left registered by an earlier subscription on the same link
    can never reach the cache or a later handshake.
    """

    def __init__(self, link: _Link) -> None:
        self.link = link


class ReadOnlyClient:
    """Connect, subscribe and register a session; never send settings or actions.

    Pass an address or platform device for the local Bleak link, ``connector=``
    for any other byte link, or ``transport=`` for a complete transport.
    """

    def __init__(
        self,
        device: object = None,
        *,
        transport: Transport | None = None,
        connector: Connector | None = None,
        command_hooks: CommandHooks | None = None,
        operation_timeout: float = 20.0,
        handshake_timeout: float = 8.0,
        settle: float = 3.0,
        stale_after: float = 5.0,
        subscription_settle: float = 0.3,
        event_sink: EventSink | None = None,
    ) -> None:
        if (
            min(operation_timeout, handshake_timeout, stale_after) <= 0
            or settle < 0
            or subscription_settle < 0
        ):
            raise ValueError("Timeouts must be positive; settle must be nonnegative")
        if transport is not None and (
            connector is not None or command_hooks is not None
        ):
            raise ValueError("Pass connector and command_hooks only without transport")
        self.address = _identity(device, transport, connector)
        self.transport: Transport = (
            transport
            if transport is not None
            else self._default_transport(device, connector, command_hooks)
        )
        self.operation_timeout = operation_timeout
        self.handshake_timeout = handshake_timeout
        self.settle = settle
        self.subscription_settle = subscription_settle
        self.stale_after = stale_after
        self.state = DeviceState()
        self.packets: deque[Packet] = deque(maxlen=128)
        self._frames = FrameBuffer()
        self._changed = asyncio.Event()
        self._ack = asyncio.Event()
        self._lock = asyncio.Lock()
        self._subscribed = False
        self._handshake_armed = False
        self._link: _Link | None = None
        self._registration: _Registration | None = None
        # True once this client, as owner, asked the transport to connect;
        # only then may cleanup close the (possibly shared) link.
        self._opened = False
        self._lease = AddressLease(self.address)
        self._session_error: ProtocolCommandError | None = None
        self.event_sink = event_sink

    @staticmethod
    def _default_transport(
        device: object, connector: Connector | None, hooks: CommandHooks | None
    ) -> Transport:
        return BleakTransport(device, connector=connector, hooks=hooks)

    async def _call[T](self, awaitable: Awaitable[T]) -> T:
        try:
            return await bounded(awaitable, self.operation_timeout)
        except Exception as exc:
            raise SessionError(f"Bluetooth operation failed: {exc}") from exc

    def _signal(self) -> None:
        self.state.revision += 1
        self._changed.set()

    def _disconnected(self) -> None:
        self._registration = None
        self.state.connected = False
        self.state.session_ready = False
        self._signal()

    def _wants_event(self, kind: str) -> bool:
        if not self.event_sink:
            return False
        owner = getattr(self.event_sink, "__self__", None)
        accepts = getattr(owner, "accepts", None)
        try:
            return True if accepts is None else bool(accepts(kind))
        except OBSERVER_ERRORS:
            return False

    def _emit(self, kind: str, details: dict[str, Any]) -> None:
        # A transcript observer must never drop telemetry or acknowledgments.
        if self.event_sink is not None and self._wants_event(kind):
            with suppress(*OBSERVER_ERRORS):
                self.event_sink(kind, details)

    def _notification(self, data: bytes) -> None:
        self._emit("ble.incoming_chunk", {"hex": bytes(data).hex()})
        error: FrameError | None = None
        try:
            packets = self._frames.feed(data)
        except FrameError as exc:
            # Frames validated before the corruption are real observations;
            # the corruption is still reported for this notification.
            packets, error = exc.packets, exc
        self._packets(packets)
        if error is not None:
            self._emit("ble.decode_error", {"error": str(error)})
            self.state.decode_errors += 1
            self.state.last_error = str(error)
            self._signal()

    def _packets(self, packets: list[Packet]) -> None:
        for packet in packets:
            self._record_packet(packet)
            self.packets.append(packet)
            error = protocol_error(packet)
            if error is not None and not self.state.session_ready:
                self._session_error = ProtocolCommandError(error, 8100)
                self._ack.set()  # Wake the handshake waiter; not a successful ACK.
            if (
                packet.device,
                packet.function,
                packet.command,
                packet.marker,
                packet.payload,
            ) == (2, 7, 8100, 0xC1, b"") and self._handshake_armed:
                # Accepted only once the handshake can have reached the machine.
                self._ack.set()
            try:
                self.state.apply(packet)
            except ValueError as exc:
                self.state.decode_errors += 1
                self.state.last_error = str(exc)
            self._packet_received(packet)
            self._changed.set()

    def _record_packet(self, packet: Packet) -> None:
        if not self._wants_event("ble.packet"):
            return
        details: dict[str, Any] = {
            "device": packet.device,
            "function": packet.function,
            "command": packet.command,
            "marker": packet.marker,
            "payload_hex": packet.payload.hex(),
            "decoder_version": DECODER_VERSION,
        }
        try:
            details["values"] = decode_values(packet)
        except ValueError as exc:
            details["decode_error"] = str(exc)
        self._emit("ble.packet", details)

    def _packet_received(self, packet: Packet) -> None:
        """Subclass hook after decoding; must not block notification delivery."""

    def _owns(self) -> bool:
        return _owners.get(self.address) is self and self._lease.held

    def _registered_notifications(
        self, registration: _Registration
    ) -> Callable[[bytes], None]:
        """Deliver notifications only while ``registration`` is current.

        A callback queued by an earlier connection, or left registered by an
        earlier subscription on this one, must not reach the next handshake or
        its cache.
        """

        def notification(data: bytes) -> None:
            link = registration.link
            if (
                self._registration is registration
                and self._link is link
                and not link.lost
            ):
                self._notification(data)

        return notification

    def _new_registration(self, link: _Link) -> _Registration:
        """Start a registration whose handshake and framing state owe nothing
        to earlier ones: bytes of a frame begun under an earlier registration
        can never be completed by, or corrupt, this one's notifications."""
        registration = self._registration = _Registration(link)
        self._frames = FrameBuffer()
        self._ack.clear()
        self._handshake_armed = False
        self._session_error = None
        return registration

    def _link_callback(self, link: _Link) -> Callable[[], None]:
        def lost() -> None:
            if self._link is link and not link.lost:
                link.lost = True
                self._disconnected()

        return lost

    async def connect(self) -> None:
        async with self._lock:
            if self.state.connected:
                return
            if self.address in _owners and _owners[self.address] is not self:
                raise SessionError("Another ReadOnlyClient owns this address")
            _owners[self.address] = self
            self.state = DeviceState(revision=self.state.revision + 1)
            self._frames = FrameBuffer()
            self.packets.clear()
            self._ack.clear()
            self._handshake_armed = False
            self._session_error = None
            self._subscribed = False
            link = self._link = _Link()
            try:
                # An explicit reconnect after link loss keeps its held lease.
                if not self._lease.held:
                    self._lease.acquire()
                self._opened = True
                await self._call(self.transport.connect(self._link_callback(link)))
                if link.lost:
                    raise SessionError("Connection lost while connecting")
                self.state.connected = True
                self._signal()
            except BaseException:
                await self._cleanup()
                raise

    async def subscribe(self) -> None:
        async with self._lock:
            if not self.state.connected:
                raise SessionError("Connect before subscribing")
            if not self._subscribed:
                assert self._link is not None  # set whenever connected
                registration = self._new_registration(self._link)
                try:
                    await self._call(
                        self.transport.subscribe(
                            self._registered_notifications(registration)
                        )
                    )
                except BaseException:
                    if self._registration is registration:
                        self._registration = None
                    raise
                self._subscribed = True

    async def open_session(self) -> None:
        """Fixed session-registration write, wait for ACK, then allow initialization.

        Read-only means no settings or actuation; opening a session can beep.
        """
        async with self._lock:
            if not self.state.connected or not self._subscribed:
                raise SessionError("Connect and subscribe before opening a session")
            if self.state.session_ready:
                return
            registration = self._registration
            if registration is None:
                raise SessionError("Subscription lost; subscribe again")
            self._ack.clear()
            self._handshake_armed = False
            self._session_error = None
            # Allow the peripheral to finish enabling its notification subscription.
            await asyncio.sleep(self.subscription_settle)
            await self._call(self._send_handshake())
            deadline = time.monotonic() + self.handshake_timeout
            while not self._ack.is_set() and self._session_error is None:
                if not self.state.connected or self._registration is not registration:
                    raise SessionError("Disconnected during handshake")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SessionError(
                        "No handshake acknowledgment; session not established"
                    )
                await wait_set(self._ack, min(remaining, 0.1))
            if self._session_error is not None:
                raise self._session_error
            await asyncio.sleep(self.settle)
            if self._session_error is not None:
                raise self._session_error
            if not self.state.connected or self._registration is not registration:
                raise SessionError("Disconnected during initialization")
            self.state.session_ready = True
            self._signal()

    async def _send_handshake(self) -> None:
        """Arm the handshake ACK only when the handshake can reach the machine.

        A matching notification during the settle or a pre-write hook is
        stale. Guarded transports arm before the final chunk, so an ACK
        delivered synchronously by the write still counts; other transports
        arm immediately before their call.
        """

        def arm() -> None:
            self._ack.clear()
            self._handshake_armed = True

        transport = self.transport
        if not getattr(transport, "transmit_hook_before_final_chunk", False):
            arm()
            await transport.open_session()
            return
        previous = getattr(transport, "on_transmit", None)
        setattr(transport, "on_transmit", arm)  # noqa: B010
        try:
            await transport.open_session()
        finally:
            setattr(transport, "on_transmit", previous)  # noqa: B010

    async def read_information(self, name: str) -> str | None:
        async with self._lock:
            if not self.state.connected:
                raise SessionError("Not connected")
            return await self._call(self.transport.read_information(name))

    def snapshot(self) -> dict[str, Any]:
        return self.state.snapshot(self.stale_after)

    async def wait_for_update(
        self, after_revision: int, timeout: float = 5.0
    ) -> dict[str, Any]:
        """Coalesced latest state, not an unbounded per-packet queue."""
        deadline = time.monotonic() + timeout
        while self.state.revision <= after_revision:
            self._changed.clear()
            # No await between revision check and clear: callbacks cannot interleave.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not await wait_set(self._changed, remaining):
                break
        return self.snapshot()

    async def unsubscribe(self) -> None:
        async with self._lock:
            # No command may rely on notifications once disabling has begun,
            # even if the call below fails or its caller is cancelled. The next
            # subscribe re-registers, and open_session handshakes again.
            if self.state.session_ready:
                self.state.session_ready = False
                self._signal()
            # Late callbacks of this registration are ignored from here on,
            # even if stop_notify fails and leaves them registered.
            self._registration = None
            if self._subscribed:
                self._subscribed = False
                await self._call(self.transport.unsubscribe())

    async def _cleanup(self) -> None:
        try:
            # Never close a link this client did not open as owner: a rival
            # rejected over a shared connector must not disturb the incumbent.
            if self._opened:
                await self._call(self.transport.disconnect())
        finally:
            self._opened = False
            self._link = None
            self._registration = None
            self._subscribed = False
            self._disconnected()
            if _owners.get(self.address) is self:
                del _owners[self.address]
            self._lease.release()

    async def disconnect(self) -> None:
        async with self._lock:
            await self._cleanup()

    async def __aenter__(self) -> Self:
        try:
            await self.connect()
            await self.subscribe()
            await self.open_session()
        except BaseException:
            await self.disconnect()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.disconnect()
