"""SSH carrier and restricted relay, with a fake ssh binary and no network."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests  # noqa: F401 - installs the test guards
from xbloom_studio import ssh_rpc
from xbloom_studio.facade import Application
from xbloom_studio.rpc import LocalServer, RemoteError
from xbloom_studio.ssh_rpc import REMOTE_METHODS, relay, ssh_call, validate_host
from xbloom_studio.storage import Store

FAKE = """#!PYTHON
import json, os, signal, subprocess, sys, time
from pathlib import Path
if os.environ["FAKE_SSH_MODE"] == "closed":
    sys.exit(0)
request = sys.stdin.buffer.read()
audit = Path(os.environ["FAKE_SSH_AUDIT"])
audit.write_text(json.dumps({"argv": sys.argv[1:], "request": request.decode()}))
mode = os.environ["FAKE_SSH_MODE"]
if mode == "success":
    print('{"ok":true,"data":{"identity":"remote"}}')
elif mode == "remote_error":
    print('{"ok":false,"error":{"code":"busy","message":"Domain failure"}}')
elif mode == "nonzero":
    print("token=never-expose", file=sys.stderr)
    sys.exit(255)
elif mode == "stdout":
    sys.stdout.write("x" * 10000)
    sys.stdout.flush()
    time.sleep(60)
elif mode == "hang":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
elif mode == "descendant":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (audit.parent / "descendant").write_text(str(child.pid))
    time.sleep(60)
elif mode == "truncated":
    sys.stdout.write('{"ok":true,"data":1}')
elif mode == "malformed":
    print('{"ok":true}')
