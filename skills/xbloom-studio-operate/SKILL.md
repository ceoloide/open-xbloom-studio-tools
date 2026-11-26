---
name: xbloom-studio-operate
description: Prepare, start, watch, stop and recover xBloom Studio machine actions (water, grinding, stored or inline recipes, exact repeats, settings and scale) through the xbloom-studio daemon. Use when the user asks the machine to do something or asks about a running or uncertain operation, not for recipe authoring.
license: MIT
compatibility: Needs a running xbloom-studio daemon (xbloom-studio serve) and access to its machine. Use the xbloom-studio-mcp machine and history groups or the xbloom-studio command.
---

# Operate the machine

Use `xbloom-studio-mcp --group machine --group history` for the machine actions
and recorded status. A client may prefix tool names. They reach one daemon
(`xbloom-studio serve`), locally or through `--ssh HOST`, which is the
machine's only owner. If the daemon is not reachable, report the setup problem.
Never fall back to a second Bluetooth connection, a direct mode or another
host.

## Permission comes first

Physical actions need the user's explicit request for that action in this
conversation. Installing the tools, saving a recipe, a recipe link or an
earlier unrelated brew is not permission. Pass `allow_control=true` only for
an action the user asked for, and `allow_untested=true` only when the user
accepts that the action is experimental (native coffee and tea recipes and
some commands, as reported by the tools). Do not ask again for something the
user already answered in this session. Pass `allow_untested=true` only where
the preview reports that the chosen route needs it; see "Upload now, start
later".

## Look before acting

- `machine_state` returns the cached state and never connects. Check the
  device, freshness and recording health; an empty or stale cache is not a
  current reading. `detail="full"` returns everything.
- `machine_connect` opens and keeps a monitoring connection. A new
  connection can beep, and the machine accepts one Bluetooth owner at a time.
  For authorized experimental work, open the session with
  `machine_connect(allow_untested=true)`. If a standard session is already
  open, use `machine_disconnect` before reconnecting. The experimental
  action still needs its own opt-in.
- `machine_prepare(request)` previews offline and never actuates. Requests:
  - water: `{"kind": "water", "volume_ml": 150, "temperature_c": 90, "flow_ml_s": 3, "pattern": "spiral"}`
  - grind: `{"kind": "grind", "size": 40, "rpm": 100, "duration_s": 5}`
    from settings the user gave; never guess a grind profile
  - stored recipe: `{"kind": "recipe", "reference": "ID or alias", "revision": 2}`
  - inline recipe: `{"kind": "recipe", "document": {...}}`
  - exact repeat: `{"kind": "repeat", "operation_id": "previous-id"}`

Use the preview's `resolved_request` when present to pin a saved recipe ID
and revision. Otherwise use the original request. Always pass the preview's
digest as `expected_recipe_sha256` so changed content is rejected. A requested
temperature is a target, not a measured outlet temperature.

## Start, watch, stop

`operation_start(request, idempotency_key, allow_control, allow_untested,
expected_recipe_sha256)` starts one attempt and returns its operation ID.
Use a new unique key for each intended attempt and keep it.

The daemon keeps running the operation if the caller disconnects. Follow it
with `history_get(ident)` for the recorded status and `machine_state` for
live readings; page `history_events` only when diagnosing.

- `operation_stop(operation_id)` stops it through the owning session, also
  during waits and host gates.
- `operation_pause_resume(operation_id, resume)` works only during an active
  device step.
- A host recipe step of kind `user` waits at a gate. Ask the user to do the
  physical step and confirm it, then call
  `operation_acknowledge(operation_id, token)` with that gate's `token`
  from the `gate` field in `machine_state`.
  It never advances a prompt shown on the machine itself; those are handled on
  the machine.

Report outcomes precisely: a rejected request did nothing; an ACK means the
machine received a command; observed activity and observed completion come
from telemetry; `unknown` means the outcome could not be confirmed.

## After any uncertainty

If a start times out, the connection drops, a response is lost or a status is
`unknown`:

1. Call read-only `operation_lookup(idempotency_key)` with the original key.
   If it returns an operation ID, use `history_get` for details. Check
   `machine_state` for current telemetry.
2. A matching recorded attempt is returned without execution. If the lookup
   finds no attempt, another start can run the machine even with the original
   key. Never submit another start automatically.
3. Explain what is known and let the user check the machine. A retry needs
   fresh state and the user's explicit go-ahead.

## Upload now, start later

