"""The parallel runner: roots, results, timeouts, interrupts and coverage."""

from __future__ import annotations

import io
import itertools
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import coverage

import tests  # noqa: F401 - installs the test guards
from scripts import run_tests, testroot

REPO = Path(__file__).resolve().parents[1]
PASS = ".\n" + "-" * 70 + "\nRan 1 test in 0.001s\n\nOK\n"
PASS_3 = "...\n" + "-" * 70 + "\nRan 3 tests in 0.002s\n\nOK (skipped=1)\n"
FAILURE = (
    "F\n" + "=" * 70 + "\nFAIL: test_b (tests.test_b.T.test_b)\n" + "-" * 70 + "\n"
    "Traceback (most recent call last):\nAssertionError: boom\n\n" + "-" * 70 + "\n"
    "Ran 1 test in 0.001s\n\nFAILED (failures=1)\n"
)


class FakeProcess:
    """Exits with ``code``, or hangs until its group is killed."""

    def __init__(self, pid, code, hang, on_wait):
        self.pid = pid
        self.code = code
        self.hang = hang
        self.on_wait = on_wait
        self.killed = threading.Event()

    def wait(self, timeout=None):
        self.on_wait(self)
        if not self.hang:
            return self.code
        if timeout is not None:
            raise subprocess.TimeoutExpired("fake", timeout)
        self.killed.wait(10)
        return -signal.SIGKILL


class Fake:
    """A spawner whose children print canned output; also records signals."""

    def __init__(self, results=None, on_wait=lambda process: None):
        self.results = results or {}
        self.on_wait = on_wait
        self.spawned = []
        self.signals = []
        self.processes = {}
        self.pids = itertools.count(100001)

    def spawn(self, argv, env, output):
        module = Path(env[testroot.ROOT]).name
        text, code, hang = self.results.get(module, (PASS, 0, False))
        output.write(text.encode())
        output.flush()
        pid = next(self.pids)
        self.spawned.append((module, list(argv), dict(env)))
        self.processes[pid] = FakeProcess(pid, code, hang, self.on_wait)
        return self.processes[pid]

    def kill(self, pid, signum):
        self.signals.append(("kill", pid, signum))

    def killpg(self, pid, signum):
        self.signals.append(("killpg", pid, signum))
        self.processes[pid].killed.set()


def clock():
    return itertools.count(0, 0.5).__next__


