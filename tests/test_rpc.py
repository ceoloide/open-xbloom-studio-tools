"""Local daemon RPC: bounded framing, private sockets, safe errors and teardown."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError, URLError

import tests  # noqa: F401 - installs the test guards
from xbloom_studio import rpc_wire
from xbloom_studio._cleanup import wait_cleanup
from xbloom_studio.client import ProtocolCommandError, SessionError
from xbloom_studio.command_schema import CommandValidationError
from xbloom_studio.control import BusyError
from xbloom_studio.errors import error_detail
from xbloom_studio.facade import Application
from xbloom_studio.machine_controls import PresetWriteError, UntestedOperationError
from xbloom_studio.ownership import AddressLease, LeaseDirectoryError
from xbloom_studio.providers import ProviderError
from xbloom_studio.recipe_validation import RecipeValidationError
from xbloom_studio.rpc import LocalServer, RemoteError, call, default_socket
from xbloom_studio.rpc_types import SocketPathError
from xbloom_studio.storage import ConflictError, Store


class WireTests(unittest.TestCase):
    def test_requests_are_one_bounded_object_line(self):
        raw = rpc_wire.frame({"method": "m", "params": {}}, 100)
        self.assertEqual(rpc_wire.request(raw), {"method": "m", "params": {}})
        bad = [
            b"",
            b'{"method":"m"}',
            b'{"method":"m"}\n{"method":"m"}\n',
            b"[]\n",
            b'{"method":" "}\n',
            b'{"method":"m","x":1}\n',
            b'{"method":"m","params":[]}\n',
            b'{"method":"m","params":NaN}\n',
            b"\xff\n",
            b"[" * 100000 + b"\n",
        ]
        for value in bad:
            with self.subTest(value=value[:20]), self.assertRaises(ValueError):
                rpc_wire.request(value)
        with self.assertRaises(ValueError):
            rpc_wire.request(raw, 3)
        with self.assertRaises(ValueError):
            rpc_wire.frame({"method": "m" * 200}, 100)

    def test_responses_are_closed_envelopes(self):
        self.assertTrue(rpc_wire.response(b'{"ok":true,"data":1}\n')["ok"])
        error = b'{"ok":false,"error":{"message":"m"}}\n'
        self.assertFalse(rpc_wire.response(error)["ok"])
        for value in (
            b'{"ok":1,"data":1}\n',
            b'{"ok":true}\n',
            b'{"ok":true,"data":1,"x":2}\n',
            b'{"ok":false,"error":"text"}\n',
            b'{"ok":false,"error":{"message":1}}\n',
            b"1\n",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                rpc_wire.response(value)


class ErrorContractTests(unittest.TestCase):
    def code(self, exc):
        return error_detail(exc)["code"]

    def test_known_errors_keep_guidance_and_never_invite_retry(self):
        report = {"valid": False, "errors": [{"path": "/x", "message": "bad"}]}
        http_error = HTTPError("https://example.com", 404, "x", None, None)
        self.addCleanup(http_error.close)
        cases = [
            (RecipeValidationError(report), "validation_error"),
            (
                CommandValidationError([{"path": "/p", "message": "m"}]),
                "validation_error",
            ),
            (PresetWriteError("m", {"backup": {}}), "preset_write_incomplete"),
            (UntestedOperationError("m"), "experimental_required"),
            (
                ProtocolCommandError({"kind": "k", "value": 1}, 8100),
                "machine_protocol_error",
            ),
            (BusyError("m"), "busy"),
            (ConflictError("m"), "conflict"),
            (KeyError("Operation not found"), "not_found"),
            (ProviderError("m"), "provider_error"),
            (http_error, "provider_error"),
            (URLError("down"), "provider_unavailable"),
            (SessionError("m"), "connection_unavailable"),
            (FileExistsError("x"), "conflict"),
            (FileNotFoundError("x"), "connection_unavailable"),
            (ConnectionRefusedError("x"), "connection_unavailable"),
            (TimeoutError("x"), "connection_unavailable"),
            (json.JSONDecodeError("m", "doc", 0), "invalid_argument"),
            (ValueError("needs allow_control"), "permission_required"),
            (ValueError("needs allow_untested"), "experimental_required"),
            (TypeError("bad"), "invalid_argument"),
        ]
        for exc, code in cases:
            with self.subTest(exc=type(exc).__name__):
                detail = error_detail(exc)
                self.assertEqual(detail["code"], code)
                self.assertFalse(detail["retryable"])
                self.assertNotIn("retry the start", detail["next_step"].lower())

    def test_unsafe_lease_directory_has_actionable_permission_error(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "leases"
            directory.mkdir()
            directory.chmod(0o755)
            with patch(
                "xbloom_studio.ownership.lease_directory", return_value=directory
            ):
                with self.assertRaises(LeaseDirectoryError) as raised:
                    AddressLease("synthetic").acquire()
        detail = error_detail(raised.exception)
        self.assertEqual(detail["code"], "permission_required")
        self.assertIn("0700", detail["next_step"])

    def test_readonly_account_home_preserves_permission_guidance(self):
        failure = OSError(errno.EROFS, "Read-only file system")
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "leases"
            directory.mkdir(mode=0o700)
            for target in ("pathlib.Path.mkdir", "xbloom_studio.ownership.os.open"):
                with (
                    self.subTest(target=target),
                    patch(
                        "xbloom_studio.ownership.lease_directory",
                        return_value=directory,
                    ),
                    patch(target, side_effect=failure),
                    self.assertRaises(LeaseDirectoryError) as raised,
                ):
                    AddressLease("synthetic").acquire()
                detail = error_detail(raised.exception)
                self.assertEqual(detail["code"], "permission_required")
                self.assertIn("write there", detail["next_step"])

    def test_unexpected_errors_hide_internals_with_a_correlation_id(self):
        with self.assertLogs("xbloom_studio.errors", "ERROR"):
            detail = error_detail(RuntimeError("secret internal path"))
        self.assertEqual(detail["code"], "internal_error")
        self.assertNotIn("secret", json.dumps(detail))
        self.assertEqual(len(detail["correlation_id"]), 32)
        quiet = error_detail(KeyError("other"), log_unexpected=False)
        self.assertEqual(quiet["code"], "internal_error")

    def test_remote_envelopes_are_filtered(self):
        remote = RemoteError({"code": "busy", "message": "m", "secret": "x"})
        filtered = error_detail(remote)
        self.assertEqual(set(filtered), {"code", "message", "diagnostic"})
        self.assertEqual((filtered["code"], filtered["message"]), ("busy", "m"))
        self.assertEqual(filtered["diagnostic"]["code"], "XB_MACHINE_BUSY")
        self.assertEqual(
            error_detail(RemoteError({"code": "other", "message": "m"}))["code"],
            "internal_error",
        )
        self.assertEqual(str(RemoteError({})), "Remote request failed")


class ServerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "xbloom-studio.sqlite3")
        self.app = Application(self.store, "00:00:5E:00:53:01")
        self.path = self.root / "run" / "daemon.sock"

    async def asyncTearDown(self):
        await self.app.close()
        self.store.close()
        self.tmp.cleanup()


class LocalServerTests(ServerCase):
    async def test_overlong_filename_is_an_actionable_socket_error(self):
        path = self.root / ("x" * 256)
        with self.assertRaises(SocketPathError):
            await LocalServer(self.app, path).start()
        self.assertFalse(self.app.machine.owned)

    async def test_stale_recovery_rejects_overlong_alias_without_unlinking(self):
        parent = self.root / ("directory-" * 10)
        parent.mkdir(mode=0o700)
        alias = self.root / "short"
        alias.symlink_to(parent, target_is_directory=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(alias / "daemon.sock"))
        path = parent / "daemon.sock"
        with self.assertRaises(SocketPathError):
            await LocalServer(self.app, path, recover_stale=True).start()
        self.assertTrue(path.is_socket())
        self.assertFalse(self.app.machine.owned)

    async def test_overlong_socket_path_has_actionable_error(self):
        path = self.root / "run" / ("x" * 110 + ".sock")
        with self.assertRaises(SocketPathError) as bind_error:
            await LocalServer(self.app, path).start()
        with self.assertRaises(SocketPathError) as connect_error:
            await call(path, "service_info")
        for exc in (bind_error.exception, connect_error.exception):
            detail = error_detail(exc)
            self.assertEqual(detail["code"], "invalid_argument")
            self.assertIn("shorter --socket path", detail["next_step"])
        self.assertFalse(self.app.machine.owned)

    async def test_private_socket_serves_and_removes_only_itself(self):
        server = await LocalServer(self.app, self.path).start()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            info = await call("~/run/daemon.sock", "service_info")
        self.assertEqual(info["device"], "00:00:5E:00:53:01")
        with self.assertRaises(RemoteError) as caught:
            await call(self.path, "history_get", {"ident": "missing"})
        self.assertEqual(caught.exception.detail["code"], "not_found")
        with self.assertRaises(RemoteError) as caught:
            await call(self.path, "no_such_method")
        self.assertEqual(caught.exception.detail["code"], "invalid_argument")
        await server.close()
        await server.close()
        self.assertFalse(self.path.exists())

    async def test_invalid_and_oversized_lines_get_error_envelopes(self):
        server = await LocalServer(self.app, self.path).start()
        try:
            for payload in (b"not json\n", b'{"method":"m","params":[]}\n'):
                reader, writer = await asyncio.open_unix_connection(str(self.path))
                writer.write(payload)
                await writer.drain()
                reply = json.loads(await reader.readline())
                writer.close()
                await writer.wait_closed()
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"]["code"], "invalid_argument")
            with self.assertRaisesRegex(ValueError, "too large"):
                await call(self.path, "m", {"x": "y" * rpc_wire.MAX_REQUEST})
        finally:
            await server.close()

    async def test_oversized_start_response_keeps_the_operation_identity(self):
        server = await LocalServer(self.app, self.path).start()
        big = {"id": "op-1", "status": "accepted", "pad": "x" * 4096}
        try:
            with (
                patch("xbloom_studio.rpc.MAX_RESPONSE", 2048),
                patch.object(self.app, "call", AsyncMock(return_value=big)),
            ):
                with self.assertRaises(RemoteError) as caught:
                    await call(
                        self.path,
                        "operation_start",
                        {"request": {}, "idempotency_key": "k"},
                    )
                detail = caught.exception.detail
                self.assertEqual(detail["code"], "response_too_large")
                self.assertEqual(
                    detail["details"], {"operation_id": "op-1", "idempotency_key": "k"}
                )
                self.assertIn("Never start again", detail["next_step"])
                with self.assertRaises(RemoteError) as caught:
                    await call(self.path, "history_list")
                self.assertEqual(caught.exception.detail["details"], {})
        finally:
            await server.close()

    async def test_unsafe_directories_and_existing_paths_are_refused(self):
        self.path.parent.mkdir(mode=0o755)
        os.chmod(self.path.parent, 0o755)
        with self.assertRaises(PermissionError) as raised:
            await LocalServer(self.app, self.path).start()
        detail = error_detail(raised.exception)
        self.assertEqual(detail["code"], "permission_required")
        self.assertIn("0700", detail["next_step"])
        os.chmod(self.path.parent, 0o700)
        self.path.write_text("not a socket")
        with self.assertRaises(FileExistsError):
            await LocalServer(self.app, self.path).start()
        with self.assertRaisesRegex(FileExistsError, "plain socket"):
            await LocalServer(self.app, self.path, recover_stale=True).start()
        self.assertTrue(self.path.exists())
        with self.assertRaises(ValueError):
            LocalServer(self.app, self.path, recover_stale="yes")

    async def test_stale_socket_recovery_requires_a_refused_unchanged_socket(self):
        self.path.parent.mkdir(mode=0o700)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.path))
        stale.close()  # Bound but never listening: connections are refused.
        server = await LocalServer(self.app, self.path, recover_stale=True).start()
        self.assertEqual(
            (await call(self.path, "service_info"))["database"], str(self.store.path)
        )
        await server.close()

    async def test_an_active_listener_is_never_treated_as_stale(self):
        other_store = Store(self.root / "other.sqlite3")
        other = Application(other_store)
        live = await LocalServer(other, self.path).start()
        try:
            with self.assertRaisesRegex(FileExistsError, "active listener"):
                await LocalServer(self.app, self.path, recover_stale=True).start()
            await call(self.path, "service_info")
        finally:
            await live.close()
            other_store.close()

    async def test_second_server_on_one_database_is_refused(self):
        server = await LocalServer(self.app, self.path).start()
        rival = Application(self.store, "00:00:5E:00:53:01")
        try:
            with self.assertRaisesRegex(ConnectionError, "owns"):
                await LocalServer(rival, self.root / "run" / "other.sock").start()
            self.assertFalse((self.root / "run" / "other.sock").exists())
        finally:
            await server.close()

    async def test_client_reports_invalid_responses_as_unavailable(self):
        self.path.parent.mkdir(mode=0o700)

        async def reply(reader, writer):
            await reader.readline()
            writer.write(b"garbage\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(reply, str(self.path))
        try:
            with self.assertRaisesRegex(ConnectionError, "Invalid or incomplete"):
                await call(self.path, "service_info")
        finally:
            server.close()
            await server.wait_closed()

    async def test_client_timeout_reports_possible_delivery(self):
        self.path.parent.mkdir(mode=0o700)
        received, closed = asyncio.Event(), asyncio.Event()

        async def withhold(reader, writer):
            try:
                await reader.readline()
                received.set()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()

        server = await asyncio.start_unix_server(withhold, str(self.path))
        try:
            with self.assertRaises(ConnectionError) as raised:
                await call(self.path, "service_info", timeout=0.25)
            detail = error_detail(raised.exception)
            self.assertEqual(detail["code"], "connection_unavailable")
            self.assertIn("original idempotency key", detail["next_step"])
            self.assertTrue(received.is_set())
            await asyncio.wait_for(closed.wait(), timeout=1)
        finally:
            server.close()
            await server.wait_closed()

    async def test_slow_handlers_are_bounded_at_close(self):
        server = await LocalServer(self.app, self.path).start()
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        await asyncio.sleep(0.05)
        with patch("asyncio.wait", AsyncMock(return_value=(set(), set()))):
            await server.close()
        writer.close()
        self.assertEqual(server.handlers, set())

    async def test_default_socket_has_private_fallback_beside_existing_leases(self):
        with (
            patch.object(Path, "home", return_value=self.path.parent),
            patch("pwd.getpwuid") as account,
            patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}),
        ):
            account.return_value.pw_dir = self.path.parent
            await self.app.machine._own()
            self.assertEqual(
                default_socket(),
                self.path.parent / ".local/state/xbloom-studio/run/daemon.sock",
            )
            server = await LocalServer(self.app).start()
            try:
                self.assertEqual(stat.S_IMODE(server.path.parent.stat().st_mode), 0o700)
            finally:
                await server.close()

    def test_default_socket_uses_the_runtime_directory(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}):
            self.assertEqual(
                default_socket(), Path("/run/user/1000/xbloom-studio/daemon.sock")
            )


class CleanupBarrierTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_waiter_lets_cleanup_finish_then_cancels(self):
        release = asyncio.Event()

        async def cleanup():
            await release.wait()
            return "done"

        task = asyncio.create_task(cleanup())
        waiter = asyncio.create_task(wait_cleanup(task))
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(task.result(), "done")


class FinishContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "xbloom-studio.sqlite3")
        self.op, _ = self.store.create_operation("D", {"kind": "x"}, {"plan": 1}, "k")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def kinds(self):
        return [e["kind"] for e in self.store.events(self.op["id"])]

    def test_only_known_transitions_of_existing_operations(self):
        for status in ("accepted", "done", "", None):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.store.finish(self.op["id"], status)
        with self.assertRaises(KeyError):
            self.store.finish("missing", "running")
        self.store.finish(self.op["id"], "running")
        self.store.finish(self.op["id"], "waiting_for_user", {"gate": 1})
        self.store.finish(self.op["id"], "running")
        self.assertEqual(self.store.operation(self.op["id"])["status"], "running")

    def test_terminal_outcome_is_final_and_identical_retry_is_a_no_op(self):
        self.store.finish(self.op["id"], "completed", {"ok": True})
        before = self.kinds()
        self.store.finish(self.op["id"], "completed", {"ok": True})
        self.assertEqual(self.kinds(), before)
        for status, result in (
            ("completed", {"ok": False}),
            ("unknown", None),
            ("running", None),
            ("failed", {"ok": True}),
        ):
            with self.subTest(status=status), self.assertRaises(ConflictError):
                self.store.finish(self.op["id"], status, result)
        row = self.store.operation(self.op["id"])
        self.assertEqual((row["status"], row["result"]), ("completed", {"ok": True}))
        self.assertEqual(self.kinds(), before)

    def test_recovered_unknown_is_never_overwritten(self):
        self.store.finish(self.op["id"], "running")
        self.assertEqual(self.store.recover(), [self.op["id"]])
        with self.assertRaises(ConflictError):
            self.store.finish(self.op["id"], "completed", {"late": True})
        self.assertEqual(self.store.operation(self.op["id"])["status"], "unknown")
        self.assertEqual(self.store.recover(), [])


if __name__ == "__main__":
    unittest.main()
