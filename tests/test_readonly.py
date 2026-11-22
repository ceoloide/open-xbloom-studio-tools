import asyncio
import fcntl
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import aclosing
from pathlib import Path
from unittest import mock

from tests import fake_bleak
from tests.frames import ACK, f32, frame, identity_block, reply, u32
from xbloom_studio import (
    ProtocolCommandError,
    ReadOnlyClient,
    ScriptedConnector,
    SessionError,
    SimulatedStudio,
    Studio,
    capabilities,
    plan,
)
from xbloom_studio.client import bounded, wait_set
from xbloom_studio.ownership import AddressLease, LeaseBusyError, lease_directory
from xbloom_studio.transport import BleakTransport

REPO = str(Path(__file__).resolve().parents[1])
SCALE = reply(20501, struct.pack("<f", 0))
WATER = reply(40523, struct.pack("<f", 0))
INFO = reply(40521, identity_block(size=29))
CONTENDER = """
import tests, os, sys
from xbloom_studio.ownership import AddressLease, LeaseBusyError
key, inside, rounds = sys.argv[1], sys.argv[2], int(sys.argv[3])
print(flush=True)
sys.stdin.readline()
busy = 0
while rounds:
    lease = AddressLease(key)
    try:
        lease.acquire()
    except LeaseBusyError:
        busy += 1
        continue
    os.mkdir(inside)
    os.rmdir(inside)
    lease.release()
    rounds -= 1
print(busy)
"""


class FakeTransport:
    """Transport-level fake: replies to the handshake in 7-byte pieces."""

    def __init__(self, data=ACK + SCALE + WATER + INFO, fail=False):
        self.data = data
        self.fail = fail
        self.calls = []
        self.callback = None
        self.drop = None

    async def connect(self, on_disconnect):
        self.calls.append("connect")
        self.drop = on_disconnect
        if self.fail:
            raise ConnectionError("not reachable")

    async def disconnect(self):
        self.calls.append("disconnect")
        if self.drop:
            self.drop()

    async def subscribe(self, callback):
        self.calls.append("subscribe")
        self.callback = callback

    async def unsubscribe(self):
        self.calls.append("unsubscribe")

    async def open_session(self):
        self.calls.append("handshake")
        for offset in range(0, len(self.data), 7):
            self.callback(self.data[offset : offset + 7])

    async def read_information(self, name):
        self.calls.append("read_" + name)
        return "value"


def client(transport=None, device="test-device", **kwargs):
    return ReadOnlyClient(
        device,
        transport=transport or FakeTransport(),
        settle=0,
        handshake_timeout=0.05,
        subscription_settle=0,
        **kwargs,
    )


