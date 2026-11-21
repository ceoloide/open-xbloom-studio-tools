"""AGENTS.md: runnable commands, real paths, a complete module map, a line cap."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import re
import shlex
import unittest
from pathlib import Path

import tests  # noqa: F401 - installs the test guards
from scripts import presubmit, run_tests
from xbloom_studio import cli

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "AGENTS.md"
MAX_LINES = 150
FENCE = re.compile(r"^```sh\n(.*?)^```", re.MULTILINE | re.DOTALL)
CITED = re.compile(r"`([^`\s]+)`")
MODULE = re.compile(r"(?:xbloom_studio|scripts|tests)(?:\.\w+)+")
PATH = re.compile(r"[\w.-]+(?:/[\w.-]+)+/?|[\w.-]+\.(?:json|lock|md|py|toml|yml)")
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
UNKNOWN = "not a known command"


def check(text: str) -> list[str]:
    """Every problem with an AGENTS.md text; empty when it is correct."""
    problems = []
    count = len(text.splitlines())
    if count > MAX_LINES:
        problems.append(f"{count} lines; at most {MAX_LINES}")
    for block in FENCE.findall(text):
        for line in block.splitlines():
            problem = check_command(line)
            if problem is not None:
                problems.append(f"{line.strip()}: {problem}")
    for name in CITED.findall(text):
        if MODULE.fullmatch(name):
            if not resolves(name):
                problems.append(f"no such module or test: {name}")
        elif PATH.fullmatch(name) and not (REPO / name).exists():
            problems.append(f"no such path: {name}")
    mapped = set(CITED.findall(text.partition("\n## Map\n")[2]))
    problems += [f"not in the map: {name}" for name in required() if name not in mapped]
    return problems


def check_command(line: str) -> str | None:
    """Why a line of an ``sh`` block is not a command we validate, or None."""
    try:
        words = shlex.split(line, comments=True)
    except ValueError as error:
        return str(error)
    if not words:
        return None
    while words and ASSIGNMENT.match(words[0]):
        words.pop(0)
    if shlex.join(words) in presubmit.FIXES.values():
        return None
    if words[:3] == ["uv", "run", "--locked"]:
        words = words[3:]
        if words[:1] == ["--python"]:
            words = words[2:]
    command, args = words[:3], words[3:]
    if command == ["python", "-m", "scripts.presubmit"]:
        return parse(presubmit.parser(), args)
    if command == ["python", "-m", "scripts.run_tests"]:
        return parse(run_tests.parser(), args) or targets(args, runner=True)
    if command == ["python", "-m", "unittest"]:
        return targets(args, runner=False)
    if command == ["python", "-m", "tests.child"] and args[:1] == ["xbloom_studio"]:
        return parse(cli.parser(), args[1:])
    return UNKNOWN


def parse(parser: argparse.ArgumentParser, args: list[str]) -> str | None:
    output = io.StringIO()
    try:
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            parser.parse_args(args)
    except SystemExit:
        return output.getvalue().strip().splitlines()[-1]
    return None


def targets(args: list[str], *, runner: bool) -> str | None:
    if runner:
        args = run_tests.parser().parse_args(args).targets
        try:
            run_tests.group(args, run_tests.modules())
        except ValueError as error:
            return str(error)
    elif not args:
        return "names no test"
    for name in args:
        loader = unittest.TestLoader()
        try:
            suite = loader.loadTestsFromName(name)
        except TypeError:
            return f"not a test: {name}"
        if loader.errors or not suite.countTestCases():
            return f"does not load: {name}"
    return None


def resolves(name: str) -> bool:
    """Whether ``name`` is a module, or an attribute path inside one."""
    parts = name.split(".")
    for end in range(len(parts), 0, -1):
        try:
            found = importlib.import_module(".".join(parts[:end]))
        except ModuleNotFoundError:
            continue
        try:
            for part in parts[end:]:
                found = getattr(found, part)
        except AttributeError:
            return False
        return True
    return False


def required() -> list[str]:
    """Every module the map must name."""
    names = [
        f"{package}.{path.stem}"
        for package in ("xbloom_studio", "scripts")
        for path in sorted((REPO / package).glob("*.py"))
        if not path.stem.startswith("_")
    ]
    return names + [
        f"tests.{p.stem}" for p in sorted((REPO / "tests").glob("test_*.py"))
    ]


def fence(*lines: str) -> str:
    return "\n```sh\n" + "".join(f"{line}\n" for line in lines) + "```\n"


class AgentsFileTests(unittest.TestCase):
    def setUp(self):
        self.text = DOC.read_text()

    def without_in_map(self, name):
        head, heading, body = self.text.partition("\n## Map\n")
        self.assertEqual(body.count(f"`{name}` "), 1, name)
        return head + heading + body.replace(f"`{name}` ", "")

    def test_agents_file_is_correct(self):
        self.assertEqual(check(self.text), [])

    def test_claude_file_only_imports_agents_file(self):
        self.assertEqual((REPO / "CLAUDE.md").read_text(), "@AGENTS.md\n")

    def test_missing_paths_and_modules_fail(self):
        for cited in ("docs/missing.md", "missing.toml"):
            with self.subTest(cited=cited):
                self.assertEqual(
                    check(self.text + f"See `{cited}`.\n"), [f"no such path: {cited}"]
                )
        for cited in ("xbloom_studio.missing", "tests.test_cli.Missing"):
            with self.subTest(cited=cited):
                self.assertEqual(
                    check(self.text + f"See `{cited}`.\n"),
                    [f"no such module or test: {cited}"],
                )

    def test_commands_that_do_not_parse_fail(self):
        cases = {
            "uv run --locked python -m scripts.presubmit --nope": "--nope",
            "uv run --locked python -m scripts.presubmit --only nope": "'nope'",
            "uv run --locked python -m scripts.presubmit --fast --only lint": "--fast",
            "uv run --locked python -m scripts.run_tests --nope": "--nope",
            "python -m scripts.run_tests tests.test_nope": "no test module",
            "python -m scripts.run_tests tests.test_cli.Nope": "does not load",
            "python -m unittest tests.test_cli.Nope": "does not load",
            "python -m unittest tests.test_cli.REPO": "does not load",
            "python -m unittest tests.test_cli.CliCase.run.__name__": "not a test",
            "python -m unittest": "names no test",
            "python -m tests.child xbloom_studio --nope machine state": "--nope",
            "python -m script.presubmit --fast": UNKNOWN,
            "python -m tests.chld xbloom_studio machine state": UNKNOWN,
            "python -m tests.child xbloom_studi machine state": UNKNOWN,
            "uv run --frozen python -m scripts.presubmit --only lockfile": UNKNOWN,
            "FOO=bar": UNKNOWN,
            "ruff format .": UNKNOWN,
            "echo 'unclosed": "No closing quotation",
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                problems = check(self.text + fence(line))
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith(f"{line}: "), problems)
                self.assertIn(expected, problems[0])

    def test_comments_and_blank_lines_are_not_commands(self):
        self.assertEqual(check(self.text + fence("", "# a comment", "  ")), [])

    def test_unmapped_modules_fail(self):
        for name in (
            "xbloom_studio.protocol",
            "scripts.testroot",
            "tests.test_protocol",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    check(self.without_in_map(name)), [f"not in the map: {name}"]
                )

    def test_map_names_every_module_but_private_ones(self):
        names = required()
        self.assertIn("tests.test_agent_docs", names)
        self.assertIn("scripts.run_tests", names)
        self.assertNotIn("xbloom_studio._cleanup", names)
        self.assertFalse([name for name in names if name.endswith("__")])

    def test_files_over_the_line_cap_fail(self):
        lines = self.text.count("\n")
        at_cap = self.text + "x\n" * (MAX_LINES - lines)
        self.assertEqual(check(at_cap), [])
        self.assertEqual(
            check(at_cap + "x\n"), [f"{MAX_LINES + 1} lines; at most {MAX_LINES}"]
        )


if __name__ == "__main__":
    unittest.main()