`recipe_upload(request, allow_control, allow_untested,
expected_recipe_sha256)` uploads one native coffee or tea recipe without
starting it and returns a single-use token. `recipe_start_uploaded(token,
idempotency_key, allow_control)` starts it once;
`recipe_cancel_uploaded(token, allow_control)` releases the reservation. If it
returns `cancelled: false`, the preparation was already invalidated and no
exit commands were sent. Other writes are rejected while a recipe is prepared. A disconnect invalidates the
token without proving preparation was cancelled, and a token is not a readback
of the stored recipe.

The preview carries two separate flags. `requires_untested` governs a direct
`operation_start` of that request. `upload_requires_untested` is only the
opt-in check for a plan that `recipe_upload` otherwise accepts (exactly one
native coffee or tea step); it does not mean the plan can be uploaded. For an
experimental plan it is `false` only on the hardware-validated route: a
structured coffee recipe (fresh or an exact repeat) for the xDripper with the
grind-size upload selected (command 8001, which does not by itself establish
grinding), no bypass, no vibration, numeric pour temperatures and pours of 1
to 127 ml. For that route, upload with `allow_control=true` and the preview's
digest as `expected_recipe_sha256`, without `allow_untested`, then use
`recipe_start_uploaded`. Without that digest, or with a changed one, the
upload is refused before anything is sent. Starting the same recipe directly
with `operation_start` still needs `allow_untested=true`. If the preview has
no `upload_requires_untested` field, the daemon is older: follow
`requires_untested` for the upload too.

## Settings and scale

Read `machine_capabilities` first: it lists each command's parameter schema
and required opt-ins. Then call `machine_command(command, params,
allow_control, allow_untested)`. Unknown fields are rejected. An ACK confirms
receipt, not the physical effect. Scale use is `enter_scale`, `tare`, then
`exit_scale` on the same connection. Calibration and firmware commands are not
available.

Preset writes are experimental. A backup of a slot's previous contents is
optional for `write_preset` and required for `write_presets`; use only
contents the user supplies, never ones built from local aliases. Nothing
reads a preset back: even a success reports `readback_verified=false`. A
preset write switches the machine to pro mode while preparing and leaves it
there; `set_mode` with `recipes` switches back if the user wants.

One `write_preset` call is one attempt of three planned frames. Its result's
`attempt` field gives the `outcome` (`saved`, `saved_cleanup_failed`,
`rejected`, `unconfirmed`, `cancelled`, `refused`, or `no_preset_frame` when
it stopped after preparation began but before any preset frame; `interrupted` or
`saved_interrupted` when the daemon stopped first; `saved_late` when a save
arrived while the outcome was being recorded), `attempts_used`,
`retry_allowed` and `next_step`. When `retry_allowed` is true for a write the
user asked for, call `machine_disconnect`, then submit `write_preset` again
with exactly the same slot, flags and payload, without a separate
`machine_connect`: it connects itself and repeats only on an idle report
under 5 s old from that connection. A machine without water is not idle: a
write, first or repeated, is then refused as busy, sends nothing and uses no
attempt. Never recompile or change the contents to retry. The daemon allows
3 attempts per machine and slot, `no_preset_frame` ones included. Stop and
ask the user once a save was observed, an attempt was cancelled or the
attempts are used up. Only after their direction, pass
`acknowledge_incomplete` with the reported `write_id` to start a new write. A
fresh caller finds the exact contents and counts with
`machine_preset_writes(slot)`, which never connects. Batch preset writes,
brew starts and other commands are never repeated automatically. A batch is
refused while a target slot has an incomplete write, and leaves the writes of
slots it may have changed `superseded`.

## Errors

Error envelopes carry `error.code`, `details` and `next_step`. A failed
`machine_command`, such as an incomplete preset write, can instead return
an MCP tool error with `status: failed`, an `error` string, `details`,
`recovery_required` and, for `write_preset`, `attempt`. Preserve those details
and any backup location. A preset write refused by the attempt limit returns
`conflict` with the write in `details.preset_write`; nothing was sent. If a
start may already have happened, inspect its operation ID or look it up by
key; do not start again.

From the command line, the `water`, `grind`, `brew`, `history repeat` and
`machine` commands of `xbloom-studio` call the same methods, with `--key`,
`--allow-control`, `--allow-untested` and `--wait`. For example
`xbloom-studio history repeat ID --key KEY --allow-control --wait` repeats a
past brew exactly; recover with `xbloom-studio operation lookup KEY`.
`xbloom-studio machine preset-writes --slot A` shows a slot's recorded write.
