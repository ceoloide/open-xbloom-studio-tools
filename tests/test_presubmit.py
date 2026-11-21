import contextlib
import io
import itertools
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401 - installs the test guards
from scripts import presubmit

REPO = Path(__file__).resolve().parents[1]
STEP_LIST = """
steps, in order:
  lockfile
  format
  lint
  types
  publication
  tests
  coverage-report (needs tests)
  coverage-gate (needs coverage-report)
  build
  distribution (needs build)
"""


def fake(failing=()):
    """A runner that fails the commands named in ``failing`` and records calls."""
    calls = []

    def run(command):
        calls.append(command[0])
        return 3 if command[0] in failing else 0

    return run, calls


def ticks(step=0.5):
    counter = itertools.count()
    return lambda: next(counter) * step


class PlanTest(unittest.TestCase):
    def test_plan_covers_every_gate_in_order(self):
        names = [name for name, _ in presubmit.steps("py", Path("w"))]
        self.assertEqual(names, list(presubmit.NAMES))
        self.assertEqual(
            names,
            [
                "lockfile",
                "format",
                "lint",
                "types",
                "publication",
                "tests",
                "coverage-report",
                "coverage-gate",
                "build",
                "distribution",
            ],
        )

    def test_tests_run_in_parallel_into_the_coverage_data(self):
        commands = dict(presubmit.steps("py", Path("w")))
        self.assertEqual(
            commands["tests"],
            [
                "py",
                "-m",
                "scripts.run_tests",
                "--coverage-data",
                str(Path("w/coverage.data")),
            ],
        )
        self.assertIn(
            f"--data-file={Path('w/coverage.data')}", commands["coverage-report"]
        )

    def test_only_adds_every_producer_in_plan_order(self):
        expected = {
            "lockfile": ["lockfile"],
            "format": ["format"],
            "lint": ["lint"],
            "types": ["types"],
            "publication": ["publication"],
            "tests": ["tests"],
            "coverage-report": ["tests", "coverage-report"],
            "coverage-gate": ["tests", "coverage-report", "coverage-gate"],
            "build": ["build"],
            "distribution": ["build", "distribution"],
        }
        self.assertEqual(set(expected), set(presubmit.NAMES))
        for name, plan in expected.items():
            with self.subTest(name):
                self.assertEqual(presubmit.select([name], False), plan)

    def test_only_several_steps_keeps_plan_order(self):
        self.assertEqual(
            presubmit.select(["distribution", "lint", "lint"], False),
            ["lint", "build", "distribution"],
        )

    def test_fast_skips_build_and_distribution(self):
        self.assertEqual(
            presubmit.select(None, True),
            [n for n in presubmit.NAMES if n not in ("build", "distribution")],
        )
        self.assertEqual(presubmit.select(None, False), list(presubmit.NAMES))


