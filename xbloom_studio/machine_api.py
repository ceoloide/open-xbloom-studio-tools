"""JSON-facing machine controls through the application connection owner.

The allowlist is deliberately narrower than :class:`~xbloom_studio.Interaction`:
operation launchers and calibration stay out. Recipe execution always uses
recorded operation scheduling.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import Callable
from contextlib import ExitStack, suppress
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._cleanup import wait_cleanup
from .client import SessionError
from .command_schema import (
    command_schema,
    recipe_configuration_untested,
    validate_command_params,
)
from .control import IDLE_STATES, BusyError, EncodedRecipe, Interaction
from .machine_controls import (
    SLOTS,
    ControlPermissionError,
    EncodedTeaRecipe,
    ExperimentalOptInError,
    MachineControls,
    PresetWriteError,
    SavedPreset,
)
from .preset_diagnostics import preset_write_diagnostic
from .preset_writes import (
    CLOSE_PENDING,
    CLOSED,
    EVIDENCE_KINDS,
    FINISHED,
    MAX_ATTEMPTS,
    STARTED,
    SUPERSEDED,
    admit,
    attempt_evidence,
    fold,
    supersede,
)
from .recipe_validation import HOST_STEPS, NATIVE, check_recipe
from .recipes import upload_requires_untested
from .storage import ConflictError

if TYPE_CHECKING:
    from .journal import Recorder
    from .storage import Store

COMMANDS = {
    "set_brightness": "Set display brightness, integer 1..15.",
    "set_weight_unit": "Set display unit: ml, g or oz.",
    "set_temperature_unit": "Set display unit: C or F.",
    "set_pour_pattern": "Set center/ring/spiral after entering manual brewer; "
    "no dispense.",
    "back_home": "Return home; may wake the display.",
    "wake": "Use the home route and report observed wake status.",
    "set_mode": "Switch pro/recipes.",
    "write_preset": "Experimental single-slot attempt of three planned frames; "
    "an incomplete write may be resubmitted identically, at most "
    f"{MAX_ATTEMPTS} attempts per machine and slot. Optional backup is not "
    "readback. Leaves the machine in pro mode.",
    "write_presets": "Write A/B/C once each; requires caller-supplied backups. "
    "Refused over an incomplete write_preset; supersedes each slot's recorded "
    "write before its first frame for that slot. Leaves the machine in pro mode.",
    "set_water_source": "Select tank/tap source by the existing validated argument "
    "contract.",
    "read_pour_radius": "Query current raw value; also prepares/home the machine.",
    "read_vibration": "Query current raw value; also prepares/home the machine.",
    "initialize_pour_radius": "Initialize radius baseline, not a passive query.",
    "set_pour_radius": "Set raw radius.",
    "set_pour_radius_level": "Set app level 0..4 with app baseline clamping.",
    "initialize_vibration": "Initialize raw vibration baseline.",
    "set_vibration_level": "Set app vibration level 0..5.",
    "set_cup_limits": "Set recipe cup-reference limits, not heater temperatures.",
    "configure_recipe": "Set dose/bypass/cup metadata; does not upload content.",
    "set_preset_count": "Experimental count; acknowledgment is not established.",
    "set_preset_order": "Experimental order; acknowledgment is not established.",
    "quit_recipe_preparation": "Leave native recipe preparation; not a native "
    "stage advance.",
    "back_to_normal": "Empty 40526 housekeeping; not a grinder-position query.",
    "configure_grinder": "Enter/configure/exit manual grinder without starting "
    "rotation.",
    "configure_brewer": "Enter/configure/exit manual brewer without dispensing.",
    "enter_scale": "Open an owned standalone scale session.",
    "tare": "Tare the owned scale; not scale calibration.",
    "exit_scale": "Close the owned standalone scale session.",
}
BACKUP_COMMANDS = ("write_preset", "write_presets", "set_preset_order")


def _signature(name: str) -> str:
    """The legacy unannotated parameter list, without ``self`` and backup path."""
    signature = inspect.signature(getattr(Interaction, name))
    return str(
        signature.replace(
            parameters=[
                value.replace(annotation=inspect.Parameter.empty)
                for key, value in signature.parameters.items()
                if key not in ("self", "backup_path")
            ],
            return_annotation=inspect.Signature.empty,
        )
    )


def command_capabilities() -> dict[str, Any]:
    """Offline descriptions. Command validation remains in the low-level API."""
    return {
        "commands": {
            name: {
                "description": description,
                "requires_allow_control": True,
                "requires_allow_untested": name in MachineControls.UNTESTED_METHODS,
                "parameters": _signature(name),
                "conditional_experimental": ["bypass_ml>0", "cup_limits"]
                if name == "configure_recipe"
                else [],
                "input_schema": command_schema(name),
            }
            for name, description in COMMANDS.items()
        },
        "preset_backup": "Service-generated private file; caller supplies previous "
        "contents, never machine readback. Optional for write_preset.",
        "prepared_recipe": "Single native coffee/tea step; session-local single-use "
        "token, no automatic expiry or retry. For a plan that can be uploaded, "
        "follow the machine_prepare preview's upload_requires_untested. "
        "If it is false and requires_untested is true, pass the preview's "
        "recipe_sha256 as expected_recipe_sha256 when uploading without "
        "allow_untested. A direct operation_start keeps requires_untested.",
        "native_stage_advance": False,
        "calibration_exposed": False,
        "grinder_upload": {
            "field": "set_grinder_size",
            "legacy_alias": "grinder_enabled",
            "true_upload": 8001,
            "false_upload": 8004,
            "actual_grinding": "not_established_by_upload_option",
        },
    }


def _preset(value: Any) -> SavedPreset:
    if not isinstance(value, dict) or set(value) != {"slot", "payload_hex", "flags"}:
        raise ValueError("Preset needs exactly slot, payload_hex and flags")
    if not isinstance(value["payload_hex"], str):
        raise ValueError("Preset payload_hex must be a string")
    return SavedPreset(
        value["slot"], bytes.fromhex(value["payload_hex"]), value["flags"]
    )


def command_parameters(name: str, params: Any) -> dict[str, Any]:
    """Convert only named JSON representations, rejecting paths and extra fields."""
    if name not in COMMANDS:
        raise ValueError("Unknown machine command; inspect machine_capabilities")
    if not isinstance(params, dict):
        raise ValueError("Command params must be an object")
    validate_command_params(name, params)
    converted = deepcopy(params)
    if "backup_path" in converted:
        raise ValueError("The service chooses the preset backup path")
    if name == "write_preset":
        for key in ("preset", "backup"):
            if key in converted:
                converted[key] = _preset(converted[key])
    if name == "write_presets":
        for key in ("presets", "backup"):
            converted[key] = [_preset(value) for value in converted[key]]
    if name == "configure_recipe" and converted.get("cup_limits") is not None:
        converted["cup_limits"] = tuple(converted["cup_limits"])
    if name in BACKUP_COMMANDS and "backup" in converted:
        converted["backup_path"] = None  # Filled by the service.
    # The daemon's retry policy consumes the acknowledgment, not the machine.
    inspect.signature(getattr(Interaction, name)).bind(
        None,
        **{k: v for k, v in converted.items() if k != "acknowledge_incomplete"},
    )
    return converted


def same_request(first: Any, second: Any) -> bool:
    """Whether two JSON requests name the same attempt.

    Numbers compare by value, so ``50`` and ``50.0`` from different adapters
    are one request; booleans never equal numbers, and everything else must
    match exactly.
    """
    if isinstance(first, bool) or isinstance(second, bool):
        return type(first) is type(second) and first == second
    if isinstance(first, (int, float)) and isinstance(second, (int, float)):
        return first == second
    if isinstance(first, dict) and isinstance(second, dict):
        return first.keys() == second.keys() and all(
            same_request(first[key], second[key]) for key in first
        )
    if isinstance(first, list) and isinstance(second, list):
        return len(first) == len(second) and all(
            same_request(a, b) for a, b in zip(first, second, strict=True)
        )
    return type(first) is type(second) and bool(first == second)


def comparable_request(request: Any) -> Any:
    """The comparison form of a start request; never stored, hashed or resolved.

    Adapters may omit a declared default or spell it out (the MCP contract
    adds them), so both forms are filled in before comparison: a bounded
    step's defaults and name, and an inline document's validator
    normalization. A recipe reference is compared as given, never resolved
    against the mutable library. Anything else, including a legacy request
    the current rules no longer accept, compares exactly as recorded.
    """
    if not isinstance(request, dict):
        return request
    kind = request.get("kind")
    if kind in ("water", "grind"):
        result = {"name": kind, **request}
        for key, rule in HOST_STEPS[kind].items():
            if "default" in rule:
                result.setdefault(key, rule["default"])
        return result
    if kind == "recipe" and "reference" in request and request.get("revision") is None:
        return {key: value for key, value in request.items() if key != "revision"}
    if kind == "recipe" and isinstance(request.get("document"), dict):
        report = check_recipe(request["document"])
        if report["valid"]:
            document = report["normalized"]
            if document["mode"] == "native":
                for key, rule in NATIVE.items():
                    if "default" in rule:
                        document["native"].setdefault(key, rule["default"])
            return {**request, "document": document}
    return request


def same_attempt(recorded: Any, request: Any) -> bool:
    """Whether ``request`` replays the attempt recorded as ``recorded``."""
    return same_request(comparable_request(recorded), comparable_request(request))


def encoded_step(step: dict[str, Any]) -> EncodedRecipe | EncodedTeaRecipe:
    """Rebuild the retained machine content of one native coffee or tea step."""
    limits = step.get("cup_limits")
    if step["kind"] == "tea":
        return EncodedTeaRecipe(
            bytes.fromhex(step["payload_hex"]),
            tuple(limits) if limits is not None else None,
        )
    return EncodedRecipe(
        bytes.fromhex(step["payload_hex"]),
        step["dose_g"],
        grinder_enabled=step.get("grinder_enabled", False),
        cup_limits=tuple(limits) if limits is not None else None,
        bypass_ml=step.get("bypass_ml", 0),
        bypass_temperature_c=step.get("bypass_temperature_c", 20),
    )


class MachineCommandAPI:
    """Mixed into MachineService; uses its guard, recorder and retained session."""

    store: Store
    device: str | None
    guard: asyncio.Lock
    closed: bool
    disconnecting: bool
    stopping: bool
    finalizing: bool
    task: asyncio.Task[None] | None
    operation_id: str | None
    projection: dict[str, Any] | None
    worker_started: asyncio.Event | None
    prepared: dict[str, Any] | None
    _prepared_plan: dict[str, Any] | None
    session: Interaction | None
    recorder: Recorder | None
    # The running preset write attempt, reported as in progress, not interrupted.
    preset_attempt: str | None = None

    if TYPE_CHECKING:

        @property
        def _session(self) -> Interaction: ...

        @property
        def _recorder(self) -> Recorder: ...

        async def _own(self) -> None: ...

        async def _connect(self, allow_untested: bool) -> None: ...

        def _resolve(self, request: Any) -> dict[str, Any]: ...

        async def _execute(
            self,
            ident: str,
            plan: dict[str, Any],
            prepared_token: str | None = None,
        ) -> None: ...

    def capabilities(self) -> dict[str, Any]:
        return command_capabilities()

    def _reject_queued_write(self) -> None:
        if self.guard.locked():
            raise BusyError("Machine is handling another request; nothing queued")

    def _require_open(self) -> None:
        if self.closed or self.disconnecting:
            raise BusyError("Machine service is closed or disconnecting")

    def _require_unoccupied(self) -> None:
        self._require_open()
        if self.task is not None and not self.task.done():
            raise BusyError("Machine already has an operation; nothing queued")
        if self.prepared is not None:
            raise BusyError(
                "A recipe owns preparation; start, cancel or disconnect first"
            )

    async def command(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        allow_control: bool = False,
        *,
        allow_untested: bool = False,
    ) -> dict[str, Any]:
        if type(allow_control) is not bool:
            raise ValueError("allow_control must be a boolean")
        if not allow_control:
            raise ControlPermissionError("Machine commands require allow_control")
        if type(allow_untested) is not bool:
            raise ValueError("allow_untested must be a boolean")
        values = command_parameters(command, {} if params is None else params)
        acknowledge = values.pop("acknowledge_incomplete", None)
        experimental = command in MachineControls.UNTESTED_METHODS or (
            command == "configure_recipe"
            and recipe_configuration_untested(
                values.get("bypass_ml", 0), values.get("cup_limits")
            )
        )
        if experimental and not allow_untested:
            raise ExperimentalOptInError("Command requires allow_untested")
        self._reject_queued_write()
        async with self.guard:
            self._require_unoccupied()
            # Policy refusals come before any connection, under the database
            # owner lease, so no other owner changes the ledger between this
            # decision and the committed attempt; the guard holds it here.
            admitted = None
            superseded: list[dict[str, Any]] = []
            if command in ("write_preset", "write_presets"):
                await self._own()
                ledger = await asyncio.to_thread(self._preset_ledger)
                if command == "write_preset":
                    preset = values["preset"]
                    admitted = admit(
                        ledger.get(preset.slot), preset.to_dict(), acknowledge
                    )
                else:
                    slots = [preset.slot for preset in values["presets"]]
                    superseded = supersede(ledger, slots)
            opened = self.session is None
            await self._connect(allow_untested)
            recorder, session = self._recorder, self._session
            recorder.emit(
                "machine.command_requested",
                {"command": command, "params": params or {}},
            )
            await recorder.flush()
            if "backup_path" in values:
                folder = Path(self.store.path).parent / "preset-backups"
                folder.mkdir(mode=0o700, exist_ok=True)
                values["backup_path"] = folder / (uuid.uuid4().hex + ".json")
            attempt: dict[str, Any] | None = None
            before = session._last_preset_write
            pending = {record["slot"]: record for record in superseded}

            async def supersede_slot(slot: str) -> None:
                # Durable before the batch's first frame for this slot, and
                # only for a slot that batch may change.
                record = pending.pop(slot, None)
                if record is not None:
                    recorder.emit(SUPERSEDED, record)
                    await recorder.flush()

            # An attempt's passive telemetry stays captured until its evidence
            # window closes, after its command's terminal record commits, so a
            # save that arrives meanwhile is journaled evidence even without
            # idle capture.
            with ExitStack() as capture:
                if admitted is not None:
                    capture.enter_context(recorder.capturing())
                try:
                    if admitted is not None:
                        attempt = self._begin_preset_write(
                            values["preset"], acknowledge, *admitted
                        )
                        await recorder.flush()
                    if pending:
                        session._before_slot_write = supersede_slot
                    with recorder.capturing():
                        result: dict[str, Any] = await getattr(session, command)(
                            **values
                        )
                except PresetWriteError as exc:
                    result = {
                        "status": "failed",
                        "error": str(exc),
                        "type": type(exc).__name__,
                        "details": deepcopy(exc.result),
                        "recovery_required": session._uncertain,
                        "diagnostic": preset_write_diagnostic(
                            refused=False,
                            cause=str(exc),
                            payload=exc.result,
                            recovery_required=session._uncertain,
                        ),
                    }
                except BaseException as exc:
                    if attempt is not None:
                        # Records the terminal record too; may raise a late
                        # cancellation instead.
                        await self._settle_preset_write(
                            command, attempt, before, capture.close, exc
                        )
                        raise
                    recorder.emit(
                        "machine.command_failed",
                        {
                            "command": command,
                            "type": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
                    if admitted is not None:
                        # The outcome is committed before the guard admits a reader.
                        with suppress(Exception):
                            await wait_cleanup(asyncio.ensure_future(recorder.flush()))
                        if opened:
                            await self._close_session(session, recorder)
                    raise
                finally:
                    session._before_slot_write = None
                if attempt is not None:
                    settled = await self._settle_preset_write(
                        command, attempt, before, capture.close, result=result
                    )
                    assert settled is not None
                    return settled
            recorder.emit(
                "machine.command_finished", {"command": command, "result": result}
            )
            await recorder.flush()
            return result

    def _preset_ledger(self) -> dict[str, Any]:
        if not self.device:
            raise ValueError("Configure a machine address for preset writes")
        rows = self.store.preset_write_events(self.device)
        return fold(rows, self.preset_attempt, self._attempt_evidence)

    def _attempt_evidence(
        self, session_id: str | None, sequence: int | None, cancellation: bool = True
    ) -> str:
        """What a record's own session committed after it."""
        if session_id is None or sequence is None:
            return "sent"
        rows = self.store.session_events_after(session_id, sequence, EVIDENCE_KINDS)
        return attempt_evidence(rows, cancellation)

    def preset_writes(self, slot: str | None = None) -> dict[str, Any]:
        """The latest recorded single-slot write per slot; never connects.

        Each carries its exact preset, attempt outcomes and the next step, so
        a fresh caller can resubmit identical contents or ask the user.
        """
        if slot is not None and slot not in SLOTS:
            raise ValueError("slot must be A, B or C")
        writes = self._preset_ledger()
        return {
            "device": self.device,
            "max_attempts": MAX_ATTEMPTS,
            "writes": [write for key, write in writes.items() if slot in (None, key)],
        }

    async def _close_session(self, session: Interaction, recorder: Recorder) -> None:
        self.session = self.recorder = None
        with suppress(Exception):
            try:
                await session.disconnect()
            finally:
                await recorder.close()

    def _begin_preset_write(
        self,
        preset: SavedPreset,
        acknowledge: str | None,
        write_id: str | None,
        repeat: bool,
    ) -> dict[str, Any]:
        """Check the session, then record the attempt; flush before any frame."""
        session, recorder = self._session, self._recorder
        if session._preset_write_used or session._uncertain:
            raise SessionError(
                "A preset write needs a fresh session; disconnect, then resubmit. "
                "Nothing was sent."
            )
        if repeat:
            state = session.snapshot()["fields"].get("machine_state_code") or {}
            if not state.get("available") or state.get("value") not in IDLE_STATES:
                raise BusyError(
                    "Repeating a preset write needs a current idle report from "
                    "this connection; nothing was sent. Check the machine (for "
                    "example its water), disconnect if still connected and "
                    "resubmit without a separate connect."
                )
            session._require_preset_water()
        attempt = {
            "write_id": write_id or uuid.uuid4().hex,
            "attempt_id": uuid.uuid4().hex,
            **preset.to_dict(),
        }
        # The acknowledgment is part of the new write's first record, so it
        # never commits alone.
        started = (
            attempt if acknowledge is None else {**attempt, "acknowledges": acknowledge}
        )
        recorder.emit(STARTED, started)
        self.preset_attempt = attempt["attempt_id"]
        return attempt

    async def _settle_preset_write(
        self,
        command: str,
        attempt: dict[str, Any],
        before: Any,
        end_capture: Callable[[], None],
        exc: BaseException | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Commit an attempt's outcome and its command's terminal record.

        Runs under the guard and the caller's capture window, which
        ``end_capture`` closes. The outcome is recorded once. Once the first
        terminal record has committed, the capture ends and :data:`CLOSED`
        is recorded in the same step, so every passive row that arrived until
        then, even one queued behind the terminal record, is sequenced inside
        the attempt's evidence window and nothing later is. The first
        terminal record is marked :data:`CLOSE_PENDING`, so a restart that
        finds it without :data:`CLOSED` keeps the window open past it until
        the next boundary. A save in that window wins over the outcome, in
        the ledger and in the summary returned here, which is read after
        :data:`CLOSED` and every row before it commit. A cancellation that
        arrives at any point here is committed as the command's cancellation
        before the guard is released, even when the summary cannot be read,
        so it ends automatic repetition (unless a save was observed) and the
        caller's ``CancelledError`` matches the ledger. With ``exc`` the
        caller then re-raises it, unless a late cancellation is raised here;
        journal failures then stay suppressed. Nothing is recorded for an
        attempt whose ``started`` record is not shown committed: an outcome
        without it would make the whole ledger unreadable, and its failed
        flush preceded every frame.
        """
        recorder, late = self._recorder, False
        cancel_recorded = isinstance(exc, asyncio.CancelledError)

        async def settle(step: Any) -> Any:
            nonlocal late
            task = asyncio.ensure_future(step)
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    late = True
            return task.result()

        def cancel(**marker: Any) -> None:
            nonlocal cancel_recorded
            cancel_recorded = True
            recorder.emit(
                "machine.command_failed",
                {"command": command, "type": "CancelledError", "error": "", **marker},
            )

        # Queued rows, the attempt's start first, finish appending here.
        committed = True
        try:
            await settle(recorder.flush())
        except Exception:
            committed = False
            with suppress(Exception):
                committed = await settle(
                    asyncio.to_thread(self._started_committed, attempt["attempt_id"])
                )
        if not committed:
            self.preset_attempt = None
            end_capture()
            cancel_recorded = cancel_recorded or late
            recorder.emit(
                "machine.command_failed",
                {
                    "command": command,
                    "type": "CancelledError" if late else type(exc).__name__,
                    "error": "" if exc is None else str(exc),
                },
            )
            with suppress(Exception):
                await settle(recorder.flush())
            if late and not cancel_recorded:
                cancel()
                with suppress(Exception):
                    await settle(recorder.flush())
            if late and not isinstance(exc, asyncio.CancelledError):
                raise asyncio.CancelledError() from exc
            if exc is None:
                raise RuntimeError("Journal failed; the attempt was not recorded")
            return None
        outcome = self._finish_preset_write(attempt, before, exc)
        summary: dict[str, Any] | None = None
        # The first terminal record says CLOSED is to follow it, so a restart
        # that finds it without one still credits rows the capture kept.
        pending = {CLOSE_PENDING: True}
        try:
            await settle(recorder.flush())
            if exc is not None:
                recorder.emit(
                    "machine.command_failed",
                    {
                        "command": command,
                        "type": type(exc).__name__,
                        "error": str(exc),
                        **pending,
                    },
                )
            elif late:
                cancel(**pending)
            else:
                assert result is not None
                # The summary is derived from the ledger after this record.
                result["attempt"] = {
                    "attempt_id": attempt["attempt_id"],
                    "write_id": attempt["write_id"],
                }
                recorder.emit(
                    "machine.command_finished",
                    {"command": command, "result": result, **pending},
                )
            window_open = True
            while True:
                await settle(recorder.flush())
                if late and not cancel_recorded:
                    cancel()
                    continue
                if window_open:
                    # Synchronous: no passive row can fall between the two.
                    window_open = False
                    end_capture()
                    recorder.emit(
                        CLOSED,
                        {"command": command, "attempt_id": attempt["attempt_id"]},
                    )
                    continue
                if exc is not None or late:
                    break
                self.preset_attempt = None
                summary = (await settle(asyncio.to_thread(self._preset_ledger)))[
                    attempt["slot"]
                ]
                if not late:
                    break
        except Exception as failure:
            if late and not cancel_recorded:
                # Durable before the guard is released, whatever failed.
                cancel()
                with suppress(Exception):
                    await settle(recorder.flush())
            if exc is None:
                if late:
                    raise asyncio.CancelledError() from failure
                raise
        finally:
            self.preset_attempt = None
        if late and not isinstance(exc, asyncio.CancelledError):
            raise asyncio.CancelledError() from exc
        if exc is not None:
            return None
        assert result is not None and summary is not None
        mine = next(
            (
                item["outcome"]
                for item in summary["attempts"]
                if item["attempt_id"] == attempt["attempt_id"]
            ),
            outcome,  # Lapsed: it provably sent nothing.
        )
        result["attempt"] = {
            "attempt_id": attempt["attempt_id"],
            "outcome": mine,
            **summary,
        }
        if result.get("status") == "failed":
            result["diagnostic"] = preset_write_diagnostic(
                refused=False,
                cause=str(result.get("error") or "Preset write did not finish."),
                payload=result.get("details"),
                attempt=result["attempt"],
                recovery_required=result.get("recovery_required"),
            )
        return result

    def _started_committed(self, attempt_id: str) -> bool:
        return any(
            row["kind"] == STARTED
            and json.loads(row["data"])["attempt_id"] == attempt_id
            for row in self.store.preset_write_events(self.device or "")
        )

    def _finish_preset_write(
        self, attempt: dict[str, Any], before: Any, exc: BaseException | None = None
    ) -> str:
        """Record this attempt's outcome; frames sent are recorded beside it.

        The session's own classification wins (an observed save first); a call
        that never reached it was cancelled or sent nothing.
        """
        latest = self._session._last_preset_write
        mine: dict[str, Any] = {}
        if latest is not None and latest is not before:
            mine = latest
        outcome: str = "refused"
        if mine.get("outcome"):
            outcome = mine["outcome"]
        elif isinstance(exc, asyncio.CancelledError):
            outcome = "cancelled"
        self._recorder.emit(
            FINISHED,
            {
                "write_id": attempt["write_id"],
                "attempt_id": attempt["attempt_id"],
                "slot": attempt["slot"],
                "outcome": outcome,
                "transmitted_write_count": mine.get("transmitted_write_count", 0),
                "partial_frame": bool(mine.get("partial_frame")),
            },
        )
        return outcome

    async def upload(
        self,
        request: Any,
        allow_control: bool = False,
        *,
        allow_untested: bool = False,
        expected_recipe_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Pin and upload one native step. Does not send an execute command."""
        if type(allow_control) is not bool:
            raise ValueError("allow_control must be a boolean")
        if not allow_control:
            raise ControlPermissionError("Upload requires allow_control")
        if type(allow_untested) is not bool:
            raise ValueError("allow_untested must be a boolean")
        self._reject_queued_write()
        async with self.guard:
            self._require_unoccupied()
            plan = await asyncio.to_thread(self._resolve, request)
            if len(plan["execution"]) != 1 or plan["execution"][0]["kind"] not in (
                "native",
                "tea",
            ):
                raise ValueError(
                    "Upload needs exactly one native coffee or tea execution step"
                )
            if (
                expected_recipe_sha256 is not None
                and plan["recipe_sha256"] != expected_recipe_sha256
            ):
                raise ConflictError("Prepared recipe changed; preview it again")
            # Only the hardware-observed coffee profile drops the opt-in here;
            # the plan's own requires_untested still governs a direct start.
            untested = upload_requires_untested(plan)
            if untested and not allow_untested:
                raise ExperimentalOptInError("Recipe requires allow_untested")
            validated = plan["requires_untested"] and not untested
            # Without the opt-in, that exemption holds only for the reviewed
            # preview: its digest pins the exact content before any connection.
            if validated and not allow_untested and expected_recipe_sha256 is None:
                raise ValueError(
                    "A validated prepared coffee upload without experimental "
                    "opt-in needs expected_recipe_sha256 from its machine_prepare "
                    "preview; nothing was sent"
                )
            plan.pop("resolved_request", None)
            step = plan["execution"][0]
            encoded = encoded_step(step)
            await self._connect(allow_untested)
            recorder, session = self._recorder, self._session
            recorder.emit("recipe.upload_requested", {"request": request, "plan": plan})
            await recorder.flush()
            # The service guard covers upload and reservation; no caller slips in.
            try:
                with recorder.capturing():
                    if validated and isinstance(encoded, EncodedRecipe):
                        result = await session._upload_validated_coffee(encoded)
                    elif isinstance(encoded, EncodedRecipe):
                        result = await session.upload_recipe(encoded)
                    else:
                        result = await session.upload_tea(encoded)
                self.prepared = {
                    **result,
                    "recipe_sha256": plan["recipe_sha256"],
                    "readback_verified": False,
                    "request": deepcopy(request),
                }
                self._prepared_plan = deepcopy(plan)
                recorder.emit("recipe.uploaded", self.prepared)
                await recorder.flush()
                return deepcopy(self.prepared)
            except BaseException as exc:
                self.prepared = self._prepared_plan = None
                session._invalidate_preparation("upload_failed")
                session._uncertain = True
                recorder.emit(
                    "recipe.upload_failed",
                    {
                        "type": type(exc).__name__,
                        "error": str(exc),
                        "recovery_required": True,
                    },
                )
                raise

    async def start_uploaded(
        self, token: str, idempotency_key: str, allow_control: bool = False
    ) -> dict[str, Any]:
        """Start the pinned upload once. Replayed keys retrieve its recorded attempt."""
        if type(allow_control) is not bool:
            raise ValueError("allow_control must be a boolean")
        if not allow_control:
            raise ControlPermissionError("Starting requires allow_control")
        if not isinstance(token, str) or not token:
            raise ValueError("A preparation token is required")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("A nonempty idempotency key is required")
        request = {"kind": "uploaded", "token": token}
        self._reject_queued_write()
        async with self.guard:
            await self._own()
            row = await asyncio.to_thread(self.store.operation_by_key, idempotency_key)
            if row:
                self._same_attempt(row, request)
                return row
            if self.task is not None and not self.task.done():
                raise BusyError("Machine already has an operation; nothing queued")
            if self.prepared is None or self.prepared["token"] != token:
                raise ConflictError("No matching prepared recipe; upload explicitly")
            # The Interaction revalidates token, session and context before execute.
            live = self._session.get_state().get("prepared") or {}
            if live.get("token") != token:
                raise ConflictError(
                    "Machine preparation is no longer valid; disconnect or cancel"
                )
            await self._recorder.flush()
            plan = deepcopy(self._prepared_plan) or {}
            return await self._accept_and_launch(
                request, plan, idempotency_key, prepared_token=token
            )

    async def cancel_uploaded(
        self, token: str, allow_control: bool = False
    ) -> dict[str, Any]:
        if type(allow_control) is not bool:
            raise ValueError("allow_control must be a boolean")
        if not allow_control:
            raise ControlPermissionError("Canceling requires allow_control")
        self._reject_queued_write()
        async with self.guard:
            self._require_open()
            if self.prepared is None or self.prepared["token"] != token:
                raise ConflictError("No matching prepared recipe")
            recorder, session = self._recorder, self._session
            try:
                live = session.get_state().get("prepared") or {}
                if live.get("token") == token:
                    with recorder.capturing():
                        result = await session.cancel_prepared(token)
                else:
                    # Release logical ownership without writing into changed context.
                    result = {"token": token, "cancelled": False}
                recorder.emit("recipe.upload_cancelled", result)
                await recorder.flush()
                return result
            except BaseException:
                session._invalidate_preparation("cancel_failed")
                session._uncertain = True
                raise
            finally:
                self.prepared = self._prepared_plan = None

    def _same_attempt(self, row: dict[str, Any], request: Any) -> None:
        """A replayed key must name the same request on this machine.

        Equivalent omitted and explicit defaults are one request; the stored
        request and its fingerprint stay exactly as first accepted.
        """
        if not same_attempt(row["request"], request) or not self._owns_device(
            row["device"]
        ):
            raise ConflictError("Idempotency key belongs to another request")

    def _owns_device(self, recorded: object) -> bool:
        from .connector import normalize_identity

        try:
            return self.device is not None and normalize_identity(recorded) == (
                self.device
            )
        except ValueError:
            return False

    async def _accept_and_launch(
        self,
        request: Any,
        plan: dict[str, Any],
        idempotency_key: str,
        prepared_token: str | None = None,
    ) -> dict[str, Any]:
        """Finish the durable acceptance boundary even if its caller is cancelled.

        Once committed, a daemon operation outlives delivery of the response. A
        retry with the same key retrieves that attempt; it cannot start it
        twice. The caller holds the guard across this boundary and scheduling.
        """
        accepted = asyncio.ensure_future(
            asyncio.to_thread(
                self.store.create_operation,
                str(self.device),
                request,
                plan,
                idempotency_key,
            )
        )
        cancelled = False
        while True:
            try:
                op, created = await asyncio.shield(accepted)
                break
            except asyncio.CancelledError:
                if accepted.cancelled():
                    raise
                cancelled = True
        if created:
            if prepared_token is not None:
                self.prepared = None
                self._prepared_plan = None
            self.stopping = False
            self.finalizing = False
            self.operation_id = op["id"]
            self.projection = deepcopy(op)
            self._recorder.operation_id = op["id"]
            self.worker_started = asyncio.Event()
            self.task = asyncio.create_task(
                self._execute(op["id"], plan, prepared_token=prepared_token)
            )
        if cancelled:
            raise asyncio.CancelledError()
        return op
