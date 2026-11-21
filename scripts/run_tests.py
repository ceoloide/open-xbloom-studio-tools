"""Run the test modules in parallel, each in its own process and test root.

Usage: uv run --locked python -m scripts.run_tests [--jobs N]
       [--coverage-data PATH] [TARGET ...]

A target is a module (``tests.test_x``) or a test ID inside one. Each module
runs as one ``unittest`` child with a private root under one run root, which
is deleted at the end. A module passes only if it exits 0, runs at least one
test and prints nothing but unittest's own lines. This script never imports
the tests: the module list comes from the file names.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import IO, Protocol

from scripts import testroot

REPO = Path(__file__).resolve().parents[1]
TIMEOUT = 300.0
GRACE = 2.0
JOIN = 10.0
NOISE_SHOWN = 20
RERUN = "uv run --locked python -m scripts.run_tests"
WARNINGS = ["-W", "default", "-W", "error::ResourceWarning"]

RAN = re.compile(r"Ran (\d+) tests? in [0-9.]+s")
OWN_LINE = re.compile(r"[.sxuFE]*|-{70}|Ran \d+ tests? in [0-9.]+s|OK( \(.*\))?")
HEADER = re.compile(r"(?:FAIL|ERROR): (\S+) \(([^)\s]+)\)")


class Process(Protocol):
    pid: int

    def wait(self, timeout: float | None = None) -> int: ...


Spawn = Callable[[Sequence[str], Mapping[str, str], IO[bytes]], Process]
Signal = Callable[[int, int], None]


@dataclass
class Outcome:
    module: str
    root: Path
    status: str = "ok"
    tests: int = 0
    seconds: float = 0.0
    output: str = ""
    noise: list[str] = field(default_factory=list)


class Stopped(Exception):
    """The run is being interrupted; start no more modules."""


class Interrupted(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.run_tests",
        description=(__doc__ or "").split("\n\n")[0],
    )
    parser.add_argument(
        "--jobs",
        type=positive,
        default=min(4, os.cpu_count() or 1),
        help="modules to run at once (default: min(4, CPUs))",
    )
    parser.add_argument(
        "--coverage-data",
        type=Path,
        metavar="PATH",
        help="run each module under coverage and combine the data into PATH",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        metavar="TARGET",
        help="test modules or test IDs (default: every tests/test_*.py)",
    )
    return parser


def positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1: {text}")
    return value


def modules(repo: Path = REPO) -> list[str]:
    """Every test module, largest file first so the slowest start early."""
    paths = sorted(
        (repo / "tests").glob("test_*.py"), key=lambda p: (-p.stat().st_size, p.name)
    )
    return [f"tests.{path.stem}" for path in paths]


def group(targets: Sequence[str], known: Sequence[str]) -> dict[str, list[str]]:
    """Map each module to run onto its targets; a bare module runs whole."""
    if not targets:
        return {module: [module] for module in known}
    chosen: dict[str, list[str]] = {}
    for target in targets:
        module = next(
            (m for m in known if target == m or target.startswith(f"{m}.")), None
        )
        if module is None:
            raise ValueError(f"no test module for {target!r}")
        chosen.setdefault(module, [])
        if target not in chosen[module]:
            chosen[module].append(target)
    order = {module: index for index, module in enumerate(known)}
    return {
        module: [module] if module in items else items
        for module, items in sorted(chosen.items(), key=lambda item: order[item[0]])
    }


def command(targets: Sequence[str], coverage: Path | None) -> list[str]:
    runner = ["-m", "unittest", *targets]
    if coverage is not None:
        runner = ["-m", "coverage", "run", "--parallel-mode"]
        runner += [f"--data-file={coverage}", "-m", "unittest", *targets]
    return [sys.executable, *WARNINGS, *runner]


def spawn(argv: Sequence[str], env: Mapping[str, str], output: IO[bytes]) -> Process:
    return subprocess.Popen(
        argv,
        cwd=REPO,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def classify(outcome: Outcome, code: int | None) -> None:
    """Set the outcome's status from its exit code (None: timed out) and output."""
    lines = outcome.output.splitlines()
    counts = [int(match[1]) for line in lines if (match := RAN.fullmatch(line))]
    outcome.tests = counts[-1] if counts else 0
    outcome.noise = [line for line in lines if not OWN_LINE.fullmatch(line)]
    if code is None:
        outcome.status = "TIMEOUT"
    elif code != 0:
        outcome.status = "FAIL"
    elif not outcome.tests:
        outcome.status = "EMPTY"
    elif outcome.noise:
        outcome.status = "NOISE"


