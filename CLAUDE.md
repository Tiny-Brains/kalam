# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`kalam` ships **no server code**. It is an Orion **1.8.1** package, loaded at boot into the
orion-server its runner image carries. It has three clocks. **`tb-match`** is a set of match lanes
that all run `tb-match-run`: claim ONE queued row, then `observe` → one `model_infer` per live seat
→ `step` until the engine stops returning views, then finish the row in place. **`tb-roster`**
registers, admits and activates on this node every version the ladder says is verified or active.
**`tb-admit`** is an admitting runner's only clock (`RUNNER_ROLE=admit`): claim one submission Soma
prepared, register it, let Orion admit it, play it over the reference observations, delete it and
report. Soma runs no model and judges the report.
`scripts/gen-kalam.py` is the source. `workflows/` and `channels/` are its gitignored output, which
the Dockerfile regenerates into the image. Kalam owns **execution only**. Soma owns the schema, the
public routes and every competitive decision, ants owns the rules, and Orion runs the models. The
packages coordinate through Postgres and never call each other. Anything human-facing (running a
runner, configuration, troubleshooting, releasing, layout) is in `README.md`.

## Checks

```sh
python3 scripts/gen-kalam.py --check   # generated files match the generator
./scripts/check-defs.sh                # --check + lint + clippy + fmt, all --deny-warnings; no stack
./scripts/check-sql.sh                 # PREPARE the generated SQL + assert the kalam role's grants;
                                       # needs tinybrains-db-1 and ../soma/migrations
docker compose up -d --build           # a real match: this runner against web's local stack

# check-defs.sh refuses an orion-server that is not 1.8.x (older ones report misleading schema
# errors). The image carries the right one:
docker run --rm --entrypoint orion-server ghcr.io/tiny-brains/kalam clippy /pkg/kalam --deny-warnings
```

There is no unit-test suite. Lint and `check-sql.sh` are the compiler, and neither exercises leases
or turns. `tinybrains conform` on a replay the runner wrote is the test that a match was *played*
identically, not just recorded.

## Rules

- **Two modes, one task list.** `[vars] mode` decides how the run reaches the queue. `api` (the only
  mode any image runs) calls Soma's runner gate, `/v1/runner/*`, and holds no database credential.
  `db` runs the statements here over `kalam-db` as the `kalam` role. It is the rollback. Both paths
  are tasks gated on the mode, and they meet in `open`, which normalises `data.claimed`, `data.row`,
  `data.token` and `data.ct` (the execution contract). Nothing below `open` knows which mode it is in.
- **The `db`-mode statements are copies of Soma's gate statements** (`soma/workflows/soma-runner-*.json`),
  and nothing compares the two. A change to claim, row, release, start, renew, finish or roster is
  made in both repositories. Don't change the SQL text here on its own.
- **A gate route's request field names are the contract**, not the column names or this generator's
  variable names. `finish` binds `data.req.result` and `data.req.engine_digest`. Send other names
  and the route binds nulls and answers `409 claim_lost`. Run `grep data.req.` in the route before
  changing a request body here.
- **The renew's two failures differ in `api` mode** (`RENEW_LOST`). `applied: false` means the claim
  is gone, so halt. A call that never arrived means almost nothing, so play on: the lease outlasts
  the renew interval, and a lost claim is refused at finish. Don't collapse them.
- **Everything is fenced on one claim token.** In `db` mode `token` mints it; in `api` mode the gate
  does and returns it on `claim.token`. `db_write` returns only `rows_affected`, which is how a fenced
  statement learns its fate (`wrote()`). Through the gate the route answers the same fact as a field
  (`applied`, `started`, `state`).
- **The match lanes differ only in `channel_id` and `concurrency.key`.** `forbid` on a per-lane key
  gives one match in flight per lane. Their shared `config` is `shared/kalam.json`. That file is a
  shared document the admin API does not accept, so `load-package.sh` runs `orion-server compile`
  and then `package apply`.
