"""Run the test modules in parallel, each in its own process and test root.

Usage: uv run --locked python -m scripts.run_tests [--jobs N]
       [--coverage-data PATH] [TARGET ...]

A target is a module (``tests.test_x``) or a test ID inside one. Each module
runs as one ``unittest`` child with a private root under one run root, which
is deleted at the end. A module passes only if it exits 0, runs at least one
test and prints nothing but unittest's own lines. This script never imports
the tests: the module list comes from the file names.

Limits: a timeout or an interrupt kills each worker's process group, which a
descendant in its own session has left. On Linux the runner adopts orphaned
descendants, so it kills every descendant left when the run ends, before it
removes the run root; elsewhere they outlive the run. If the runner itself is
killed with a signal it cannot catch (SIGKILL) or does not handle (such as
SIGHUP or SIGQUIT), its workers, their descendants and its run root are left
behind. A worker's root is exactly as long as the root of a bare ``python -m
unittest`` run, so a long ``XBLOOM_STUDIO_TEST_TMPBASE`` that breaks socket
paths here breaks them there too: the socket tests allow a base of at most 41
bytes.

Removing a run root never follows a link and never changes the mode of
anything but a directory. The runner creates the run root with mode 0700:
other users cannot enter it, and a process of the same user that races the
removal could already do anything the runner can, so the removal does not
defend against one.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import IO, Protocol, TypeVar

from scripts import testroot

REPO = Path(__file__).resolve().parents[1]
TIMEOUT = 300.0
GRACE = 2.0
JOIN = 10.0
POLL = 0.2
NOISE_SHOWN = 20
RERUN = "uv run --locked python -m scripts.run_tests"
LINUX = sys.platform.startswith("linux")
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
WARNINGS = ["-W", "default", "-W", "error::ResourceWarning"]

RAN = re.compile(r"Ran (\d+) tests? in [0-9.]+s")
OWN_LINE = re.compile(r"[.sxuFE]*|-{70}|Ran \d+ tests? in [0-9.]+s|OK( \(.*\))?")
HEADER = re.compile(r"(?:FAIL|ERROR): (\S+) \(([^)\s]+)\)")
SEPARATOR = "=" * 70
# The suffix ``coverage run --parallel-mode`` gives its data files.
# Exactly what coverage 7.16/7.17 writes (coverage.sqldata.filename_suffix and
# the data hash): host with dots replaced, os.getpid(), 6 random characters
# and an optional 10-character hash, both drawn from ASCII letters and digits.
# Its reader's SUFFIX_PATTERN is looser; anything matched here is deleted.
SHARD = re.compile(r"\.[^.]+\.pid[1-9][0-9]*\.X[A-Za-z0-9]{6}x(\.H[A-Za-z0-9]{10}h)?")
# Run roots are named like bare roots (``xbs-test-`` plus 8) less 3 bytes, so a
# worker's ``<run root>/<index>`` is no longer than a bare root for < 100 modules.
RUN_PREFIX = "xbs-r-"
# Workers must print plain text, or the output rule and ID parsing fail.
COLORS = {"PYTHON_COLORS": "0", "NO_COLOR": "1"}


class Process(Protocol):
    pid: int

    def wait(self, timeout: float | None = None) -> int: ...


T = TypeVar("T")
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
    index: int = 0


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
    """Rerunnable IDs of the failed tests, or the module when any is unclear."""
    if outcome.status != "FAIL" or "unexpected successes=" in outcome.output:
        return [outcome.module]
    ids: list[str] = []
    lines = outcome.output.splitlines()
    for previous, line in zip(lines, lines[1:], strict=False):
        if previous != SEPARATOR:
            continue  # unittest puts the separator above every failure header
        found = test_id(line, outcome.module)
        if found is None:
            return [outcome.module]
        if found not in ids:
            ids.append(found)
    return ids or [outcome.module]


def test_id(header: str, module: str) -> str | None:
    """``pkg.C.test_y`` from a 3.10 or a 3.11+ failure header in ``module``."""
    match = HEADER.match(header)
    if not match:
        return None
    name, where = match.groups()
    found = where if where.endswith(f".{name}") else f"{where}.{name}"
    if name.startswith("test") and found.startswith(f"{module}."):
        return found
    return None


def quietly(send: Signal, pid: int, signum: int) -> None:
    try:
        send(pid, signum)
    except ProcessLookupError:
        pass


def subreaper(flag: int) -> int:
    """Set whether this process adopts its orphaned descendants; return the
    previous setting. Only Linux has one: elsewhere this does nothing."""
    if not LINUX:
        return 0
    prctl = ctypes.CDLL(None).prctl
    previous = ctypes.c_int()
    prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(previous))
    prctl(PR_SET_CHILD_SUBREAPER, ctypes.c_ulong(flag))
    return previous.value


def children() -> set[int]:
    """This process's children, as /proc lists them; none without /proc."""
    found: set[int] = set()
    with contextlib.suppress(FileNotFoundError), os.scandir("/proc") as entries:
        for entry in entries:
            with contextlib.suppress(OSError):  # not a process, or one that ended
                text = Path(entry.path, "stat").read_bytes()
                if int(text.rsplit(b")", 1)[1].split()[1]) == os.getpid():
                    found.add(int(entry.name))
    return found