def failed_ids(outcome: Outcome) -> list[str]:
    """Rerunnable IDs of the failed tests, or the module when they are unclear."""
    if outcome.status != "FAIL":
        return [outcome.module]
    ids: list[str] = []
    for line in outcome.output.splitlines():
        match = HEADER.match(line)
        if not match:
            continue
        name, where = match.groups()
        found = where if where.endswith(f".{name}") else f"{where}.{name}"
        if not (name.startswith("test") and found.startswith(f"{outcome.module}.")):
            found = outcome.module
        if found not in ids:
            ids.append(found)
    return ids or [outcome.module]


def quietly(send: Signal, pid: int, signum: int) -> None:
    try:
        send(pid, signum)
    except ProcessLookupError:
        pass


class Pool:
    """Worker threads that run one module per child process."""

    def __init__(
        self,
        run_root: Path,
        *,
        environ: Mapping[str, str],
        coverage: Path | None,
        spawn: Spawn,
        kill: Signal,
        killpg: Signal,
        clock: Callable[[], float],
        timeout: float,
        grace: float,
        build: Callable[[Sequence[str], Path | None], list[str]],
    ) -> None:
        self.run_root = run_root
        self.environ = environ
        self.coverage = coverage
        self.spawn = spawn
        self.kill = kill
        self.killpg = killpg
        self.clock = clock
        self.timeout = timeout
        self.grace = grace
        self.build = build
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.live: set[int] = set()

    def run(self, module: str, targets: Sequence[str]) -> Outcome:
        outcome = Outcome(module, self.run_root / module)
        outcome.root.mkdir(mode=0o700)
        env = dict(self.environ)
        testroot.apply(env, outcome.root)
        log = self.run_root / f"{module}.log"
        started = self.clock()
        with log.open("wb") as output:
            with self.lock:
                if self.stopping.is_set():
                    raise Stopped(module)
                process = self.spawn(self.build(targets, self.coverage), env, output)
                self.live.add(process.pid)
            code: int | None
            try:
                code = process.wait(self.timeout)
            except subprocess.TimeoutExpired:
                code = None
                quietly(self.kill, process.pid, signal.SIGUSR1)  # stack dump
                self.stopping.wait(self.grace)
                quietly(self.killpg, process.pid, signal.SIGKILL)
                process.wait()
            with self.lock:
                self.live.discard(process.pid)
        outcome.seconds = self.clock() - started
        outcome.output = log.read_text("utf-8", errors="replace")
        classify(outcome, code)
        return outcome

    def stop(self) -> None:
        with self.lock:
            self.stopping.set()
            for pid in self.live:
                quietly(self.killpg, pid, signal.SIGKILL)


def execute(
    plan: Mapping[str, Sequence[str]],
    *,
    jobs: int,
    out: IO[str],
    coverage: Path | None = None,
    environ: Mapping[str, str] = os.environ,
    spawn: Spawn = spawn,
    kill: Signal = os.kill,
    killpg: Signal = os.killpg,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = TIMEOUT,
    grace: float = GRACE,
    build: Callable[[Sequence[str], Path | None], list[str]] = command,
) -> list[Outcome]:
    """Run every module of ``plan``; print a line as each one finishes."""
    run_root: Path | None = None
    pool: Pool | None = None
    threads: list[threading.Thread] = []
    try:
        run_root = testroot.create(environ, prefix="xbs-run-")
        pool = Pool(
            run_root,
            environ=environ,
            coverage=coverage,
            spawn=spawn,
            kill=kill,
            killpg=killpg,
            clock=clock,
            timeout=timeout,
            grace=grace,
            build=build,
        )
        workers = min(jobs, len(plan))
        print(f"{len(plan)} modules, {workers} jobs, root {run_root}", file=out)
        todo: queue.SimpleQueue[tuple[str, Sequence[str]]] = queue.SimpleQueue()
        for module, targets in plan.items():
            todo.put((module, targets))
        done: queue.SimpleQueue[Outcome | BaseException | None] = queue.SimpleQueue()

        def work(pool: Pool) -> None:
            try:
                while True:
                    try:
                        module, targets = todo.get_nowait()
                    except queue.Empty:
                        break
                    done.put(pool.run(module, targets))
            except BaseException as error:  # raised again by the main thread
                done.put(error)
            finally:
                done.put(None)

        for _ in range(workers):
            threads.append(threading.Thread(target=work, args=(pool,), daemon=True))
            threads[-1].start()
        outcomes: list[Outcome] = []
        finished = 0
        while finished < workers:
            item = done.get()
            if item is None:
                finished += 1
            elif isinstance(item, BaseException):
                raise item
            else:
                outcomes.append(item)
                print(line(item), file=out, flush=True)
        return outcomes
    finally:
        if pool is not None:
            pool.stop()
        # A worker still starting a module could add to the root while it goes.
        for thread in threads:
            thread.join(JOIN)
        if run_root is not None:
            shutil.rmtree(run_root, ignore_errors=True)


