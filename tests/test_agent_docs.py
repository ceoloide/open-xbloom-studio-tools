"""AGENTS.md: runnable commands, real paths, a complete module map, a line cap."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import re
import shlex
import unittest
from collections.abc import Sequence
from pathlib import Path

import tests  # noqa: F401 - installs the test guards
from scripts import presubmit, run_tests, testroot
from xbloom_studio import cli

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "AGENTS.md"
MAX_LINES = 150
# Any fence a Markdown reader would see; only an exact ```sh block is allowed.
FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})(.*)")
CITED = re.compile(r"`([^`\s]+)`")
LINK = re.compile(r"\]\(([^)\s]+)\)")
MODULE = re.compile(r"(?:xbloom_studio|scripts|tests)(?:\.\w+)+")
PATH = re.compile(r"[\w.-]+(?:/[\w.-]+)*/?")
SUFFIX = re.compile(r"\.(?:json|lock|md|py|toml|yml)")
LINE_NUMBER = re.compile(r"(?::\d+)+")
ASSIGNMENT = re.compile(r"([A-Za-z_]\w*)=(.*)")
VARIABLES = {"UV_PROJECT_ENVIRONMENT", testroot.TMPBASE}
TMPBASE_BYTES = 26
PYTHONS = {"3.10", "3.11", "3.12", "3.13", "3.14"}
UNKNOWN = "not a known command"
# Commands the plan requires the real file to document.
REQUIRED_COMMANDS = {
    "uv run --locked python -m scripts.presubmit --fast",
    "uv run --locked python -m scripts.presubmit",
    "uv run --locked python -m scripts.run_tests tests.test_cli",
    "uv run --locked python -m scripts.run_tests"
    " tests.test_cli.PureCommandTests.test_interrupt_exits_130",
    "UV_PROJECT_ENVIRONMENT=.venv-310 uv run --locked --python 3.10"
    " python -m scripts.presubmit --fast",
    "uv run --locked python -m tests.child xbloom_studio"
    " --connector simulator --direct machine state",
    *presubmit.FIXES.values(),
}


def check(text: str, mapped: Sequence[str] | None = None) -> list[str]:
    """Every problem with an AGENTS.md text; empty when it is correct."""
    problems = []
    count = len(text.splitlines())
    if count > MAX_LINES:
        problems.append(f"{count} lines; at most {MAX_LINES}")
    blocks, fence_problems = sh_blocks(text)
    problems += fence_problems
    for block in blocks:
        for line in block:
            problem = check_command(line)
            if problem is not None:
                problems.append(f"{line.strip()}: {problem}")
    for name in CITED.findall(text):
        if MODULE.fullmatch(name):
            if not resolves(name):
                problems.append(f"no such module or test: {name}")
        elif is_path(name) and not exists(LINE_NUMBER.sub("", name)):
            problems.append(f"no such path: {name}")
    for target in LINK.findall(text):
        if not re.match(r"[a-z]+:|#", target) and not exists(target.split("#")[0]):
            problems.append(f"no such link target: {target}")
    in_map = set(CITED.findall("\n".join(map_rows(text))))
    names = required() if mapped is None else mapped
    problems += [f"not in the map: {name}" for name in names if name not in in_map]
    return problems


def sh_blocks(text: str) -> tuple[list[list[str]], list[str]]:
    """The lines of each fenced block, and every fence that is not exactly sh."""
    blocks: list[list[str]] = []
    problems = []
    opener: str | None = None
    current: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        fence = FENCE.fullmatch(line)
        if opener is None:
            if fence:
                opener, current = fence[1], []
                if line == "```sh":
                    blocks.append(current)
                else:
                    problems.append(f"line {number}: fences must be exactly ```sh")
        elif fence and fence[1].startswith(opener) and not fence[2].strip():
            if line != "```" and blocks and current is blocks[-1]:
                problems.append(f"line {number}: fences must close with exactly ```")
            opener = None
        else:
            current.append(line)
    if opener is not None:
        problems.append("unclosed fence")
    return blocks, problems


def commands(text: str) -> list[str]:
    """Each command in the sh blocks, without its comment."""
    blocks, _ = sh_blocks(text)
    return [
        shlex.join(words)
        for block in blocks
        for line in block
        if (words := split(line))
    ]


def split(line: str) -> list[str]:
    # As in sh, only an unquoted '#' that starts a word begins a comment;
    # shlex's comment mode would also cut "--fast#typo" down to "--fast".
    # The comment is cut before parsing, so a quote inside it is never parsed.
    quote, escaped = None, False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return shlex.split(line[:index])
    return shlex.split(line)


def check_command(line: str) -> str | None:
    """Why a line of an ``sh`` block is not a command we validate, or None."""
    try:
        words = split(line)
    except ValueError as error:
        return str(error)
    if not words:
        return None
    while words and (assignment := ASSIGNMENT.fullmatch(words[0])):
        name, value = assignment.groups()
        if name not in VARIABLES:
            return f"unexpected variable {name}"
        if name == testroot.TMPBASE and len(value.encode()) > TMPBASE_BYTES:
            return f"{name} longer than {TMPBASE_BYTES} bytes"
        words.pop(0)
    if shlex.join(words) in presubmit.FIXES.values():
        return None
    if words[:3] == ["uv", "run", "--locked"]:
        words = words[3:]
        if words[:1] == ["--python"]:
            if words[1:2] == [] or words[1] not in PYTHONS:
                return f"--python must be one of {', '.join(sorted(PYTHONS))}"
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
    except SystemExit as stop:
        lines = output.getvalue().strip().splitlines()
        if stop.code == 0 or not lines:
            return f"exits {stop.code} without running"
        return lines[-1]
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
        if loader.errors:
            return f"does not load: {name}"
        if not suite.countTestCases():
            return f"has no tests: {name}"
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


def is_path(name: str) -> bool:
    """Whether a cited name is meant as a repository path."""
    name = LINE_NUMBER.sub("", name)
    return bool(
        PATH.fullmatch(name)
        and ("/" in name or name.startswith(".") or SUFFIX.search(name))
    )


def exists(name: str) -> bool:
    path = REPO / name
    return path.is_dir() if name.endswith("/") else path.exists()


def map_rows(text: str) -> list[str]:
    """The table rows between ``## Map`` and the next heading."""
    lines = text.splitlines()
    start = lines.index("## Map") + 1 if "## Map" in lines else len(lines)
    rows = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        if line.startswith("|"):
            rows.append(line)
    return rows


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


