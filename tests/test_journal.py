"""Journal ordering through CommandHooks, with a real SQLite store."""

import asyncio
import json
import sqlite3
import struct
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from tests.frames import u32
from tests.machine import Machine, interaction
from xbloom_studio import SessionError
from xbloom_studio.journal import JournalHooks, Recorder
from xbloom_studio.protocol import crc16
from xbloom_studio.storage import Store
from xbloom_studio.transport import ControlTransport


def frame(code):
    body = b"\x58\x01\x01" + struct.pack("<HI", code, 12) + b"\x01"
    return body + struct.pack("<H", crc16(body))


def committed_intents(path):
    """Read committed rows with an independent connection (WAL snapshot)."""
    with closing(sqlite3.connect(path)) as reader:
        rows = reader.execute(
            "SELECT data FROM events WHERE kind='command.intent'"
        ).fetchall()
    return [json.loads(row[0])["frame_hex"] for row in rows]


class CheckedMachine(Machine):
    """Records, at each frame's first byte, whether its intent was committed."""

    def __init__(self, path, **kwargs):
        super().__init__(**kwargs)
        self.path = path
        self.observations = []
        self.counts = {}

    async def write(self, data):
        if data[:1] == b"\x58" and len(data) >= 5:
            code = int.from_bytes(data[3:5], "little")
            self.counts[data.hex()] = self.counts.get(data.hex(), 0) + 1
            matches = sum(
                frame_hex.startswith(data.hex())
                for frame_hex in committed_intents(self.path)
            )
            self.observations.append((code, matches >= self.counts[data.hex()]))
        await super().write(data)


class JournalTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "journal.db")
        self.recorder = Recorder(self.store, "sim-control")

    async def asyncTearDown(self):
        try:
            await self.recorder.close()
        except RuntimeError:
            pass
        self.store.close()
        self.tmp.cleanup()

    def kinds(self, command=None):
        return [
            e["kind"]
            for e in self.store.events()
            if command is None
            or (e["kind"].startswith("command.") and e["data"]["command"] == command)
        ]


