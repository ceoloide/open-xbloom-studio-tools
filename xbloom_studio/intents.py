"""Deterministic read-only intent plans shared by entry points and integrations."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING, Any

from .client import ReadOnlyClient, SessionError
from .connector import Connector
from .events import EVENT_FIELDS

if TYPE_CHECKING:
    from .control import Interaction

FIELDS = (
    "scale_weight",
    "water_counter",
    "machine_state_code",
    "firmware",
    "serial_number",
    "device_info_raw",
    "water_sufficient",
    "water_fault_code",
    "asleep",
    "dispensed_water_estimate",
    "led_brightness",
    "temperature_unit",
    "weight_unit",
    "configured_mode",
    "grind_setting",
    "water_source",
) + tuple(name for name in EVENT_FIELDS if name != "grind_setting")
PLANS = {
    "read_state": [
        "connect",
        "subscribe",
        "open_session",
        "await_fresh_fields",
        "snapshot",
        "disconnect",
    ],
    "monitor_state": [
        "connect",
        "subscribe",
        "open_session",
        "observe_updates",
        "disconnect",
    ],
    "read_device_info": [
        "connect",
        "subscribe",
        "open_session",
        "await_identity",
        "snapshot",
        "disconnect",
    ],
}


def capabilities() -> dict[str, Any]:
    return {
        "read_only": True,
        "connection_may_beep": True,
        "default_reconnect_attempts": 0,
        "atomic_operations": [
            "discover",
            "connect",
            "disconnect",
            "subscribe",
            "unsubscribe",
            "open_session",
            "read_information",
            "snapshot",
            "wait_for_update",
        ],
        "only_permitted_write": {
            "command": 8100,
            "purpose": "register_telemetry_session",
        },
        "fields": list(FIELDS),
        "intents": list(PLANS),
        "unsupported": [
            "tank_level",
            "temperature",
            "recipe_contents",
            "reliable_current_mode",
            "grinder_rpm",
            "settings_writes",
            "mode_switch",
            "brewing",
            "grinding",
        ],
        "limitations": [
            "Dispensed-water estimate divides a raw counter by 1000, is not "
            "calibrated and can reset; not tank level",
            "Machine state is a raw code, not proof of activity",
            "One Bluetooth owner; release other apps before connecting",
            "Reconnects can beep; disabled by default",
        ],
    }


def plan(intent: str) -> dict[str, Any]:
    if intent not in PLANS:
        raise ValueError(f"Unknown or unsupported intent: {intent}")
    return {
        "intent": intent,
        "actions": PLANS[intent].copy(),
        "read_only": True,
        "connection_may_beep": True,
    }


class Studio:
    """Intent facade over a read-only client, connector or discovered device.

    These methods own the client's session; don't call them concurrently on
    one facade. A held monitor session serves consumers through snapshots
    without reconnecting.
    """

    def __init__(
        self,
        device: object = None,
        *,
        client: ReadOnlyClient | None = None,
        connector: Connector | None = None,
    ) -> None:
        if client is not None and (device is not None or connector is not None):
            raise ValueError("Provide a client, or a device and/or connector")
        if client is None and device is None and connector is None:
            raise ValueError("Provide a device address/BLEDevice, connector or client")
        self.client = (
            client
            if client is not None
            else ReadOnlyClient(device, connector=connector)
        )
        self._intent_lock = asyncio.Lock()

    def interaction(self, *, allow_control: bool = False, **kwargs: Any) -> Interaction:
        """Create an explicitly owned control context; does not connect yet.

        The context uses this client's connector unless a transport or
        connector is given. Monitoring and control must use the context's
        cache while it owns the machine.
        """
        from .control import Interaction

        transport = self.client.transport
        device = getattr(transport, "device", self.client.address)
        connector = getattr(transport, "connector", None)
        if "transport" not in kwargs:
            if "connector" not in kwargs and connector:
                kwargs["connector"] = connector
            # The client's journal and transcript observer carry over, so a
            # control context is never silently unjournaled.
            hooks = getattr(transport, "hooks", None)
            if "command_hooks" not in kwargs and hooks is not None:
                kwargs["command_hooks"] = hooks
        if "event_sink" not in kwargs and self.client.event_sink is not None:
            kwargs["event_sink"] = self.client.event_sink
        return Interaction(device, allow_control=allow_control, **kwargs)

    async def read_state(
        self,
        *,
        fields: Iterable[str] = ("scale_weight", "water_counter"),
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        fields = tuple(fields)
        if timeout <= 0 or not fields or any(f not in FIELDS for f in fields):
            raise ValueError("Provide known fields and a positive observation timeout")
        async with self._intent_lock:
            async with self.client:
                deadline = time.monotonic() + timeout
                while True:
                    snapshot = self.client.snapshot()
                    missing = [
                        f
                        for f in fields
                        if not snapshot["fields"].get(f, {}).get("available", False)
                    ]
                    if not missing or time.monotonic() >= deadline:
                        break
                    if not snapshot["connected"]:
                        raise SessionError("Disconnected while reading state")
                    await self.client.wait_for_update(
                        snapshot["revision"], max(0, deadline - time.monotonic())
                    )
                return {
                    "intent": "read_state",
                    "complete": not missing,
                    "missing_fields": missing,
                    "state": snapshot,
                    "session_closed_after_read": True,
                }

    async def read_device_info(self, *, timeout: float = 5.0) -> dict[str, Any]:
        result = await self.read_state(
            fields=("firmware", "serial_number"), timeout=timeout
        )
        result["intent"] = "read_device_info"
        return result

    async def monitor_state(
        self,
        *,
        duration: float = 60.0,
        interval: float = 1.0,
        reconnect_attempts: int = 0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield bounded-rate snapshots; unavailable/stale values remain explicit.

        Every opened session yields at least one snapshot, even when opening
        it took longer than duration. Each allowed reconnect can beep. Zero
        retries is the quiet default. Use contextlib.aclosing when ending an
        async iteration early.
        """
        if (
            duration <= 0
            or interval <= 0
            or type(reconnect_attempts) is not int
            or reconnect_attempts < 0
        ):
            raise ValueError(
                "Positive duration/interval and nonnegative integer "
                "reconnect_attempts required"
            )
        async with self._intent_lock:
            deadline = time.monotonic() + duration
            retries = 0
            while time.monotonic() < deadline:
                try:
                    async with self.client:
                        while True:
                            state = self.client.snapshot()
                            if not state["connected"]:
                                raise SessionError("Bluetooth disconnected")
                            yield {
                                "intent": "monitor_state",
                                "state": state,
                                "reconnects": retries,
                            }
                            await asyncio.sleep(
                                min(interval, max(0, deadline - time.monotonic()))
                            )
                            if time.monotonic() >= deadline:
                                break
                    return
                except (ConnectionError, TimeoutError, OSError) as exc:
                    yield {
                        "intent": "monitor_state",
                        "state": self.client.snapshot(),
                        "error": str(exc),
                        "reconnects": retries,
                    }
                    if retries >= reconnect_attempts:
                        return
                    retries += 1
                    await asyncio.sleep(
                        min(2**retries, max(0, deadline - time.monotonic()))
                    )
