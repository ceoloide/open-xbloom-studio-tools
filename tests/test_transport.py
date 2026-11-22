import asyncio
import importlib.util
import inspect
import struct
import sys
import unittest
from unittest import mock

from tests import fake_bleak
from tests.frames import frame
from xbloom_studio.connector import Connector, check_write_size, normalize_identity
from xbloom_studio.simulator import ScriptedConnector
from xbloom_studio.transport import (
    COMMAND,
    HANDSHAKE,
    SERVICE,
    STATUS,
    BleakConnector,
    BleakTransport,
    ControlTransport,
    GuardedFrameTransport,
    GuardedTransport,
    discover,
    validate_control_frame,
    validate_handshake,
)

ADDRESS = "00:00:5E:00:53:01"
HANDSHAKE_HEX = "580101a41f1400000001b900000001000000bdd1"
# A 53-byte recipe upload: 12 envelope bytes and 41 content bytes.
UPLOAD = frame(8004, bytes(range(41)))
INVALID_FRAMES = {
    "not allowlisted": frame(8101),
    "handshake": bytes.fromhex(HANDSHAKE_HEX),
    "function 2 on a function 1 command": frame(8022, function=2),
    "function 1 on a function 2 command": frame(11511, b"\0" * 4),
    "incoming device": frame(8022, device=2),
    "incoming marker": frame(8022, marker=0xC1),
    "corrupt checksum": frame(8022)[:-1] + b"\0",
    "truncated": frame(8022)[:-1],
}


class Hooks:
    def __init__(self, log, *, fail=None, stall=False):
        self.log = log
        self.fail = fail
        self.stall = stall

    async def before_write(self, packet, frame_bytes):
        self.log.append(("before", packet.command, frame_bytes))
        if self.stall:
            await asyncio.sleep(10)
        if self.fail:
            raise self.fail

    def write_completed(self, packet):
        self.log.append(("completed", packet.command))

    def write_failed(self, packet, error):
        self.log.append(("failed", packet.command, type(error).__name__))


class Recording(ScriptedConnector):
    """Scripted link that also records chunk writes in a shared log."""

    def __init__(self, log, **kwargs):
        super().__init__(responder=lambda packet: [], **kwargs)
        self.log = log

    async def write(self, data):
        self.log.append(("write", bytes(data)))
        await super().write(data)


class IdentityTests(unittest.TestCase):
    def test_aliases_normalize_to_one_owner_key(self):
        device = fake_bleak.Device("00-00-5e-00-53-01")
        for alias in (
            ADDRESS,
            ADDRESS.lower(),
            "00-00-5E-00-53-01",
            device,
            f" {ADDRESS} ",
        ):
            self.assertEqual(normalize_identity(alias), ADDRESS)
        self.assertEqual(normalize_identity("ab-cd"), "AB-CD")  # not a hardware address
        uuid = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
        self.assertEqual(normalize_identity(uuid), uuid.upper())
        for missing in (None, "", "  ", fake_bleak.Device(None)):
            with self.assertRaises(ValueError):
                normalize_identity(missing)

    def test_write_size_bounds(self):
        self.assertEqual(check_write_size(20), 20)
        for value in (0, 513, 2.0, True, "20"):
            with self.assertRaises(ValueError):
                check_write_size(value)


class FixedFrameTests(unittest.TestCase):
    def test_handshake_and_control_validation(self):
        self.assertEqual(HANDSHAKE.hex(), HANDSHAKE_HEX)
        self.assertEqual(validate_handshake(HANDSHAKE).command, 8100)
        for bad in (frame(8100, bytes.fromhex("b900000001000001")), frame(8022)):
            with self.assertRaises(ValueError):
                validate_handshake(bad)
        self.assertEqual(validate_control_frame(frame(8022)).command, 8022)
        self.assertEqual(
            validate_control_frame(frame(11510, b"\0\0\1", function=2)).function, 2
        )
        for name, bad in INVALID_FRAMES.items():
            with self.subTest(name), self.assertRaises(ValueError):
                validate_control_frame(bad)


class ScriptedConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_contract(self):
        link = ScriptedConnector(information={"model": "SIM"})
        self.assertIsInstance(link, Connector)
        with self.assertRaises(ConnectionError):
            await link.write(b"x")
        with self.assertRaises(ConnectionError):
            await link.start_notify(lambda data: None)
        drops = []
        await link.connect(lambda: drops.append(1))
        with self.assertRaises(ConnectionError):
            await link.connect(lambda: None)
        with self.assertRaises(ValueError):
            await link.write(b"x" * 21)
        with self.assertRaises(ValueError):
            await link.write(b"")
        with self.assertRaises(ValueError):
            await link.read_information("unknown")
        self.assertEqual(await link.read_information("model"), "SIM")
        self.assertIsNone(await link.read_information("serial"))
        link.notify(b"before subscribing")
        self.assertEqual(link.dropped, [b"before subscribing"])
        link.drop()
        await link.disconnect()
        self.assertEqual(drops, [1])  # once, even after an explicit disconnect
        self.assertFalse(link.is_connected)
        link.connect_error = TimeoutError("no link")
        with self.assertRaises(TimeoutError):
            await link.connect(lambda: None)
        self.assertEqual(
            link.calls, ["start_notify", "connect", "connect", "disconnect", "connect"]
        )

    async def test_failure_injection_raises_after_bytes_reach_link(self):
        link = ScriptedConnector()
        link.fail_commands[8022] = OSError("radio")
        await link.connect(lambda: None)
        with self.assertRaises(OSError):
            await link.write(frame(8022))
        self.assertEqual(link.writes, [frame(8022)])
        self.assertEqual([p.command for p in link.sent], [8022])


