# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`kalam` ships **no server code**. It is an Orion **1.9.1** package, which the orion-server in its
runner image applies to itself at boot (`[packages] apply`). It has three clocks. **`kalam-match`** is
ONE channel whose `concurrency.slots` is how many matches this node plays at once, each run of
`kalam-match-run`: claim ONE queued row, then `observe` → one `model_infer` fanned out over the
seats asked this turn → `step` until the engine stops returning views, then finish the row in place. **`kalam-roster`**
registers, admits and activates on this node every version the ladder says is verified or active.
**`kalam-admit`** is an admitting runner's only clock (`RUNNER_ROLE=admit`): claim one submission Soma
prepared, register it, let Orion admit it, play it over the reference observations (one
`model_infer` fanned out over them, one at a time), delete it and report. Soma runs no model and judges the report.
Everything is authored JSON and committed — `workflows/`, `channels/`, `connectors/`, `sql/`,
`shared/` — with no generator and no build step. `entrypoint.sh` copies the package aside and shapes
THAT for this node's role and slot count before the server applies it. Kalam owns **execution only**. Soma owns the schema, the
public routes and every competitive decision, ants owns the rules, and Orion runs the models. The
packages coordinate through Postgres and never call each other. Anything human-facing (running a
runner, configuration, troubleshooting, releasing, layout) is in `README.md`.

## Checks

```sh
./scripts/check-defs.sh                # lint + clippy + fmt + clippy -c, all --deny-warnings; no stack
docker compose up -d --build           # a real match: this runner against web's local stack

# There is no check-sql.sh here any more: this package ships no SQL. The grant boundary it used to
# assert is soma's -- scripts/check-sql.sh there, and scripts/verify/run.sh against a live database.

# shared/package.json declares requires.orion, and lint/clippy/fmt/compile each check the running
# binary against it first -- there is no version test in any script here. The image carries one in
# range:
docker run --rm --entrypoint orion-server ghcr.io/tiny-brains/kalam clippy /pkg/kalam --deny-warnings
```

There is no unit-test suite. Lint is the compiler, and it does not exercise leases or turns. `tinybrains conform` on a replay the runner wrote is the test that a match was *played*
identically, not just recorded.

## Rules

- **A runner reaches the queue one way: Soma's gate.** Every statement is a call to `/v1/runner/*`,
  and this machine holds no database credential. `open` normalises the claim into `data.claimed`,
  `data.row`, `data.token` and `data.ct` (the execution contract), and nothing below it knows how
  the row arrived. There was a second, `db` path until the statements were deleted; the gate's are
  the only copy now, and `soma/scripts/verify/run.sh` reads each one out of the workflow that ships
  it, so no second copy can drift.
- **A gate route's request field names are the contract**, not the column names. `finish` binds `data.req.result` and `data.req.engine_digest`. Send other names
  and the route binds nulls and answers `409 claim_lost`. Run `grep data.req.` in the route before
  changing a request body here.
- **The renew's two failures differ in `api` mode** (`RENEW_LOST`). `applied: false` means the claim
  is gone, so halt. A call that never arrived means almost nothing, so play on: the lease outlasts
  the renew interval, and a lost claim is refused at finish. Don't collapse them.
- **Everything is fenced on one claim token.** The gate mints it and returns it on `claim.token`;
  a runner never invents one. Each fenced route answers its own fate as a field -- `applied`,
  `started`, `state` -- and a task that reads the wrong one cannot tell a lost claim from a win.
