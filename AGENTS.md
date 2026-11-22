# Working on xbloom-studio

For coding agents, and for people. `tests.test_agent_docs` checks this file:
each `sh` command parses against the script it runs, cited paths and modules
exist, every module is in the map, and the file is at most 150 lines.

The package `xbloom_studio` is the `xbloom-studio` command, its daemon and the
`xbloom-studio-mcp` server for xBloom Studio machines. Tests are offline, with a
simulated machine. Python 3.10 to 3.14; `uv run --locked` syncs .venv from the
committed `uv.lock`. Run everything from the repository root.

## Commands

```sh
uv run --locked python -m scripts.presubmit --fast   # the inner loop: all but build, distribution
uv run --locked python -m scripts.presubmit          # every check, as CI runs it
uv run --locked python -m scripts.presubmit --only lint types
uv run --locked python -m scripts.run_tests tests.test_cli   # one module
uv run --locked python -m scripts.run_tests tests.test_cli.PureCommandTests.test_interrupt_exits_130
uv run --locked python -m unittest tests.test_cli    # serial, one process; for pdb
UPDATE_CONTRACTS=1 uv run --locked python -m unittest tests.test_contracts  # rewrite the MCP golden; fails if it changed
```

The oldest supported Python, in a second environment that `.gitignore` covers:

```sh
UV_PROJECT_ENVIRONMENT=.venv-310 uv run --locked --python 3.10 python -m scripts.presubmit --fast
```

The command line from source, against the simulator, with the test guards:

```sh
uv run --locked python -m tests.child xbloom_studio --connector simulator --direct machine state
```

Each such command gets a fresh private root and database; to keep state
between commands, pass `--db` with a path outside the repository.

The fixes presubmit prints for the lockfile, lint and format steps, in order:

```sh
uv lock
uv run --locked ruff check --fix .
uv run --locked ruff format .
```

### Presubmit

Steps, in order: lockfile, format, lint, types, publication, tests,
coverage-report (needs tests), coverage-gate (needs coverage-report), build,
distribution (needs build). A failing step does not stop the run; a step whose
producer failed is `skipped (needs X)`. The run ends with a table, a `rerun:`
line per failure and a `fix:` line where one exists. `--fast` and `--only` are
exclusive; `--only` adds the steps the named ones need. With a stale lockfile
`uv run --locked` stops before presubmit starts, so the lockfile rerun line
uses `uv run --frozen`. CI (`.github/workflows/ci.yml`) runs the full
presubmit on Python 3.10-3.14.

### Test runner