def scripted(identity="sim-readonly", **kwargs):
    link = ScriptedConnector(identity, **kwargs)
    return link, ReadOnlyClient(connector=link, settle=0, subscription_settle=0)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_client_connects_without_extra_optin(self):
        t = FakeTransport()
        c = ReadOnlyClient("test-device", transport=t, settle=0, subscription_settle=0)
        async with c:
            self.assertTrue(c.snapshot()["session_ready"])
        self.assertEqual(t.calls, ["connect", "subscribe", "handshake", "disconnect"])

    async def test_connector_session_writes_only_the_handshake(self):
        link, c = scripted()
        async with c:
            snapshot = c.snapshot()
            self.assertTrue(snapshot["session_ready"])
            self.assertEqual(
                snapshot["fields"]["serial_number"]["value"], "J15SIMULATED0"
            )
            self.assertEqual(await c.read_information("manufacturer"), "Simulator")
        self.assertEqual(link.writes, [frame(8100, bytes.fromhex("b900000001000000"))])
        self.assertEqual(link.calls, ["connect", "start_notify", "disconnect"])
        self.assertFalse(c.snapshot()["connected"])

    async def test_subscription_settles_before_handshake(self):
        link, c = scripted()
        c.subscription_settle = 0.02
        await c.connect()
        await c.subscribe()
        await c.subscribe()  # idempotent
        loop = asyncio.get_running_loop()
        started = loop.time()
        await c.open_session()
        self.assertGreaterEqual(loop.time() - started, 0.015)
        await c.open_session()  # already ready: no second handshake
        self.assertEqual(len(link.sent), 1)
        await c.disconnect()

    async def test_session_steps_are_ordered(self):
        c = client()
        with self.assertRaisesRegex(SessionError, "Connect before"):
            await c.subscribe()
        with self.assertRaisesRegex(SessionError, "Connect and subscribe"):
            await c.open_session()
        with self.assertRaisesRegex(SessionError, "Not connected"):
            await c.read_information("model")
        await c.connect()
        await c.connect()  # already connected
        with self.assertRaisesRegex(SessionError, "Connect and subscribe"):
            await c.open_session()
        self.assertEqual(await c.read_information("model"), "value")
        await c.disconnect()

    async def test_unsubscribe_ends_session_readiness(self):
        t = FakeTransport()
        c = client(t)
        async with c:
            await c.unsubscribe()
            await c.unsubscribe()
            self.assertFalse(c.snapshot()["session_ready"])
        self.assertEqual(t.calls.count("unsubscribe"), 1)

    async def test_read_intent_order_and_zero_not_missing(self):
        t = FakeTransport()
        result = await Studio(client=client(t)).read_state()
        self.assertTrue(result["complete"])
        self.assertEqual(result["state"]["fields"]["scale_weight"]["value"], 0)
        self.assertEqual(t.calls, ["connect", "subscribe", "handshake", "disconnect"])

    async def test_identity_intent(self):
        result = await Studio(client=client()).read_device_info(timeout=0.01)
        self.assertTrue(result["complete"])
        self.assertEqual(result["intent"], "read_device_info")
        self.assertEqual(result["state"]["fields"]["firmware"]["value"], "V9.9Z.99")

    async def test_missing_observation_is_partial_not_zero(self):
        result = await Studio(client=client(FakeTransport(ACK))).read_state(
            timeout=0.01
        )
        self.assertFalse(result["complete"])
        self.assertEqual(result["missing_fields"], ["scale_weight", "water_counter"])
        self.assertEqual(result["state"]["fields"], {})

    async def test_late_observation_completes_read(self):
        link, c = scripted()
        studio = Studio(client=c)
        reading = asyncio.create_task(
            studio.read_state(fields=["water_counter"], timeout=2)
        )
        while not c.state.session_ready:
            await asyncio.sleep(0)
        link.notify(WATER)
        self.assertTrue((await reading)["complete"])

    async def test_disconnect_during_read_is_an_error(self):
        link, c = scripted()
        reading = asyncio.create_task(
            Studio(client=c).read_state(fields=["water_counter"], timeout=2)
        )
        while not c.state.session_ready:
            await asyncio.sleep(0)
        link.drop()
        with self.assertRaisesRegex(SessionError, "Disconnected while reading"):
            await reading

    async def test_no_ack_fails_and_disconnects(self):
        t = FakeTransport(SCALE)
        with self.assertRaisesRegex(SessionError, "acknowledgment"):
            await Studio(client=client(t)).read_state()
        self.assertEqual(t.calls[-1], "disconnect")

    async def test_wrong_acknowledgments_cannot_establish_session(self):
        for data in (
            reply(11511, b"\0" * 4, 0xC2),
            reply(8100, b"\x01"),
            reply(8100, marker=0xC2),
            frame(8100, device=2, function=1, marker=0xC1),
        ):
            with self.subTest(data=data.hex()), self.assertRaises(SessionError):
                await Studio(client=client(FakeTransport(data))).read_state()

    async def test_handshake_rejection_is_explicit(self):
        error = reply(65534, u32(77), 0xCD)
        with self.assertRaises(ProtocolCommandError) as caught:
            await Studio(client=client(FakeTransport(error))).read_state()
        self.assertEqual(caught.exception.details["pending_command"], 8100)
        self.assertEqual(caught.exception.details["correlation"], "pending_at_receipt")
        self.assertEqual(caught.exception.details["value"], 77)

    async def test_error_during_settle_is_raised(self):
        link, c = scripted()
        c.settle = 0.05
        await c.connect()
        await c.subscribe()
        opening = asyncio.create_task(c.open_session())
        while not link.sent:
            await asyncio.sleep(0)
        link.notify(reply(65533, marker=0xCD))
        with self.assertRaises(ProtocolCommandError):
            await opening
        await c.disconnect()

    async def test_disconnect_during_handshake_or_settle(self):
        silent = SimulatedStudio(silent={8100})
        link, c = scripted(responder=silent)
        c.handshake_timeout = 1
        await c.connect()
        await c.subscribe()
        opening = asyncio.create_task(c.open_session())
        while not link.sent:
            await asyncio.sleep(0)
        link.drop()
        with self.assertRaisesRegex(SessionError, "during handshake"):
            await opening
        await c.disconnect()
        link, c = scripted()
        c.settle = 0.05
        await c.connect()
        await c.subscribe()
        opening = asyncio.create_task(c.open_session())
        while not link.sent:
            await asyncio.sleep(0)
        link.drop()
        with self.assertRaisesRegex(SessionError, "during initialization"):
            await opening
        await c.disconnect()

    async def test_connect_failure_releases_ownership(self):
        with self.assertRaises(ConnectionError):
            await Studio(client=client(FakeTransport(fail=True))).read_state()
        self.assertTrue((await Studio(client=client()).read_state())["complete"])

    async def test_operation_timeout_is_a_session_error(self):
        class Hanging(FakeTransport):
            async def connect(self, on_disconnect):
                await asyncio.sleep(1)

        c = client(Hanging(), operation_timeout=0.01)
        with self.assertRaisesRegex(SessionError, "Bluetooth operation failed"):
            await c.connect()
        self.assertTrue((await Studio(client=client()).read_state())["complete"])

    async def test_one_owner_per_identity_across_aliases(self):
        a = client(device="00:00:5e:00:53:01")
        b = client(device="00-00-5E-00-53-01")
        c = ReadOnlyClient(
            connector=ScriptedConnector("00:00:5E:00:53:01"),
            settle=0,
            subscription_settle=0,
        )
        self.assertEqual(a.address, b.address)
        async with a:
            for rival in (b, c):
                with self.assertRaisesRegex(SessionError, "owns"):
                    await rival.connect()
        async with b:
            self.assertTrue(b.snapshot()["session_ready"])

    async def test_identity_sources(self):
        link = ScriptedConnector("Sim-2")
        self.assertEqual(ReadOnlyClient(connector=link).address, "SIM-2")
        self.assertEqual(ReadOnlyClient("sim-2", connector=link).address, "SIM-2")
        with self.assertRaisesRegex(ValueError, "differ"):
            ReadOnlyClient("other", connector=link)
        self.assertEqual(
            ReadOnlyClient(transport=BleakTransport(connector=link)).address, "SIM-2"
        )
        for kwargs in ({}, {"transport": FakeTransport()}):
            with self.assertRaises(ValueError):
                ReadOnlyClient(**kwargs)
        with self.assertRaisesRegex(ValueError, "without transport"):
            ReadOnlyClient("x", transport=FakeTransport(), connector=link)
        with self.assertRaisesRegex(ValueError, "without transport"):
            ReadOnlyClient("x", transport=FakeTransport(), command_hooks=object())
        for kwargs in (
            {"settle": -1},
            {"operation_timeout": 0},
            {"subscription_settle": -1},
        ):
            with self.assertRaises(ValueError):
                ReadOnlyClient("x", **kwargs)

    async def test_default_transport_is_the_local_bluetooth_link(self):
        module = fake_bleak.module(found=fake_bleak.Device("00:00:5E:00:53:01"))
        with mock.patch.dict(sys.modules, {"bleak": module}):
            c = ReadOnlyClient("00:00:5e:00:53:01", settle=0, subscription_settle=0)
            self.assertIsInstance(c.transport, BleakTransport)
            await c.connect()
            await c.subscribe()
            ble = fake_bleak.Client.instances[-1]
            opening = asyncio.create_task(c.open_session())
            while not ble.writes:
                await asyncio.sleep(0)
            ble.notify(object(), bytearray(ACK))
            await opening
            self.assertEqual(
                ble.writes,
                [
                    (
                        "0000ffe1-0000-1000-8000-00805f9b34fb",
                        frame(8100, bytes.fromhex("b900000001000000")),
                        False,
                    )
                ],
            )
            await c.disconnect()
            self.assertFalse(ble.is_connected)

    async def test_cancellation_disconnects(self):
        t = FakeTransport(ACK)
        task = asyncio.create_task(Studio(client=client(t)).read_state(timeout=60))
        while "handshake" not in t.calls:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(t.calls[-1], "disconnect")

    async def test_monitor_early_close_releases_session(self):
        t = FakeTransport()
        async with aclosing(Studio(client=client(t)).monitor_state()) as stream:
            async for update in stream:
                self.assertTrue(update["state"]["session_ready"])
                break
        self.assertEqual(t.calls[-1], "disconnect")

    async def test_monitor_no_automatic_reconnect(self):
        t = FakeTransport()
        studio = Studio(client=client(t))
        async with aclosing(studio.monitor_state(duration=1, interval=0.001)) as stream:
            await anext(stream)
            t.drop()
            second = await anext(stream)
            self.assertFalse(second["state"]["connected"])
            self.assertEqual(second["reconnects"], 0)
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
        self.assertEqual(t.calls.count("connect"), 1)

    async def test_monitor_bounded_reconnect_and_duration(self):
        t = FakeTransport(fail=True)
        studio = Studio(client=client(t))
        with mock.patch("xbloom_studio.intents.asyncio.sleep") as sleep:
            sleep.return_value = None
            updates = [
                u async for u in studio.monitor_state(duration=5, reconnect_attempts=1)
            ]
        self.assertEqual([u["reconnects"] for u in updates], [0, 1])
        self.assertEqual(t.calls.count("connect"), 2)
        updates = [
            u
            async for u in Studio(client=client()).monitor_state(
                duration=0.02, interval=0.01
            )
        ]
        self.assertTrue(updates)
        for kwargs in (
            {"duration": 0},
            {"interval": 0},
            {"reconnect_attempts": -1},
            {"reconnect_attempts": 1.5},
        ):
            with self.assertRaises(ValueError):
                await anext(Studio(client=client()).monitor_state(**kwargs))

    async def test_monitor_reports_a_session_that_opened_after_its_duration(self):
        class SlowTransport(FakeTransport):
            async def connect(self, on_disconnect):
                await asyncio.sleep(0.02)
                await super().connect(on_disconnect)

        studio = Studio(client=client(SlowTransport()))
        updates = [u async for u in studio.monitor_state(duration=0.01)]
        self.assertEqual(len(updates), 1)
        self.assertTrue(updates[0]["state"]["session_ready"])

    async def test_staleness_and_disconnection(self):
        c = client(stale_after=0.001)
        async with c:
            await asyncio.sleep(0.003)
            self.assertFalse(c.snapshot()["fields"]["scale_weight"]["available"])
        self.assertFalse(c.snapshot()["connected"])

    async def test_wait_for_update_times_out_with_latest_state(self):
        c = client()
        async with c:
            revision = c.snapshot()["revision"]
            result = await c.wait_for_update(revision, timeout=0.01)
            self.assertEqual(result["revision"], revision)
            self.assertEqual(
                (await c.wait_for_update(revision, timeout=0))["revision"], revision
            )

    async def test_reconnect_does_not_reuse_old_fields(self):
        t = FakeTransport()
        c = client(t)
        async with c:
            self.assertIn("scale_weight", c.snapshot()["fields"])
        t.data = ACK
        async with c:
            self.assertEqual(c.snapshot()["fields"], {})

    async def test_corrupt_frame_is_counted_and_following_frame_works(self):
        c = client()
        async with c:
            c._notification(SCALE[:-1] + bytes([SCALE[-1] ^ 1]))
            c._notification(SCALE)
            c._notification(reply(20501, b"\x00"))
            self.assertEqual(c.snapshot()["decode_errors"], 2)
            self.assertEqual(c.snapshot()["fields"]["scale_weight"]["value"], 0)

    async def test_event_sink_respects_recorder_filter(self):
        class Recorder:
            def __init__(self, accepted):
                self.accepted = accepted
                self.events = []

            def accepts(self, kind):
                return kind in self.accepted

            def emit(self, kind, details):
                self.events.append((kind, details))

        recorder = Recorder({"ble.packet", "ble.decode_error"})
        c = client(event_sink=recorder.emit)
        async with c:
            c._notification(b"\x00")
            c._notification(reply(20501, b"\x00"))
        kinds = [kind for kind, _ in recorder.events]
        self.assertNotIn("ble.incoming_chunk", kinds)
        self.assertIn("ble.decode_error", kinds)
        bad = [
            d for k, d in recorder.events if k == "ble.packet" and "decode_error" in d
        ]
        self.assertEqual(bad[0]["command"], 20501)
        events = []
        c = client(event_sink=lambda kind, details: events.append(kind))
        async with c:
            pass
        self.assertIn("ble.incoming_chunk", events)

    async def test_invalid_input_never_connects(self):
        t = FakeTransport()
        for kwargs in ({"fields": ["temperature"]}, {"fields": []}, {"timeout": 0}):
            with self.assertRaises(ValueError):
                await Studio(client=client(t)).read_state(**kwargs)
        self.assertEqual(t.calls, [])
        with self.assertRaises(ValueError):
            Studio()
        with self.assertRaises(ValueError):
            Studio("x", client=client())

    async def test_studio_accepts_a_connector(self):
        link = ScriptedConnector("sim-studio")
        studio = Studio(connector=link)
        self.assertEqual(studio.client.address, "SIM-STUDIO")
        studio.client.settle = studio.client.subscription_settle = 0
        self.assertTrue((await studio.read_device_info(timeout=1))["complete"])

    async def test_bounded_packet_history(self):
        c = client()
        async with c:
            for _ in range(1000):
                c._notification(SCALE)
            self.assertEqual(len(c.packets), 128)


class OwnershipBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_rival_never_closes_a_shared_or_unopened_link(self):
        # S1: a rival over the same connector, or one refused a lease, never
        # opened the link, so its cleanup must not close it.
        link, incumbent = scripted("00:00:5E:00:53:02")
        async with incumbent:
            rival = ReadOnlyClient(connector=link, settle=0, subscription_settle=0)
            with self.assertRaisesRegex(SessionError, "owns"):
                async with rival:
                    pass
            self.assertTrue(link.is_connected)
            self.assertTrue(incumbent.snapshot()["connected"])
            self.assertNotIn("disconnect", link.calls)
        lease = AddressLease("SIM-LEASED")
        lease.acquire()
        try:
            leased, c = scripted("sim-leased")
            with self.assertRaisesRegex(ConnectionError, "Another library process"):
                async with c:
                    pass
            self.assertEqual(leased.calls, [])
        finally:
            lease.release()

    def test_injected_transport_identity_is_compared_with_device(self):
        # S1: an injected transport's identity is normalized and must match an
        # explicit device; a legacy transport without one uses the device.
        transport = BleakTransport(connector=ScriptedConnector("Sim-3"))
        self.assertEqual(ReadOnlyClient("sim-3", transport=transport).address, "SIM-3")
        with self.assertRaisesRegex(ValueError, "differ"):
            ReadOnlyClient("other", transport=transport)
        self.assertEqual(
            ReadOnlyClient("legacy", transport=FakeTransport()).address, "LEGACY"
        )

    async def test_handshake_ack_before_its_write_boundary_is_stale(self):
        # S6: an ACK during the subscription settle or the pre-write hook is
        # not an acknowledgment of a handshake that was not yet sent.
        link, c = scripted(responder=SimulatedStudio(silent={8100}))
        c.handshake_timeout = 0.05
        c.subscription_settle = 0.02
        await c.connect()
        await c.subscribe()
        opening = asyncio.create_task(c.open_session())
        await asyncio.sleep(0)
        link.notify(ACK)
        with self.assertRaisesRegex(SessionError, "No handshake acknowledgment"):
            await opening
        await c.disconnect()

        class Echo:
            async def before_write(self, packet, frame_bytes):
                link.notify(ACK)

            def write_completed(self, packet):
                pass

            def write_failed(self, packet, error):
                pass

        link = ScriptedConnector(responder=SimulatedStudio(silent={8100}))
        c = ReadOnlyClient(
            connector=link,
            command_hooks=Echo(),
            settle=0,
            subscription_settle=0,
            handshake_timeout=0.05,
        )
        with self.assertRaisesRegex(SessionError, "No handshake acknowledgment"):
            async with c:
                pass

    async def test_frames_before_a_corrupt_suffix_are_kept_and_reported(self):
        # S7: valid frames before corruption are delivered; the notification
        # is still reported as corrupt.
        events = []
        c = client(event_sink=lambda kind, details: events.append(kind))
        async with c:
            c._notification(reply(20501, f32(12.5)) + b"\x00garbage")
            snapshot = c.snapshot()
            self.assertEqual(snapshot["fields"]["scale_weight"]["value"], 12.5)
            self.assertEqual(snapshot["decode_errors"], 1)
            self.assertIn("header", snapshot["last_error"])
            self.assertEqual(events[-2:], ["ble.packet", "ble.decode_error"])
            c._notification(SCALE)
            self.assertEqual(c.snapshot()["fields"]["scale_weight"]["value"], 0)

    async def test_failing_incoming_observer_cannot_drop_telemetry_or_ack(self):
        # S5: transcript observer failures never drop a notification.
        class Picky:
            def accepts(self, kind):
                raise RuntimeError("filter")

            def emit(self, kind, details):
                raise AssertionError("filtered out")

        def sink(kind, details):
            raise RuntimeError("observer")

        for event_sink in (sink, Picky().emit):
            link = ScriptedConnector()
            c = ReadOnlyClient(
                connector=link, event_sink=event_sink, settle=0, subscription_settle=0
            )
            async with c:
                snapshot = c.snapshot()
                self.assertTrue(snapshot["session_ready"])
                self.assertIn("serial_number", snapshot["fields"])

    async def test_link_lost_while_connect_is_pending_is_not_connected(self):
        # S9: an early disconnect cannot be overwritten by connected=True.
        class Dropping(ScriptedConnector):
            async def connect(self, on_disconnect):
                await super().connect(on_disconnect)
                await asyncio.sleep(0)
                self.drop()

        link = Dropping("sim-dropping")
        c = ReadOnlyClient(connector=link, settle=0, subscription_settle=0)
        with self.assertRaisesRegex(SessionError, "lost while connecting"):
            await c.connect()
        self.assertFalse(c.snapshot()["connected"])
        self.assertEqual(link.calls, ["connect", "disconnect"])
        async with scripted("sim-dropping")[1] as other:
            self.assertTrue(other.snapshot()["session_ready"])

    async def test_explicit_reconnect_after_link_loss_reuses_the_held_lease(self):
        # S10: the lost session keeps ownership, reconnects with the same
        # lease and ignores late callbacks from the lost link.
        link, c = scripted("sim-reconnect")
        async with c:
            handle = c._lease._lock
            stale = link._on_disconnect
            link.drop()
            self.assertFalse(c.snapshot()["connected"])
            rival = ReadOnlyClient(connector=ScriptedConnector("sim-reconnect"))
            with self.assertRaisesRegex(SessionError, "owns"):
                await rival.connect()
            await c.connect()
            await c.subscribe()
            await c.open_session()
            self.assertTrue(c.snapshot()["session_ready"])
            self.assertIs(c._lease._lock, handle)
            stale()
            self.assertTrue(c.snapshot()["connected"])
        self.assertFalse(c._lease.held)
        lease = AddressLease(c.address)
        lease.acquire()
        lease.release()


class LifecycleRoundTwoTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsubscribe_clears_readiness_first_and_resubscribe_recovers(
        self,
    ):
        # S3: readiness is cleared before stop_notify starts, even when it
        # fails or its caller is cancelled; subscribe and handshake recover.
        class Stuck(ScriptedConnector):
            mode = None

            async def stop_notify(self):
                self.calls.append("stop_notify")
                if self.mode == "fail":
                    raise OSError("radio")
                if self.mode == "hang":
                    await asyncio.sleep(10)
                self._callback = None

        link = Stuck("sim-unsubscribe")
        c = ReadOnlyClient(connector=link, settle=0, subscription_settle=0)
        async with c:
            link.mode = "fail"
            with self.assertRaisesRegex(SessionError, "radio"):
                await c.unsubscribe()
            self.assertFalse(c.snapshot()["session_ready"])
            link.mode = None
            await c.subscribe()
            await c.open_session()
            self.assertTrue(c.snapshot()["session_ready"])
            self.assertEqual(
                link.calls[-3:], ["stop_notify", "stop_notify", "start_notify"]
            )
            link.mode = "hang"
            calls = len(link.calls)
            unsubscribing = asyncio.create_task(c.unsubscribe())
            while len(link.calls) == calls:
                await asyncio.sleep(0)
            self.assertFalse(c.snapshot()["session_ready"])
            unsubscribing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await unsubscribing
            self.assertFalse(c.snapshot()["session_ready"])
            link.mode = None
            await c.subscribe()
            await c.open_session()
            self.assertTrue(c.snapshot()["session_ready"])

    async def test_stale_notification_callback_is_ignored_after_reconnect(self):
        # S9: callbacks are bound to their connection; a callback from before
        # the reconnect cannot feed the cache or acknowledge the handshake.
        link, c = scripted("sim-generation")
        c.handshake_timeout = 0.05
        async with c:
            stale = link._callback
            link.drop()
            await c.connect()
            await c.subscribe()
            stale(reply(8023, u32(0x22)))
            self.assertEqual(len(c.packets), 0)
            link.responder = lambda packet: (stale(ACK), [])[1]
            with self.assertRaisesRegex(SessionError, "No handshake acknowledgment"):
                await c.open_session()
            self.assertFalse(c.snapshot()["session_ready"])
            self.assertEqual(len(c.packets), 0)

    async def test_incoming_observer_cancelled_error_never_drops_notifications(
        self,
    ):
        # S5: an explicit CancelledError from a synchronous observer or filter
        # is suppressed like any other observer failure.
        class Picky:
            def accepts(self, kind):
                raise asyncio.CancelledError()

            def emit(self, kind, details):
                raise AssertionError("filtered out")

        def sink(kind, details):
            raise asyncio.CancelledError()

        for event_sink in (sink, Picky().emit):
            c = ReadOnlyClient(
                connector=ScriptedConnector(),
                event_sink=event_sink,
                settle=0,
                subscription_settle=0,
            )
            async with c:
                self.assertTrue(c.snapshot()["session_ready"])
                self.assertIn("serial_number", c.snapshot()["fields"])


class LifecycleRoundThreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_same_link_callback_cannot_feed_next_handshake(self):
        # S3: each subscription has its own identity. A callback of an earlier
        # subscription on the same link, even one left registered by a failed
        # unsubscribe, can neither satisfy nor poison the next handshake.
        class Stuck(ScriptedConnector):
            fail = False

            async def stop_notify(self):
                self.calls.append("stop_notify")
                if self.fail:
                    raise OSError("radio")
                self._callback = None

        error = reply(65534, u32(7), marker=0xCD)
        link = Stuck("sim-registration")
        c = ReadOnlyClient(
            connector=link, settle=0, subscription_settle=0, handshake_timeout=0.05
        )
        async with c:
            first = link._callback
            await c.unsubscribe()
            await c.subscribe()
            packets = len(c.packets)
            first(reply(8023, u32(0x22)))
            first(error)
            self.assertEqual(len(c.packets), packets)
            link.responder = lambda packet: (first(ACK), [])[1]
            with self.assertRaisesRegex(SessionError, "No handshake acknowledgment"):
                await c.open_session()
            self.assertFalse(c.snapshot()["session_ready"])
            link.responder = SimulatedStudio()
            await c.open_session()
            self.assertTrue(c.snapshot()["session_ready"])

            second = link._callback
            link.fail = True
            with self.assertRaisesRegex(SessionError, "radio"):
                await c.unsubscribe()
            self.assertIs(link._callback, second)  # still registered
            second(error)
            second(ACK)
            self.assertEqual(len(c.packets), packets + 4)
            link.fail = False
            await c.subscribe()
            await c.open_session()
            self.assertTrue(c.snapshot()["session_ready"])

    async def test_failed_subscribe_invalidates_its_registration(self):
        # S3: a callback registered by a subscribe that then failed is dead.
        class Failing(ScriptedConnector):
            fail = True

            async def start_notify(self, callback):
                await super().start_notify(callback)
                if self.fail:
                    self.fail = False
                    raise OSError("radio")

        link = Failing("sim-failed-subscribe")
        c = ReadOnlyClient(connector=link, settle=0, subscription_settle=0)
        await c.connect()
        try:
            with self.assertRaisesRegex(SessionError, "radio"):
                await c.subscribe()
            stale = link._callback
            stale(reply(65534, u32(7), marker=0xCD))
            self.assertEqual(len(c.packets), 0)
            await c.subscribe()
            await c.open_session()
            self.assertTrue(c.snapshot()["session_ready"])
        finally:
            await c.disconnect()


class LifecycleRoundFourTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_registration_discards_an_old_fragment(self):
        # S3: a frame begun under one registration is never completed by the
        # next one's notifications; a fresh standalone ACK is decoded intact.
        link = ScriptedConnector("sim-fragment")
        c = ReadOnlyClient(
            connector=link, settle=0, subscription_settle=0, handshake_timeout=0.2
        )
        async with c:
            fragment = reply(8023, u32(0x22))[:10]
            link._callback(fragment)
            self.assertEqual(bytes(c._frames.buffer), fragment)
            await c.unsubscribe()
            await c.subscribe()
            errors = c.snapshot()["decode_errors"]
            packets = len(c.packets)
            await c.open_session()
            self.assertTrue(c.snapshot()["session_ready"])
            self.assertEqual(c.snapshot()["decode_errors"], errors)
            self.assertEqual(c.packets[packets].command, 8100)
            self.assertNotEqual(
                c.snapshot()["fields"]["machine_state_code"]["value"], 0x22
            )


class BoundedWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_racing_completion_still_propagates(self):
        async def instant():
            return "done"

        task = asyncio.create_task(bounded(instant(), 5))
        await asyncio.sleep(0)  # inner operation completes on this step
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        event = asyncio.Event()
        event.set()
        waiting = asyncio.create_task(wait_set(event, 5))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting

    async def test_inner_operation_finishes_before_cancel_or_timeout_returns(self):
        finished = []

        async def slow():
            try:
                await asyncio.sleep(5)
            finally:
                await asyncio.sleep(0)
                finished.append(True)

        with self.assertRaises(asyncio.TimeoutError):
            await bounded(slow(), 0.01)
        self.assertEqual(finished, [True])
        task = asyncio.create_task(bounded(slow(), 5))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(finished, [True, True])

        async def failing():
            await asyncio.sleep(5)

        self.assertEqual(await bounded(asyncio.sleep(0, "value"), 1), "value")
        with self.assertRaises(asyncio.TimeoutError):
            await bounded(failing(), 0)
        self.assertFalse(await wait_set(asyncio.Event(), 0.001))


