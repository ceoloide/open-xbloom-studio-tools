"""The MCP tools' input contracts, pinned by one golden file."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import tests  # noqa: F401 - installs the test guards
from xbloom_studio.mcp_server import GROUPS, create_server

GOLDEN = Path(__file__).with_name("contracts") / "mcp_tools.json"
UPDATE = "UPDATE_CONTRACTS=1 uv run --locked python -m unittest tests.test_contracts"
HINTS = ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint")
TOP_LEVEL = {"type", "title", "properties", "required", "$defs", "additionalProperties"}
# Values that are data, not schemas: a "title" inside them is kept.
DATA = {"const", "default", "enum", "examples"}


async def _noop(method: str, params: dict[str, Any]) -> Any:
    return None


def strip(schema: Any) -> Any:
    """``schema`` without any string-valued ``title`` or ``description`` key.

    An ``anyOf`` with an unconstrained ``{}`` branch accepts anything, so it
    is dropped: Python 3.10 turns ``x: Any = None`` into ``Optional[Any]``.
    """
    if isinstance(schema, list):
        return [strip(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    stripped = {
        key: value if key in DATA else strip(value)
        for key, value in schema.items()
        if not (key in ("title", "description") and isinstance(value, str))
    }
    if {} in stripped.get("anyOf", ()):
        del stripped["anyOf"]
    return stripped


def project(listed: dict[str, tuple[str, Any]]) -> dict[str, Any]:
    """The golden document for ``{tool name: (group, tool)}``."""
    tools: dict[str, Any] = {}
    defs: dict[str, Any] = {}
    owners: dict[str, str] = {}
    for name, (group, tool) in sorted(listed.items()):
        schema = dict(tool.input_schema)
        if name == "machine_command":
            schema.pop("oneOf")  # command_schema(), pinned by test_command_schema
        unknown = sorted(set(schema) - TOP_LEVEL)
        if unknown:
            raise AssertionError(f"MCP tool {name}: unexpected top-level {unknown}")
        for key, value in strip(schema.get("$defs", {})).items():
            if key in defs and defs[key] != value:
                raise AssertionError(
                    f"$defs {key} differs between {owners[key]} and {name}"
                )
            defs[key], owners[key] = value, owners.get(key, name)
        tools[name] = {
            "group": group,
            **{hint: getattr(tool.annotations, hint) for hint in HINTS},
            "required": sorted(schema.get("required", [])),
            "additionalProperties": schema["additionalProperties"],
            "properties": strip(schema["properties"]),
            "$defs": sorted(schema.get("$defs", {})),
        }
    return {"$defs": defs, "tools": tools}


def current() -> dict[str, Any]:
    listed: dict[str, tuple[str, Any]] = {}
    for group in GROUPS:
        server = create_server(groups=(group,), caller=_noop)
        for tool in asyncio.run(server.list_tools()):
            if tool.name in listed:
                raise AssertionError(f"MCP tool {tool.name} is in two groups")
            listed[tool.name] = (group, tool)
    every = asyncio.run(create_server(caller=_noop).list_tools())
    if sorted(t.name for t in every) != sorted(listed):
        raise AssertionError("the single-group servers miss a tool")
    return project(listed)


def first_difference(old: Any, new: Any, path: str = "") -> str | None:
    """The JSON pointer of the first place ``new`` differs from ``old``."""
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            if key not in old or key not in new:
                return f"{path}/{key}"
            found = first_difference(old[key], new[key], f"{path}/{key}")
            if found is not None:
                return found
        return None
    if isinstance(old, list) and isinstance(new, list):
        for index, (a, b) in enumerate(zip(old, new, strict=False)):
            found = first_difference(a, b, f"{path}/{index}")
            if found is not None:
                return found
        return None if len(old) == len(new) else f"{path}/{min(len(old), len(new))}"
    return None if old == new and type(old) is type(new) else path or "/"


def view(document: dict[str, Any], tool: str) -> Any:
    """One tool's entry with the definitions it names, for a readable diff."""
    entry = document["tools"].get(tool)
    if entry is None:
        return None
    defs = document["$defs"]
    return {**entry, "$defs": {name: defs.get(name) for name in entry["$defs"]}}


def mismatch(old: dict[str, Any], new: dict[str, Any]) -> str | None:
    """Why ``new`` differs from the golden ``old``, naming a tool and a path."""
    for tool in sorted(set(old["tools"]) | set(new["tools"])):
        path = first_difference(view(old, tool), view(new, tool))
        if path is not None:
            return f"MCP tool {tool}: contract differs at {path}"
    path = first_difference(old, new)
    return None if path is None else f"MCP tools: contract differs at {path}"


class ContractTests(unittest.TestCase):
    def test_tool_inputs_match_the_golden(self):
        document = current()
        text = json.dumps(document, indent=1, sort_keys=True) + "\n"
        if os.environ.get("UPDATE_CONTRACTS") == "1":
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(text)
        golden = json.loads(GOLDEN.read_text())
        problem = mismatch(golden, document)
        if problem is not None:
            self.fail(f"{problem}\nIf the change is intended, run: {UPDATE}")
        self.assertEqual(GOLDEN.read_text(), text)


class ProjectionTests(unittest.TestCase):
    def test_strip_keeps_a_property_named_title(self):
        schema = {
            "title": "T",
            "properties": {"title": {"title": "Title", "type": "string"}},
            "default": {"title": "data"},
        }
        self.assertEqual(
            strip(schema),
            {"properties": {"title": {"type": "string"}}, "default": {"title": "data"}},
        )

    def test_strip_drops_an_anyof_that_accepts_anything(self):
        optional_any = {"anyOf": [{}, {"type": "null"}], "default": None}
        self.assertEqual(strip(optional_any), {"default": None})
        optional_int = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
        self.assertEqual(strip(optional_int), optional_int)

    def test_first_difference_is_a_json_pointer(self):
        self.assertEqual(first_difference({"a": [1, 2]}, {"a": [1, 3]}), "/a/1")
        self.assertEqual(first_difference({"a": [1]}, {"a": [1, 2]}), "/a/1")
        self.assertEqual(first_difference({"a": 1}, {"b": 1}), "/a")
        self.assertEqual(first_difference(1, True), "/")
        self.assertIsNone(first_difference({"a": [1]}, {"a": [1]}))

    def test_unknown_top_level_key_fails(self):
        tool = fake({"properties": {}, "additionalProperties": False, "anyOf": []})
        with self.assertRaisesRegex(AssertionError, r"recipe_get.*\['anyOf'\]"):
            project({"recipe_get": ("recipes", tool)})

    def test_conflicting_defs_fail(self):
        a, b = (
            {
                "properties": {},
                "additionalProperties": False,
                "$defs": {"Water": {"type": kind}},
            }
            for kind in ("object", "array")
        )
        with self.assertRaisesRegex(AssertionError, "Water differs between a and b"):
            project({"a": ("recipes", fake(a)), "b": ("recipes", fake(b))})


def fake(schema: dict[str, Any]) -> Any:
    hints = dict.fromkeys(HINTS, False)
    return SimpleNamespace(input_schema=schema, annotations=SimpleNamespace(**hints))