def fence(*lines: str, opener: str = "```sh", closer: str = "```") -> str:
    return f"\n{opener}\n" + "".join(f"{line}\n" for line in lines) + f"{closer}\n"


# A small valid document: each case below changes one thing in it.
MAPPED = ["xbloom_studio.cli", "tests.test_cli"]
FIXTURE = """# Guide

See `docs/diagnostics.md`, `.gitignore`, `scripts/`, `tests/test_cli.py:12`,
[the catalog](docs/diagnostics.md#top), [uv](https://docs.astral.sh/uv/) and
`tests.test_cli.PureCommandTests`.

```sh
uv run --locked python -m scripts.presubmit --fast  # comment
python -m scripts.run_tests tests.test_cli
python -m unittest tests.test_cli.PureCommandTests.test_interrupt_exits_130
```

## Map

| Area | Modules | Tests |
|---|---|---|
| CLI | `xbloom_studio.cli` | `tests.test_cli` |
"""


def problems_with(text: str) -> list[str]:
    return check(text, MAPPED)


class CheckerTests(unittest.TestCase):
    def test_fixture_is_valid(self):
        self.assertEqual(problems_with(FIXTURE), [])

    def test_missing_paths_and_modules_fail(self):
        for cited in (
            "docs/missing.md",
            "missing.toml",
            ".gitignroe",
            "docs/diagnostics.md/",
            "scriptz/",
            "tests/test_nope.py:12",
        ):
            with self.subTest(cited=cited):
                self.assertEqual(
                    problems_with(FIXTURE + f"See `{cited}`.\n"),
                    [f"no such path: {cited}"],
                )
        for cited in ("xbloom_studio.missing", "tests.test_cli.Missing"):
            with self.subTest(cited=cited):
                self.assertEqual(
                    problems_with(FIXTURE + f"See `{cited}`.\n"),
                    [f"no such module or test: {cited}"],
                )
        for target in ("docs/missing.md", "docs/missing.md#top", "scriptz/"):
            with self.subTest(link=target):
                self.assertEqual(
                    problems_with(FIXTURE + f"See [this]({target}).\n"),
                    [f"no such link target: {target}"],
                )

    def test_commands_that_do_not_parse_fail(self):
        cases = {
            "uv run --locked python -m scripts.presubmit --nope": "--nope",
            "uv run --locked python -m scripts.presubmit --fast#typo": "--fast#typo",
            "python -m scripts.presubmit '#fast'": "#fast",
            'python -m scripts.presubmit "x #fast"': "x #fast",
            "python -m scripts.presubmit \\#fast": "#fast",
            'python -m scripts.presubmit "a\\" #b"': 'a" #b',
            "uv run --locked python -m scripts.presubmit --only nope": "'nope'",
            "python -m scripts.presubmit --fast --only lint": "--fast",
            "python -m scripts.presubmit --help": "exits 0 without running",
            "python -m scripts.run_tests --nope": "--nope",
            "python -m scripts.run_tests tests.test_nope": "no test module",
            "python -m scripts.run_tests tests.test_cli.Nope": "does not load",
            "python -m scripts.run_tests tests.test_cli tests.test_cli.Nope": (
                "does not load: tests.test_cli.Nope"
            ),
            "python -m scripts.run_tests tests.test_cli.CliCase": "has no tests",
            "python -m unittest tests.test_cli.Nope": "does not load",
            "python -m unittest tests.test_cli tests.test_cli.Nope": (
                "does not load: tests.test_cli.Nope"
            ),
            "python -m unittest tests.test_cli.CliCase": "has no tests",
            "python -m unittest tests.test_cli.REPO": "does not load",
            "python -m unittest tests.test_cli.CliCase.run.__name__": "not a test",
            "python -m unittest": "names no test",
            "python -m tests.child xbloom_studio --nope machine state": "--nope",
            "python -m script.presubmit --fast": UNKNOWN,
            "python -m tests.chld xbloom_studio machine state": UNKNOWN,
            "python -m tests.child xbloom_studi machine state": UNKNOWN,
            "uv run --frozen python -m scripts.presubmit --only lockfile": UNKNOWN,
            "uv run --locked --python 3.9 python -m scripts.presubmit": "--python",
            "uv run --locked --python": "--python",
            "FOO=1 python -m scripts.presubmit": "unexpected variable FOO",
            "XBLOOM_STUDIO_TEST_ROOT=x python -m scripts.presubmit": (
                "unexpected variable XBLOOM_STUDIO_TEST_ROOT"
            ),
            f"{testroot.TMPBASE}=/tmp/{'x' * 22} python -m scripts.presubmit": (
                "longer than 26 bytes"
            ),
            "UV_PROJECT_ENVIRONMENT=.venv-310": UNKNOWN,
            "ruff format .": UNKNOWN,
            "echo 'unclosed": "No closing quotation",
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                problems = problems_with(FIXTURE + fence(line))
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith(f"{line}: "), problems)
                self.assertIn(expected, problems[0][len(line) + 2 :])

    def test_accepted_prefixes(self):
        lines = (
            f"{testroot.TMPBASE}=/tmp/{'x' * 21} python -m scripts.presubmit",
            "UV_PROJECT_ENVIRONMENT=.venv-314 uv run --locked --python 3.14"
            " python -m scripts.presubmit",
        )
        self.assertEqual(problems_with(FIXTURE + fence(*lines)), [])

    def test_every_line_of_every_block_is_checked(self):
        text = FIXTURE + fence(
            "python -m scripts.presubmit --fast",
            "python -m nope",
            "python -m scripts.presubmit",
        )
        text += fence("python -m scripts.presubmit --fast")
        self.assertEqual(problems_with(text), [f"python -m nope: {UNKNOWN}"])

    def test_comments_and_blank_lines_are_not_commands(self):
        text = FIXTURE + fence(
            "",
            "# a comment",
            "  ",
            "python -m unittest tests.test_cli # x",
            "python -m scripts.presubmit --fast # the runner's inner loop",
            "python -m scripts.presubmit --fast #runner's inner loop",
            "python -m unittest 'tests.test_cli' # '#' \"#\"",
        )
        self.assertEqual(problems_with(text), [])

    def test_fences_other_than_sh_fail(self):
        line = 2 + FIXTURE.count("\n")  # the opener after fence()'s blank line
        for opener in (
            "```bash",
            "```",
            "```sh ",
            "```SH",
            "  ```sh",
            "~~~sh",
            "````sh",
        ):
            with self.subTest(opener=opener):
                closer = FENCE.fullmatch(opener)[1]
                text = FIXTURE + fence("python -m nope", opener=opener, closer=closer)
                self.assertEqual(
                    problems_with(text),
                    [f"line {line}: fences must be exactly ```sh"],
                )
        text = FIXTURE + fence("python -m scripts.presubmit", closer="  ```")
        self.assertEqual(
            problems_with(text),
            [f"line {line + 2}: fences must close with exactly ```"],
        )
        text = FIXTURE + "\n```sh\npython -m scripts.presubmit\n"
        self.assertEqual(problems_with(text), ["unclosed fence"])

    def test_unmapped_modules_fail(self):
        removed = FIXTURE.replace(" `xbloom_studio.cli` |", " |")
        self.assertEqual(problems_with(removed), ["not in the map: xbloom_studio.cli"])
        removed = FIXTURE.replace(" `tests.test_cli` |", " |")
        self.assertEqual(problems_with(removed), ["not in the map: tests.test_cli"])

    def test_only_table_rows_under_the_map_heading_count(self):
        removed = FIXTURE.replace(" `xbloom_studio.cli` |", " |")
        for text in (
            removed.replace("## Map\n", "## Map\n\nSee `xbloom_studio.cli`.\n"),
            removed + "\n## Later\n\n| `xbloom_studio.cli` |\n",
            removed.replace("# Guide\n", "# Guide\n\n| `xbloom_studio.cli` |\n"),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    problems_with(text), ["not in the map: xbloom_studio.cli"]
                )

    def test_line_cap(self):
        at_cap = FIXTURE + "x\n" * (MAX_LINES - FIXTURE.count("\n"))
        self.assertEqual(problems_with(at_cap), [])
        self.assertEqual(
            problems_with(at_cap + "x\n"),
            [f"{MAX_LINES + 1} lines; at most {MAX_LINES}"],
        )


class AgentsFileTests(unittest.TestCase):
    def setUp(self):
        self.text = DOC.read_text()

    def test_agents_file_is_correct(self):
        self.assertEqual(check(self.text), [])

    def test_agents_file_documents_the_required_commands(self):
        self.assertEqual(REQUIRED_COMMANDS - set(commands(self.text)), set())

    def test_fixes_are_documented_in_the_order_to_apply_them(self):
        fixes = list(presubmit.FIXES.values())
        self.assertEqual([c for c in commands(self.text) if c in fixes], fixes)

    def test_map_names_every_module_but_private_ones(self):
        names = required()
        self.assertIn("tests.test_agent_docs", names)
        self.assertIn("scripts.run_tests", names)
        self.assertNotIn("xbloom_studio._cleanup", names)
        self.assertFalse([name for name in names if name.endswith("__")])

    def test_claude_file_only_imports_agents_file(self):
        self.assertEqual((REPO / "CLAUDE.md").read_text(), "@AGENTS.md\n")

    def test_readme_links_to_agents_file(self):
        self.assertIn("](AGENTS.md)", (REPO / "README.md").read_text())


if __name__ == "__main__":
    unittest.main()