class RepoCase(unittest.TestCase):
    """A scratch repository with test modules a (largest), b and c."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        (self.repo / "tests").mkdir()
        for name, size in (("a", 30), ("b", 20), ("c", 10)):
            (self.repo / "tests" / f"test_{name}.py").write_text("#" * size)
        (self.repo / "tests" / "helper.py").write_text("")

    def main(self, fake, *argv):
        out = io.StringIO()
        code = run_tests.main(
            ["--jobs", "1", *argv],
            out=out,
            repo=self.repo,
            spawn=fake.spawn,
            kill=fake.kill,
            killpg=fake.killpg,
            clock=clock(),
        )
        return code, out.getvalue()


class RunTests(RepoCase):
    def test_passing_run_prints_one_line_per_module_and_a_total(self):
        fake = Fake({"tests.test_a": (PASS_3, 0, False)})
        code, output = self.main(fake)
        self.assertEqual(code, 0)
        lines = output.splitlines()
        self.assertRegex(lines[0], r"^3 modules, 1 jobs, root .*/xbs-run-[^/]+$")
        self.assertEqual(
            [line.split()[:3] for line in lines[1:4]],
            [["ok", "tests.test_a", "3"], ["ok", "tests.test_b", "1"]]
            + [["ok", "tests.test_c", "1"]],
        )
        self.assertIn(" 1 test ", lines[2])
        self.assertEqual(lines[-1], "passed: 5 tests in 3 modules in 3.5s")
        self.assertFalse(Path(lines[0].rsplit(" ", 1)[1]).exists())
        argv = fake.spawned[0][1]
        self.assertEqual(
            argv,
            [sys.executable, *run_tests.WARNINGS] + ["-m", "unittest", "tests.test_a"],
        )

    def test_every_bad_result_fails_and_names_the_module(self):
        importing = (
            "E\n" + "=" * 70 + "\n"
            "ERROR: test_c (unittest.loader._FailedTest.test_c)\n"
            "ImportError: Failed to import test module: test_c\n"
            "Ran 1 test in 0.001s\n\nFAILED (errors=1)\n"
        )
        cases = {
            "failing test": ("tests.test_b", FAILURE, 1, False, "FAIL"),
            "import error": ("tests.test_c", importing, 1, False, "FAIL"),
            "zero tests": (
                "tests.test_a",
                "\nRan 0 tests in 0.000s\n\nOK\n",
                0,
                0,
                "EMPTY",
            ),
            "no summary": ("tests.test_a", "", 0, False, "EMPTY"),
            "noise": ("tests.test_b", "careful\n" + PASS, 0, False, "NOISE"),
            "timeout": ("tests.test_c", "hung\n", None, True, "TIMEOUT"),
        }
        for name, (module, text, exit, hang, status) in cases.items():
            with self.subTest(name):
                out = io.StringIO()
                fake = Fake({module: (text, exit, hang)})
                plan = {module: [module]}
                outcomes = run_tests.execute(
                    plan,
                    jobs=4,
                    out=out,
                    spawn=fake.spawn,
                    kill=fake.kill,
                    killpg=fake.killpg,
                    grace=0,
                )
                self.assertEqual([o.status for o in outcomes], [status])
                self.assertFalse(run_tests.report(outcomes, 1.0, [], out))
                printed = out.getvalue()
                self.assertIn(f"{status:<7} {module}", printed)
                self.assertIn(f"== {status} {module}", printed)
                self.assertIn(f"rerun: {run_tests.RERUN} ", printed)
                self.assertRegex(printed, r"FAILED: 1 of 1 modules not ok; \d+ tests")
                if status in ("FAIL", "TIMEOUT"):
                    self.assertIn(text.rstrip("\n"), printed)

    def test_main_exits_one_when_a_module_fails(self):
        fake = Fake({"tests.test_b": (FAILURE, 1, False)})
        code, output = self.main(fake)
        self.assertEqual(code, 1)
        self.assertIn("failed: tests.test_b.T.test_b\n", output)
        self.assertIn(f"rerun: {run_tests.RERUN} tests.test_b.T.test_b\n", output)
        self.assertTrue(output.splitlines()[-1].startswith("FAILED: 1 of 3 modules"))

    def test_noise_shows_at_most_twenty_lines(self):
        outcome = run_tests.Outcome("tests.test_a", Path("r"))
        outcome.output = "".join(f"line {n}\n" for n in range(25)) + PASS
        run_tests.classify(outcome, 0)
        shown = list(run_tests.details(outcome))
        self.assertEqual(outcome.status, "NOISE")
        self.assertEqual(shown[1:21], [f"line {n}" for n in range(20)])
        self.assertEqual(shown[21:], ["... and 5 more"])

    def test_timeout_dumps_the_stacks_then_kills_the_group(self):
        fake = Fake({"tests.test_a": ("", None, True)})
        out = io.StringIO()
        outcomes = run_tests.execute(
            {"tests.test_a": ["tests.test_a"]},
            jobs=1,
            out=out,
            spawn=fake.spawn,
            kill=fake.kill,
            killpg=fake.killpg,
            timeout=7,
            grace=0,
        )
        self.assertEqual(outcomes[0].status, "TIMEOUT")
        self.assertEqual(
            fake.signals,
            [("kill", 100001, signal.SIGUSR1), ("killpg", 100001, signal.SIGKILL)],
        )

    def test_signals_to_a_finished_process_are_ignored(self):
        def gone(pid, signum):
            raise ProcessLookupError(pid)

        run_tests.quietly(gone, 1, signal.SIGUSR1)

    def test_modules_get_their_own_roots_under_the_run_root(self):
        fake = Fake()
        environ = {
            **os.environ,
            testroot.ROOT: "/nonexistent/stale",
            testroot.DEVICE: "00:00:5E:00:53:99",
        }
        plan = {"tests.test_a": ["tests.test_a"], "tests.test_b": ["tests.test_b"]}
        out = io.StringIO()
        run_tests.execute(plan, jobs=2, out=out, environ=environ, spawn=fake.spawn)
        run_root = Path(out.getvalue().splitlines()[0].rsplit(" ", 1)[1])
        self.assertTrue(run_root.name.startswith("xbs-run-"))
        self.assertFalse(run_root.exists())
        roots = set()
        for module, _, env in fake.spawned:
            root = Path(env[testroot.ROOT])
            roots.add(root)
            self.assertEqual(root, run_root / module)
            self.assertNotIn(testroot.DEVICE, env)
            self.assertEqual(env["HOME"], str(root / "home"))
            self.assertEqual(env["TMPDIR"], str(root / "tmp"))
        self.assertEqual(len(roots), 2)

    def test_worker_errors_stop_the_run_and_remove_the_root(self):
        def broken(argv, env, output):
            raise OSError("cannot spawn")

        out = io.StringIO()
        with self.assertRaisesRegex(OSError, "cannot spawn"):
            run_tests.execute({"tests.test_a": ["x"]}, jobs=1, out=out, spawn=broken)
        run_root = Path(out.getvalue().splitlines()[0].rsplit(" ", 1)[1])
        self.assertFalse(run_root.exists())

    def test_unusable_base_fails_before_anything_runs(self):
        environ = {testroot.TMPBASE: "/nonexistent"}
        with self.assertRaisesRegex(RuntimeError, testroot.TMPBASE):
            run_tests.execute({}, jobs=1, out=io.StringIO(), environ=environ)

    def test_a_stopped_pool_starts_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = run_tests.Pool(
                Path(directory),
                environ={},
                coverage=None,
                spawn=Fake().spawn,
                kill=os.kill,
                killpg=os.killpg,
                clock=clock(),
                timeout=1,
                grace=0,
                build=run_tests.command,
            )
            pool.stop()
            with self.assertRaises(run_tests.Stopped):
                pool.run("tests.test_a", ["tests.test_a"])

    def test_interrupt_kills_live_workers_and_removes_the_root(self):
        for signum, expected in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=signum.name):
                before = signal.getsignal(signum)

                def interrupt(process, signum=signum):
                    if process.hang:  # process-directed, as from a shell
                        os.kill(os.getpid(), signum)

                fake = Fake({"tests.test_b": ("", 0, True)}, on_wait=interrupt)
                code, output = self.main(fake)
                self.assertEqual(code, expected)
                self.assertIn(("killpg", 100002, signal.SIGKILL), fake.signals)
                self.assertEqual(len(fake.spawned), 2)
                self.assertIn(f"interrupted by {signum.name}", output)
                run_root = Path(output.splitlines()[0].rsplit(" ", 1)[1])
                self.assertFalse(run_root.exists())
                self.assertIs(signal.getsignal(signum), before)


class CoverageTests(RepoCase):
    def test_stale_data_is_removed_and_a_run_without_shards_fails(self):
        data = self.repo / "cov.data"
        stale = [data, self.repo / "cov.data.old-shard"]
        for path in stale:
            path.write_text("stale")
        code, output = self.main(Fake(), "--coverage-data", str(data), "tests.test_a")
        self.assertEqual(code, 1)
        self.assertFalse(any(path.exists() for path in stale))
        self.assertIn(f"coverage: no data was written to {data}.*\n", output)
        self.assertNotIn("rerun:", output)
        self.assertTrue(output.splitlines()[-1].startswith("FAILED: 0 of 1 modules"))

    def test_shards_are_combined_into_the_data_file(self):
        data = self.repo / "cov.data"

        def shard(argv, env, output):
            self.assertIn(f"--data-file={data}", argv)
            self.assertEqual(argv[4:7], ["error::ResourceWarning", "-m", "coverage"])
            name = Path(env[testroot.ROOT]).name
            written = coverage.CoverageData(basename=str(data), suffix=name)
            written.add_lines({str(self.repo / f"{name}.py"): [1, 2]})
            written.write()
            return fake.spawn(argv, env, output)

        fake = Fake()
        out = io.StringIO()
        code = run_tests.main(
            ["--coverage-data", str(data)], out=out, repo=self.repo, spawn=shard
        )
        self.assertEqual(code, 0, out.getvalue())
        combined = coverage.CoverageData(basename=str(data))
        combined.read()
        self.assertEqual(len(combined.measured_files()), 3)
        self.assertEqual(run_tests.shards(data), [])

    def test_a_failed_combine_is_reported(self):
        data = self.repo / "cov.data"
        Path(f"{data}.broken").write_text("not coverage data")
        problems = run_tests.combine(data)
        self.assertRegex(problems[0], r"^coverage: combine exited [1-9]")


class FailedIdTests(unittest.TestCase):
    def ids(self, *headers, status="FAIL"):
        outcome = run_tests.Outcome("tests.test_x", Path("r"), status=status)
        outcome.output = "\n".join(["F", *headers, "Ran 1 test in 0.1s"])
        return run_tests.failed_ids(outcome)

    def test_both_header_forms_give_the_full_test_id(self):
        expected = ["tests.test_x.C.test_y"]
        self.assertEqual(self.ids("FAIL: test_y (tests.test_x.C.test_y)"), expected)
        self.assertEqual(self.ids("ERROR: test_y (tests.test_x.C)"), expected)

    def test_subtests_are_folded_into_one_id(self):
        ids = self.ids(
            "FAIL: test_y (tests.test_x.C.test_y) [first]",
            "FAIL: test_y (tests.test_x.C.test_y) (n=2)",
            "FAIL: test_y (tests.test_x.C) [first]",
            "ERROR: test_z (tests.test_x.C)",
        )
        self.assertEqual(ids, ["tests.test_x.C.test_y", "tests.test_x.C.test_z"])

    def test_unclear_failures_fall_back_to_the_module(self):
        for header in (
            "ERROR: setUpClass (tests.test_x.C)",
            "ERROR: setUpModule (tests.test_x)",
            "ERROR: test_x (unittest.loader._FailedTest)",
            "ERROR: test_x (unittest.loader._FailedTest.test_x)",
            "Traceback (most recent call last):",
        ):
            with self.subTest(header):
                self.assertEqual(self.ids(header), ["tests.test_x"])
        self.assertEqual(
            self.ids("FAIL: test_y (tests.test_x.C)", status="NOISE"), ["tests.test_x"]
        )


class PlanTests(unittest.TestCase):
    KNOWN = ["tests.test_a", "tests.test_ab", "tests.test_c"]

    def test_targets_are_grouped_by_module(self):
        targets = [
            "tests.test_ab.T.test_one",
            "tests.test_a.T",
            "tests.test_ab.T.test_one",
            "tests.test_ab.T.test_two",
            "tests.test_c.T.test_x",
            "tests.test_c",
        ]
        self.assertEqual(
            run_tests.group(targets, self.KNOWN),
            {
                "tests.test_a": ["tests.test_a.T"],
                "tests.test_ab": [
                    "tests.test_ab.T.test_one",
                    "tests.test_ab.T.test_two",
                ],
                "tests.test_c": ["tests.test_c"],
            },
        )
        self.assertEqual(run_tests.group([], self.KNOWN), {m: [m] for m in self.KNOWN})

    def test_unknown_target_is_a_usage_error(self):
        with self.assertRaisesRegex(ValueError, "tests.test_zz"):
            run_tests.group(["tests.test_zz.T"], self.KNOWN)
        with (
            redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit) as raised,
        ):
            run_tests.main(["tests.test_zz"], repo=REPO)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("no test module for 'tests.test_zz'", err.getvalue())

    def test_parser_checks_the_job_count(self):
        parser = run_tests.parser()
        args = parser.parse_args([])
        self.assertEqual(args.jobs, min(4, os.cpu_count() or 1))
        self.assertIsNone(args.coverage_data)
        self.assertEqual(parser.parse_args(["--jobs", "8", "tests.test_a"]).jobs, 8)
        with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            parser.parse_args(["--jobs", "0"])
        self.assertIn("must be at least 1", err.getvalue())

    def test_module_list_matches_unittest_discovery(self):
        suite = unittest.defaultTestLoader.discover("tests", top_level_dir=str(REPO))
        found = {
            test.id().split(".")[1]
            for module in suite
            for case in module
            for test in case
        }
        listed = run_tests.modules()
        self.assertEqual({name.split(".")[1] for name in listed}, found)
        sizes = [
            (REPO / "tests" / f"{n.split('.')[1]}.py").stat().st_size for n in listed
        ]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_runner_never_imports_the_tests(self):
        code = "import scripts.run_tests, sys; print('tests' in sys.modules)"
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        self.assertEqual(result.stdout, "False\n")


# Imports the guards, reports this worker's root and its own child's root.
WORKER = (
    "import json, subprocess, sys, tests\n"
    "from tests import isolation\n"
    "child = subprocess.run([sys.executable, '-c', 'import tests\\n"
    "from tests import isolation\\nprint(isolation.root)'],"
    " capture_output=True, text=True, check=True).stdout.strip()\n"
    "print(json.dumps({'root': str(isolation.root), 'owner': isolation.owner,"
    " 'child': child}))\n"
)


class RealWorkerTests(unittest.TestCase):
    def test_real_workers_get_distinct_inherited_roots(self):
        plan = {"tests.test_a": ["tests.test_a"], "tests.test_b": ["tests.test_b"]}
        out = io.StringIO()
        outcomes = run_tests.execute(
            plan,
            jobs=2,
            out=out,
            build=lambda targets, data: [sys.executable, "-c", WORKER],
        )
        run_root = Path(out.getvalue().splitlines()[0].rsplit(" ", 1)[1])
        self.assertFalse(run_root.exists())
        reports = {}
        for outcome in outcomes:
            self.assertEqual(outcome.root, run_root / outcome.module)
            reports[outcome.module] = json.loads(outcome.output)
        self.assertEqual(sorted(reports), sorted(plan))
        for module, report in reports.items():
            self.assertEqual(report["root"], str(run_root / module))
            self.assertFalse(report["owner"])
            self.assertEqual(report["child"], report["root"])
        self.assertNotEqual(str(tests.isolation.root), reports["tests.test_a"]["root"])


if __name__ == "__main__":
    unittest.main()
