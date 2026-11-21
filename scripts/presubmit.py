"""Run every repository check and report all of them; CI runs this command.

Usage: uv run --locked python -m scripts.presubmit [--fast | --only STEP ...]

A failing step does not stop the run: the steps after it still run, except
those that need its output, which are reported as ``skipped (needs X)``. The
run ends with a table of every step, then a rerun command for each failure
and, where one exists, the command that fixes it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO

Runner = Callable[[Sequence[str]], int]

NAMES = (
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
)
# A step that reads another step's output, and so cannot run without it.
PRODUCERS = {
    "coverage-report": "tests",
    "coverage-gate": "coverage-report",
    "distribution": "build",
}
SLOW = ("build", "distribution")
RERUN = "uv run --locked python -m scripts.presubmit --only"
FIXES = {
    "lockfile": "uv lock",
    "format": "uv run --locked ruff format .",
    "lint": "uv run --locked ruff check --fix .",
}


def steps(python: str, workdir: Path) -> list[tuple[str, list[str]]]:
    data = str(workdir / "coverage.data")
    report = str(workdir / "coverage.json")
    dist = str(workdir / "dist")
    return [
        ("lockfile", ["uv", "lock", "--check"]),
        ("format", [python, "-m", "ruff", "format", "--check", "."]),
        ("lint", [python, "-m", "ruff", "check", "."]),
        ("types", [python, "-m", "mypy"]),
        ("publication", [python, "-m", "scripts.publication_policy"]),
        (
            "tests",
            [python, "-m", "scripts.run_tests", "--coverage-data", data],
        ),
        (
            "coverage-report",
            [python, "-m", "coverage", "json", f"--data-file={data}", "-o", report],
        ),
        ("coverage-gate", [python, "-m", "scripts.check_coverage", report]),
        ("build", ["uv", "build", "--quiet", "--out-dir", dist]),
        ("distribution", [python, "-m", "scripts.check_dist", dist]),
    ]


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.presubmit",
        description=(__doc__ or "").split("\n\n")[0],
    )
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument(
        "--fast",
        action="store_true",
        help=f"skip the slow steps: {', '.join(SLOW)}",
    )
    choice.add_argument(
        "--only",
        nargs="+",
        choices=NAMES,
        metavar="STEP",
        help=(
            f"run only these steps and the steps they need; steps: {', '.join(NAMES)}"
        ),
    )
    return parser


def select(only: Sequence[str] | None, fast: bool) -> list[str]:
    """Return the step names to run, in plan order."""
    if only is None:
        return [name for name in NAMES if not (fast and name in SLOW)]
    wanted: set[str] = set()
    for name in only:
        current: str | None = name
        while current is not None:
            wanted.add(current)
            current = PRODUCERS.get(current)
    return [name for name in NAMES if name in wanted]


def run_steps(
    plan: Sequence[tuple[str, Sequence[str]]],
    run: Runner,
    *,
    clock: Callable[[], float] = time.monotonic,
    out: IO[str] = sys.stdout,
) -> int:
    rows: list[tuple[str, str, float | None]] = []
    passed: set[str] = set()
    failed: list[str] = []
    for name, command in plan:
        producer = PRODUCERS.get(name)
        if producer is not None and producer not in passed:
            rows.append((name, f"skipped (needs {producer})", None))
            continue
        print(f"==> {name}", file=out, flush=True)
        started = clock()
        status = run(command)
        seconds = clock() - started
        if status == 0:
            passed.add(name)
            rows.append((name, "passed", seconds))
        else:
            failed.append(name)
            rows.append((name, f"failed (exit {status})", seconds))
    ok = len(passed) == len(plan)
    report(rows, failed, ok, out)
    return 0 if ok else 1


def report(
    rows: Sequence[tuple[str, str, float | None]],
    failed: Sequence[str],
    ok: bool,
    out: IO[str],
) -> None:
    width = max(len("step"), *(len(name) for name, _, _ in rows))
    status_width = max(len("status"), *(len(status) for _, status, _ in rows))
    print(file=out)
    print(f"{'step':<{width}}  {'status':<{status_width}}  seconds", file=out)
    for name, status, seconds in rows:
        shown = "-" if seconds is None else f"{seconds:.1f}"
        print(f"{name:<{width}}  {status:<{status_width}}  {shown:>7}", file=out)
    for name in failed:
        print(f"rerun: {RERUN} {name}", file=out)
        if name in FIXES:
            print(f"fix: {FIXES[name]}", file=out)
    if ok:
        print("presubmit passed", file=out)
    else:
        print(f"presubmit failed: {', '.join(failed)}", file=out)


def run_command(command: Sequence[str]) -> int:
    return subprocess.run(command, check=False).returncode


def main(
    argv: Sequence[str] | None = None,
    run: Runner = run_command,
    *,
    clock: Callable[[], float] = time.monotonic,
    out: IO[str] = sys.stdout,
) -> int:
    args = parser().parse_args(argv)
    names = select(args.only, args.fast)
    with tempfile.TemporaryDirectory(prefix="presubmit-") as workdir:
        plan = [
            step for step in steps(sys.executable, Path(workdir)) if step[0] in names
        ]
        return run_steps(plan, run, clock=clock, out=out)


if __name__ == "__main__":
    raise SystemExit(main())
