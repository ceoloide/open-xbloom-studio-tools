# xbloom-studio

xbloom-studio is an independent open-source project building local tools for
xBloom Studio coffee and tea machines: a Python library, the `xbloom-studio`
command line with a local daemon, the `xbloom-studio-mcp` server and agent
skills.

This project is not affiliated with, endorsed by, or sponsored by xBloom.
Product names are used only to describe compatibility and remain the property
of their respective owners.

## Status

Alpha (0.0.2a0, the `0.0.2-alpha` prerelease). Interfaces may change. One
route is validated on hardware: uploading, then starting, a structured
xDripper coffee recipe within the limits under
[Recipes and history](#recipes-and-history). Brewing other coffee or tea from
encoded recipes, starting a recipe directly, preset writes and some settings
commands are not validated on hardware and stay behind an explicit
experimental opt-in.

## Install

Python 3.10 or newer. Install the source from the default branch in a fresh
virtual environment with the extras you need:
`ble` for local Bluetooth through [Bleak](https://github.com/hbldh/bleak) and
`mcp` for the MCP server. The core library has no dependencies.

```sh
git clone https://github.com/ceoloide/open-xbloom-studio-tools
cd open-xbloom-studio-tools
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ".[ble,mcp]"
```

Activate `.venv` from the checkout in each terminal before running the
commands below.

To install exactly this prerelease, run `git checkout 0.0.2-alpha` after
cloning. The earlier `0.0.1-alpha` tag predates these names: it installs the
`open-bloom-studio` distribution with the `xbloom` and `xbloom-mcp` commands.

## Try it offline

The bundled simulator is a deterministic, in-memory machine for tests and
examples; it does not model physical timing.

```sh
xbloom-studio recipe templates
xbloom-studio --connector simulator --direct water --volume-ml 100 --key first --allow-control
xbloom-studio operation lookup first
```

Every start needs an idempotency key. Repeating a matching request returns its
recorded attempt if one exists. After any uncertainty, run
`xbloom-studio operation lookup KEY` with the original key. If it returns an
operation ID, use `xbloom-studio history get ID` for details. If no attempt is
found, submitting another start can run the machine even with the original key.
Wait for fresh machine state and explicit authorization before retrying. A
start whose delivery stays unconfirmed is recorded as `unknown`.

## Use a machine

Controls require `--allow-control` (`allow_control=True` in Python); commands
that are not hardware-tested additionally require `--allow-untested`.
Read-only sessions never change settings.
The pour radius and vibration queries and the vibration level setter require
`--allow-control` because they prepare the machine and return it home.
Initializing either parameter or changing pour radius also requires
`--allow-untested`.

`xbloom-studio serve` keeps one owner of the machine and the database behind a
private Unix socket; other `xbloom-studio` commands and `xbloom-studio-mcp`
then use it. Set `XBLOOM_STUDIO_DEVICE` to your Bluetooth address, then run the
daemon in its own terminal:

```sh
xbloom-studio --device "$XBLOOM_STUDIO_DEVICE" serve
```

After the daemon prints a JSON response with `serving`, open a second terminal
in the checkout:

```sh
. .venv/bin/activate
xbloom-studio machine connect
xbloom-studio water --volume-ml 100 --key second --allow-control --wait
```

For experimental work, open the session with
`xbloom-studio machine connect --allow-untested`. If you already opened a
standard session, run `xbloom-studio machine disconnect` before reconnecting.
Experimental actions still need their own `--allow-untested`.

A single-slot preset write (`write_preset`) is one attempt of three planned
frames. If an attempt is rejected or unconfirmed, the daemon allows the
identical slot, flags and payload again from a fresh connection, at most three
attempts per machine and slot (an attempt whose preparation failed before any
preset frame counts too), recorded in the database journal. A different
write to that slot, or any write after a cancellation or the last attempt,
waits for an explicit `acknowledge_incomplete` with the reported write ID.
A batch write (`write_presets`) is never repeated, is refused while a target
slot has an incomplete single-slot write, and marks each slot's recorded write
superseded before its first frame for that slot; the mark lapses when that
frame provably sent no byte.
`xbloom-studio machine preset-writes --slot A` shows the recorded write without
connecting. A save event is not a readback of the stored preset.

For an offline run, start the daemon with
`xbloom-studio --connector simulator serve`. Run the daemon and the CLI under
the same account; they share ownership locks in
`~/.local/state/xbloom-studio/leases` under that account's home. Stop any older
daemon and finish direct-mode work before upgrading. The default socket is
`$XDG_RUNTIME_DIR/xbloom-studio/daemon.sock` when configured, or
`~/.local/state/xbloom-studio/run/daemon.sock` otherwise. A daemon that
restarts marks interrupted work `unknown` and does not replay it.

For a daemon on another host, add `--ssh <host-alias>` to any `xbloom-studio`
command. It runs `xbloom-studio relay` there over your SSH configuration with
strict host-key checking. Pin the relay as a forced command for that key, for
example
`restrict,command="xbloom-studio relay --socket /run/xbloom-studio/daemon.sock"`;
it forwards only a fixed method list.

### Other links

Any byte link implementing `xbloom_studio.Connector` (lifecycle, write size,
notifications and a stable machine identity; see `xbloom_studio/connector.py`)
runs behind the same handshake, control gates, acknowledgments, serialization
and uncertainty tracking. Pass `--connector package.module:factory` to `serve`
or `--direct`; the factory is called with the configured device (or `None`)
and returns the connector. In Python:

```python
import asyncio

from xbloom_studio import Interaction, ScriptedConnector, Studio


async def main():
    connector = ScriptedConnector()  # or BleakConnector("<address>"), or yours
    print(await Studio(connector=connector).read_device_info())
    async with Interaction(connector=connector, allow_control=True) as machine:
        print(await machine.dispense_water(volume_ml=100))


asyncio.run(main())
```

`xbloom_studio.facade.Application(store, connector=...)` exposes the same
`call(method, params)` dispatch as the CLI, daemon and MCP server.

## Recipes and history

Recipes are JSON documents validated offline
(`xbloom-studio recipe validate FILE`, or `check_recipe` in Python), which
reports every problem with its path and allowed values.
`xbloom-studio recipe constraints` lists every field and bound. A local SQLite
library, by default `$XDG_DATA_HOME/xbloom-studio/xbloom-studio.sqlite3`, keeps
recipe revisions, aliases and every attempt with its exact plan, so a repeat
reuses the stored plan byte for byte.

Structured coffee and tea recipes are experimental on hardware. One upload
route is validated: a structured coffee recipe for the xDripper with the
grind-size upload selected (`grinder_enabled` or `set_grinder_size` true,
sent as command 8001; the option selects that upload and does not by itself
establish that the machine grinds), no bypass, no vibration, numeric pour
temperatures and pours of 1 to 127 ml each. Its evidence is one observed brew
(15 g, 240 ml at 91 C and 3 ml/s): the pinned upload succeeded, one start
command ran it, and the machine returned to idle.

Preview the request first: `machine_prepare` over MCP, or
`xbloom-studio call machine_prepare --params FILE` with a JSON file holding
`{"request": ...}`. The preview's `upload_requires_untested` is
the opt-in check for a plan that the upload otherwise accepts (exactly one
native coffee or tea step); it does not say that a plan can be uploaded. When
it is `false` for an experimental plan, upload with `--allow-control` and the
preview's digest, without `--allow-untested`:
`xbloom-studio machine upload ID --allow-control --expected-recipe-sha256 DIGEST`,
then
`xbloom-studio machine start-uploaded TOKEN --key KEY --allow-control --wait`.
That upload is refused before any connection if the digest is missing or no
longer matches; a repeat of that brew uploads the same way. Passing
`--allow-untested` explicitly keeps the earlier rules, where the digest is
optional. The plan's own `requires_untested` is unchanged and still governs
a direct start (`brew`, `history repeat`, `operation_start`), which keeps
needing `--allow-untested`. Every other recipe, including opaque `native`
bytes with the same settings, keeps its opt-in. A daemon that predates this
omits `upload_requires_untested` and requires the opt-in for this upload too:
follow `requires_untested`.

The bundled starter recipes, coffee examples and tea profiles are original to
this project. Recipe imports are user-directed: one public recipe page at a
time, or an export you supply, read offline.

## Diagnostics

Errors from the CLI, daemon RPC, SSH relay and MCP server keep their stable
machine `error.code` and exit status `2`, and include a structured
`error.diagnostic` object with a stable code (such as `XB_RECIPE_NOT_FOUND` or
`XB_RECIPE_VALIDATION`), plain cause, truthful location, concrete fix,
canonical documentation URL and agent repair guide (with repository
checkout-relative paths clearly labeled alongside canonical URLs). On an
interactive terminal, `xbloom-studio` prints a plain-text diagnostic block on
`stderr` (with a minimal fail-closed redacted excerpt for local JSON files when
safe); non-TTY callers keep JSON on `stderr`. Override the stderr format
explicitly with the global option `--error-format human` or
`--error-format json` before the subcommand, for example
`xbloom-studio --error-format human recipe get missing-id`. See
[docs/diagnostics.md](docs/diagnostics.md) for the complete per-code catalog
and agent repair guides.

## MCP server and agent skills

`xbloom-studio-mcp` serves the same methods as MCP tools over stdio and needs a
running daemon (`xbloom-studio-mcp --ssh <host-alias>` for a remote one). Tools
come in three groups, so a client can be given only the roles it needs:

```json
{
  "mcpServers": {
    "xbloom-studio-recipes": {"command": "xbloom-studio-mcp", "args": ["--group", "recipes"]},
    "xbloom-studio-history": {"command": "xbloom-studio-mcp", "args": ["--group", "history"]},
    "xbloom-studio-machine": {"command": "xbloom-studio-mcp", "args": ["--group", "machine"]}
  }
}
```

If the MCP host does not inherit the activated virtual environment's
`PATH`, set each `command` to the absolute path of
`.venv/bin/xbloom-studio-mcp` in your checkout.

If the MCP host uses a different environment from the daemon, add
`--socket` and the daemon's absolute socket path to each local server's
`args`. This matters when the host does not inherit `XDG_RUNTIME_DIR`.

The `skills/` folder holds four standalone skills that follow the [Agent Skills
specification](https://agentskills.io/specification) and teach an agent to use
these tools safely: `xbloom-studio-recipes` (author, store and import recipes),
`xbloom-studio-tea` (craft tea drafts), `xbloom-studio-history` (find past
brews and add feedback) and `xbloom-studio-operate` (run, watch, stop and
recover machine work). Copy the folders into your agent's skills directory, for
example `~/.claude/skills/` for Claude Code. The skills ask for explicit user
permission before any physical action and never retry an uncertain start.

## Development

Coding agents, and people, start with [AGENTS.md](AGENTS.md): the commands
that validate a change, the rules the checks enforce and a map from each
module to its tests.

The repository uses [uv](https://docs.astral.sh/uv/) with a committed
lockfile. One command runs every check that CI runs: formatting, lint, strict
typing, the publication policy, the offline tests with coverage gates, and the
build and installed-artifact checks for the sdist and wheel, including the
CLI, daemon and MCP stdio server against the simulator.

```sh
uv run --locked python -m scripts.presubmit
```

A failing step does not stop the run. It ends with a table of every step,
and for each failure a command that reruns only that step (with the steps it
needs) and, for the lockfile, format and lint steps, the command that fixes
it. The lockfile step's rerun command uses `uv run --frozen`, because with a
stale lockfile `uv run --locked` stops before presubmit starts. `--fast` skips
the build and distribution checks; `--only STEP ...` runs the named steps;
`--help` lists the step names.

```sh
uv run --locked python -m scripts.presubmit --fast
uv run --locked python -m scripts.presubmit --only lint types
```

Tests use no real machine.

## License

MIT. See [LICENSE](LICENSE).
