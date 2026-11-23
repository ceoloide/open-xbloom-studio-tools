import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401 - installs the test guards
from scripts import check_coverage


def summary(lines, statements, branches, num_branches):
    return {
        "covered_lines": lines,
        "num_statements": statements,
        "covered_branches": branches,
        "num_branches": num_branches,
    }


def report(**files):
    return {name: {"summary": data} for name, data in files.items()}


class CoverageGateTest(unittest.TestCase):
    def test_line_and_branch_are_gated_separately(self):
        failures = check_coverage.failures
        self.assertEqual(failures({"p": summary(90, 100, 9, 10)}, 90), [])
        self.assertEqual(
            failures({"p": summary(100, 100, 8, 10)}, 90),
            ["p: branch coverage 80.00% is below 90.00%"],
        )
        self.assertEqual(
            failures({"p": summary(89, 100, 10, 10)}, 90),
            ["p: line coverage 89.00% is below 90.00%"],
        )

    def test_nothing_measured_passes(self):
        self.assertEqual(check_coverage.failures({"p": summary(0, 0, 0, 0)}, 90), [])

    def test_each_source_root_is_gated_alone(self):
        groups = check_coverage.group_totals(
            report(
                **{
                    "pkg/a.py": summary(1, 4, 0, 2),
                    "pkg/sub/b.py": summary(0, 0, 0, 0),
                    "tools/c.py": summary(1000, 1000, 100, 100),
                }
            )
        )
        self.assertEqual(groups["pkg"], summary(1, 4, 0, 2))
        self.assertEqual(
            check_coverage.failures(groups, 90),
            [
                "pkg: line coverage 25.00% is below 90.00%",
                "pkg: branch coverage 0.00% is below 90.00%",
            ],
        )

    def test_main_reads_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.json"
            for data, expected in (
                (summary(9, 10, 1, 1), 0),
                (summary(1, 2, 0, 0), 1),
            ):
                path.write_text(
                    json.dumps(
                        {
                            "files": report(
                                **{
                                    "xbloom_studio/a.py": data,
                                    "scripts/b.py": summary(10, 10, 1, 1),
                                }
                            )
                        }
                    )
                )
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(check_coverage.main([str(path)]), expected)
                self.assertIn("xbloom_studio: line", out.getvalue())
                self.assertEqual(bool(err.getvalue()), expected == 1)

    def test_scripts_require_full_line_and_branch_coverage_by_default(self):
        files = report(
            **{
                "scripts/tool.py": summary(99, 100, 99, 100),
                "xbloom_studio/api.py": summary(90, 100, 9, 10),
            }
        )
        status, _, err = run_main(files)
        self.assertEqual(status, 1)
        for kind in ("line", "branch"):
            self.assertIn(f"scripts: {kind} coverage 99.00% is below 100.00%", err)
        self.assertNotIn("xbloom_studio", err)
        self.assertEqual(run_main(files, "--minimum", "90")[0], 0)

    def test_main_rejects_missing_source_root(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.json"
            path.write_text(
                json.dumps({"files": report(**{"scripts/a.py": summary(1, 1, 0, 0)})})
            )
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                self.assertEqual(check_coverage.main([str(path)]), 1)
            self.assertIn("missing coverage data for xbloom_studio", err.getvalue())


def run_main(files, *args):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "coverage.json"
        path.write_text(json.dumps({"files": files}))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = check_coverage.main([str(path), *args])
    return status, out.getvalue(), err.getvalue()


class PerFileGateTest(unittest.TestCase):
    def test_untested_file_fails_under_passing_aggregate(self):
        files = report(
            **{
                "xbloom_studio/big.py": summary(9000, 9000, 100, 100),
                "scripts/tool.py": summary(50, 50, 10, 10),
            }
        )
        files["xbloom_studio/new.py"] = {
            "summary": summary(0, 300, 0, 0),
            "missing_lines": list(range(1, 301)),
            "executed_lines": [],
        }
        status, out, err = run_main(files)
        self.assertEqual(status, 1)
        self.assertIn("xbloom_studio: line 96.77%, branch 100.00%", out)
        self.assertEqual(
            err.splitlines(),
            [
                "xbloom_studio/new.py: line coverage 0.00% is below 60.00%",
                "  missing lines: 1-300",
            ],
        )

    def test_branch_only_failure_prints_arcs(self):
        files = {
            "pkg/a.py": {
                "summary": summary(20, 20, 1, 4),
                "missing_lines": [],
                "missing_branches": [[3, 5], [7, -1], [9, -9]],
            }
        }
        self.assertEqual(
            check_coverage.file_failures(files),
            [
                "pkg/a.py: branch coverage 25.00% is below 50.00%",
                "  missing branches: 3->5, 7->exit, 9->exit",
            ],
        )

    def test_floor_applies_from_twenty_statements(self):
        files = report(
            **{
                "pkg/small.py": summary(0, 19, 0, 10),
                "pkg/edge.py": summary(12, 20, 5, 10),
                "pkg/low.py": summary(11, 20, 5, 10),
                "pkg/a.py": summary(0, 20, 0, 0),
            }
        )
        self.assertEqual(
            check_coverage.file_failures(files),
            [
                "pkg/a.py: line coverage 0.00% is below 60.00%",
                "pkg/low.py: line coverage 55.00% is below 60.00%",
            ],
        )

    def test_report_without_missing_keys_still_names_file(self):
        files = report(
            **{
                "xbloom_studio/new.py": summary(0, 300, 0, 4),
                "scripts/tool.py": summary(1, 1, 0, 0),
            }
        )
        status, _, err = run_main(files)
        self.assertEqual(status, 1)
        self.assertEqual(
            err.splitlines(),
            [
                "xbloom_studio: line coverage 0.00% is below 90.00%",
                "  xbloom_studio/new.py: 300 of 300 lines uncovered",
                "xbloom_studio: branch coverage 0.00% is below 90.00%",
                "  xbloom_studio/new.py: 4 of 4 branches uncovered",
                "xbloom_studio/new.py: line coverage 0.00% is below 60.00%, "
                "branch coverage 0.00% is below 50.00%",
            ],
        )

    def test_executed_lines_split_missing_ranges(self):
        self.assertEqual(
            check_coverage.line_ranges([8, 1, 2, 4, 7, 11], [9, 10, 5]),
            "1-4, 7-8, 11",
        )

    def test_without_executed_lines_ranges_join_across_gaps(self):
        # coverage always writes executed_lines; without it no line interrupts.
        files = {"pkg/a.py": {"summary": summary(0, 20, 0, 0), "missing_lines": [1, 5]}}
        self.assertEqual(
            check_coverage.file_failures(files)[1:], ["  missing lines: 1-5"]
        )

    def test_arcs_to_or_from_missing_lines_are_dropped(self):
        files = {
            "pkg/a.py": {
                "summary": summary(18, 20, 1, 5),
                "missing_lines": [10, 11],
                "executed_lines": [3, 5, 7, 8],
                "missing_branches": [[3, 10], [5, -1], [7, 8], [10, 11]],
            }
        }
        self.assertEqual(
            check_coverage.file_failures(files)[1:],
            ["  missing lines: 10-11", "  missing branches: 5->exit, 7->8"],
        )


class AggregateRankingTest(unittest.TestCase):
    def test_line_and_branch_failures_rank_their_own_gaps(self):
        files = report(
            **{
                "pkg/a.py": summary(0, 100, 10, 10),
                "pkg/b.py": summary(90, 100, 0, 50),
                "pkg/c.py": summary(50, 100, 30, 40),
                "pkg/d.py": summary(95, 100, 9, 10),
                "pkg/e.py": summary(95, 100, 9, 10),
                "pkg/f.py": summary(95, 100, 9, 10),
                "pkg/g.py": summary(95, 100, 9, 10),
                "pkg/h.py": summary(95, 100, 9, 10),
                "tools/i.py": summary(0, 100, 0, 100),
            }
        )
        groups = check_coverage.group_totals(files)
        del groups["tools"]
        self.assertEqual(
            check_coverage.failures(groups, 90, files),
            [
                "pkg: line coverage 76.88% is below 90.00%",
                "  pkg/a.py: 100 of 100 lines uncovered",
                "  pkg/c.py: 50 of 100 lines uncovered",
                "  pkg/b.py: 10 of 100 lines uncovered",
                "  pkg/d.py: 5 of 100 lines uncovered",
                "  pkg/e.py: 5 of 100 lines uncovered",
                "pkg: branch coverage 56.67% is below 90.00%",
                "  pkg/b.py: 50 of 50 branches uncovered",
                "  pkg/c.py: 10 of 40 branches uncovered",
                "  pkg/d.py: 1 of 10 branches uncovered",
                "  pkg/e.py: 1 of 10 branches uncovered",
                "  pkg/f.py: 1 of 10 branches uncovered",
            ],
        )

    def test_ranking_counts_gaps_and_breaks_ties_by_name(self):
        files = report(
            **{
                "pkg/z.py": summary(0, 1, 0, 0),
                "pkg/y.py": summary(0, 1, 0, 0),
                "pkg/x.py": summary(0, 1, 0, 0),
                "pkg/w.py": summary(0, 1, 0, 0),
                "pkg/small.py": summary(1, 10, 0, 2),
                "pkg/big.py": summary(90, 100, 0, 0),
                "pkg/full.py": summary(100, 100, 5, 5),
            }
        )
        self.assertEqual(
            check_coverage.failures(check_coverage.group_totals(files), 90, files),
            [
                "pkg: line coverage 89.25% is below 90.00%",
                "  pkg/big.py: 10 of 100 lines uncovered",
                "  pkg/small.py: 9 of 10 lines uncovered",
                "  pkg/w.py: 1 of 1 lines uncovered",
                "  pkg/x.py: 1 of 1 lines uncovered",
                "  pkg/y.py: 1 of 1 lines uncovered",
                "pkg: branch coverage 71.43% is below 90.00%",
                "  pkg/small.py: 2 of 2 branches uncovered",
            ],
        )


if __name__ == "__main__":
    unittest.main()