class BleakConnectorTests(unittest.IsolatedAsyncioTestCase):
    def patch(self, **kwargs):
        module = fake_bleak.module(**kwargs)
        patcher = mock.patch.dict(sys.modules, {"bleak": module})
        patcher.start()
        self.addCleanup(patcher.stop)
        return module

    async def test_connect_by_address_and_by_device(self):
        device = fake_bleak.Device(ADDRESS)
        module = self.patch(found=device)
        link = BleakConnector(ADDRESS.lower(), timeout=4)
        self.assertEqual((link.identity, link.write_size), (ADDRESS, 20))
        self.assertIsInstance(link, Connector)
        self.assertFalse(link.is_connected)
        drops = []
        await link.connect(lambda: drops.append(1))
        self.assertEqual(module.BleakScanner.find_calls, [(ADDRESS.lower(), 4)])
        client = fake_bleak.Client.instances[-1]
        self.assertIs(client.device, device)
        self.assertEqual(client.timeout, 4)
        self.assertTrue(link.is_connected)
        await link.disconnect()
        await link.disconnect()
        self.assertEqual(drops, [1])
        direct = BleakConnector(device)
        await direct.connect(lambda: None)
        self.assertIs(fake_bleak.Client.instances[-1].device, device)
        self.assertEqual(len(module.BleakScanner.find_calls), 1)

    async def test_connect_failures(self):
        self.patch(found=None)
        with self.assertRaisesRegex(ConnectionError, "not advertising"):
            await BleakConnector(ADDRESS).connect(lambda: None)
        self.patch(found=fake_bleak.Device(ADDRESS))
        fake_bleak.Client.missing = (STATUS,)
        link = BleakConnector(ADDRESS)
        with self.assertRaisesRegex(ConnectionError, "characteristic missing"):
            await link.connect(lambda: None)
        await link.disconnect()
        self.assertFalse(link.is_connected)

    async def test_io(self):
        self.patch(found=fake_bleak.Device(ADDRESS))
        link = BleakConnector(ADDRESS)
        with self.assertRaises(ConnectionError):
            await link.write(b"x")
        await link.stop_notify()  # no link: nothing to stop
        await link.connect(lambda: None)
        client = fake_bleak.Client.instances[-1]
        received = []
        await link.start_notify(received.append)
        client.notify(object(), bytearray(b"\x58\x02"))
        self.assertEqual(received, [b"\x58\x02"])
        self.assertIs(type(received[0]), bytes)
        await link.write(b"x" * 20)
        self.assertEqual(client.writes, [(COMMAND, b"x" * 20, False)])
        with self.assertRaises(ValueError):
            await link.write(b"x" * 21)
        client.values["00002a24-0000-1000-8000-00805f9b34fb"] = b"Studio\0\0"
        self.assertEqual(await link.read_information("model"), "Studio")
        client.values["00002a29-0000-1000-8000-00805f9b34fb"] = b"\0"
        self.assertIsNone(await link.read_information("manufacturer"))
        fake_bleak.Client.missing = ()
        client.services.missing = {"00002a25-0000-1000-8000-00805f9b34fb"}
        self.assertIsNone(await link.read_information("serial"))
        with self.assertRaises(ValueError):
            await link.read_information("arbitrary_uuid")
        await link.stop_notify()
        self.assertIn(("stop_notify", STATUS), client.calls)

    async def test_discovery_filters_and_validates(self):
        named = fake_bleak.Device(ADDRESS, "device name")
        module = self.patch(
            advertised={
                "a": (named, fake_bleak.Advertisement("xBloom Studio")),
                "b": (fake_bleak.Device("x", "XBLOOM-1"), fake_bleak.Advertisement()),
                "c": (
                    fake_bleak.Device("y"),
                    fake_bleak.Advertisement(None, [SERVICE.upper()]),
                ),
                "d": (fake_bleak.Device("z", "Headphones"), fake_bleak.Advertisement()),
                "e": (fake_bleak.Device("w"), fake_bleak.Advertisement()),
            }
        )
        found = await discover(timeout=2)
        self.assertEqual([item["address"] for item in found], [ADDRESS, "x", "y"])
        self.assertEqual(found[0]["name"], "xBloom Studio")
        self.assertEqual(found[1]["name"], "XBLOOM-1")
        self.assertEqual(module.BleakScanner.discover_calls, [(2, True)])
        with self.assertRaises(ValueError):
            await discover(timeout=0)

    async def test_missing_optional_dependency(self):
        with mock.patch.dict(sys.modules, {"bleak": None}):
            with self.assertRaisesRegex(RuntimeError, r"xbloom-studio\[ble\]"):
                await discover()
            with self.assertRaisesRegex(RuntimeError, "ble"):
                await BleakConnector(ADDRESS).connect(lambda: None)

    @unittest.skipUnless(importlib.util.find_spec("bleak"), "bleak not installed")
    def test_installed_bleak_accepts_the_calls_used(self):
        import bleak

        client = inspect.signature(bleak.BleakClient)
        self.assertIn("timeout", client.parameters)
        self.assertIn("disconnected_callback", client.parameters)
        for method, parameters in (
            ("connect", ()),
            ("disconnect", ()),
            ("start_notify", ("char_specifier", "callback")),
            ("stop_notify", ("char_specifier",)),
            ("write_gatt_char", ("char_specifier", "data", "response")),
            ("read_gatt_char", ("char_specifier",)),
        ):
            signature = inspect.signature(getattr(bleak.BleakClient, method))
            for name in parameters:
                self.assertIn(name, signature.parameters, method)
        self.assertIn(
            "return_adv", inspect.signature(bleak.BleakScanner.discover).parameters
        )
        self.assertIn(
            "timeout",
            inspect.signature(bleak.BleakScanner.find_device_by_address).parameters,
        )


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def links(self):
        """One scripted and one Bleak-backed link, both connected and subscribed."""
        patcher = mock.patch.dict(
            sys.modules, {"bleak": fake_bleak.module(found=fake_bleak.Device(ADDRESS))}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        scripted = ScriptedConnector(ADDRESS, responder=lambda packet: [])
        bleak_link = BleakConnector(ADDRESS)
        result = []
        for connector in (scripted, bleak_link):
            transport = ControlTransport(connector=connector)
            await transport.connect(lambda: None)
            await transport.subscribe(lambda data: None)
            result.append(transport)
        return result

    @staticmethod
    def chunks(transport):
        link = transport.connector
        if isinstance(link, ScriptedConnector):
            return list(link.writes)
        return [data for _, data, _ in fake_bleak.Client.instances[-1].writes]

    async def test_handshake_requires_subscription_and_is_one_fixed_write(self):
        for transport in await self.links():
            transport._subscribed = False
            with self.assertRaisesRegex(RuntimeError, "Subscribe"):
                await transport.open_session()
            self.assertEqual(self.chunks(transport), [])
            await transport.subscribe(lambda data: None)
            await transport.open_session()
            self.assertEqual(self.chunks(transport), [bytes.fromhex(HANDSHAKE_HEX)])

    async def test_segmentation_byte_transcript_is_identical_on_both_links(self):
        transcripts = []
        for transport in await self.links():
            await transport._write_frame(UPLOAD)
            chunks = self.chunks(transport)
            self.assertEqual([len(chunk) for chunk in chunks], [20, 20, 13])
            self.assertEqual(b"".join(chunks), UPLOAD)
            transcripts.append(chunks)
        self.assertEqual(transcripts[0], transcripts[1])

    async def test_connector_write_size_sets_segments(self):
        link = ScriptedConnector(responder=lambda packet: [], write_size=7)
        transport = ControlTransport(connector=link)
        await transport.connect(lambda: None)
        await transport.subscribe(lambda data: None)
        await transport._write_frame(UPLOAD)
        self.assertEqual([len(c) for c in link.writes], [7] * 7 + [4])
        self.assertEqual([p.command for p in link.sent], [8004])

    async def test_invalid_outbound_frames_never_reach_either_link(self):
        for transport in await self.links():
            log = []
            transport.hooks = Hooks(log)
            for name, bad in INVALID_FRAMES.items():
                with self.subTest(name), self.assertRaises(ValueError):
                    await transport._write_frame(bad)
            self.assertEqual(self.chunks(transport), [])
            self.assertEqual(log, [])

    async def test_read_only_transport_has_no_frame_write(self):
        transport = BleakTransport(connector=ScriptedConnector())
        self.assertFalse(hasattr(transport, "_write_frame"))

    async def test_hook_guard_and_write_order(self):
        log = []
        link = Recording(log)
        transport = ControlTransport(connector=link, hooks=Hooks(log))
        transport.event_sink = lambda kind, data: log.append((kind, data["hex"]))
        await transport.connect(lambda: None)
        await transport.subscribe(lambda data: None)
        await transport._write_frame_guarded(
            frame(8022), command_guard=lambda: log.append(("guard",))
        )
        self.assertEqual(
            log,
            [
                ("before", 8022, frame(8022)),
                ("ble.outgoing_chunk", frame(8022).hex()),
                ("guard",),
                ("write", frame(8022)),
                ("completed", 8022),
            ],
        )

    async def test_hook_failure_blocks_writes_except_bounded_stop(self):
        log = []
        link = Recording(log)
        transport = ControlTransport(
            connector=link, hooks=Hooks(log, fail=OSError("disk"))
        )
        await transport.connect(lambda: None)
        await transport.subscribe(lambda data: None)
        with self.assertRaises(OSError):
            await transport._write_frame(frame(4506, b"\0" * 20))
        with self.assertRaises(OSError):
            await transport.open_session()
        self.assertEqual(link.writes, [])
        for stop in (4507, 3505, 40519):
            await transport._write_frame(frame(stop))
        transport.hooks = Hooks(log, stall=True)
        started = asyncio.get_running_loop().time()
        await transport._write_frame(frame(40519))
        self.assertLess(asyncio.get_running_loop().time() - started, 1)
        self.assertEqual([p.command for p in link.sent], [4507, 3505, 40519, 40519])
        self.assertEqual(log[-1], ("completed", 40519))

    async def test_transmit_hook_runs_immediately_before_final_chunk(self):
        # S6: the ACK boundary is the chunk that completes the frame.
        for payload, expected in ((bytes(range(41)), [2]), (b"", [0])):
            link = ScriptedConnector(responder=lambda packet: [])
            transport = ControlTransport(connector=link)
            seen = []
            writes = link.writes
            await transport.connect(lambda: None)
            await transport.subscribe(lambda data: None)
            await transport._write_frame_guarded(
                frame(8004, payload),
                command_guard=lambda s=seen, w=writes: s.append(("guard", len(w))),
                on_transmit=lambda s=seen, w=writes: s.append(len(w)),
            )
            self.assertEqual(seen, [("guard", 0), *expected])

    async def test_stop_journal_deadline_never_waits_for_resisting_hook(self):
        # S5: a hook that resists cancellation runs on detached; the stop is
        # written within its own bound and the hook's late failure is retrieved.
        finished = asyncio.Event()

        class Resisting(Hooks):
            async def before_write(self, packet, frame_bytes):
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.3)
                    finished.set()
                    raise OSError("late journal") from None

        log = []
        link = Recording(log)
        transport = ControlTransport(connector=link, hooks=Resisting(log))
        await transport.connect(lambda: None)
        await transport.subscribe(lambda data: None)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(transport._write_frame(frame(4507)), 0.2)
        self.assertLess(loop.time() - started, 0.2)
        self.assertEqual([p.command for p in link.sent], [4507])
        self.assertEqual(len(transport._journals), 1)
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0)
        self.assertEqual(transport._journals, set())

        class Broken(Hooks):
            def before_write(self, packet, frame_bytes):
                raise OSError("not even a coroutine")

        transport.hooks = Broken(log)
        await transport._write_frame(frame(3505))
        self.assertEqual([p.command for p in link.sent], [4507, 3505])

    async def test_observer_and_post_write_hook_failures_never_fail_writes(self):
        # S5: transcript observers and post-write hooks cannot drop a frame,
        # turn a sent frame into a failure or hide the original write error.
        class Faulty(Hooks):
            def write_completed(self, packet):
                raise RuntimeError("observer")

            def write_failed(self, packet, error):
                raise RuntimeError("observer")

        def sink(kind, data):
            raise RuntimeError("transcript")

        link = Recording([])
        transport = ControlTransport(connector=link, hooks=Faulty([]))
        transport.event_sink = sink
        await transport.connect(lambda: None)
        await transport.subscribe(lambda data: None)
        for code in (4507, 8022):
            await transport._write_frame(frame(code))
        link.fail_commands[8022] = TimeoutError("radio")
        with self.assertRaisesRegex(TimeoutError, "radio"):
            await transport._write_frame(frame(8022))
        self.assertEqual([p.command for p in link.sent], [4507, 8022, 8022])

    async def test_guard_or_link_failure_reports_write_failed(self):
        log = []
        link = Recording(log)
        transport = ControlTransport(connector=link, hooks=Hooks(log))
        await transport.connect(lambda: None)
        with self.assertRaisesRegex(ConnectionError, "subscribed"):
            await transport._write_frame(frame(8022))
        await transport.subscribe(lambda data: None)

        def guard():
            raise RuntimeError("context changed")

        with self.assertRaises(RuntimeError):
            await transport._write_frame_guarded(frame(8022), command_guard=guard)
        link.fail_commands[8022] = TimeoutError()
        with self.assertRaises(TimeoutError):
            await transport._write_frame(frame(8022))
        self.assertEqual(
            [entry for entry in log if entry[0] == "failed"],
            [
                ("failed", 8022, "ConnectionError"),
                ("failed", 8022, "RuntimeError"),
                ("failed", 8022, "TimeoutError"),
            ],
        )
        self.assertEqual(link.writes, [frame(8022)])

    async def test_lifecycle_and_construction(self):
        link = ScriptedConnector("sim-1")
        transport = BleakTransport(connector=link)
        self.assertEqual((transport.identity, transport.device), ("sim-1", "sim-1"))
        await transport.unsubscribe()  # not subscribed: no connector call
        await transport.connect(lambda: None)
        await transport.subscribe(lambda data: None)
        await transport.subscribe(lambda data: None)
        self.assertIsNone(await transport.read_information("serial"))
        with self.assertRaises(ValueError):
            await transport.read_information("arbitrary")
        await transport.unsubscribe()
        await transport.disconnect()
        self.assertEqual(
            link.calls, ["connect", "start_notify", "stop_notify", "disconnect"]
        )
        with self.assertRaises(ValueError):
            BleakTransport()
        with self.assertRaises(TypeError):
            BleakTransport(connector=object())
        with self.assertRaises(ValueError):
            BleakTransport(connector=ScriptedConnector(write_size=600))
        self.assertIsInstance(BleakTransport(ADDRESS).connector, BleakConnector)
        self.assertEqual(ControlTransport(ADDRESS, timeout=3).connector.timeout, 3)

    async def test_disconnect_resets_subscription_even_if_link_fails(self):
        class Failing(ScriptedConnector):
            async def disconnect(self):
                raise OSError("adapter")

        transport = BleakTransport(connector=Failing())
        await transport.connect(lambda: None)
        await transport.subscribe(lambda data: None)
        with self.assertRaises(OSError):
            await transport.disconnect()
        self.assertFalse(transport._subscribed)

    async def test_floats_in_frames_use_little_endian(self):
        # Guards the documented byte order of a float payload end to end.
        link = ScriptedConnector(responder=lambda packet: [])
        transport = ControlTransport(connector=link)
        await transport.connect(lambda: None)
        await transport.subscribe(lambda data: None)
        payload = struct.pack("<ff", 100.0, 5.0)
        await transport._write_frame(frame(8104, payload))
        self.assertEqual(link.writes[0][10:18].hex(), "0000c8420000a040")