class PlanTests(unittest.TestCase):
    def test_capabilities_and_plans_are_read_only(self):
        info = capabilities()
        self.assertTrue(info["read_only"])
        self.assertEqual(info["only_permitted_write"]["command"], 8100)
        self.assertIn("pour_radius_baseline", info["fields"])
        self.assertEqual(plan("read_state")["actions"][0], "connect")
        with self.assertRaises(ValueError):
            plan("brew")


def lock_files():
    return set(lease_directory().glob("*.lock"))


class LeaseTests(unittest.TestCase):
    def test_release_removes_its_lock_file(self):
        before = lock_files()
        for _ in range(5):
            lease = AddressLease(f"cycle:{uuid.uuid4().hex}")
            lease.acquire()
            lease.release()
        self.assertEqual(lock_files(), before)

    def test_acquire_retries_when_its_file_is_released_before_the_lock(self):
        key = f"handover:{uuid.uuid4().hex}"
        holder = AddressLease(key)
        holder.acquire()
        flock = fcntl.flock

        def release_first(handle, operation):
            holder.release()
            mocked.side_effect = flock
            flock(handle, operation)

        lease = AddressLease(key)
        with mock.patch("fcntl.flock", side_effect=release_first) as mocked:
            lease.acquire()
        self.addCleanup(lease.release)
        with self.assertRaises(LeaseBusyError):
            AddressLease(key).acquire()

    def test_release_spares_a_lock_file_it_does_not_hold(self):
        key = f"removed:{uuid.uuid4().hex}"
        first = AddressLease(key)
        before = lock_files()
        first.acquire()
        for path in lock_files() - before:
            path.unlink()
        second = AddressLease(key)
        second.acquire()
        self.addCleanup(second.release)
        first.release()
        with self.assertRaises(LeaseBusyError):
            AddressLease(key).acquire()

    def test_contending_processes_never_hold_one_lease_together(self):
        key = f"contended:{uuid.uuid4().hex}"
        before = lock_files()
        with tempfile.TemporaryDirectory() as shared:
            inside = os.path.join(shared, "inside")
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", CONTENDER, key, inside, "300"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env={**os.environ, "PYTHONPATH": REPO},
                )
                for _ in range(4)
            ]
            for process in processes:
                process.stdout.readline()
            for process in processes:
                process.stdin.write("\n")
                process.stdin.flush()
            outcomes = [process.communicate(timeout=60) for process in processes]
        for process, (_, error) in zip(processes, outcomes, strict=True):
            self.assertEqual(process.returncode, 0, error)
        self.assertGreater(sum(int(out) for out, _ in outcomes), 0)
        self.assertEqual(lock_files(), before)

    def test_lease_directory_uses_the_account_home(self):
        with tempfile.TemporaryDirectory() as root:
            with (
                mock.patch("pwd.getpwuid") as account,
                mock.patch.dict(
                    os.environ,
                    {
                        "HOME": root + "/other-home",
                        "XDG_STATE_HOME": root + "/other-state",
                        "TMPDIR": root + "/private-tmp",
                    },
                ),
            ):
                account.return_value.pw_dir = root
                self.assertEqual(
                    lease_directory(),
                    Path(root) / ".local/state/xbloom-studio/leases",
                )

    def test_processes_with_distinct_tmpdirs_share_ownership(self):
        code = (
            "import tests, sys; from xbloom_studio.ownership import AddressLease; "
            "AddressLease(sys.argv[1]).acquire()"
        )
        with tempfile.TemporaryDirectory() as other_tmp:
            for resource in ("machine", "application"):
                with self.subTest(resource=resource):
                    key = f"{resource}:{uuid.uuid4().hex}"
                    lease = AddressLease(key)
                    lease.acquire()
                    try:
                        outcome = subprocess.run(
                            [sys.executable, "-c", code, key],
                            env={**os.environ, "PYTHONPATH": REPO, "TMPDIR": other_tmp},
                            capture_output=True,
                            text=True,
                        )
                        self.assertNotEqual(outcome.returncode, 0)
                        self.assertIn("Another library process", outcome.stderr)
                    finally:
                        lease.release()

    def test_another_process_cannot_take_lease_and_release_is_reusable(self):
        lease = AddressLease("process-test")
        lease.acquire()
        self.assertTrue(lease.held)
        code = (
            "import tests; from xbloom_studio.ownership import AddressLease; "
            "AddressLease('process-test').acquire()"
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": REPO},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Another library process", result.stderr)
        finally:
            lease.release()
            lease.release()
        self.assertFalse(lease.held)
        self.assertEqual(
            subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                env={**os.environ, "PYTHONPATH": REPO},
            ).returncode,
            0,
        )


if __name__ == "__main__":
    unittest.main()
