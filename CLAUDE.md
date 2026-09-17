# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`kalam` ships **no server code**. It is an Orion **1.8.1** package — two cron workflows, five
channels, six connectors, and the Ants wasm component — loaded into an orion-server that DevOps
owns. Two clocks since the 1.8.1 rebuild (devops/docs/decisions.md, the R-series):

- **`tb-match`** claims ONE queued row and plays it: `observe` → one `model_infer` per live seat →
  `step`, until the engine stops returning views, then finishes the row in place. Four channels
  point at it, so a replica plays four matches at once and the DB claim keeps them off each other's
  rows. The *wave* it replaces existed to amortise one batched inference call across many seats —
  a number that is two on every Ants map.
- **`tb-roster`** reconciles this node's model set with the shared schema: every version the ladder
  says is verified or active is registered here, admitted here and activated here. **No clock ever
  calls a replica** — models are a per-node entity because each replica is its own Orion, and a
  clock that reconciles from Postgres needs no replica list anywhere.

There is no Axon sidecar, no residency barrier and no `/play`: Orion's own `models` entity fetches
the artifact from the bucket by digest and runs it under `tract`.

**Two modes, one task list — and in `api` mode the statements are not in this repo.** `KALAM_MODE`
decides how a replica reaches the queue. `db` runs the eight statements (reap, claim, row, start,
release, renew, finish, roster) here, over `kalam-db` on the `kalam` role. `api` holds no database
credential and calls **Soma's runner gate** instead — `/v1/runner/*`, one route per statement, in
`soma/workflows/soma-runner-*.json` — and skips the reap, which the gate runs once at the centre.
Both paths are tasks in the same list, gated on the mode, and they meet at `data.ct`, the execution
contract: built from `[vars]` in `db`, read off the claim in `api`, where it comes from the row's own
season. Nothing below that line knows which mode it is in, and `tinybrains conform` has proved both
play the same match. Three things follow:

- **The `db` copies are a rollback, and nothing compares them with Soma's.** They leave when the
  `api` path has soaked (N10, `devops/docs/decisions.md` §5). Until then a change to one of the eight
  statements is made in both repositories, or the two modes quietly play by different rules.
- **The gate's request field names are the contract**, not the column names or this generator's
  variable names. `finish` binds `data.req.result` and `data.req.engine_digest`; sending `seats` and
  `engine_digest_played` bound two nulls and answered `409 claim_lost`. `grep data.req.` in the route
  before changing a body here.
- **A renew's two failures differ in `api` mode.** `applied: false` means the claim is gone and the
  run halts; a call that never arrived means almost nothing and the match plays on — the gate clamps
  `renew_every_n_turns` so the lease has ~10× the interval of headroom, and a claim that really was
  lost is refused at finish. `RENEW_LOST` in the generator is that rule; do not collapse it.

`devops/docs/architecture.md` §3a is the system-level picture and `devops/docs/deployment.md` §11
the operator's page for a runner on a machine the deployment does not own.

**`scripts/gen-kalam.py` is the source; `workflows/*.json` and `channels/*.json` are build output.**
The SQL and JSONLogic are unreadable inline in JSON and readable in the generator, so they live
there and it inlines them. `--check` fails if what is on disk has drifted; the Dockerfile runs it.

`docs/design.md` is the specification and `soma/docs/schema.md` §4 owns the statements. Where this
repo and those documents disagree, say so and fix one of them; update `README.md`'s Status block
when work lands.

Ownership is strict: Kalam owns **execution only**. Soma owns every competitive decision (admission,
pairing, rating, promotion — its clocks, the `jodi` repository until 16 September 2026), the
schema and the public routes, Ants owns the rules, Orion
owns inference. The packages coordinate through Postgres and never call each other. In `db` mode
the one HTTP call this package makes is to **its own node's admin API**, which is where its model set
lives; in `api` mode it also calls Soma's runner gate, which runs the same statements and
coordinates nothing.

## Commands


```sh
python3 scripts/gen-kalam.py               # regenerate the workflows + channels (build output)
python3 scripts/gen-kalam.py --check       # fail if what is on disk drifted from the generator
./scripts/check-defs.sh                    # drift + lint + clippy + fmt, all --deny-warnings; no stack
./scripts/check-sql.sh                     # PREPARE every shipped statement + assert the role's grants
                                           # (including the roster's column-level read of model_versions)
KALAM_ALLOW_PRIVATE_URLS=1 R2_ENDPOINT=http://minio:9000 ./scripts/load-package.sh
docker build -t tinybrains/kalam:dev .     # the artifact image -- the package that actually ships

# check-defs.sh asserts orion-server is 1.8.x before it runs anything, because an older one reports
# misleading schema errors on definitions that are correct. If the host binary is old, every replica
# mounts the package volume and carries the right one:
docker exec tinybrains-kalam-1-1 orion-server clippy /pkg/kalam --deny-warnings
```

