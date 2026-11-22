"""The MCP tools' input contracts, pinned by one golden file."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import tests  # noqa: F401 - installs the test guards
from xbloom_studio.mcp_server import GROUPS, create_server

GOLDEN = Path(__file__).with_name("contracts") / "mcp_tools.json"
UPDATE = "UPDATE_CONTRACTS=1 uv run --locked python -m unittest tests.test_contracts"
HINTS = ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint")
TOP_LEVEL = {"type", "title", "properties", "required", "$defs", "additionalProperties"}
# Values that are data, not schemas: a "title" inside them is kept.
DATA = {"const", "default", "enum", "examples", "discriminator"}
# Maps from a name to a schema: every name is kept, every value is a schema.
NAMED = {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}


async def _noop(method: str, params: dict[str, Any]) -> Any:
    return None


def strip(schema: Any) -> Any:
    """``schema`` without any string-valued ``title`` or ``description`` key."""
    if isinstance(schema, list):
        return [strip(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    stripped = {}
    for key, value in schema.items():
        if key in ("title", "description") and isinstance(value, str):
            continue
        if key in NAMED and isinstance(value, dict):
            value = {name: strip(item) for name, item in value.items()}
        elif key not in DATA:
            value = strip(value)
        stripped[key] = value
    return stripped


def project(listed: dict[str, tuple[str, Any]]) -> dict[str, Any]:
    """The golden document for ``{tool name: (group, tool)}``."""
    tools: dict[str, Any] = {}
    defs: dict[str, Any] = {}
    owners: dict[str, str] = {}
    for name, (group, tool) in sorted(listed.items()):
        schema = dict(tool.input_schema)
        if name == "machine_command":
            if "oneOf" not in schema:
                raise AssertionError(f"MCP tool {name}: no top-level oneOf")
            del schema["oneOf"]  # command_schema(), pinned by test_command_schema
        unknown = sorted(set(schema) - TOP_LEVEL)
        if unknown:
            raise AssertionError(f"MCP tool {name}: unexpected top-level {unknown}")
        for key in ("properties", "additionalProperties"):
            if key not in schema:
                raise AssertionError(f"MCP tool {name}: no top-level {key}")
        schema = strip(schema)
        for key, value in schema.get("$defs", {}).items():
            if key in defs and first_difference(defs[key], value) is not None:
                raise AssertionError(
                    f"$defs {key} differs between {owners[key]} and {name}"
                )
            defs[key], owners[key] = value, owners.get(key, name)
        tools[name] = {
            "group": group,
            **{hint: getattr(tool.annotations, hint) for hint in HINTS},
            "required": sorted(schema.get("required", [])),
            "additionalProperties": schema["additionalProperties"],
            "properties": schema["properties"],
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
            here = f"{path}/{key.replace('~', '~0').replace('/', '~1')}"
            if key not in old or key not in new:
                return here
            found = first_difference(old[key], new[key], here)
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


def compare(document: dict[str, Any]) -> str | None:
    """Why ``document`` does not match the golden; with UPDATE_CONTRACTS=1,
    rewrite the golden first and fail only if that changed it."""
    text = json.dumps(document, indent=1, sort_keys=True) + "\n"
    saved = GOLDEN.read_text() if GOLDEN.exists() else None
    if os.environ.get("UPDATE_CONTRACTS") == "1":
        if saved == text:
            return None
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(text)
        return (
            f"golden rewritten: review the diff of {GOLDEN.name}, explain"
            " it in the PR body, and rerun without UPDATE_CONTRACTS"
        )
    hint = f"If the change is intended, run: {UPDATE}"
    try:
        golden = json.loads(saved or "")
    except ValueError:
        return f"{GOLDEN.name} is missing or not JSON\n{hint}"
    if not (
        isinstance(golden, dict)
        and isinstance(golden.get("tools"), dict)
        and isinstance(golden.get("$defs"), dict)
    ):
        return f"{GOLDEN.name} needs top-level $defs and tools objects\n{hint}"
    problem = mismatch(golden, document)
    if problem is not None:
        return f"{problem}\n{hint}"
    if saved != text:
        return f"{GOLDEN.name} is not in its canonical format\n{hint}"
    return None


class ContractTests(unittest.TestCase):
    def test_tool_inputs_match_the_golden(self):
        problem = compare(current())
        if problem is not None:
            self.fail(problem)

    def test_update_fails_only_when_it_changes_the_golden(self):
        document = {"$defs": {}, "tools": {}}
        text = json.dumps(document, indent=1, sort_keys=True) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "mcp_tools.json"
            golden.write_text("{}")
            with (
                patch(f"{__name__}.GOLDEN", golden),
                patch.dict(os.environ, {"UPDATE_CONTRACTS": "1"}),
            ):
                self.assertIn("golden rewritten", compare(document) or "")
                self.assertEqual(golden.read_text(), text)
                self.assertIsNone(compare(document))

    def test_golden_problems_print_the_update_command(self):
        document = {"$defs": {}, "tools": {}}
        broken = {
            "missing": None,
            "not JSON": "{",
            "a list": "[]",
            "no tools": '{"$defs": {}}',
            "tools not an object": '{"$defs": {}, "tools": []}',
            "not canonical": json.dumps(document),
        }
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "mcp_tools.json"
            with patch(f"{__name__}.GOLDEN", golden), patch.dict(os.environ):
                os.environ.pop("UPDATE_CONTRACTS", None)
                for case, saved in broken.items():
                    with self.subTest(case=case):
                        golden.unlink(missing_ok=True)
                        if saved is not None:
                            golden.write_text(saved)
                        self.assertIn(UPDATE, compare(document) or "")


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

    def test_strip_keeps_names_and_strips_values_of_named_maps(self):
        named = {"title": "Title", "type": "string"}
        schema = {
            "properties": {key: named for key in ("default", "examples", "anyOf")},
            "discriminator": {"mapping": {"title": "#/$defs/A"}},
        }
        self.assertEqual(
            strip(schema),
            {
                "properties": {
                    key: {"type": "string"} for key in ("default", "examples", "anyOf")
                },
                "discriminator": {"mapping": {"title": "#/$defs/A"}},
            },
        )

    def test_first_difference_is_a_json_pointer(self):
        self.assertEqual(first_difference({"a": [1, 2]}, {"a": [1, 3]}), "/a/1")
        self.assertEqual(first_difference({"a": [1]}, {"a": [1, 2]}), "/a/1")
        self.assertEqual(first_difference({"a": 1}, {"b": 1}), "/a")
        self.assertEqual(first_difference(1, True), "/")
        self.assertEqual(first_difference({"a/b~": 1}, {"a/b~": 2}), "/a~1b~0")
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

    def test_defs_that_differ_only_in_json_type_conflict(self):
        a, b = (
            {
                "properties": {},
                "additionalProperties": False,
                "$defs": {"Flag": {"const": value}},
            }
            for value in (True, 1)
        )
        with self.assertRaisesRegex(AssertionError, "Flag differs between a and b"):
            project({"a": ("recipes", fake(a)), "b": ("recipes", fake(b))})

    def test_missing_top_level_keys_name_the_tool(self):
        cases = {
            "recipe_get": ({"properties": {}}, "no top-level additionalProperties"),
            "history_get": (
                {"additionalProperties": False},
                "no top-level properties",
            ),
            "machine_command": (
                {"properties": {}, "additionalProperties": False},
                "no top-level oneOf",
            ),
        }
        for name, (schema, message) in cases.items():
            with self.subTest(tool=name):
                with self.assertRaisesRegex(AssertionError, f"{name}: {message}"):
                    project({name: ("machine", fake(schema))})


def fake(schema: dict[str, Any]) -> Any:
    hints = dict.fromkeys(HINTS, False)
    return SimpleNamespace(input_schema=schema, annotations=SimpleNamespace(**hints))