def line(outcome: Outcome) -> str:
    tests = f"{outcome.tests} test{'' if outcome.tests == 1 else 's'}"
    return (
        f"{outcome.status:<7} {outcome.module:<34} {tests:>10} {outcome.seconds:6.1f}s"
    )


def details(outcome: Outcome) -> Iterator[str]:
    if outcome.status == "NOISE":
        shown = outcome.noise[:NOISE_SHOWN]
        more = len(outcome.noise) - len(shown)
        yield f"== NOISE {outcome.module}: lines that are not unittest's own"
        yield from shown
        if more:
            yield f"... and {more} more"
    else:
        yield f"== {outcome.status} {outcome.module}"
        yield outcome.output.rstrip("\n")


def shards(coverage: Path) -> list[Path]:
    return sorted(coverage.parent.glob(f"{coverage.name}.*"))


def combine(coverage: Path) -> list[str]:
    """Merge the shards into ``coverage``; return the problems found."""
    found = shards(coverage)
    if not found:
        return [f"coverage: no data was written to {coverage}.*"]
    argv = [sys.executable, "-m", "coverage", "combine", "--quiet"]
    argv += [f"--data-file={coverage}", *map(str, found)]
    result = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, check=False)
    if result.returncode:
        return [
            f"coverage: combine exited {result.returncode}",
            result.stdout + result.stderr,
        ]
    return []


def report(
    outcomes: Sequence[Outcome], seconds: float, problems: Sequence[str], out: IO[str]
) -> bool:
    """Print the failures, a rerun line and a summary; True when all passed."""
    bad = [outcome for outcome in outcomes if outcome.status != "ok"]
    for outcome in bad:
        for text in details(outcome):
            print(text, file=out)
    for problem in problems:
        print(problem, file=out)
    total = sum(outcome.tests for outcome in outcomes)
    counts = f"{total} tests in {len(outcomes)} modules in {seconds:.1f}s"
    if not bad and not problems:
        print(f"passed: {counts}", file=out)
        return True
    ids = [found for outcome in bad for found in failed_ids(outcome)]
    for found in ids:
        print(f"failed: {found}", file=out)
    if ids:
        print(f"rerun: {RERUN} {' '.join(ids)}", file=out)
    print(f"FAILED: {len(bad)} of {len(outcomes)} modules not ok; {counts}", file=out)
    return False


def main(
    argv: Sequence[str] | None = None,
    *,
    out: IO[str] = sys.stdout,
    repo: Path = REPO,
    spawn: Spawn = spawn,
    kill: Signal = os.kill,
    killpg: Signal = os.killpg,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    usage = parser()
    args = usage.parse_args(argv)
    try:
        plan = group(args.targets, modules(repo))
    except ValueError as error:
        usage.error(str(error))
    coverage: Path | None = args.coverage_data
    if coverage is not None:
        coverage = coverage.resolve()
        for stale in [coverage, *shards(coverage)]:
            stale.unlink(missing_ok=True)

    def interrupt(signum: int, frame: FrameType | None) -> None:
        # Once is enough: cleanup must not be interrupted by a second signal.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise Interrupted(signum)

    previous = {
        signum: signal.signal(signum, interrupt)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    started = clock()
    try:
        outcomes = execute(
            plan,
            jobs=args.jobs,
            out=out,
            coverage=coverage,
            spawn=spawn,
            kill=kill,
            killpg=killpg,
            clock=clock,
        )
    except Interrupted as error:
        print(f"interrupted by {error}; workers killed, run root removed", file=out)
        return 128 + error.signum
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    problems = []
    if coverage is not None:
        problems = combine(coverage)
    return 0 if report(outcomes, clock() - started, problems, out) else 1


if __name__ == "__main__":
    raise SystemExit(main())