- **A runner has one role** (`RUNNER_ROLE`, `entrypoint.sh`). `match` loads the lanes and the roster
  and drops `tb-admit`; `admit` loads `tb-admit` alone, one cron worker, so an admission never shares
  a node with a match and its probe is never timed under a match's load. `load-package.sh` does the
  dropping; the workflows load either way.
- **An admitting runner executes and never decides.** It registers the registration Soma rebuilt,
  never the competitor's manifest; sends Orion's admission record and stats as they came, and the
  probe's tally; and judges nothing, not a stage, a budget or a timing. Whose fault a refusal is, is
  Soma's `admission_facts()`. `tb-admit` is `api` only: there is no `db` copy to keep in step.
- **The admission node keeps nothing between walks**: `tb-admit` deletes what it registered, and a
  409 on `register` clears a dead walk's leftover. Never archive instead: Orion activates only a
  `draft`, so an archived model can never be admitted again.
- **The worker pool is the lanes plus one, and the one is the roster's.** Orion has one cron pool
  per node and a match holds its worker for the whole match, so `entrypoint.sh` loads only
  `RUNNER_CRON_WORKERS` lanes (`stage-set.py --drop` leaves the rest out) and sizes `cron.workers`
  one larger. Never size the pool to the lanes: long matches then skip the roster's ticks, and every
  lane refuses the trials of versions it never registers.
- **`group_runs()` folds consecutive tasks that share a condition into a task group.** A group's
  condition is evaluated once, and a falsy one skips the span *without evaluating the members'*,
  which is what makes stripping the members' conditions equivalent.
- **A seat is a task.** The task list is fixed, so the generator emits `MAX_SEATS` copies of each
  per-seat task, each conditioned on the row's `seat_count`, and the claim refuses a wider row.
  `MAX_SEATS` must reach the top seat count in the cartridge's `limits.boards`. web's
  `scripts/check/configs.sh` reads the `MAX_SEATS = <n>` line, so keep that exact form.
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
  runner has its own SQLite. Shared state makes each lane a fleet-wide singleton, which looks exactly
  like idle capacity.

## Gotchas

Orion:

- **`package apply` stops at the first workflow it can't activate.** Everything after it stays a
  draft, and a draft cron channel has no schedule. `/readyz` and the plugin list still look healthy,
  which is why the entrypoint checks the count of active `pkg:kalam` channels and kills the node if
  the check fails.
- **An absent `env://` skips the connector, and an empty one resolves.** A workflow that names a
  skipped connector can't activate, and a connector needs **every** `env://` it names. That is why
  compose sets the `db`-mode variables to `""` rather than omitting them.
- **A connector resolves `env://NAME` only when it is the whole string.** `"Bearer env://X"` is a
  literal, so `kalam-orion` reads the whole header from `ORION_ADMIN_BEARER`.
- **An http connector's `url` can't be `env://`, and `allow_private_urls` is a boolean.** Every
  offline gate validates both before references resolve, so `stage-set.py` writes them into a staged
  copy at load. A storage connector's `endpoint` does take `env://`.
- **Storage connectors are SSRF-checked too.** `kalam-orion` is always allowed private addresses
  (it is this node). Everything else follows `KALAM_ALLOW_PRIVATE_URLS`, and without it a local
  models bucket fails the roster at the `head` stage.
- **A plugin can't be deleted (409) while an active workflow calls its functions.**
  `load-package.sh`'s retire sweep ignores that failure.
- **Every admin API reply is wrapped in `{"data": …}`.** The barrier reads
  `temp_data.m0.data.status`. If it read `temp_data.m0.status` instead, `null` would never equal
  `"active"`, and every match would be released for ever with both sides looking healthy.
- **`{"now": []}` returns an ISO-8601 instant.** `played_ms` is computed in Postgres from it.
- **A cron occurrence's `data` is unreadable**: it returns nowhere, and a trace carries no per-task
  detail (and is dropped above `trace_queue.max_result_size_bytes`). A failing run can be diagnosed
  only by *which* task failed.