`scripts.run_tests` runs each module in its own process and private root, at
most `--jobs` at once (default min(4, CPUs)). A module's result is `ok`,
`FAIL`, `EMPTY` (no tests ran), `NOISE` (a line that is not unittest's own) or
`TIMEOUT` (300 s; it dumps every thread's stack, then kills the module's
process group). A failed module gets a `rerun:` line: the failed test IDs
where it can read them, else the module. Known limits: a descendant that starts
its own session survives the kill; a runner killed by SIGKILL, SIGHUP or SIGQUIT
leaves its workers and run root (`xbs-r-*` under /dev/shm or /tmp) to delete.

Known rare flake: `tests.test_control.ReviewRegressionTests.test_grinder_pulse_is_budgeted_from_the_start_frame`
can fail under heavy load. Rerun it before chasing it.

## Rules enforced by a check

- Offline: tests may connect only to loopback and local Unix sockets
  (`tests.offline`, checked by `tests.test_offline`). Every test module imports
  `tests` at module level, which installs the guards (`tests.test_isolation`).
- Isolation: each test process tree gets a private home, data, runtime and temp
  root, never the account's real leases. A child process that runs the
  package starts as `python -m tests.child MODULE ...`, never
  `python -m xbloom_studio` (`tests.test_isolation`).
- Publication: no secrets, home-directory paths, real e-mail, IPv4 or hardware
  addresses, and no symlinks, in any tracked or unignored file. Fixtures use
  MACs 00:00:5E:00:53:xx, RFC 5737 addresses (192.0.2.x, 198.51.100.x,
  203.0.113.x) and example.com mail (the `publication` step,
  `scripts.publication_policy`). That is why `CLAUDE.md` is a file that only
  says `@AGENTS.md`.
- Quiet output: a passing module prints only unittest's own lines, and a
  ResourceWarning is an error; anything else fails it as `NOISE`
  (`scripts.run_tests`).
- Style and types: `ruff format`, `ruff check` and strict mypy over the package
  and `scripts` (the `format`, `lint` and `types` steps; see `pyproject.toml`).
- Coverage: at least 90 % line and branch coverage for each of `xbloom_studio`
  and `scripts` (`scripts.check_coverage`).
- Agent skills and README examples match the shipped CLI and MCP tools
  (`tests.test_skills`); the built sdist and wheel have exactly the expected
  files and pass an installed probe (`scripts.check_dist`).
- MCP tool inputs match `tests/contracts/mcp_tools.json` (`tests.test_contracts`).

## Policy no check enforces

- Never call live `xbloom-studio` MCP tools or a real machine to verify a
  change. Use the simulator: `--connector simulator`, `xbloom_studio.simulator`
  and `tests.machine`.
- New tests are small, test one behaviour and are named for it, like
  `test_interrupt_exits_130`. No class names from review rounds, such as
  `ReviewRegressionTests`.
- Keep `scripts` at 100 % line and branch coverage, and do not lower the
  package's.
- Change the MCP golden only on purpose, and explain its diff in the PR body.

## Environment

- `XBLOOM_STUDIO_TEST_TMPBASE` is the only knob: the directory for new test
  roots (default: the first usable of /dev/shm and /tmp, else Python's temp
  directory). Keep the resolved path (symlinks followed) to 26 bytes or less;
  a longer base breaks a socket-path test, under bare unittest too.
- Never export `XBLOOM_STUDIO_TEST_ROOT`. It is internal, set by whoever creates
  a root; a stale value makes `import tests` raise.
- `XBLOOM_STUDIO_DEVICE` is removed from every test environment.

## Map

| Area | Modules | Tests |
|---|---|---|
| Wire protocol | `xbloom_studio.protocol` `xbloom_studio.events` `xbloom_studio.telemetry` | `tests.test_protocol` `tests.test_events` `tests.test_catalog` |
| Link and session | `xbloom_studio.connector` `xbloom_studio.transport` `xbloom_studio.simulator` `xbloom_studio.client` `xbloom_studio.ownership` | `tests.test_transport` `tests.test_readonly` |
| Machine control | `xbloom_studio.application` `xbloom_studio.control` `xbloom_studio.machine_controls` `xbloom_studio.machine_api` `xbloom_studio.command_schema` `xbloom_studio.intents` `xbloom_studio.journal` `xbloom_studio.preset_writes` | `tests.test_application` `tests.test_control` `tests.test_machine_controls` `tests.test_command_schema` `tests.test_journal` `tests.test_preset_writes` `tests.test_validated_upload` |
| Recipes | `xbloom_studio.recipes` `xbloom_studio.coffee` `xbloom_studio.tea` `xbloom_studio.recipe_validation` `xbloom_studio.tea_validation` `xbloom_studio.grind_profiles` `xbloom_studio.serialization` | `tests.test_recipe_encoding` `tests.test_recipe_validation` |
| Guidance and imports | `xbloom_studio.coffee_guidance` `xbloom_studio.tea_guidance` `xbloom_studio.templates` `xbloom_studio.providers` `xbloom_studio.provider_exports` `xbloom_studio.provider_native` | `tests.test_guidance` `tests.test_providers` |
| Storage | `xbloom_studio.storage` `xbloom_studio.migrations` `xbloom_studio.pagination` | `tests.test_storage` `tests.test_migrations` |
| Entry points | `xbloom_studio.facade` `xbloom_studio.cli` `xbloom_studio.rpc` `xbloom_studio.rpc_types` `xbloom_studio.rpc_wire` `xbloom_studio.ssh_rpc` `xbloom_studio.mcp_server` `xbloom_studio.mcp_contracts` | `tests.test_facade` `tests.test_cli` `tests.test_rpc` `tests.test_ssh_rpc` `tests.test_mcp` `tests.test_contracts` `tests.test_daemon_processes` `tests.test_skills` |
| Errors | `xbloom_studio.errors` `xbloom_studio.diagnostics` (catalog: `docs/diagnostics.md`) | `tests.test_diagnostics` |
| Test guards | `scripts.testroot` `scripts.run_tests` `tests.isolation` `tests.offline` `tests.child` | `tests.test_testroot` `tests.test_run_tests` `tests.test_isolation` `tests.test_offline` |
| Repository checks | `scripts.presubmit` `scripts.publication_policy` `scripts.check_coverage` `scripts.check_dist` | `tests.test_presubmit` `tests.test_publication_policy` `tests.test_check_coverage` `tests.test_check_dist` `tests.test_agent_docs` |