def sweep(keep: set[int]) -> None:
    """Kill and reap this process's children but ``keep`` until none is left.

    A killed child's orphans become this subreaper's children, for the next
    round. A child's pid stays its own until it is reaped, so the kill never
    reaches an unrelated process.
    """
    while found := children() - keep:
        for pid in found:
            quietly(os.kill, pid, signal.SIGKILL)
        for pid in found:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)


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

    def run(self, index: int, module: str, targets: Sequence[str]) -> Outcome:
        # A short name keeps socket paths under the root within AF_UNIX limits.
        name = str(index)
        outcome = Outcome(module, self.run_root / name, index=index)
        outcome.root.mkdir(mode=0o700)
        env = dict(self.environ)
        testroot.apply(env, outcome.root)
        env.pop("FORCE_COLOR", None)
        env.update(COLORS)
        log = self.run_root / f"{name}.log"
        started = self.clock()
        with log.open("wb") as output:
            with self.lock:
                if self.stopping.is_set():
                    raise Stopped(module)
                process = self.spawn(self.build(targets, self.coverage), env, output)
                self.live.add(process.pid)
            code: int | None
            try:
                try:
                    code = process.wait(self.timeout)
                except subprocess.TimeoutExpired:
                    code = None
                    quietly(self.kill, process.pid, signal.SIGUSR1)  # stack dump
                    self.stopping.wait(self.grace)
                    quietly(self.killpg, process.pid, signal.SIGKILL)
                    process.wait()
            except BaseException:
                # Never leave a worker running unowned: kill and reap it first.
                quietly(self.killpg, process.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    process.wait()
                raise
            finally:
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
    problems: list[str] | None = None,
    coverage: Path | None = None,
    environ: Mapping[str, str] = os.environ,
    spawn: Spawn = spawn,
    kill: Signal = os.kill,
    killpg: Signal = os.killpg,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = TIMEOUT,
    grace: float = GRACE,
    build: Callable[[Sequence[str], Path | None], list[str]] = command,
    signals: Sequence[int] = (),
) -> list[Outcome]:
    """Run every module of ``plan``; print a line as each one finishes.

    Each of ``signals`` stops the run with ``Interrupted``, after the
    workers are killed and the run root is removed. A root that cannot be
    removed is added to ``problems``. Before the removal, on Linux, every
    descendant left is killed: this process adopts its orphans for the run.
    """
    done: queue.SimpleQueue[Outcome | BaseException | None] = queue.SimpleQueue()
    received: list[int] = []

    def interrupt(signum: int, frame: FrameType | None) -> None:
        # Raising here could abort startup or cleanup at any point, so the
        # main loop raises it instead. SimpleQueue.put is reentrant.
        received.append(signum)
        done.put(Interrupted(signum))

    previous = {signum: signal.signal(signum, interrupt) for signum in signals}
    was_subreaper = subreaper(1)
    keep = children()
    run_root: Path | None = None
    pool: Pool | None = None
    threads: list[threading.Thread] = []
    outcomes: list[Outcome] = []
    try:
        run_root = testroot.create(environ, prefix=RUN_PREFIX)
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
        todo: queue.SimpleQueue[tuple[int, str, Sequence[str]]] = queue.SimpleQueue()
        for index, (module, targets) in enumerate(plan.items()):
            todo.put((index, module, targets))

        def work(pool: Pool) -> None:
            try:
                while True:
                    try:
                        index, module, targets = todo.get_nowait()
                    except queue.Empty:
                        break
                    done.put(pool.run(index, module, targets))
            except BaseException as error:  # raised again by the main thread
                done.put(error)
            finally:
                done.put(None)

        for _ in range(workers):
            thread = threading.Thread(target=work, args=(pool,), daemon=True)
            thread.start()
            threads.append(thread)
        finished = 0
        while finished < workers:
            item = take(done)
            if item is None:
                finished += 1
            elif isinstance(item, BaseException):
                raise item
            else:
                outcomes.append(item)
                print(line(item), file=out, flush=True)
    finally:
        try:
            if pool is not None:
                pool.stop()
            # A worker still starting a module could add to the root while it goes.
            for thread in threads:
                thread.join(JOIN)
            sweep(keep)
            if run_root is not None and not remove(run_root) and problems is not None:
                problems.append(f"run root not removed: {run_root}")
        finally:
            subreaper(was_subreaper)
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    if received:  # handled after the last worker had finished
        raise Interrupted(received[0])
    return sorted(outcomes, key=lambda outcome: outcome.index)


