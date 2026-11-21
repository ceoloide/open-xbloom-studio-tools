"""Simulated peripheral helpers for interaction tests."""

from __future__ import annotations

import asyncio
import sys

from tests.frames import reply, u32
from xbloom_studio import Interaction, ScriptedConnector, SimulatedStudio


class Machine(ScriptedConnector):
    """Scripted link with per-command overrides and a blockable frame write."""

    def __init__(self, identity="sim-control", *, auto_complete=False, **kwargs):
        self.studio = SimulatedStudio(auto_complete=auto_complete)
        self.overrides = {}
        super().__init__(identity, responder=self.respond, **kwargs)
        self.block_code = None
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self.partial = False

    def respond(self, packet):
        override = self.overrides.get(packet.command)
        return override(packet) if override else self.studio(packet)

    def emit(self, code, payload=b"", marker=0xC1):
        self.notify(reply(code, payload, marker))

    def state(self, value):
        self.emit(8023, u32(value))

    def codes(self):
        return [packet.command for packet in self.sent]

    def payloads(self, code):
        return [packet.payload for packet in self.sent if packet.command == code]

    async def write(self, data):
        starts = data[:1] == b"\x58" and len(data) >= 5
        if starts and int.from_bytes(data[3:5], "little") == self.block_code:
            self.partial = True
            self.blocked.set()
            await self.release.wait()
            self.partial = False
        await super().write(data)


def interaction(machine=None, **kwargs):
    options = {
        "allow_control": True,
        "settle": 0,
        "subscription_settle": 0,
        "transition_delay": 0,
        "ack_timeout": 0.03,
        "write_timeout": 0.5,
    }
    options.update(kwargs)
    return Interaction(connector=machine or Machine(), **options)


async def until(predicate, timeout=1.0):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout)


async def transmitted(machine, code):
    await until(lambda: code in machine.codes())


async def blocked_write(machine, task):
    """Wait for the blocked frame and surface early worker failures."""

    async def wait():
        while not machine.blocked.is_set():
            if task.done():
                task.result()
                raise AssertionError(f"Worker ended before blocking: {machine.codes()}")
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), 2)


def expect_shielded_failures(test, count):
    """Collect the asyncio errors a test causes on purpose behind a shield.

    Python 3.14 logs an exception raised by a shielded future whose caller was
    cancelled; earlier versions do not. Every other context still reaches the
    loop's default handler.
    """
    loop = asyncio.get_running_loop()
    seen = []

    def handler(loop, context):
        message = context.get("message", "")
        if message.endswith("exception in shielded future"):
            seen.append(message)
        else:
            loop.default_exception_handler(context)

    test.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
    loop.set_exception_handler(handler)
    expected = count if sys.version_info >= (3, 14) else 0
    test.addCleanup(lambda: test.assertEqual(len(seen), expected, seen))