class OrderingTest(JournalTestCase):
    async def test_committed_intent_precedes_every_frame_even_with_backlog(self):
        machine = CheckedMachine(self.store.path)
        hooks = JournalHooks(self.recorder)
        # Each append is a durable commit; give the backlog time to drain.
        session = interaction(
            machine,
            command_hooks=hooks,
            event_sink=self.recorder.emit,
            write_timeout=10,
        )
        async with session:
            entered, release = threading.Event(), threading.Event()
            append = self.store.append

            def stalled(kind, data, **context):
                if kind == "sample" and data["value"] == 0:
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("journal writer was not released")
                return append(kind, data, **context)

            with mock.patch.object(self.store, "append", side_effect=stalled):
                for value in range(20):
                    self.recorder.emit("sample", {"value": value})
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                sending = asyncio.create_task(session.back_home())
                for _ in range(20):
                    await asyncio.sleep(0.005)
                # The intent is queued behind a held writer: no byte of the
                # command may reach the connector until it is committed.
                self.assertEqual([code for code, _ in machine.observations], [8100])
                self.assertFalse(sending.done())
                release.set()
                await asyncio.wait_for(sending, 5)
            await session.back_home()  # A repeated frame needs a new intent.
        self.assertEqual([code for code, _ in machine.observations], [8100, 8022, 8022])
        self.assertTrue(all(ok for _, ok in machine.observations))
        await self.recorder.flush()
        self.assertEqual(
            self.kinds(8022),
            ["command.intent", "command.write_completed"] * 2,
        )
        sequences = [e["sequence"] for e in self.store.events()]
        self.assertEqual(sequences, sorted(sequences))

    async def test_state_change_during_flush_prevents_the_start(self):
        machine = Machine()
        loop = asyncio.get_running_loop()
        append = self.store.append

        def low_water_while_committing(kind, data, **context):
            if kind == "command.intent" and data["command"] == 4506:
                # Runs in the journal's writer thread: the commit returns only
                # after the loop has delivered the event to the session.
                delivered = threading.Event()

                def deliver():
                    machine.emit(40522, u32(0))
                    delivered.set()

                loop.call_soon_threadsafe(deliver)
                if not delivered.wait(5):
                    raise TimeoutError("low-water event was not delivered")
            return append(kind, data, **context)

        with mock.patch.object(
            self.store, "append", side_effect=low_water_while_committing
        ):
            async with interaction(
                machine, command_hooks=JournalHooks(self.recorder), write_timeout=10
            ) as session:
                result = await (await session.start_water(volume_ml=10)).wait()
        self.assertEqual(result["status"], "failed")
        self.assertIn("Water sufficiency", result["error"])
        self.assertNotIn(4506, machine.codes())
        await self.recorder.flush()
        # The committed intent is an attempt, never proof of delivery.
        self.assertEqual(self.kinds(4506), ["command.intent", "command.write_failed"])

    async def test_persisted_intent_does_not_imply_delivery(self):
        class Failing(Machine):
            async def write(self, data):
                if data[:1] == b"\x58" and int.from_bytes(data[3:5], "little") == 8022:
                    raise OSError("radio failure")
                await super().write(data)

        machine = Failing()
        async with interaction(
            machine, command_hooks=JournalHooks(self.recorder), write_timeout=10
        ) as session:
            with self.assertRaises((OSError, SessionError)):
                await session.back_home()
        await self.recorder.flush()
        self.assertEqual(self.kinds(8022), ["command.intent", "command.write_failed"])
        failed = [e for e in self.store.events() if e["kind"] == "command.write_failed"]
        self.assertIn("radio failure", failed[0]["data"]["error"])
        self.assertNotIn(8022, machine.codes())


