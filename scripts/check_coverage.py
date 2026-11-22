"""Enforce line and branch coverage minimums from a coverage JSON report.

Each top-level source root (the package, the repository tools) is gated on
its own, so well-covered tooling cannot hide untested package code. Each file
with at least FILE_STATEMENTS statements must also reach FILE_MINIMUM, so a
new untested module cannot hide inside a passing root. Every failure says
where the uncovered code is.
"""

from __future__ import annotations

import argparse
import bisect
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePath
from typing import Any

MINIMUM_PERCENT = 90.0
FILE_MINIMUM = {"line": 60.0, "branch": 50.0}
FILE_STATEMENTS = 20
LARGEST = 5
REQUIRED_ROOTS = {"scripts", "xbloom_studio"}
COUNTS = ("covered_lines", "num_statements", "covered_branches", "num_branches")
UNCOVERED = {
    "line": ("lines", "num_statements", "covered_lines"),
    "branch": ("branches", "num_branches", "covered_branches"),
}


def percentages(totals: Mapping[str, int]) -> dict[str, float]:
    """Return line and branch percentages; nothing measured counts as covered."""
    result = {}
    for kind, covered, total in (
        ("line", "covered_lines", "num_statements"),
        ("branch", "covered_branches", "num_branches"),
    ):
        count = totals[total]
        result[kind] = 100.0 if count == 0 else 100.0 * totals[covered] / count
    return result


def group_totals(files: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    groups: dict[str, dict[str, int]] = {}
    for name, data in files.items():
        group = groups.setdefault(PurePath(name).parts[0], dict.fromkeys(COUNTS, 0))
        for count in COUNTS:
            group[count] += data["summary"][count]
    return groups


def largest_gaps(
    files: Mapping[str, Mapping[str, Any]], group: str, kind: str
) -> list[str]:
    """Name the group's files with the most uncovered lines or branches."""
    noun, total, covered = UNCOVERED[kind]
    gaps = sorted(
        (
            data["summary"][covered] - data["summary"][total],
            name,
            data["summary"][total],
        )
        for name, data in files.items()
        if PurePath(name).parts[0] == group
        and data["summary"][covered] < data["summary"][total]
    )
    return [
        f"  {name}: {-gap} of {count} {noun} uncovered"
        for gap, name, count in gaps[:LARGEST]
    ]


def failures(
    groups: Mapping[str, Mapping[str, int]],
    minimum: float,
    files: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    result = []
    for group, totals in sorted(groups.items()):
        for kind, value in percentages(totals).items():
            if value < minimum:
                result.append(
                    f"{group}: {kind} coverage {value:.2f}% is below {minimum:.2f}%"
                )
                result += largest_gaps(files or {}, group, kind)
    return result


def line_ranges(missing: Iterable[int], executed: Iterable[int]) -> str:
    """Join missing lines into ranges no executed line interrupts."""
    ran = sorted(executed)
    spans: list[list[int]] = []
    for line in sorted(missing):
        if spans and bisect.bisect(ran, spans[-1][1]) == bisect.bisect(ran, line):
            spans[-1][1] = line
        else:
            spans.append([line, line])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)


def arcs(missing: Iterable[Sequence[int]]) -> str:
    """Format branch arcs; a negative destination means leaving the code object."""
    return ", ".join(f"{a}->{'exit' if b < 0 else b}" for a, b in missing)


def file_failures(files: Mapping[str, Mapping[str, Any]]) -> list[str]:
    result = []
    for name, data in sorted(files.items()):
        if data["summary"]["num_statements"] < FILE_STATEMENTS:
            continue
        low = [
            f"{kind} coverage {value:.2f}% is below {FILE_MINIMUM[kind]:.2f}%"
            for kind, value in percentages(data["summary"]).items()
            if value < FILE_MINIMUM[kind]
        ]
        if not low:
            continue
        result.append(f"{name}: {', '.join(low)}")
        lines = data.get("missing_lines", [])
        if lines:
            ranges = line_ranges(lines, data.get("executed_lines", []))
            result.append(f"  missing lines: {ranges}")
        branches = data.get("missing_branches", [])
        if branches:
            result.append(f"  missing branches: {arcs(branches)}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--minimum", type=float, default=MINIMUM_PERCENT)
    args = parser.parse_args(argv)
    files = json.loads(args.report.read_text("utf-8"))["files"]
    groups = group_totals(files)
    problems = [
        f"missing coverage data for {root}"
        for root in sorted(REQUIRED_ROOTS - groups.keys())
    ]
    problems += failures(groups, args.minimum, files)
    problems += file_failures(files)
    for problem in problems:
        print(problem, file=sys.stderr)
    for group, totals in sorted(groups.items()):
        values = percentages(totals)
        print(f"{group}: line {values['line']:.2f}%, branch {values['branch']:.2f}%")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
