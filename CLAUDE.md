# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`kalam` ships **no server code**. It is an Orion **1.8.1** package — two cron workflows, five
channels, five connectors, and the Ants wasm component — loaded into an orion-server that DevOps
owns. Two clocks since the 1.8.1 rebuild (devops/docs/decisions.md, the R-series):

- **`tb-match`** claims ONE queued row and plays it: `observe` → one `model_infer` per live seat →
  `step`, until the engine stops returning views, then finishes the row in place. Four channels
  point at it, so a replica plays four matches at once and the DB claim keeps them off each other's
  rows. The *wave* it replaces existed to amortise one batched inference call across many seats —
  a number that is two on every Ants map.
- **`tb-roster`** reconciles this node's model set with the shared schema: every version the ladder
  says is verified or active is registered here, admitted here and activated here. **Jodi never
  calls a replica** — models are a per-node entity because each replica is its own Orion, and a
  clock that reconciles from Postgres needs no replica list anywhere.

There is no Axon sidecar, no residency barrier and no `/play`: Orion's own `models` entity fetches
the artifact from the bucket by digest and runs it under `tract`.

**`scripts/gen-kalam.py` is the source; `workflows/*.json` and `channels/*.json` are build output.**
The SQL and JSONLogic are unreadable inline in JSON and readable in the generator, so they live
there and it inlines them. `--check` fails if what is on disk has drifted; the Dockerfile runs it.

`docs/design.md` is the specification and `soma/docs/schema.md` §4 owns the statements. Where this
repo and those documents disagree, say so and fix one of them; update `README.md`'s Status block
when work lands.

Ownership is strict: Kalam owns **execution only**. Jodi owns every competitive decision (admission,
pairing, rating, promotion), Soma owns the schema and the public routes, Ants owns the rules, Orion
owns inference. The packages coordinate through Postgres and never call each other — and the one
HTTP call this package makes is to **its own node's admin API**, which is where its model set lives.

## Commands

```sh
python3 scripts/gen-kalam.py               # regenerate the workflows + channels (build output)
python3 scripts/gen-kalam.py --check       # fail if what is on disk drifted from the generator
./scripts/check-sql.sh                     # PREPARE every shipped statement + assert the role's grants
                                           # (including the roster's column-level read of model_versions)
KALAM_ALLOW_PRIVATE_URLS=1 R2_ENDPOINT=http://minio:9000 ./scripts/load-package.sh
docker build -t tinybrains/kalam:dev .     # the artifact image -- the package that actually ships

# lint needs the PINNED 1.8.1 binary. `orion-server` on the host is often older and reports
# misleading schema errors; every replica mounts the package volume, so:
docker exec tinybrains-kalam-1-1 orion-server lint /pkg/kalam --deny-warnings
```

`check-sql.sh` needs Docker and a running `tinybrains-db-1`; it reads Soma's migrations from
`../soma/migrations` (`MIGRATIONS` overrides) and recreates a `kalam_sqlcheck` scratch database.
`load-package.sh` targets `ORION_ADMIN` and sweeps everything tagged `pkg:kalam` before re-creating
it, so it is re-runnable; `docker compose run --rm loader` from devops does the same for all three
packages. There is no unit-test suite — lint plus `check-sql.sh` are the compiler for this repo, and
neither exercises leases or turn execution. A real match needs the DevOps stack.

## Architecture

**Everything is fenced on one claim token.** `token` mints a uuid before the claim; every statement
afterwards carries `claim_token = ($1)::uuid`, so a replica whose lease was reaped writes nothing
anywhere. `db_write` returns only `rows_affected`, which is how a fenced statement learns its fate —
`wrote()` in the generator, and `halt_unless` on it.

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

**A seat is a task.** The task list is fixed, so `MAX_SEATS` (4) `model_infer` tasks are generated,
each conditioned on the seat existing, being live and not having forfeited — and the claim refuses
a row with more seats than that rather than playing it short one.

**The head is decoded here, not in the manifest** (decision R3). A `result` expression's root is the
output tensors alone, so it cannot see the observation and cannot gather at the ants' cells: the
workflow asks for `raw: true` and does the gather itself, branching on the head's rank —
`[1,5,H,W]` gathers, `[n,5]` is already in `mine` order. `devops/cli/src/model.rs` does the same in
Rust, and `tinybrains conform` is what keeps them equal.

**`devops/cli/src/wave.rs` is a deliberate second implementation** of five of this repo's rules — the
explicit `{m, seat, action}` form, omission-as-no-op for a forfeited seat, cumulative strikes,
`engine_rank + seat_count` for forfeit ranks, and the flat echoed refs — plus the head decode above.
`tinybrains conform` diffs a replay envelope against a local re-run and is what keeps the two
honest. A change to any of them here is a change there.

## What breaks if you forget it

- **A generator run that never reached the image ships a stale package**, and nothing at runtime
  notices. `check-sql.sh` reads the JSON on disk; the image regenerates it, so the two agree only if
  you rebuild. After editing anything here: `docker compose build kalam-artifacts && docker compose
  run --rm --no-deps kalam-artifacts` — a package volume holds the package's own scripts too, so a
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
