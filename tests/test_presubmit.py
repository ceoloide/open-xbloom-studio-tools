import contextlib
import io
import sys
import unittest
from pathlib import Path

import tests  # noqa: F401 - installs the test guards
from scripts import presubmit


class PresubmitTest(unittest.TestCase):
    def test_plan_covers_every_gate_in_order(self):
        names = [name for name, _ in presubmit.steps("py", Path("w"))]
        self.assertEqual(
            names,
            [
                "lockfile",
                "format",
                "lint",
                "types",
                "publication",
                "tests",
                "coverage report",
                "coverage gate",
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
            f"--data-file={Path('w/coverage.data')}", commands["coverage report"]
        )

    def test_stops_at_first_failure(self):
        calls = []

        def run(command):
            calls.append(command[0])
            return 0 if len(calls) < 2 else 3

        plan = [("a", ["one"]), ("b", ["two"]), ("c", ["three"])]
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(presubmit.run_steps(plan, run), 1)
        self.assertEqual(calls, ["one", "two"])
        self.assertIn("failed at b (exit 3)", err.getvalue())

    def test_main_runs_whole_plan(self):
        commands = []
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(presubmit.main(lambda c: commands.append(c) or 0), 0)
        self.assertEqual(len(commands), 10)
        self.assertIn("presubmit passed", out.getvalue())

    def test_run_command_returns_exit_status(self):
        code = "raise SystemExit(3)"
        self.assertEqual(presubmit.run_command([sys.executable, "-c", code]), 3)


if __name__ == "__main__":
    unittest.main()