def take(done: queue.SimpleQueue[T]) -> T:
    """The next item of ``done``.

    The kernel may hand a process-directed signal to a worker thread, and
    Python runs the handler only once the main thread wakes up, so wake up
    every ``POLL`` seconds.
    """
    while True:
        try:
            return done.get(timeout=POLL)
        except queue.Empty:
            pass


def remove(root: Path) -> bool:
    """Delete ``root``, making its directories writable if needed.

    Never follows a symbolic link: shutil.rmtree does not, and the retry
    changes and opens each directory without following links, relative to its
    parent's descriptor.
    """
    shutil.rmtree(root, ignore_errors=True)
    if not os.path.lexists(root):
        return True
    unlock(root)
    shutil.rmtree(root, ignore_errors=True)
    return not os.path.lexists(root)


def unlock(root: Path) -> None:
    """Give the owner rwx on directory ``root`` and every directory below it.

    Walks with a stack of open directories, one per level, so it needs as
    many descriptors as the tree is deep, not as it is wide.
    """
    top = enter(None, str(root))
    stack = [top] if top else []
    try:
        while stack:
            fd, names = stack[-1]
            name = next(names, None)
            if name is None:
                os.close(fd)
                stack.pop()
                continue
            child = enter(fd, name)
            if child:
                stack.append(child)
    finally:
        for fd, _ in stack:
            os.close(fd)


def enter(parent: int | None, name: str) -> tuple[int, Iterator[str]] | None:
    """Unlock and open directory ``name``; return it and its subdirectories.

    None for anything that is not a directory, a link included, or that
    cannot be read even after the change.
    """
    try:
        if not stat.S_ISDIR(os.lstat(name, dir_fd=parent).st_mode):
            return None  # never chmod a file, which may be linked elsewhere
        # Refuses a link that replaced the directory, whatever it points at.
        with contextlib.suppress(OSError, ValueError, NotImplementedError):
            os.chmod(name, stat.S_IRWXU, dir_fd=parent, follow_symlinks=False)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError:
        return None  # gone, unreadable or a link; the caller reports the rest
    try:
        with os.scandir(fd) as entries:
            names = [e.name for e in entries if e.is_dir(follow_symlinks=False)]
    except OSError:
        os.close(fd)
        return None
    return fd, iter(names)


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
    """The regular files ``coverage run --parallel-mode`` wrote for ``coverage``."""
    prefix = coverage.name
    try:
        with os.scandir(coverage.parent) as entries:
            return sorted(
                Path(entry.path)
                for entry in entries
                if entry.name.startswith(prefix)
                and SHARD.fullmatch(entry.name[len(prefix) :])
                and entry.is_file(follow_symlinks=False)
            )
    except FileNotFoundError:
        return []


def combine(coverage: Path) -> list[str]:
    """Merge the shards into ``coverage``; return the problems found."""
    found = shards(coverage)
    if not found:
        return [f"coverage: no data was written to {coverage}.*"]
    argv = [sys.executable, "-m", "coverage", "combine", "--quiet"]
    argv += [f"--data-file={coverage}", *map(str, found)]
    result = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, check=False)
    # combine deletes each shard it used; one left over was skipped.
    skipped = [path for path in found if os.path.lexists(path)]
    if result.returncode or skipped:
        return [
            f"coverage: combine exited {result.returncode}",
            *(f"coverage: not combined: {path}" for path in skipped),
            (result.stdout + result.stderr).rstrip("\n"),
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
    if not plan:
        usage.error(f"no test modules match {repo / 'tests' / 'test_*.py'}")
    coverage: Path | None = args.coverage_data
    if coverage is not None:
        # Absolute, but a final symlink is replaced, never followed.
        coverage = coverage.absolute()
        if coverage.is_dir() and not coverage.is_symlink():
            usage.error(f"--coverage-data is a directory: {coverage}")
        for stale in [coverage, *shards(coverage)]:
            stale.unlink(missing_ok=True)
    problems: list[str] = []
    started = clock()
    try:
        outcomes = execute(
            plan,
            jobs=args.jobs,
            out=out,
            problems=problems,
            coverage=coverage,
            spawn=spawn,
            kill=kill,
            killpg=killpg,
            clock=clock,
            signals=(signal.SIGINT, signal.SIGTERM),
        )
    except Interrupted as error:
        print(f"interrupted by {error}; workers killed", file=out)
        for problem in problems:
            print(problem, file=out)
        return 128 + error.signum
    if coverage is not None:
        problems += combine(coverage)
    return 0 if report(outcomes, clock() - started, problems, out) else 1


if __name__ == "__main__":
    raise SystemExit(main())
