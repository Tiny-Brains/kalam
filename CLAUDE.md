# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`kalam` ships **no server code**. It is an Orion **1.7.0** package — one cron channel, one workflow,
four connectors, and a vendored Ants wasm component — loaded into an orion-server that DevOps owns.
It plays matches in *waves*: claim up to `wave_k` queued rows in one statement, hold their models in
the Axon sidecar beside it, and drive `observe → one /play call → step` for the whole wave at once,
finishing each row as its own match ends.

**`scripts/gen-kalam.py` is the source; `workflows/tb-wave-run.json` and `channels/tb-wave.json` are
committed build output.** The SQL and JSONLogic are unreadable inline in JSON and readable in the
generator, so they live there and it inlines them. Edit the generator, re-run it, commit both.

`docs/design.md` is the specification and `soma/docs/schema.md` §4 owns the statements. Where this
repo and those documents disagree, say so and fix one of them; update `README.md`'s Status block and
`../design/tracker.md` when work lands.

Ownership is strict: Kalam owns **execution only**. Jodi owns every competitive decision (admission,
pairing, rating, promotion), Soma owns the schema and the public routes, Ants owns the rules, Axon
owns inference. The packages coordinate through Postgres and never call each other.

## Commands

```sh
python3 scripts/gen-kalam.py               # regenerate the workflow + channel; commit the output
./scripts/check-sql.sh                     # PREPARE all 9 shipped statements + assert the role's grants
KALAM_ALLOW_PRIVATE_URLS=1 R2_ENDPOINT=http://minio:9000 ./scripts/load-package.sh
./scripts/vendor-engine.sh ../ants         # re-vendor the engine; see the warning below

# lint needs the PINNED 1.7.0 binary. `orion-server` on the host is often older and reports
# misleading schema errors; devops mounts ../kalam read-only into every replica, so:
docker exec tinybrains-kalam-1-1 orion-server lint /pkg/kalam --deny-warnings
```

`check-sql.sh` needs Docker and a running `tinybrains-db-1`; it reads Soma's migrations from
`../soma/migrations` (`MIGRATIONS` overrides) and recreates a `kalam_sqlcheck` scratch database.
`load-package.sh` targets `ORION_ADMIN` and sweeps everything tagged `pkg:kalam` before re-creating
it, so it is re-runnable; `docker compose run --rm loader` from devops does the same for all three
packages. There is no unit-test suite — lint plus `check-sql.sh` are the compiler for this repo, and
neither exercises leases or turn execution. A real wave needs the DevOps stack.

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
  x]}` on a first write is silent). It goes out through `observe`, onto the play row, back on the
  loader's echoed `ref`, and into the next turn. A fixed task list has no other way to accumulate
  anything per seat, and neither the engine nor the loader looks inside one. A *nested*
  `refs[m][seat]` shifts silently the moment one match in the wave ends.

**The wave is read twice, and it has to be.** The models are read before the residency barrier (for
the hold); the wave is read *after* `start` and filtered on `status = 'running'`, numbered by
Postgres with `row_number() - 1 AS m`. Number it at claim time instead and any barrier refusal
desynchronises the engine's match indices from the refs — `observe` then attaches no ref to any view
and the wave plays a thousand turns naming a model that does not exist, with no error anywhere.
JSONLogic cannot number a list, so Postgres must.

**The barrier's unit is a model, not a row.** Axon answers about `(weights_hash, adapter_hash)`
pairs; mapping a refusal back to the rows that seat it is a join from element scope into root scope,
so `release` and `fail` take the refused *hashes* and let Postgres do the join. The split branches on
`fault` (`loader` → back to the queue, no attempt spent; `model` → failed at once with the seat),
never on the reason word, so Axon can add reasons without a workflow change.

**A halt cannot release the models**, so every exit from a wave — idle, lease lost, complete — is an
`/unload` task followed by a terminal task, not a `filter`.

**The drain finishes one ended match per sweep** from a `data.pending` queue, with a tail drain after
the last match ends (terminal on "nothing live and nothing pending"). That is why `loop.max` is
`max_turns + wave_k`, not `max_turns`. The alternative was `3K` conditioned tasks evaluated every
turn for something that fires once per match.

**`devops/cli/src/wave.rs` is a deliberate second implementation** of five of this repo's rules — the
explicit `{m, seat, action}` form, omission-as-no-op for a forfeited seat, cumulative strikes,
`engine_rank + seat_count` for forfeit ranks, and the flat echoed refs. `tinybrains conform` diffs a
replay envelope against a local re-run and is what keeps the two honest. A change to any of those
five here is a change there.

## What breaks if you forget it

- **A generator run that is not committed ships a stale package**, and nothing at runtime notices.
  `check-sql.sh` reads the *shipped* JSON, so it is the guard.
- **`engine_digest` must equal the vendored component's sha256 and `games.active_engine_digest`.**
  The claim filters on it, so a mismatch is not an error anywhere — the replica claims nothing, for
  ever, and the queue grows. `scripts/vendor-engine.sh` **rewrites the pinned component**: running it
  is an engine cutover (`devops/scripts/deploy/declare-engine.sh`), not a cleanup step. Re-sign with
  `devops/scripts/setup/sign-plugins.sh` afterwards or the node comes up `degraded`.
- **`strike_ceiling` must equal Jodi's `forfeit_strikes`** — Kalam applies it, Jodi's count clock
  reads its consequences off the row. `devops/scripts/check/configs.sh` asserts it.
- **A missing `[vars]` value is silent and catastrophic**: `{">=": [1, null]}` is true here, so an
  unresolved `strike_ceiling` forfeits every seat on turn 0 and the wave dies two turns later at
  `step`, naming neither the variable nor the cause. The `vars` task exists to make that loud.
- **`model-loader` sets `max_retries: 0`.** A retried `/play` replays a turn.
- **Kalam's Orion must not share cluster state.** The `forbid`/`wave` concurrency key is local by
  virtue of local SQLite — one wave per replica, N replicas in parallel. Shared state makes it a
  fleet-wide singleton, which looks exactly like idle capacity.
- **The `kalam` role cannot write ratings**, and `check-sql.sh` fails if it can reach `rated_at`.
- **A workflow must never interpret `wave_state`**, an observation, an action, or a ref. That
  boundary is enforced by review, not by types; interpreting it is a second engine.
- **Orion facts that are invisible until they bite**: `storage_presign` returns a plain string;
  `http_call` always prefixes its connector's base URL onto `path` (hence `substr(url,
  length(blob_endpoint))`, exact only because the storage connector sets `force_path_style`);
  `http_call` parses replies as JSON unless given `response_format: "text"`, and an S3 `PUT` answers
  with an empty body; `{"merge": [A, B]}` concatenates two computed arrays but `{"merge": <one
  expression>}` does not flatten at all.