class ParserTest(unittest.TestCase):
    def parse(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as exit:
                presubmit.parser().parse_args(argv)
        return exit.exception.code, err.getvalue()

    def test_fast_and_only_are_exclusive(self):
        code, err = self.parse("--fast", "--only", "lint")
        self.assertEqual(code, 2)
        self.assertIn("not allowed with argument", err)

    def test_unknown_step_lists_the_names(self):
        code, err = self.parse("--only", "nope")
        self.assertEqual(code, 2)
        self.assertIn("nope", err)
        for name in presubmit.NAMES:
            self.assertIn(name, err)

    def test_help_lists_one_step_per_line_at_any_width(self):
        for columns in ("40", "72", "140"):
            with self.subTest(columns=columns):
                with mock.patch.dict(os.environ, {"COLUMNS": columns}):
                    text = presubmit.parser().format_help()
                self.assertTrue(text.endswith(STEP_LIST), text)
                for name in presubmit.NAMES:
                    self.assertRegex(text, rf"(?m)^  {re.escape(name)}( \(|$)")


class RunTest(unittest.TestCase):
    def run_plan(self, plan, run):
        out = io.StringIO()
        code = presubmit.run_steps(plan, run, clock=ticks(), out=out)
        return code, out.getvalue()

    def test_reports_every_failure_and_keeps_going(self):
        run, calls = fake(failing={"format-cmd", "types-cmd"})
        plan = [
            ("lockfile", ["lock-cmd"]),
            ("format", ["format-cmd"]),
            ("lint", ["lint-cmd"]),
            ("types", ["types-cmd"]),
        ]
        code, out = self.run_plan(plan, run)
        self.assertEqual(code, 1)
        self.assertEqual(calls, ["lock-cmd", "format-cmd", "lint-cmd", "types-cmd"])
        self.assertRegex(out, r"\nformat +failed \(exit 3\) +0\.5\n")
        self.assertRegex(out, r"\ntypes +failed \(exit 3\) +0\.5\n")
        self.assertRegex(out, r"\nlint +passed +0\.5\n")
        self.assertIn(
            "rerun: uv run --locked python -m scripts.presubmit --only format\n"
            "rerun: uv run --locked python -m scripts.presubmit --only types\n"
            "fix: uv run --locked ruff format .\n",
            out,
        )
        self.assertNotIn("fix: uv run --locked ruff check", out)
        self.assertTrue(out.endswith("presubmit failed: format, types\n"))

    def test_failed_tests_skip_both_coverage_steps(self):
        run, calls = fake(failing={"tests-cmd"})
        plan = [
            ("tests", ["tests-cmd"]),
            ("coverage-report", ["report-cmd"]),
            ("coverage-gate", ["gate-cmd"]),
            ("build", ["build-cmd"]),
            ("distribution", ["dist-cmd"]),
        ]
        code, out = self.run_plan(plan, run)
        self.assertEqual(code, 1)
        self.assertEqual(calls, ["tests-cmd", "build-cmd", "dist-cmd"])
        self.assertRegex(out, r"\ncoverage-report +skipped \(needs tests\) +-\n")
        self.assertRegex(
            out, r"\ncoverage-gate +skipped \(needs coverage-report\) +-\n"
        )
        self.assertRegex(out, r"\ndistribution +passed +0\.5\n")
        self.assertIn("--only tests\n", out)
        self.assertNotIn("--only coverage", out)
        self.assertTrue(out.endswith("presubmit failed: tests\n"))

    def test_empty_plan_prints_an_empty_table(self):
        code, out = self.run_plan([], fake()[0])
        self.assertEqual(code, 0)
        self.assertEqual(out, "\nstep  status  seconds\npresubmit passed\n")

    def test_failed_build_skips_distribution(self):
        run, calls = fake(failing={"build-cmd"})
        code, out = self.run_plan(
            [("build", ["build-cmd"]), ("distribution", ["dist-cmd"])], run
        )
        self.assertEqual(code, 1)
        self.assertEqual(calls, ["build-cmd"])
        self.assertRegex(out, r"\ndistribution +skipped \(needs build\) +-\n")
        self.assertTrue(out.endswith("presubmit failed: build\n"))

    def test_fixes_follow_the_reruns_lint_before_format(self):
        run, _ = fake(failing={"lock-cmd", "format-cmd", "lint-cmd"})
        plan = [
            ("lockfile", ["lock-cmd"]),
            ("format", ["format-cmd"]),
            ("lint", ["lint-cmd"]),
        ]
        code, out = self.run_plan(plan, run)
        self.assertEqual(code, 1)
        self.assertIn(
            "rerun: uv run --frozen python -m scripts.presubmit --only lockfile\n"
            "rerun: uv run --locked python -m scripts.presubmit --only format\n"
            "rerun: uv run --locked python -m scripts.presubmit --only lint\n"
            "fix: uv lock\n"
            "fix: uv run --locked ruff check --fix .\n"
            "fix: uv run --locked ruff format .\n"
            "presubmit failed: lockfile, format, lint\n",
            out,
        )

    def test_only_the_lockfile_rerun_is_frozen(self):
        # With a stale uv.lock, ``uv run --locked`` exits before presubmit runs.
        for name in presubmit.NAMES:
            flag = "--frozen" if name == "lockfile" else "--locked"
            self.assertEqual(
                presubmit.rerun(name),
                f"uv run {flag} python -m scripts.presubmit --only {name}",
            )

    def test_passing_run_prints_table_and_passes(self):
        run, _ = fake()
        code, out = self.run_plan([("lint", ["lint-cmd"])], run)
        self.assertEqual(code, 0)
        self.assertIn("==> lint\n", out)
        self.assertRegex(out, r"\nstep +status +seconds\nlint +passed +0\.5\n")
        self.assertNotIn("rerun:", out)
        self.assertTrue(out.endswith("presubmit passed\n"))


class MainTest(unittest.TestCase):
    def main(self, *argv):
        run, calls = fake()
        out = io.StringIO()
        code = presubmit.main(list(argv), run, clock=ticks(), out=out)
        return code, calls, out.getvalue()

    def test_main_runs_whole_plan(self):
        code, calls, out = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 10)
        self.assertTrue(out.endswith("presubmit passed\n"))

    def test_main_fails_when_a_step_fails_and_prints_to_stdout(self):
        run, _ = fake(failing={sys.executable})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(presubmit.main(["--only", "lint"], run), 1)
        self.assertTrue(out.getvalue().endswith("presubmit failed: lint\n"))

    def test_fast_lists_eight_steps(self):
        code, calls, out = self.main("--fast")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 8)
        self.assertNotIn("build", out)

    def test_only_runs_the_step_and_its_producers(self):
        code, calls, out = self.main("--only", "coverage-gate")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [sys.executable] * 3)
        self.assertIn("==> tests\n==> coverage-report\n==> coverage-gate\n", out)

    def test_run_command_returns_exit_status(self):
        code = "raise SystemExit(3)"
        self.assertEqual(presubmit.run_command([sys.executable, "-c", code]), 3)

    def test_a_command_that_cannot_start_fails_its_step(self):
        missing = str(REPO / "no-such-command")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(presubmit.run_command([missing]), 127)
        self.assertIn(f"cannot run {missing}: ", err.getvalue())

    def test_module_exits_1_when_a_step_fails(self):
        # No ``uv`` on PATH, so the lockfile step cannot start.
        with tempfile.TemporaryDirectory() as empty:
            result = subprocess.run(
                [sys.executable, "-m", "scripts.presubmit", "--only", "lockfile"],
                cwd=REPO,
                env={**os.environ, "PATH": empty},
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 1, result)
        self.assertRegex(result.stdout, r"\nlockfile +failed \(exit 127\) ")
        self.assertIn(
            "--frozen python -m scripts.presubmit --only lockfile", result.stdout
        )
        self.assertIn("cannot run uv: ", result.stderr)


if __name__ == "__main__":
    unittest.main()