- **Keep `task_details: false` on the match lanes and on `tb-admit`.** With it on, Orion builds a
  full trace of every write, the per-seat policy tensors included, outside `max_snapshot_bytes`, and
  `errors_only` drops it only after it has been built and serialized. A runner's memory then grows by
  gigabytes.
- **Loop bounds.** `tb-match-run` loops at most `MATCH_LOOP_MAX` (1010) sweeps: one per turn plus
  the finishing sweep, so it is also the ceiling on `max_turns`, which Soma's `season_rule_spec()`
  caps at 1000 to match. web's `configs.sh` reads the `MATCH_LOOP_MAX = <n>` line, so keep that
  exact form. `[engine] max_loop_iterations` must sit above it. The
  lane's `timeout_ms` (40 min) is sized for 1000 turns × 1000 ms plus overhead, and the shutdown
  force timeout (2700 s) must stay above it.
- **`models.max_timeout_ms` clamps `model_infer`'s deadline silently.** It must be at least the
  season ceiling for `turn_ms`.
- **Model preload.** `preload = "referenced"` warms only literal model ids, and `tb-match` computes
  its model id. That is why the roster tags every registration `ladder` and the config sets
  `preload_tags`.
- **Replay PUT.** `storage_presign` returns a plain string. `http_call` always prefixes the
  connector's base URL onto `path`, so the URL is trimmed with `substr(url, length(endpoint))`,
  which is exact only because the storage connector sets `force_path_style`. `http_call` parses
  replies as JSON unless given `response_format: "text"`, and an S3 PUT answers with an empty body.
- **A mapping whose logic is `null` writes nothing.** The slot keeps the previous sweep's value.
  `mapping()` emits `False` to clear a slot, and `temp_data` survives a sweep, so per-item slots are
  cleared explicitly.
- **`${…}` in `docker/*.toml.tmpl` is substituted even inside comments.** Only `${X}` and `${X:-d}`
  work: `:?` is an invalid name, and a nested default leaves a stray `}` on the value.
  Requiredness belongs in `docker-compose.yml`.

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

- **A generator change that never reached an image changes nothing.** Rebuild after any edit.
- **`tinybrains conform` compares an allowlist of fields that excludes `seats`**, so the
  non-reproducible cost counters in a replay can't break conformance. Keep anything
  non-deterministic out of the other fields.
- **`engine.ops_budget` must equal Soma's `adapter_ops_max`,** and `orion_version` must equal Soma's.
  web's `configs.sh` checks both, and the admission claim refuses a runner on another Orion.
- **`ADMIT_LOOP_MAX` must reach Soma's `admit_observations`.** `tb-admit` plays one observation a
  sweep and reports on the last; a claim carrying more stops at the loop's end with no report, and
  every submission expires. web's `configs.sh` reads the `ADMIT_LOOP_MAX = <n>` line, so keep that
  exact form.
- **`models.max_probe_ms` is pinned in `runner.toml.tmpl`**, because the admitting runner admits
  under it and every match runner's roster re-admits the same versions under it.

## Removing db mode

Delete it once the api path has proven itself. It goes as one change:

- [ ] `connectors/kalam-db.json` and `connectors/kalam-blobs.json`
- [ ] in `gen-kalam.py`: the `MODE_DB` tasks (`reap`, `claim`, `row`, `release`, `start`, `renew`,
      `presign`, `finish`, `roster`), the `K_*` and `R_ROSTER` statements, `MODE_DB`/`T0_DB`, the
      `db` arm of every `{"if": [MODE_API, …]}`, and the `[vars]`-built `data.ct` and its `vars` checks
- [ ] `docker/replica-db.toml.tmpl`, and `mode` in `runner.toml.tmpl`
- [ ] `docker-compose.yml`: the empty `KALAM_DB_URL`, `R2_BUCKET`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`
- [ ] `load-package.sh`: the `kalam-db` and `kalam-blobs` staging lines
- [ ] `scripts/check-sql.sh`, since the package would ship no SQL
- [ ] web's `scripts/check/configs.sh`: the execution-contract block, and point its other
      `replica-db.toml.tmpl` checks (ops budget, Orion version, drain, no cluster) at `runner.toml.tmpl`
