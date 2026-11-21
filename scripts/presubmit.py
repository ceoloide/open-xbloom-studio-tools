"""Run every repository check in order; CI runs exactly this command.

Usage: uv run --locked python -m scripts.presubmit
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

Runner = Callable[[Sequence[str]], int]


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
            "coverage report",
            [python, "-m", "coverage", "json", f"--data-file={data}", "-o", report],
        ),
        ("coverage gate", [python, "-m", "scripts.check_coverage", report]),
        ("build", ["uv", "build", "--quiet", "--out-dir", dist]),
        ("distribution", [python, "-m", "scripts.check_dist", dist]),
    ]


def run_steps(plan: Sequence[tuple[str, Sequence[str]]], run: Runner) -> int:
    for name, command in plan:
        print(f"==> {name}", flush=True)
        status = run(command)
        if status != 0:
            print(f"presubmit failed at {name} (exit {status})", file=sys.stderr)
            return 1
    print("presubmit passed")
    return 0


def run_command(command: Sequence[str]) -> int:
    return subprocess.run(command, check=False).returncode


def main(run: Runner = run_command) -> int:
    with tempfile.TemporaryDirectory(prefix="presubmit-") as workdir:
        return run_steps(steps(sys.executable, Path(workdir)), run)


if __name__ == "__main__":
    raise SystemExit(main())