"""


async def exited(pid, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    status = Path(f"/proc/{pid}/stat")
    while loop.time() < deadline:
        try:
            if status.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        except FileNotFoundError:
            return True
        await asyncio.sleep(0.01)
    return False


def quiet_asyncio(test):
    """Debug-mode event loops log every pipe and subprocess event at INFO."""
    logger = logging.getLogger("asyncio")
    test.addCleanup(logger.setLevel, logger.level)
    logger.setLevel(logging.WARNING)


class CarrierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        quiet_asyncio(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audit = self.root / "audit"
        binary = self.root / "ssh"
        binary.write_text(FAKE.replace("PYTHON", sys.executable, 1))
        binary.chmod(0o700)
        self.env = patch.dict(
            os.environ,
            PATH=str(self.root) + os.pathsep + os.environ["PATH"],
            FAKE_SSH_AUDIT=str(self.audit),
            FAKE_SSH_MODE="success",
        )
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def mode(self, value):
        os.environ["FAKE_SSH_MODE"] = value

    async def test_argv_is_fixed_and_the_request_is_data(self):
        config = self.root / "config with spaces"
        document = {"name": "$(touch /tmp/never)"}
        value = await ssh_call(
            "operator@alias",
            "recipe_validate",
            {"document": document},
            config_file=config,
        )
        self.assertEqual(value, {"identity": "remote"})
        audit = json.loads(self.audit.read_text())
        argv = audit["argv"]
        self.assertEqual(argv[-4:], ["--", "operator@alias", "xbloom-studio", "relay"])
        self.assertEqual(argv[argv.index("-F") + 1], str(config))
        for option in (
            "-oBatchMode=yes",
            "-oStrictHostKeyChecking=yes",
            "-oClearAllForwardings=yes",
            "-oForwardAgent=no",
        ):
            self.assertIn(option, argv)
        sent = json.loads(audit["request"])
        self.assertEqual(sent["params"]["document"], document)

    async def test_invalid_targets_and_requests_never_start_a_process(self):
        for host in ("-oProxyCommand=x", "a b", "a;touch", "a\n", "", None, "x" * 256):
            with self.subTest(host=host), self.assertRaises(ValueError):
                await ssh_call(host, "service_info")
        for timeout in (0, -1, float("inf"), float("nan"), True, None):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                await ssh_call("host", "service_info", timeout=timeout)
        with self.assertRaises(ValueError):
            await ssh_call("host", "x", [])
        self.assertFalse(self.audit.exists())
        self.assertEqual(validate_host("host-alias"), "host-alias")

    async def test_remote_domain_errors_are_preserved(self):
        self.mode("remote_error")
        with self.assertRaises(RemoteError) as caught:
            await ssh_call("host", "operation_start")
        self.assertEqual(caught.exception.detail["code"], "busy")

    async def test_transport_failures_are_uncertain_and_never_leak_output(self):
        for mode in ("nonzero", "truncated", "malformed"):
            self.mode(mode)
            with self.subTest(mode=mode):
                with self.assertRaises(ConnectionError) as caught:
                    await ssh_call("host", "operation_start")
                self.assertIn("operation_lookup", str(caught.exception))
                self.assertNotIn("never-expose", str(caught.exception))

    async def test_output_limits_and_timeouts_reap_the_process_group(self):
        self.mode("stdout")
        with patch.object(ssh_rpc, "MAX_RESPONSE", 100):
            with self.assertRaisesRegex(ConnectionError, "limit"):
                await ssh_call("host", "service_info")
        self.mode("hang")
        with self.assertRaisesRegex(ConnectionError, "timed out"):
            await ssh_call("host", "service_info", timeout=0.5)
        self.mode("descendant")
        with self.assertRaises(ConnectionError):
            await ssh_call("host", "service_info", timeout=1)
        pid = int((self.root / "descendant").read_text())
        self.assertTrue(await exited(pid))

    async def test_missing_ssh_is_reported_before_submission(self):
        with patch.dict(os.environ, PATH=str(self.root / "empty")):
            with self.assertRaisesRegex(ConnectionError, "no RPC request"):
                await ssh_call("host", "service_info")

    async def test_closed_input_is_uncertain(self):
        self.mode("closed")
        spawn, processes = asyncio.create_subprocess_exec, []

        async def spawned(*args, **kwargs):
            processes.append(await spawn(*args, **kwargs))
            return processes[-1]

        with (
            patch.object(asyncio, "create_subprocess_exec", spawned),
            self.assertRaisesRegex(ConnectionError, "connection closed"),
        ):
            await ssh_call("host", "recipe_validate", {"document": "x" * 1_000_000})
        # Python 3.10 never retrieves the stdin-closed error and logs it when
        # the future is freed; retrieve it here.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await processes[0].stdin.wait_closed()


class StdIO:
    def __init__(self, data=b""):
        read, write = os.pipe()
        os.write(write, data)
        os.close(write)
        self.buffer = os.fdopen(read, "rb", buffering=0)


class Output:
    def __init__(self, broken=False):
        self.buffer = io.BytesIO()
        if broken:

            def fail(data):
                raise BrokenPipeError()

            self.buffer.write = fail


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        quiet_asyncio(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "xbloom-studio.sqlite3")
        self.app = Application(self.store, "00:00:5E:00:53:01")
        self.socket = self.root / "run" / "daemon.sock"
        self.server = await LocalServer(self.app, self.socket).start()

    async def asyncTearDown(self):
        await self.server.close()
        self.store.close()
        self.tmp.cleanup()

    async def relay(self, request, broken=False):
        data = request if isinstance(request, bytes) else json.dumps(request).encode()
        stdin, stdout = StdIO(data + b"\n"), Output(broken)
        try:
            with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout):
                code = await relay(self.socket)
        finally:
            stdin.buffer.close()
        if broken:
            return code, None
        return code, json.loads(stdout.buffer.getvalue())

    async def test_relay_forwards_allowed_methods_to_the_fixed_socket(self):
        code, reply = await self.relay({"method": "service_info", "params": {}})
        self.assertEqual(code, 0)
        self.assertEqual(reply["data"]["device"], "00:00:5E:00:53:01")
        code, reply = await self.relay(
            {"method": "history_get", "params": {"ident": "x"}}
        )
        self.assertEqual(reply["error"]["code"], "not_found")

    async def test_relay_refuses_local_only_and_malformed_requests(self):
        for request in (
            {"method": "history_backup", "params": {"destination": "/tmp/x"}},
            {"method": "provider_write", "params": {}},
            b"not json",
        ):
            with self.subTest(request=request):
                code, reply = await self.relay(request)
                self.assertEqual(code, 0)
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"]["code"], "invalid_argument")
        self.assertNotIn("history_backup", REMOTE_METHODS)

    async def test_relay_bounds_responses_and_reports_closed_stdout(self):
        with patch.object(ssh_rpc, "MAX_RESPONSE", 1200):
            code, reply = await self.relay({"method": "machine_capabilities"})
        self.assertEqual(reply["error"]["code"], "connection_unavailable")
        code, _ = await self.relay({"method": "service_info"}, broken=True)
        self.assertEqual(code, 1)

    def test_relay_command_line_fixes_its_socket(self):
        with patch.object(ssh_rpc, "relay") as fake:

            async def done(path):
                return 0

            fake.side_effect = done
            self.assertEqual(ssh_rpc.main(["--socket", "/x/sock"]), 0)
            self.assertEqual(ssh_rpc.main([]), 0)
        self.assertEqual(
            [c.args[0] for c in fake.call_args_list],
            ["/x/sock", "/run/xbloom-studio/daemon.sock"],
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            ssh_rpc.main(["--help"])
        self.assertTrue(out.getvalue().startswith("usage: xbloom-studio relay "))


if __name__ == "__main__":
    unittest.main()