class LifecycleRoundTwoTests(unittest.IsolatedAsyncioTestCase):
    async def connected(self, link, **kwargs):
        transport = ControlTransport(connector=link, **kwargs)
        await transport.connect(lambda: None)
        await transport.subscribe(lambda data: None)
        return transport

    async def test_explicit_cancelled_error_from_observers_is_suppressed(self):
        # S5: synchronous observers cannot receive a task cancellation, so a
        # CancelledError they raise is their own and never fails a write.
        class Cancelling(Hooks):
            def write_completed(self, packet):
                super().write_completed(packet)
                raise asyncio.CancelledError()

            def write_failed(self, packet, error):
                super().write_failed(packet, error)
                raise asyncio.CancelledError()

        def sink(kind, data):
            raise asyncio.CancelledError()

        log = []
        link = Recording(log)
        transport = await self.connected(link, hooks=Cancelling(log))
        transport.event_sink = sink
        for code in (4507, 8022):
            await transport._write_frame(frame(code))
        link.fail_commands[8022] = TimeoutError("radio")
        with self.assertRaisesRegex(TimeoutError, "radio"):
            await transport._write_frame(frame(8022))
        self.assertEqual([p.command for p in link.sent], [4507, 8022, 8022])
        self.assertEqual(log[-1], ("failed", 8022, "TimeoutError"))

    async def test_real_cancellation_at_awaited_write_still_propagates(self):
        # S5: only synchronous observer errors are suppressed.
        class Slow(Recording):
            async def write(self, data):
                await asyncio.sleep(10)

        log = []
        transport = await self.connected(Slow(log), hooks=Hooks(log))
        writing = asyncio.create_task(transport._write_frame(frame(8022)))
        await asyncio.sleep(0.01)
        writing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await writing
        self.assertEqual(log[-1], ("failed", 8022, "CancelledError"))

    async def test_observer_runs_before_guard_and_ack_arming(self):
        # S6: each chunk's observer runs before the first-byte guard and the
        # final-chunk arming, so neither can be invalidated by the observer;
        # the transcript stays byte-for-byte identical to the writes.
        log = []
        link = Recording(log)
        transport = await self.connected(link)
        transport.event_sink = lambda kind, data: log.append((kind, data["hex"]))
        raw = frame(8004, bytes(range(20)))
        await transport._write_frame_guarded(
            raw,
            command_guard=lambda: log.append(("guard",)),
            on_transmit=lambda: log.append(("arm",)),
        )
        first, last = raw[:20], raw[20:]
        self.assertEqual(
            log,
            [
                ("ble.outgoing_chunk", first.hex()),
                ("guard",),
                ("write", first),
                ("ble.outgoing_chunk", last.hex()),
                ("arm",),
                ("write", last),
            ],
        )
        outgoing = [h for kind, *h in log if kind == "ble.outgoing_chunk"]
        self.assertEqual(bytes.fromhex("".join(h[0] for h in outgoing)), raw)

    async def test_guard_rejection_after_observer_marks_chunk_withheld(self):
        # S6: an update during the observer is seen by the guard; the observed
        # chunk is reported withheld, so the transcript never claims a write.
        log = []
        link = Recording(log)
        transport = await self.connected(link, hooks=Hooks(log))
        changed = []
        transport.event_sink = lambda kind, data: (
            log.append((kind, data["hex"], data.get("error"))),
            changed.append(True),
        )

        def guard():
            if changed:
                raise RuntimeError("context changed")

        with self.assertRaisesRegex(RuntimeError, "context changed"):
            await transport._write_frame_guarded(frame(8022), command_guard=guard)
        self.assertEqual(link.writes, [])
        self.assertEqual(
            log[1:],
            [
                ("ble.outgoing_chunk", frame(8022).hex(), None),
                (
                    "ble.outgoing_chunk_withheld",
                    frame(8022).hex(),
                    "RuntimeError: context changed",
                ),
                ("failed", 8022, "RuntimeError"),
            ],
        )

    async def test_interrupted_unsubscribe_blocks_writes_and_resubscribe_recovers(
        self,
    ):
        # S3: writes stop being admitted before stop_notify; an interrupted
        # stop_notify is retried before the next registration.
        class Stuck(ScriptedConnector):
            fail = True

            async def stop_notify(self):
                await super().stop_notify()
                if self.fail:
                    raise OSError("radio")

        link = Stuck(responder=lambda packet: [])
        transport = await self.connected(link)
        with self.assertRaisesRegex(OSError, "radio"):
            await transport.unsubscribe()
        with self.assertRaisesRegex(ConnectionError, "subscribed"):
            await transport._write_frame(frame(8022))
        link.fail = False
        await transport.subscribe(lambda data: None)
        await transport._write_frame(frame(8022))
        self.assertEqual(
            link.calls,
            ["connect", "start_notify", "stop_notify", "stop_notify", "start_notify"],
        )
        self.assertEqual([p.command for p in link.sent], [8022])


class GuardedTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_guarded_transport_protocols_distinguish_readonly_and_control(self):
        readonly = BleakTransport(connector=ScriptedConnector("ro"))
        control = ControlTransport(connector=ScriptedConnector("ctrl"))
        self.assertIsInstance(readonly, GuardedTransport)
        self.assertNotIsInstance(readonly, GuardedFrameTransport)
        self.assertIsInstance(control, GuardedTransport)
        self.assertIsInstance(control, GuardedFrameTransport)
        for attr in (
            "command_guard",
            "on_transmit",
            "on_chunk",
            "command_guard_after_journal",
            "transmit_hook_before_final_chunk",
        ):
            self.assertFalse(hasattr(readonly, attr), attr)
            self.assertFalse(hasattr(control, attr), attr)

    async def test_open_session_guarded_runs_on_transmit_before_final_chunk(self):
        link = ScriptedConnector(responder=lambda packet: [], write_size=8)
        transport = BleakTransport(connector=link)
        await transport.connect(lambda: None)
        await transport.subscribe(lambda data: None)
        seen: list[int] = []
        await transport.open_session_guarded(
            on_transmit=lambda w=link.writes, s=seen: s.append(len(w))
        )
        self.assertEqual(seen, [2])
        self.assertEqual(b"".join(link.writes), HANDSHAKE)

    async def test_failed_or_cancelled_guarded_write_does_not_leak_hooks(self):
        link = ScriptedConnector(responder=lambda packet: [])
        transport = ControlTransport(connector=link)
        await transport.connect(lambda: None)
        await transport.subscribe(lambda data: None)
        calls: list[str] = []

        def fail_guard() -> None:
            calls.append("fail_guard")
            raise RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            await transport._write_frame_guarded(
                frame(8022),
                command_guard=fail_guard,
                on_transmit=lambda: calls.append("fail_transmit"),
                on_chunk=lambda final: calls.append(f"fail_chunk:{final}"),
            )

        def cancel_guard() -> None:
            calls.append("cancel_guard")
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await transport._write_frame_guarded(
                frame(8022),
                command_guard=cancel_guard,
                on_transmit=lambda: calls.append("cancel_transmit"),
                on_chunk=lambda final: calls.append(f"cancel_chunk:{final}"),
            )

        await transport._write_frame(frame(8022))
        self.assertEqual(calls, ["fail_guard", "cancel_guard"])
        self.assertEqual([p.command for p in link.sent], [8022])


if __name__ == "__main__":
    unittest.main()