class FailureTest(JournalTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.machine = Machine()
        self.transport = ControlTransport(
            connector=self.machine, hooks=JournalHooks(self.recorder)
        )
        await self.transport.connect(lambda: None)
        await self.transport.subscribe(lambda data: None)

    async def asyncTearDown(self):
        await self.transport.disconnect()
        await super().asyncTearDown()

    async def test_journal_failure_blocks_writes_but_not_stop(self):
        with mock.patch.object(self.store, "append", side_effect=OSError("disk full")):
            with self.assertRaises(RuntimeError):
                await self.transport._write_frame(frame(4506))
            self.assertEqual(self.machine.codes(), [])
            await self.transport._write_frame(frame(4507))
            self.assertEqual(self.machine.codes(), [4507])
            with self.assertRaises(RuntimeError):
                await self.transport._write_frame(frame(8022))
        self.assertTrue(self.recorder.fault.is_set())
        with self.assertRaisesRegex(RuntimeError, "disk full"):
            await self.recorder.flush()

    async def test_cancelled_flush_records_the_write_outcome_at_capacity_one(self):
        self.recorder.slots = threading.BoundedSemaphore(1)
        started, release = threading.Event(), threading.Event()
        original = self.store.append
        submit = self.recorder.executor.submit
        pending_at_completion = []
        outcome_observed = threading.Event()

        def observe_pending(_):
            pending_at_completion.append(self.recorder.pending)
            if len(pending_at_completion) == 2:
                outcome_observed.set()

        def observe_submit(*args, **kwargs):
            future = submit(*args, **kwargs)
            future.add_done_callback(observe_pending)
            return future

        def blocked(kind, data, **context):
            if kind == "command.intent":
                started.set()
                release.wait(5)
            return original(kind, data, **context)

        with (
            mock.patch.object(self.store, "append", side_effect=blocked),
            mock.patch.object(
                self.recorder.executor, "submit", side_effect=observe_submit
            ),
        ):
            write = asyncio.create_task(self.transport._write_frame(frame(4506)))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                write.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await write
            finally:
                release.set()
            await self.recorder.flush()
            self.assertTrue(await asyncio.to_thread(outcome_observed.wait, 1))
            self.assertEqual(pending_at_completion, [1, 0])
            self.recorder.emit("after", {})
            await self.recorder.flush()
        self.assertEqual(self.machine.codes(), [])
        self.assertEqual(self.kinds(4506), ["command.intent", "command.write_failed"])
        self.assertIn("after", self.kinds())

    async def test_cancelled_gap_flush_does_not_duplicate_the_gap(self):
        self.recorder.slots = threading.BoundedSemaphore(1)
        started, release, gap_started = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        original = self.store.append
        submit = self.recorder.executor.submit
        reported_at_completion = []

        def observe_submit(*args, **kwargs):
            future = submit(*args, **kwargs)
            future.add_done_callback(
                lambda _: reported_at_completion.append(self.recorder.reported_lost)
            )
            return future

        def blocked(kind, data, **context):
            if kind == "hold":
                started.set()
                release.wait(5)
            if kind == "capture.gap":
                gap_started.set()
                release.wait(5)
            return original(kind, data, **context)

        with (
            mock.patch.object(self.store, "append", side_effect=blocked),
            mock.patch.object(
                self.recorder.executor, "submit", side_effect=observe_submit
            ),
        ):
            try:
                self.recorder.emit("hold", {})
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                self.recorder.emit("overflow", {})
                release.set()
                await asyncio.wrap_future(self.recorder.last)
                release.clear()
                flush = asyncio.create_task(self.recorder.flush())
                self.assertTrue(await asyncio.to_thread(gap_started.wait, 1))
                flush.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await flush
            finally:
                release.set()
            with self.assertRaisesRegex(RuntimeError, "queue full"):
                await self.recorder.flush()
        self.assertEqual(self.kinds().count("capture.gap"), 1)
        self.assertEqual(reported_at_completion, [0, 1])

    async def test_stalled_writer_and_full_queue_do_not_block_stop(self):
        self.recorder.slots = threading.BoundedSemaphore(2)
        started, release = threading.Event(), threading.Event()
        original = self.store.append

        def blocked(*args, **kwargs):
            started.set()
            release.wait(5)
            return original(*args, **kwargs)

        with mock.patch.object(self.store, "append", side_effect=blocked):
            try:
                self.recorder.emit("first", {})
                await asyncio.to_thread(started.wait, 1)
                self.recorder.emit("second", {})
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(self.recorder.flush(), 0.01)
                self.recorder.emit("overflow", {})
                self.assertEqual(self.recorder.lost, 1)
                await asyncio.wait_for(self.transport._write_frame(frame(4507)), 0.5)
                self.assertEqual(self.machine.codes(), [4507])
            finally:
                release.set()
            with self.assertRaisesRegex(RuntimeError, "queue full"):
                await self.recorder.flush()
        kinds = self.kinds()
        self.assertIn("first", kinds)
        self.assertIn("second", kinds)
        self.assertEqual(kinds[-1], "capture.gap")
        gap = self.store.events()[-1]
        # The overflowing event, then the stop's own intent and completion.
        self.assertEqual(gap["data"], {"lost_events": 3, "reason": "queue_overflow"})


class RecorderTest(JournalTestCase):
    async def test_close_refuses_new_events_before_draining(self):
        started, release = threading.Event(), threading.Event()
        original = self.store.append

        def blocked(kind, data, **context):
            if kind == "first":
                started.set()
                release.wait(5)
            return original(kind, data, **context)

        with mock.patch.object(self.store, "append", side_effect=blocked):
            try:
                self.recorder.emit("first", {})
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                close_task = asyncio.create_task(self.recorder.close())
                await asyncio.sleep(0)
                self.recorder.emit("late", {})
            finally:
                release.set()
            with self.assertRaisesRegex(RuntimeError, "after journal closed"):
                await close_task
        self.assertEqual(self.kinds(), ["first"])

    async def test_disabled_idle_telemetry_does_not_fail_close(self):
        self.recorder.capture_idle_telemetry = False
        started, release = threading.Event(), threading.Event()
        original = self.store.append

        def blocked(*args, **kwargs):
            started.set()
            release.wait(5)
            return original(*args, **kwargs)

        with mock.patch.object(self.store, "append", side_effect=blocked):
            try:
                self.recorder.emit("first", {})
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                close_task = asyncio.create_task(self.recorder.close())
                await asyncio.sleep(0)
                self.recorder.emit("ble.packet", {})
            finally:
                release.set()
            await close_task
        self.assertEqual(self.kinds(), ["first"])

    async def test_gap_counts_cover_only_new_losses(self):
        self.recorder.slots = threading.BoundedSemaphore(1)
        for count in (2, 1):
            self.assertTrue(self.recorder.slots.acquire(blocking=False))
            try:
                for _ in range(count):
                    self.recorder.emit("lost", {})
            finally:
                self.recorder.slots.release()
            with self.assertRaisesRegex(RuntimeError, "queue full"):
                await self.recorder.flush()
        self.assertEqual(
            [event["data"]["lost_events"] for event in self.store.events()], [2, 1]
        )

    async def test_idle_telemetry_gate_and_capture_windows(self):
        with self.assertRaises(ValueError):
            Recorder(self.store, "sim", capture_idle_telemetry="no")
        for capacity in (0, True, 1.5):
            with self.assertRaises(ValueError):
                Recorder(self.store, "sim", capacity=capacity)
        gated = Recorder(self.store, "sim", capture_idle_telemetry=False)
        try:
            machine = Machine()
            async with interaction(machine, event_sink=gated.emit) as session:
                await gated.flush()
                baseline = len(self.store.events())
                for _ in range(5):
                    machine.emit(20501, struct.pack("<f", 1.5))
                await asyncio.sleep(0)
                await gated.flush()
                self.assertEqual(len(self.store.events()), baseline)
                self.assertFalse(gated.accepts("ble.packet"))
                self.assertTrue(gated.accepts("command.intent"))
                self.assertEqual(
                    session.get_state()["machine"]["fields"]["scale_weight"]["value"],
                    1.5,
                )
                with gated.capturing():
                    self.assertTrue(gated.accepts("ble.packet"))
                    machine.emit(20501, struct.pack("<f", 2.5))
                    await asyncio.sleep(0)
                gated.operation_id = None
                await gated.flush()
                self.assertGreater(len(self.store.events()), baseline)
        finally:
            await gated.close()
        gated.emit("late", {})
        self.assertIn("after journal closed", str(gated.error))

    async def test_events_keep_context_and_close_is_idempotent_for_errors(self):
        self.recorder.operation_id = None
        self.recorder.emit("one", {"nested": {"value": 1}})
        data = {"value": 2}
        self.recorder.emit("two", data)
        data["value"] = 3  # Events are copied at emit time.
        await self.recorder.flush()
        rows = self.store.events()
        self.assertEqual(
            [r["data"] for r in rows], [{"nested": {"value": 1}}, {"value": 2}]
        )
        self.assertEqual({r["session_id"] for r in rows}, {self.recorder.session_id})
        self.assertEqual([r["device"] for r in rows], ["sim-control"] * 2)
        hooks = JournalHooks(self.recorder)
        hooks.write_failed(mock.Mock(command=1), ValueError("x"))
        await self.recorder.flush()
        self.assertEqual(
            self.store.events()[-1]["data"], {"command": 1, "error": "ValueError('x')"}
        )
        self.recorder.loop = mock.Mock()
        self.recorder.loop.call_soon_threadsafe.side_effect = RuntimeError("closed")
        self.recorder._fail(OSError("late"))
        self.assertIsInstance(self.recorder.error, OSError)