**The four `tb-match-N` channels differ in `channel_id` and `concurrency.key` and nothing else.**
That is the whole of what makes them separate lanes: `forbid` on a per-channel key gives one match
in flight per lane, N lanes per replica. One channel with `policy: "allow"` would be unbounded, so
the lanes are the bound and not an accident. Their shared `config` is declared once, in
`shared/kalam.json`.

**`group_runs()` in the generator collapses each run of consecutive tasks sharing one condition
into a task group**, which carries the condition once instead of per member — what
`orion-server clippy` reports as `perf.redundant_step_condition`. A group's condition is evaluated
once on entry and a falsy result skips the span *without evaluating the members' conditions*, which
is what makes stripping them from the members equivalent rather than merely similar.

**Shared values live in `shared/kalam.json`**, a shared document the admin API does not accept, so
the set must be **compiled** before it is applied — which is what `load-package.sh` now does, with
`orion-server compile` followed by `orion-server package apply`.

`check-sql.sh` needs Docker and a running `tinybrains-db-1`; it reads Soma's migrations from
`../soma/migrations` (`MIGRATIONS` overrides) and recreates a `kalam_sqlcheck` scratch database.
`load-package.sh` targets `ORION_ADMIN` and sweeps everything tagged `pkg:kalam` before re-creating
it, so it is re-runnable; `docker compose run --rm loader` from devops does the same for all three
packages. There is no unit-test suite — lint plus `check-sql.sh` are the compiler for this repo, and
neither exercises leases or turn execution. A real match needs the DevOps stack.

## Architecture

**Everything is fenced on one claim token.** In `db` mode `token` mints a uuid before the claim; in
`api` mode the gate mints it and answers with it on `claim.token`. Every statement afterwards carries
`claim_token = ($1)::uuid`, so a replica whose lease was reaped writes nothing anywhere. `db_write`
returns only `rows_affected`, which is how a fenced statement learns its fate — `wrote()` in the
generator, and `halt_unless` on it; through the gate the route answers the same fact as a field
(`applied`, `started`).

**Element scope does not nest inside root scope.** Inside a `map`/`filter`/`reduce` body,
`{"var": "data.x"}` and `{"var": "metadata.vars.x"}` are `null`, and `{"==": [0, null]}` is **true**
under this engine's loose equality — so the mistake selects the falsy elements rather than failing.
Two consequences shape most of the workflow:

- **`reduce`'s seed is the only expression evaluated at root that threads into the body.** Every
  filter-by-a-root-value in the workflow is a reduce whose accumulator carries that value beside the
  list being built. `sift()` is the one place that is written; unwrap its `.items`.
- **Per-seat state rides in the `refs`** — a flat list, each entry carrying its own `m` and `seat`
  plus `strikes`, `forfeited`, and the three `infer_*` cost counters (seeded at `0`: `{"+": [null,
  x]}` on a first write is silent). It goes out through `observe`, comes back on the echoed `ref`,
  and goes into the next turn. A fixed task list has no other way to accumulate anything per seat,
  and the engine never looks inside one. `m` is 0 for the whole run now — one match — but it stays
  on every ref because the cartridge's API is wave-shaped and matching, not indexing, is what makes
  a ref safe.

**The row is read after `start`, not before it.** `row` filters on `status = 'running'`, so a row
whose start wrote nothing — a stale claim token — is read as nothing rather than played by a
replica that no longer owns it.

**The barrier is two GETs against this node's own admin API**, one per seat, once per match. It
replaces the residency barrier and answers a smaller question: can this node serve the model this
row names? A seat whose model is not `active` here releases the row with a refusal spent, which the
roster clock normally makes impossible — it is the lag case, not the common one. Reading the reply
means reading through the admin API's `data` envelope: `temp_data.m0.data.status`, not
`temp_data.m0.status`, or every match is released for ever with both sides looking healthy.

**A seat is a task.** The task list is fixed, so `MAX_SEATS` (8) `model_infer` tasks are generated,
each conditioned on the seat existing, being live and not having forfeited — and the claim refuses
a row with more seats than that rather than playing it short one. **The seat count a match is played
at is the row's**, which pair copied from the preset, which the map declares: a two-seat match runs
two inferences a turn and skips six. `MAX_SEATS` is only the task list's ceiling, and it is the
platform's — mapgen refuses a recipe above eight and the site draws two to eight — so no preset the
ladder pairs is one a replica cannot claim. **The renew interval is clamped by the row's seat count**
in both modes, `renew_every_n_turns × turn_ms × (seat_count + 1) ≤ lease_seconds`, because a turn can
cost every seat's deadline plus the step.