- **The match channel is ONE channel with `slots`.** `{"policy": "forbid", "key": "match", "slots":
  N}` admits up to N runs of that key at once, where `forbid` used to mean exactly one — so the four
  cloned channels that differed only in `channel_id` and `concurrency.key` are gone. A run reads its
  own slot as `metadata.trigger.singleton_slot`. `slots` is a LITERAL, never a reference (Orion
  takes lock cardinality as an authoring decision), so the committed `slots` is only a default and
  `entrypoint.sh` writes the number this node plays (`RUNNER_CRON_WORKERS`, up to Orion's 64) into
  its copy. It is a deployment setting: never make a node's capacity wait for a package change. `shared/kalam.json` holds the shared
  `config`; it is a shared document the admin API does not accept, so the set is always compiled
  before it is applied.
- **A runner has one role** (`RUNNER_ROLE`), and **`scripts/load-package.sh` is the one place that
  knows what a role's package looks like.** `match` gets the match channel and the roster; `admit`
  gets `kalam-admit` alone, one cron worker, so an admission never shares a node with a match and its
  probe is never timed under a match's load. It shapes a COPY, so the image's own tree is never
  written to and a restart shapes it the same way whatever the last boot did. All three workflows
  ship either way: a workflow with no channel never runs.
  **`entrypoint.sh` calls that script (`--compile-only`) rather than repeating the rule** — a node
  and an operator must shape a package identically, and writing it twice is how they stop doing so.
- **An admitting runner executes and never decides.** It registers the registration Soma rebuilt,
  never the competitor's manifest; sends Orion's admission record and stats as they came, and the
  probe's tally; and judges nothing, not a stage, a budget or a timing. Whose fault a refusal is, is
  Soma's `admission_facts()`.
- **The admission node keeps nothing between walks**: `kalam-admit` deletes what it registered, and a
  409 on `register` clears a dead walk's leftover. Never archive instead: Orion activates only a
  `draft`, so an archived model can never be admitted again.
- **The worker pool is the slots plus one, and the one is the roster's.** Orion has one cron pool
  per node and a match holds its worker for the whole match, so `entrypoint.sh` sets the channel's
  `slots` to `RUNNER_CRON_WORKERS` and sizes `cron.workers` one larger. Never size the pool to the
  slots: long matches then skip the roster's ticks, and every slot refuses the trials of versions it
  never registers.
- **`group_runs()` folds consecutive tasks that share a condition into a task group.** A group's
  condition is evaluated once, and a falsy one skips the span *without evaluating the members'*,
  which is what makes stripping the members' conditions equivalent.
- **A call per seat is ONE task with `for_each`; a per-seat decision is an `$each`, written ONCE.**
  `has` (can this node serve each seat's model) and `infer` (this turn's moves) fan one handler out
  over an array: `infer` over `temp_data.live`, the seats asked this turn with their model and view,
  results in `temp_data.infs` in that order. `acts` puts each back on its seat by counting the asked
  seats before it. Everything else per seat is an `$each` over `constants.seats` in
  `shared/kalam.json` — `{{seat}}` interpolates into an id, a name or a `var` path,
  `{"$param": "seat"}` inserts it typed — conditioned on the row's `seat_count`, and the claim refuses
  a wider row. **`constants.seats` must reach the top seat count in the cartridge's
  `limits.boards`**; web's `scripts/check/configs.sh` reads its length.
- **A failed inference is a strike, and `on_null: "unset"` is what makes it one.** `model_infer`
  writes nothing when it fails, and a mapping that yields `null` is skipped, so a per-seat slot
  (`p`, `s`, `act`) would keep the last turn's value: a seat that timed out after playing once
  would replay its last move and never be struck. `infer`'s `into` holds `null` for a failed
  element, and those three mappings carry `"on_null": "unset"`, so a failure decodes to no action
  and a seat not asked is charged nothing. `tinybrains` strikes the same way; a replay with a
  failing seat is identical to the CLI's.
- **Seats of one match are asked in parallel, bounded by the cores.** `infer`'s `max_concurrency` is
  a literal (1 as committed) that `load-package.sh` sets from `KALAM_SEAT_CONCURRENCY`, which
  `entrypoint.sh` derives as cores ÷ slots unless `RUNNER_SEAT_CONCURRENCY` names it. Each call's
  deadline runs from the moment it asks, **the wait for Orion's inference permit included**, and
  there is one permit per core — so slots × seats above the cores strikes seats for the node's load.
  The admission probe stays at 1: it measures one inference alone.
- **The board rides the claim.** The row carries `map` whole, and `world` passes it to `worldgen`,
  because the component carries no boards. `K_ROW` joins `season_maps` exactly as the gate's claim
  does.
- **The renew interval is clamped by the row's seat count** in both modes:
  `renew_every_n_turns × turn_ms × (seat_count + 1) ≤ lease_seconds`, because a turn can cost every
  seat's deadline plus the step.
- **The platform decodes the policy head, not the manifest.** A `result` expression sees only the
  output tensors, so it can't gather at the ants' cells. `infer` asks for `raw: true`, and `decode()`
  branches on the head's rank: `[1,5,H,W]` is gathered at the ants' flat indices, and `[n,5]` is
  already in `mine` order.
- **`cli/src/wave.rs` and `cli/src/model.rs` are a deliberate second implementation** of the head
  decode, the explicit `{m, seat, action}` form, omission as the no-op (a forfeited seat, or one with
  no ants), cumulative strikes, `engine_rank + seat_count` for forfeit ranks, and the flat echoed
  refs. `tinybrains conform` keeps them equal. A change here is a change there.
- **A seat with no ants is not asked.** Its decoded action would be `[]`, which is falsy and would
  be struck as a miss.
- **The strike ceiling comes off the match row** (`matches.strike_ceiling`, stamped by pair from the
  season), and it rides in each ref. It must never reappear in `[vars]`: web's `configs.sh` asserts
  its absence.
- **Kalam never** interprets `wave_state`, an observation, an action or a ref (that would be a
  second engine), retries an inference (a retried turn is a turn played twice), times anything
  itself (`timeout_ms` on the task is the bound), or calls another package. Its HTTP calls go to its
  own node's admin API, the gate and the buckets.
- **Never put a runner in cluster mode or shared state.** The `forbid` keys are local because each
  runner has its own SQLite, which lives on a tmpfs (`/var/lib/orion/state`) and is gone on every
  start: a crash's `running` occurrences must not hold the match slots after it. Shared state makes each lane a fleet-wide singleton, which looks exactly
  like idle capacity.

## Gotchas

Orion:

- **`package apply` stops at the first workflow it can't activate**, and everything after it stays a
  draft — a draft cron channel has no schedule. That used to leave `/readyz` green on a node serving
  nothing, which is why the entrypoint used to count this package's active channels and kill the
  node itself.
  `[packages] apply` is that invariant now: `/readyz` answers 503 (`components.packages: "applying"`)
  until every listed package is serving, and any failure — including a member the reload quarantines
  — exits the process non-zero.
- **An absent `env://` skips the connector, and SINCE 1.9.0 SO DOES AN EMPTY ONE.** An endpoint is
  scheme-checked again after its references resolve, so `""` is refused ("uses no scheme") exactly as
  `ftp://` would be. A workflow that names a skipped connector can't activate, and a connector needs
  **every** `env://` it names — so a variable a shipped connector reads must be set, and set to
  something well-formed. `RUNNER_BLOB_ENDPOINT` is the one to watch: `kalam-blobs-put` names it and
  `put` names that connector, so an unset one stops the node rather than failing a replay.
- **A connector resolves `env://NAME` only when it is the whole string.** `"Bearer env://X"` is a
  literal, so `kalam-orion` reads the whole header from `ORION_ADMIN_BEARER`.
- **An http connector's `url` CAN be `env://` now, and any connector boolean can be a reference.**
  `allow_private_urls` is `var://allow_private_urls`, from `[vars]`, and `kalam-api`,
  `kalam-orion` and `kalam-blobs-put` name their URLs as `env://`. Nothing stages a copy of the set
  any more. `var://` does NOT work in a URL — the scheme check runs on the authored string — so a
  URL is `env://` and a boolean is either.
- **Storage connectors are SSRF-checked too.** `kalam-orion` is always allowed private addresses,
  authored as a literal `true` because it is THIS NODE calling itself and one variable could never
  express both postures. Everything else follows `[vars] allow_private_urls`, and without it a local
  models bucket fails the roster at the `head` stage. `entrypoint.sh` normalises the variable to a
  bare TOML boolean: `${X:-false}` falls back only when X is UNSET, and compose passes it EMPTY.
- **A plugin can't be deleted (409) while an active workflow calls its functions.** `apply --prune`
  refuses before it writes anything when something outside the prune still uses a member.
- **Every admin API reply is wrapped in `{"data": …}`.** The barrier reads
  `temp_data.m0.data.status`. If it read `temp_data.m0.status` instead, `null` would never equal
  `"active"`, and every match would be released for ever with both sides looking healthy.
- **`{"now": []}` returns an ISO-8601 instant.** `played_ms` is computed in Postgres from it.
- **A cron occurrence's `data` is unreadable**: it returns nowhere, and a trace carries no per-task
  detail (and is dropped above `trace_queue.max_result_size_bytes`). A failing run can be diagnosed
  only by *which* task failed.
- **Keep `task_details: false` on every channel here.** Since 1.9.1 it is also what turns
  `capture_changes` on: with it, every write's old and new value is copied into the run's audit
  trail and kept until the run ends, and the per-step trace copies each again, outside
  `max_snapshot_bytes`. `errors_only` drops the trace only after it was built. A match's memory then
  grows with its turns, by gigabytes.
- **A `for_each` element runs on a deep copy of the message** (tensors are shared, nothing else), so
  `max_concurrency` copies are alive at once, and every write in a copy is captured whatever the run
  says — the fold drops them again when capture is off. `orion-server test` and `dry-run` run with
  capture on and a per-step trace, so a long match there costs tens of GB that a node never spends:
  test a match offline in tens of turns, not a thousand.
- **Orion's static analysis does not read `for_each.into` or `for_each.over`** as a write or a read.
  `perf.redundant_step_condition` can then propose a group that is wrong, and `clippy --fix` would
  apply it: check what reads an `into` path before accepting one.
- **Loop bounds.** `kalam-match-run` loops at most `MATCH_LOOP_MAX` (1010) sweeps: one per turn plus
  the finishing sweep, so it is also the ceiling on `max_turns`, which Soma's `season_rule_spec()`
  caps at 1000 to match. web's `configs.sh` reads the `MATCH_LOOP_MAX = <n>` line, so keep that
  exact form. `[engine] max_loop_iterations` must sit above it. The
  channel's `timeout_ms` (40 min) is sized for 1000 turns × 1000 ms plus overhead, and the shutdown
  force timeout (2700 s) must stay above it.
- **`models.max_timeout_ms` clamps `model_infer`'s deadline silently.** It must be at least the
  season ceiling for `turn_ms`.
- **Model preload.** `preload = "referenced"` warms only literal model ids, and `kalam-match` computes
  its model id. That is why the roster tags every registration `ladder` and the config sets
  `preload_tags`.
- **Replay PUT.** `storage_presign` returns a plain string. `http_call` always prefixes the
  connector's base URL onto `path`, so the URL is trimmed with `substr(url, length(endpoint))`,
  which is exact only because the storage connector sets `force_path_style`. `http_call` parses
  replies as JSON unless given `response_format: "text"`, and an S3 PUT answers with an empty body.
- **A mapping whose logic is `null` writes nothing.** The slot keeps the previous sweep's value, and
  `temp_data` survives a sweep, so a per-item slot is cleared explicitly with
  `{"path": …, "unset": true}`, which removes it (a read then sees `null`).
- **`${…}` in `docker/*.toml.tmpl` takes three forms and SKIPS comments.** `${X}`, `${X:-default}`
  and `${X:?message}` — which stops the boot with that sentence and the file, line and column when X
  is unset or empty, so requiredness can live beside the setting instead of only in
  `docker-compose.yml`. Defaults and messages nest. A form written out in a comment no longer makes
  its variable required.

JSONLogic (datalogic):

- **Element scope doesn't nest inside root scope.** Inside a `map`/`filter`/`reduce` body,
  `{"var": "data.x"}` and `{"var": "metadata.vars.x"}` are `null`. Inside a *nested* iterator the
  enclosing element isn't addressable at all. A `reduce` seed is the only root value that reaches
  the body, so every filter-by-a-root-value is a reduce carrying it in the accumulator: that is
  `sift()` (unwrap `.items`). Per-seat state rides in the flat `refs` for the same reason.
- **Loose equality.** `{"==": [0, null]}` is true, so a comparison against a path that doesn't
  resolve selects the falsy elements instead of failing. Use `===`/`!==`. Likewise `{">=": [1, null]}`
  is true, which is why the `vars` task halts on any missing `[vars]` value.
- **`{"+": [null, x]}` is silent.** Seed every counter at 0 (the refs' `strikes` and `infer_*` fields).
- **`{"val": [...]}` path segments are evaluated,** so an index can be computed. That is how the
  decode turns an argmax into a direction; there is no `at` operator.
- **`{"merge": [A, B]}` concatenates two computed arrays**, but `{"merge": <one expression>}` doesn't
  flatten at all.
- **Floor milliseconds where they become microseconds.** `inference_ms` is a float and the
  `infer_us_*` columns are integers, and `jsonb_to_recordset` refuses `10051.542`, which makes the
  finish write nothing. `moves` floors once.

Other:

- **A definition change that never reached an image changes nothing.** Rebuild after any edit.
- **`tinybrains conform` compares an allowlist of fields that excludes `seats`**, so the
  non-reproducible cost counters in a replay can't break conformance. Keep anything
  non-deterministic out of the other fields.
- **`engine.ops_budget` must equal Soma's `adapter_ops_max`,** and `orion_version` must equal Soma's.
  web's `configs.sh` checks both, and the admission claim refuses a runner on another Orion.
- **Soma's `admit_observations` × `admit_infer_ms` must fit `kalam-admit`'s `timeout_ms`.** The probe
  plays every observation the claim carries, one at a time, each up to its deadline; a run cut off
  by its timeout never reports, and the submission expires. web's `configs.sh` checks it.
- **`models.max_probe_ms` is pinned in `runner.toml.tmpl`**, because the admitting runner admits
  under it and every match runner's roster re-admits the same versions under it. It equals the
  cartridge's `limits.turn_ms` (web's `configs.sh` checks it), and `tinybrains check` measures the
  probe against that same `turn_ms`, so a change to one is a change to all three.
