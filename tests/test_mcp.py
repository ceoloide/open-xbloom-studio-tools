"""MCP adapter: closed contracts over the same application dispatch."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.machine import Machine
from xbloom_studio import mcp_server
from xbloom_studio.facade import Application
from xbloom_studio.mcp_contracts import compact_state
from xbloom_studio.mcp_server import create_server
from xbloom_studio.recipe_validation import HOST_STEPS
from xbloom_studio.storage import Store

FAST = {"settle": 0, "subscription_settle": 0, "transition_delay": 0}
WATER = {"kind": "water", "volume_ml": 50, "temperature_c": 20}


class ToolCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "xbloom-studio.sqlite3")
        self.machine = Machine("SIM-MCP", auto_complete=True)
        self.app = Application(
            self.store, connector=self.machine, interaction_options=FAST
        )
        self.calls = []

        async def caller(method, params):
            self.calls.append((method, params))
            return await self.app.call(method, params)

        self.server = create_server(caller=caller)

    async def asyncTearDown(self):
        await self.app.close()
        self.store.close()
        self.tmp.cleanup()

    async def tool(self, name, arguments=None):
        result = await self.server.call_tool(name, arguments or {})
        return result.is_error, result.structured_content


class ContractTests(ToolCase):
    async def test_tools_advertise_closed_strict_schemas(self):
        tools = {tool.name: tool for tool in await self.server.list_tools()}
        self.assertEqual(len(tools), 42)
        for name, tool in tools.items():
            with self.subTest(tool=name):
                self.assertFalse(tool.input_schema.get("additionalProperties", False))
                self.assertIn("properties", tool.output_schema)
        command = tools["machine_command"].input_schema
        self.assertIn("set_brightness", command["properties"]["command"]["enum"])
        self.assertNotIn("start_water", command["properties"]["command"]["enum"])
        self.assertIn("oneOf", command)
        description = tools["machine_command"].description
        self.assertIn("allow_control=True", description)
        self.assertIn("check machine_capabilities", description)
        self.assertIn("commands also require allow_untested", description)
        start = tools["operation_start"]
        self.assertTrue(start.annotations.destructive_hint)
        self.assertTrue(start.annotations.idempotent_hint)
        self.assertTrue(tools["machine_state"].annotations.read_only_hint)
        self.assertTrue(tools["provider_search"].annotations.open_world_hint)
        self.assertNotIn("history_backup", tools)
        self.assertNotIn("provider_write", tools)

    async def test_raw_arguments_are_validated_before_dispatch(self):
        cases = [
            ("recipe_get", {"reference": "x", "extra": 1}, "/extra"),
            ("recipe_get", {"reference": "   "}, "/reference"),
            (
                "operation_start",
                {
                    "request": {"kind": "water", "volume_ml": "50"},
                    "idempotency_key": "k",
                },
                "/request/water/volume_ml",
            ),
            (
                "operation_start",
                {"request": WATER, "idempotency_key": "k", "allow_control": "true"},
                "/allow_control",
            ),
            ("history_feedback", {"ident": "o", "note": "n", "rating": 6}, "/rating"),
            (
                "recipe_upload",
                {"request": WATER, "expected_recipe_sha256": "XYZ"},
                "/expected_recipe_sha256",
            ),
        ]
        for name, arguments, path in cases:
            with self.subTest(tool=name):
                error, value = await self.tool(name, arguments)
                self.assertTrue(error)
                self.assertEqual(value["error"]["code"], "invalid_argument")
                paths = [e["path"] for e in value["error"]["details"]["errors"]]
                self.assertIn(path, paths)
        self.assertEqual(self.calls, [])
        error, value = await self.tool(
            "machine_command", {"command": "set_brightness", "params": {"level": 99}}
        )
        self.assertEqual(value["error"]["code"], "validation_error")
        self.assertEqual(self.machine.calls, [])

    async def test_failed_machine_command_marks_tool_error_and_keeps_evidence(self):
        outcome = {
            "status": "failed",
            "error": "incomplete",
            "type": "PresetWriteError",
            "details": {"backup": {"slot": "A"}},
            "recovery_required": True,
        }

        async def caller(method, params):
            self.assertEqual(method, "machine_command")
            return outcome

        server = create_server(caller=caller)
        result = await server.call_tool(
            "machine_command", {"command": "back_home", "allow_control": True}
        )
        self.assertTrue(result.is_error)
        self.assertEqual(
            {k: v for k, v in result.structured_content.items() if k != "diagnostic"},
            outcome,
        )
        self.assertEqual(
            result.structured_content["diagnostic"]["code"],
            "XB_PRESET_WRITE_INCOMPLETE",
        )

    async def test_preset_write_attempts_are_reported_and_discoverable(self):
        preset = {"slot": "A", "payload_hex": "6e6577", "flags": 4}
        arguments = {
            "command": "write_preset",
            "params": {"preset": preset},
            "allow_control": True,
            "allow_untested": True,
        }
        error, value = await self.tool("machine_command", arguments)
        self.assertFalse(error, value)
        self.assertIsNone(value["backup"])
        self.assertEqual(value["attempt"]["state"], "saved")
        error, listing = await self.tool("machine_preset_writes", {"slot": "A"})
        self.assertFalse(error, listing)
        write = listing["writes"][0]
        self.assertEqual((write["preset"], write["attempts_used"]), (preset, 1))
        error, value = await self.tool("machine_command", arguments)
        self.assertTrue(error)
        self.assertEqual(value["error"]["code"], "conflict")
        self.assertEqual(
            value["error"]["details"]["preset_write"]["write_id"], write["write_id"]
        )

    async def test_machine_command_reports_the_acknowledged_mode(self):
        cases = (
            ("set_mode", {"mode": "recipes"}, "recipes"),
            (
                "write_preset",
                {"preset": {"slot": "A", "payload_hex": "6e", "flags": 4}},
                "pro",
            ),
        )
        for command, params, mode in cases:
            with self.subTest(command):
                error, value = await self.tool(
                    "machine_command",
                    {
                        "command": command,
                        "params": params,
                        "allow_control": True,
                        "allow_untested": True,
                    },
                )
                self.assertFalse(error, value)
                self.assertEqual(value["acknowledged_mode"], mode)

    async def test_unknown_tools_and_domain_errors_are_structured(self):
        error, value = await self.tool("history_backup", {"destination": "/tmp/x"})
        self.assertEqual((error, value["error"]["code"]), (True, "unknown_tool"))
        error, value = await self.tool("history_get", {"ident": "missing"})
        self.assertEqual(value["error"]["code"], "not_found")
        error, value = await self.tool(
            "operation_start", {"request": WATER, "idempotency_key": "k"}
        )
        self.assertEqual(value["error"]["code"], "permission_required")

    async def test_start_stop_and_lookup_through_tools(self):
        error, started = await self.tool(
            "operation_start",
            {"request": WATER, "idempotency_key": "mcp", "allow_control": True},
        )
        self.assertFalse(error)
        self.assertEqual(self.calls[-1][1]["request"]["volume_ml"], 50)
        await self.app.machine.task
        error, found = await self.tool("operation_lookup", {"idempotency_key": "mcp"})
        self.assertEqual(found["operation"]["id"], started["id"])
        error, state = await self.tool("machine_state")
        self.assertEqual(state["operation"]["id"], started["id"])
        self.assertNotIn("plan", state["operation"])
        self.assertNotIn("events", state["machine"]["machine"])
        error, full = await self.tool("machine_state", {"detail": "full"})
        self.assertIn("plan", full["operation"])
        error, value = await self.tool(
            "recipe_templates",
        )
        self.assertEqual(len(value["result"]), 3)
        error, value = await self.tool(
            "machine_command",
            {
                "command": "set_brightness",
                "params": {"level": 4},
                "allow_control": True,
            },
        )
        self.assertTrue(value["acknowledged"])

    async def test_request_contract_follows_the_shared_step_rules(self):
        tools = {tool.name: tool for tool in await self.server.list_tools()}
        schema = tools["operation_start"].input_schema["$defs"]
        for kind, model in (("water", "WaterRequest"), ("grind", "GrindRequest")):
            properties = schema[model]["properties"]
            required = set(schema[model]["required"])
            for name, rule in HOST_STEPS[kind].items():
                with self.subTest(kind=kind, field=name):
                    self.assertEqual(name in required, bool(rule.get("required")))
                    if "default" in rule:
                        self.assertEqual(properties[name]["default"], rule["default"])
                    if "minimum" in rule:
                        values = properties[name].get("anyOf", [properties[name]])[0]
                        self.assertEqual(values["minimum"], rule["minimum"])
                        self.assertEqual(values["maximum"], rule["maximum"])
        accepted = [
            {"kind": "grind", "size": 20.0, "rpm": 80.0, "duration_s": 2},
            {**WATER, "timeout": 60},
            {**WATER, "volume_ml": 1000, "flow_ml_s": 0.1, "timeout": 3600},
        ]
        for request in accepted:
            with self.subTest(request=request):
                error, value = await self.tool("machine_prepare", {"request": request})
                self.assertFalse(error, value)
                self.assertEqual(self.calls[-1][1]["request"]["kind"], request["kind"])
        self.assertEqual(self.calls[0][1]["request"]["size"], 20.0)
        self.calls.clear()
        rejected = [
            ({"kind": "water", "volume_ml": 50}, "/request/water/temperature_c"),
            ({**WATER, "volume_ml": 1000, "flow_ml_s": 0.1}, "/request/water"),
            ({**WATER, "timeout": 0}, "/request/water/timeout"),
            ({**WATER, "timeout": None}, "/request/water/timeout"),
            ({**WATER, "name": "   "}, "/request/water"),
            ({**WATER, "name": None}, "/request/water/name"),
            (
                {"kind": "grind", "size": 20.5, "rpm": 80, "duration_s": 2},
                "/request/grind/size",
            ),
            (
                {"kind": "grind", "size": True, "rpm": 80, "duration_s": 2},
                "/request/grind/size",
            ),
        ]
        for request, path in rejected:
            with self.subTest(request=request):
                error, value = await self.tool("machine_prepare", {"request": request})
                self.assertTrue(error)
                self.assertEqual(value["error"]["code"], "invalid_argument")
                paths = [e["path"] for e in value["error"]["details"]["errors"]]
                self.assertIn(path, paths)
                # The application refuses the same request with the same rules.
                with self.assertRaises(ValueError):
                    self.app.machine.prepare(request)
        self.assertEqual(self.calls, [])

    async def test_mcp_defaults_replay_the_attempt_from_other_adapters(self):
        error, started = await self.tool(
            "operation_start",
            {"request": WATER, "idempotency_key": "cross", "allow_control": True},
        )
        self.assertFalse(error, started)
        sent = self.calls[-1][1]["request"]
        self.assertEqual(sent, {**WATER, "flow_ml_s": 3, "pattern": "center"})
        await self.app.machine.task
        direct = await self.app.call(
            "operation_start",
            {"request": WATER, "idempotency_key": "cross", "allow_control": True},
        )
        self.assertEqual(direct["id"], started["id"])
        error, again = await self.tool(
            "operation_start",
            {"request": WATER, "idempotency_key": "cross", "allow_control": True},
        )
        self.assertEqual(again["id"], started["id"])
        self.assertEqual(self.machine.codes().count(4506), 1)

    async def test_output_contract_failures_keep_the_operation_identity(self):
        async def odd(method, params):
            return {"id": "op-7", "unexpected": True}

        server = create_server(caller=odd)
        with self.assertLogs("xbloom_studio.mcp_server", "ERROR"):
            result = await server.call_tool(
                "operation_start",
                {"request": WATER, "idempotency_key": "k", "allow_control": True},
            )
        error = result.structured_content["error"]
        self.assertEqual(error["code"], "output_contract_error")
        self.assertEqual(error["details"], {"operation_id": "op-7"})
        with self.assertLogs("xbloom_studio.mcp_server", "ERROR"):
            result = await server.call_tool("recipe_get", {"reference": "x"})
        self.assertEqual(result.structured_content["error"]["details"], {})

    async def test_every_tool_reaches_the_application(self):
        recipe = await self.app.call(
            "recipe_create", {"document": {"name": "R", "steps": [WATER]}}
        )
        arguments = {
            "recipe_examples": {"coffee": {"origin": "Kenya"}},
            "recipe_validate": {"document": {"name": "R", "steps": [WATER]}},
            "recipe_tea_validate": {"document": {}},
            "recipe_preview": {"document": {"name": "R", "steps": [WATER]}},
            "recipe_create": {"document": {"name": "S", "steps": [WATER]}},
            "recipe_get": {"reference": "R"},
            "recipe_compare": {"reference": "R", "before": 1, "after": 1},
            "recipe_update": {
                "reference": "R",
                "document": {"name": "R", "steps": [WATER]},
                "expected_revision": 1,
            },
            "recipe_alias": {"reference": "R", "alias": "B"},
            "recipe_match": {"coffee": {"name": "R"}},
            "recipe_compile": {"reference": "R"},
            "recipe_archive": {"reference": "S", "expected_revision": 1},
            "history_list": {},
            "history_events": {},
            "machine_prepare": {"request": WATER},
            "machine_preset_writes": {"slot": "A"},
        }
        for name, params in arguments.items():
            with self.subTest(tool=name):
                error, value = await self.tool(name, params)
                self.assertFalse(error, value)
        self.assertEqual(recipe["name"], "R")


class ServerSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_groups_select_tools(self):
        async def caller(method, params):
            return {}

        server = create_server(groups=("recipes",), caller=caller)
        names = {tool.name for tool in await server.list_tools()}
        self.assertIn("recipe_get", names)
        self.assertNotIn("operation_start", names)
        with self.assertRaises(ValueError):
            create_server(groups=("everything",), caller=caller)

    async def test_transports_are_exclusive_and_ssh_is_validated(self):
        with self.assertRaises(ValueError):
            create_server(ssh_config="x")
        with self.assertRaises(ValueError):
            create_server(socket_path="/x", ssh_host="host")
        with self.assertRaises(ValueError):
            create_server(ssh_host="-oProxyCommand=x")
        with patch("xbloom_studio.ssh_rpc.ssh_call") as ssh:

            async def answer(*args, **kwargs):
                return {"collective": {}, "xrecipe": {}}

            ssh.side_effect = answer
            server = create_server(ssh_host="host", ssh_config="cfg")
            result = await server.call_tool("provider_capabilities", {})
        self.assertFalse(result.is_error)
        ssh.assert_called_once_with(
            "host", "provider_capabilities", {}, config_file="cfg"
        )

    async def test_default_transport_is_the_local_daemon_socket(self):
        server = create_server(socket_path="/nonexistent/daemon.sock")
        result = await server.call_tool("machine_capabilities", {})
        self.assertEqual(
            result.structured_content["error"]["code"], "connection_unavailable"
        )

    def test_missing_sdk_is_reported(self):
        with patch.dict(sys.modules, {"mcp.server.mcpserver": None}):
            with self.assertRaisesRegex(RuntimeError, "mcp extra"):
                create_server(caller=lambda m, p: None)
            with patch("sys.stderr") as stderr:
                self.assertEqual(mcp_server.main([]), 2)
        self.assertIn("mcp extra", "".join(c.args[0] for c in stderr.write.mock_calls))

    def test_canonical_server_and_command_names(self):
        self.assertEqual(create_server(caller=lambda m, p: None).name, "xbloom-studio")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            mcp_server.main(["--help"])
        self.assertTrue(out.getvalue().startswith("usage: xbloom-studio-mcp "))

    def test_command_line_runs_stdio(self):
        with patch.object(mcp_server, "create_server") as create:
            self.assertEqual(
                mcp_server.main(["--socket", "/s", "--group", "machine"]), 0
            )
            self.assertEqual(mcp_server.main(["--ssh", "host"]), 0)
        first, second = create.call_args_list
        self.assertEqual(first.args, ("/s", ["machine"]))
        self.assertEqual(second.kwargs, {"ssh_host": "host", "ssh_config": None})
        create.return_value.run.assert_called_with(transport="stdio")

    def test_compact_state_keeps_identity_and_drops_bulk(self):
        state = {
            "operation": {"id": "o", "status": "running", "plan": {"recipe_id": "r"}},
            "machine": {
                "machine": {"events": [1], "fields": {"device_info_raw": 1, "a": 2}},
                "operation": {"id": "x", "events": [], "state": {}},
            },
        }
        compact = compact_state(state)
        self.assertEqual(
            compact["operation"], {"id": "o", "status": "running", "recipe_id": "r"}
        )
        self.assertEqual(compact["machine"]["machine"], {"fields": {"a": 2}})
        self.assertEqual(compact["machine"]["operation"], {"id": "x"})
        self.assertIn("events", state["machine"]["machine"])
        self.assertEqual(compact_state({"machine": None}), {"machine": None})


if __name__ == "__main__":
    unittest.main()