**The head is decoded here, not in the manifest** (decision R3). A `result` expression's root is the
output tensors alone, so it cannot see the observation and cannot gather at the ants' cells: the
workflow asks for `raw: true` and does the gather itself, branching on the head's rank —
`[1,5,H,W]` gathers, `[n,5]` is already in `mine` order. `cli/src/model.rs` does the same in
Rust, and `tinybrains conform` is what keeps them equal.

**`cli/src/wave.rs` is a deliberate second implementation** of five of this repo's rules — the
explicit `{m, seat, action}` form, omission-as-no-op for a forfeited seat or one with no ants, cumulative strikes,
`engine_rank + seat_count` for forfeit ranks, and the flat echoed refs — plus the head decode above.
`tinybrains conform` diffs a replay envelope against a local re-run and is what keeps the two
honest. A change to any of them here is a change there.

## What breaks if you forget it

- **A generator run that never reached the image ships a stale package**, and nothing at runtime
  notices. `check-sql.sh` reads the JSON on disk; the image regenerates it, so the two agree only if
  you rebuild. After editing anything here: `docker compose --profile build build kalam-pkg` —
  devops mounts this package straight from its image, so a
  stale volume fails like a bug in the change you just made.
- **`engine_digest` must equal the component's sha256 and `games.active_engine_digest`.** The claim
  filters on it, so a mismatch is not an error anywhere — the replica claims nothing, for ever, and
  the queue grows. **This repo no longer keeps its own copy of the component**: `Dockerfile` takes it
  from the cartridge's artifact image via `ANTS_REF`, so ants and kalam cannot disagree by
  construction — which they once did, from an edit that changed no behaviour at all. Moving
  `ANTS_REF` is an engine cutover (`devops/scripts/deploy/declare-engine.sh`), not a cleanup step.
  Re-sign with `devops/scripts/setup/sign-plugins.sh` afterwards or the node comes up `degraded`.
- **The strike ceiling comes off the MATCH ROW, and must not reappear in `[vars]`.** Pair stamps
  `matches.strike_ceiling` from the season (decision 54), so a trial is judged by the rule it was
  played under even if the deploy's number moved in between. `devops/scripts/check/configs.sh`
  asserts that this package's config does *not* set one — a second copy is what a future edit would
  wire back in. What still must agree across two files is `engine.ops_budget` here and the game's
  `adapter_ops_max`: admitted under one ceiling and struck under another is a competitor forfeiting
  for a rule nobody published.
- **A missing `[vars]` value is silent and catastrophic**: `{">=": [1, null]}` is true here, so an
  unresolved ceiling forfeits every seat on turn 0 and the match dies two turns later at `step`,
  naming neither the variable nor the cause. The `vars` task exists to make that loud.
- **`kalam-orion` carries the WHOLE header value in one variable.** A connector resolves
  `env://NAME` only when the reference is the entire string, so `"Bearer env://ORION_ADMIN_KEY"` is
  a literal and the node answers 401 with a message about the key. `ORION_ADMIN_BEARER` is what
  compose sets, and Orion reads `Authorization: Bearer <token>` and nothing else.
- **A storage connector is SSRF-checked too.** `kalam-models` dials a compose service name that
  resolves to a private address, so `load-package.sh` sets `allow_private_urls` under
  `KALAM_ALLOW_PRIVATE_URLS` — the same treatment the database connector gets. Without it the
  roster's registration fails at the `head` stage with a message about an internal IP.
- **Kalam's Orion must not share cluster state.** The `forbid`/`match-N` concurrency keys are local
  by virtue of local SQLite — four matches per replica, N replicas in parallel. Shared state makes
  each key a fleet-wide singleton, which looks exactly like idle capacity.
- **The `kalam` role cannot write ratings**, and `check-sql.sh` fails if it can reach `rated_at`.
- **A workflow must never interpret `wave_state`**, an observation, an action, or a ref. That
  boundary is enforced by review, not by types; interpreting it is a second engine.
- **Microseconds are floored where milliseconds become them.** `stats_output.inference_ms` is a
  float and `infer_us_total` is a `bigint`; `jsonb_to_recordset` refuses `10051.542` for one, and
  the finish statement then writes nothing with no explanation. `acts` floors once, at the one place
  the conversion happens.
- **Orion facts that are invisible until they bite**: `storage_presign` returns a plain string;
  `http_call` always prefixes its connector's base URL onto `path` (hence `substr(url,
  length(blob_endpoint))`, exact only because the storage connector sets `force_path_style`);
  `http_call` parses replies as JSON unless given `response_format: "text"`, and an S3 `PUT` answers
  with an empty body; `{"merge": [A, B]}` concatenates two computed arrays but `{"merge": <one
  expression>}` does not flatten at all.
