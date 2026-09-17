# Design — the match

Kalam claims **one** `pending` match row and plays it: `observe`, one `model_infer` per live seat,
`step`, until the engine stops returning views, then finishes the row in place. It knows nothing
about ratings or the roster.

The row it claims, and every statement it runs, is
[soma/docs/schema.md](https://github.com/Tiny-Brains/soma/blob/main/docs/schema.md) §4. The engine is
a plugin — [ants](https://github.com/Tiny-Brains/ants). **The model is Orion's own**: a `models`
entity on this node, registered by this package's second clock, run under `tract` from an artifact
fetched out of the models bucket by digest. How many replicas run, and what bounds a draining match,
is [devops/docs/deployment.md](https://github.com/Tiny-Brains/devops/blob/main/docs/deployment.md).

The package is generated. [`scripts/gen-kalam.py`](../scripts/gen-kalam.py) holds the SQL and the
task graph readably and inlines both into `workflows/*.json` and `channels/*.json`. Those files are
the package; this page is why they are shaped as they are.

> **What this page replaced.** Until 14 September 2026 this was *Design — the wave*: one claim took
> up to `wave_k` rows and played them together, so that one call to a loader service could batch
> every live seat's inference. **Every Ants map is two-player**, so that call was amortising one
> batch over a number that is two. The wave, the batcher, the residency hold table, the claim's
> affinity ordering and the loader itself are deleted rather than ported (decision R7), and
> concurrency moved to *concurrent matches*: four channels, one row each. The git history of this
> file is where the old design lives.

## 1. What this page fixes

- the two clocks, their schedules, concurrency keys and timeouts
- the match run's task list, in order, with the condition each task carries
- the roster barrier — what a node does with a row naming a model it cannot serve
- the turn — observe, one inference per seat, step — and what carries between turns
- **the head decode**, which is the platform's and not the manifest's
- strikes, the forfeit, and how a fixed task list counts anything per seat at all
- renew: the interval, and what a partial renew means
- the replay envelope's fields
- drain on SIGTERM, below the orchestrator's grace period
- `[vars]`: capacity, timing and the two equalities that are correctness rather than tuning
- the roster clock: how a version verified by Soma's admit clock becomes playable on this node

---

## 2. The measurements the sizing rests on

| | Measured | Consequence |
|---|---|---|
| every map in the catalogue | **`"players": 2`**, all 24 | a match is two inferences a turn. The wave existed for a number that is two |
| a warm `model_infer` | **0.7 ms**; 6.8 ms on a cold session | residency is a cache, not a hold table. A cold load is 0.7% of a 1000 ms turn |
| a shipped adapter at 64×96 | **86,051 operations**; 229,415 at 128×128 | ≈14 a cell, 4.4× headroom under a 1,000,000 budget — and the CLI and Orion agree to the operation |
| the observation over a match's life | grows **65%** as the known-water mask fragments | size anything against a mature match, never a fresh one |
| the strike accumulator | 107 injected timeouts → **107 strikes, 8 forfeits** | §4.5 |

**Throughput is the one number nobody has.** It used to come from batching inside a call; it now
comes from concurrent runs, and what bounds it is Orion's per-sweep and per-task overhead at 1000
sweeps × N concurrent matches. On a laptop container a match takes 4–10 s and a replica plays four
at once. A fleet and a full queue is what measures it.

---

## 3. The two clocks

### 3.1 `tb-match-1` … `tb-match-4`

```jsonc
{ "channel_id": "tb-match-1", "channel_type": "async", "protocol": "cron",
  "workflow_id": "tb-match-run", "tags": ["pkg:kalam"],
  "transport_config": {
      "schedule": "*/5 * * * * *",
      "timezone": "UTC",
      "misfire_policy": "skip",                 // a missed poll is not worth catching up
      "concurrency": { "policy": "forbid", "key": "match-1" } },
  "config": { "timeout_ms": 2400000,            // above the longest match
              "tracing": { "errors_only": true, "task_details": true } } }
```

**Four channels, one workflow, four concurrency keys.** `forbid` is per key, so `match-1` … `match-4`
are four independent slots and a replica plays four matches at once. `match_concurrency` in `[vars]`
is what says four, and the generator emits that many channels — the number is in one place and the
channel files are build output.

- **The keys are local in effect.** Kalam's Orion runs on local SQLite with no `[cluster]` block, so
  a key is per replica: four matches per replica, N replicas in parallel. Put N replicas over one
  shared state database and each key becomes a fleet-wide singleton, which looks exactly like idle
  capacity. This is the opposite of Soma's clocks' use of the same mechanism and worth saying because the
  spelling is identical.
- **Nothing coordinates the four.** The claim does: `FOR UPDATE SKIP LOCKED` on one row, so four
  slots and N replicas take disjoint rows rather than queueing behind each other.
- **`misfire_policy: "skip"`.** A poll missed while a match was running has nothing to catch up: the
  queue is still there and the next tick claims from it.
- **`errors_only` tracing from day one.** A thousand-turn match is thousands of task executions in
  one occurrence, and tracing a clean one writes more trace than match.
- **A run that claims nothing ends at its fifth task.** At a 5-second poll on an empty queue that is
  the common case and it must be cheap.
- **The timeout is above the longest match.** Worst case is `max_turns × turn_ms` — 1000 × 1 s —
  plus the platform's own time: about 19 minutes. 40 minutes is the bound.

### 3.2 `tb-roster`

```jsonc
{ "channel_id": "tb-roster", "schedule": "*/15 * * * * *",
  "concurrency": { "policy": "forbid", "key": "roster" },
  "config": { "timeout_ms": 120000 } }
```

**A model is a per-node entity, and this clock is how the fleet agrees about one.** Each `kalam-N`
is its own Orion with its own state database, so a version Soma verified is not registered anywhere
else by virtue of having been verified. This clock reads `model_versions` — every row the ladder
says is `verified` or `active` — and registers, admits and activates each one *here*.

**No clock ever calls a replica**, and that is the decision rather than an accident of wiring
(R8). A fan-out from the admit clock would need a replica list somewhere, would have to retry a
replica that was restarting, and would have to be told when a replica joined. The database is
already the only channel between packages; a clock that reconciles from it needs none of that.

Six tasks: `roster` reads what should be playable, `more` terminates, `item` takes one and clears
the last, `have` asks this node whether it knows it, `register` registers it, `activate` activates
whatever passed admission. Every step is idempotent, so a replica that has been down catches up by
ticking.

**What is registered is not what was uploaded**, and the difference is two things. The manifest's
`name` becomes the platform's model id — an Orion label may not begin with a digit, so a bare uuid
is refused and the id is `tb.v<uuid>` (decision R9) — and a competitor's chosen name is not the
platform's to honour. And the document is rebuilt **field by field** in SQL with
`jsonb_build_object`, so a field naming somebody else's bucket key has nowhere to survive. The
stored text stays the competitor's exact bytes, because that is what the hash is over.

---

## 4. The run — `tb-match-run`

One workflow, `loop: { "counter": "i", "max": 1010 }`, 42 tasks. The whole task list runs once per
turn; turn-0 tasks are conditioned on the counter; a terminal task ends it.

**The loop `max` is `max_turns` plus a small tail**, not `max_turns`: the finish happens inside the
loop, on the sweep after the engine stops returning views. `[engine] max_loop_iterations` must sit
above it.

### 4.1 Turn 0 — configure, then claim

| Task | Function | What |
|---|---|---|
| `token` | `map` | this attempt's claim token, minted before the claim and carried in `data`, plus `opened_at` for the played duration |
| `vars` | `filter`, halt | every `[vars]` value resolves. A missing one is silent and catastrophic: `{">=": [1, null]}` is true here, so an unresolved ceiling forfeits every seat on turn 0 and the match dies two turns later at `step`, naming neither the variable nor the cause |
| `reap` | `db_write` | schema §4.1. Its own statement, not a CTE in the claim: a CTE's writes are invisible to the claim in the same snapshot, and a reaped row would wait one more poll. `rows_affected` is worth a metric — it counts crashes |
| `claim` | `db_write` | schema §4.2, with `$1` the engine digest from `[vars]`, `$2` the token, `$3` the lease, `$4` `MAX_SEATS` |
| `idle` | nothing claimed | terminal. This is where an idle replica's run ends |

**The claim takes one row, trials first, then oldest, `FOR UPDATE SKIP LOCKED`.** What went with the
wave: the resident-weights affinity ordering — an optimisation of a residency model that no longer
exists — and the preset grouping, which existed so one `observe` could serve a whole wave of one
board.

**`seat_count <= $4` is new and deliberate.** A seat is a task and the task list is fixed, so
`MAX_SEATS` (8) `model_infer` tasks are generated and the claim refuses a wider row rather than
playing it short a seat. The catalogue ships sixteen presets from two seats to eight, and eight is
exactly the ceiling mapgen enforces, so every preset the ladder pairs is one a replica can claim.
The seat count played is always the row's: every seat task is conditioned on `seat_count`.

### 4.2 The roster barrier

| Task | Condition | What |
|---|---|---|
| `row` | turn 0 | schema §4.3 by token: the row, its seats and their model ids |
| `open` | turn 0 | what the run rides on — the refs, the counters, the replay stream |
| `has0`…`has3` | the seat exists | `http_call` `GET /models/{id}` against **this node's own admin API** |
| `barrier` | turn 0 | how many seats this node cannot serve |
| `release` | any seat unserved | schema §4.4's release statement, a refusal spent |
| `unready` | any seat unserved | terminal |
| `start` | every seat servable | schema §4.4's start statement: `claimed` → `running` |
| `started` | turn 0 | `filter`, halt — the start wrote nothing, so the token is stale |

**The barrier replaced the residency barrier and answers a smaller question.** Not "is this model
held in memory here" — nothing holds models any more, the session cache loads on demand — but "does
this node know this model at all, and is it active?" A seat whose model is not `active` here
releases the row with a refusal spent, which the roster clock normally makes impossible: it is the
lag case, not the common one. A replica permanently behind fails rows as `MODEL_UNAVAILABLE` rather
than passing them round for ever, which is what `refusal_ceiling` is for.

> **Read through the admin API's envelope.** Every admin reply is wrapped in `{"data": …}`, so the
> barrier tests `temp_data.m0.data.status` and not `temp_data.m0.status`. Read the wrong one and
> `null != "active"` is TRUE — every match released, for ever, with both halves looking healthy. It
> cost an afternoon.

**The row is read after `start`, not before it.** `row` filters on `status = 'running'`, so a row
whose start wrote nothing — a stale claim token — is read as nothing rather than played by a replica
that no longer owns it.

### 4.3 The refs — where per-seat state lives

The refs are built once at turn 0 and rebuilt every turn after that. A ref is:

```jsonc
{ "m": 0, "seat": 1,
  "model": "tb.v…", "weights_hash": "sha256:…", "manifest_hash": "sha256:…",
  "strikes": 0, "forfeited": false, "strike_ceiling": 5,
  "infer_us_total": 78360, "infer_us_max": 1001, "infer_turns": 150 }
```

The three `infer_*` counters are the seat's **cost**, accumulated exactly as `strikes` is and for the
same reason — a fixed task list has nowhere else to keep a per-seat number across turns. Each turn
adds that seat's `stats_output.inference_ms`. **All three are seeded at `0` rather than left
absent**, because `{"+": [null, x]}` on the first write is the silent-null failure this document
keeps warning about. A forfeited seat is not inferred at all, so its counters freeze at the last
turn it was played — which is why `infer_turns` is carried instead of reusing `matches.turns` as a
divisor.

**Microseconds are floored where milliseconds become them.** `stats_output.inference_ms` is a float
and `match_seats.infer_us_total` is a `bigint`; `jsonb_to_recordset` refuses `10051.542`, and the
finish statement then writes nothing with no explanation. `acts` floors once, at the one place the
conversion happens.

They land in two places at finish: `match_seats` (three columns, granted to the Kalam role) and the
replay envelope's `seats`. `tinybrains conform` compares an explicit field allowlist that excludes
`seats`, so carrying a non-reproducible number in a replay cannot make a deterministic one fail to
conform.

`m` is 0 for the whole run now — there is one match — but it stays on every ref because the
cartridge's API is wave-shaped and **matching, not indexing, is what makes a ref safe**.

This is not decoration. It is the only way a fixed task list can hold anything per seat across
turns, and the reason is one measured fact: **element scope does not nest inside root scope.**
Inside a `map` or `filter` body, `{"var": "data.anything"}` is `null`. And inside a *nested*
iterator the enclosing element is not addressable at all — `{"val": [[1], …]}` reads the innermost
frame and every higher level resolves against the root — so every cross-product in this workflow is
carried in a reduce's accumulator instead. Verified, not assumed.

Two properties that are easy to get wrong, and were:

- **The list is flat and the engine matches on `(m, seat)`.**
- **Compare with `===`, or compare against something that resolves.** `{"==": [0, null]}` is
  **true** under this engine's loose equality, so a join written against a path that does not
  resolve does not fail and does not return nothing — it selects the falsy elements and looks
  perfect for exactly as long as the value it is compared against is zero.

### 4.4 The turn — and the head decode

| Task | Condition | What |
|---|---|---|
| `observe` | — | `tb.<game>.observe(wave_state, refs)`; every live seat's view |
| `turn` | — | this turn's views and the board they are on |
| `infer0`…`infer3` | the seat exists, is live, has not forfeited | `model_infer`, `raw: true`, `stats_output`, `timeout_ms = turn_ms`, `continue_on_error` |
| `act0`…`act3` | per seat | the head, decoded |
| `acts`, `moves` | — | the actions, the strikes, and the next turn's refs |
| `step` | — | `tb.<game>.step(wave_state, actions)` |
| `carry` | — | the new state and the replay stream |

**A seat is a task.** The task list is fixed, so `MAX_SEATS` inference tasks are generated and each
is conditioned three ways. Each carries `continue_on_error`, because a seat that fails is a strike
and not the end of the match.

**The head is decoded HERE, not in the manifest** (decision R3), and this is the one place the
platform reads a tensor. A `result` expression's root is built from the output tensors alone, so a
manifest cannot gather at the ants' cells — the indices are in the *input*, and the input is not
there. So the task asks for `raw: true` and the workflow does the gather, branching on the head's
rank:

```python
per_cell = {"transpose": [{"gather": [{"reshape": [policy, [5, cells]]}, idx, 1]}, [1, 0]]}
chosen   = {"argmax": [{"if": [{"==": [{"length": [{"shape": [policy]}]}, 4]},
                               per_cell, policy]}, 1]}
```

`[1, 5, H, W]` is reshaped to `[5, H*W]`, gathered at the ants' flat indices and transposed to
`[n, 5]`; `[n, 5]` is already in `mine` order and passes through. Then one `argmax` and one lookup
into `["N", "E", "S", "W", "-"]` — the action alphabet, which is the **cartridge's** and is published
in the competitor guide's *What your model answers*. A per-cell head's channel order is part of the
game's contract, not the competitor's.

> `{"val": […]}`'s path segments are **evaluated**, so `{"val": [[1], "data", "dirs", {"var": ""}]}`
> indexes an array by a computed index. That is how an index becomes a name here; there is no `at`
> operator, and the nested-`if` chain it replaces cost four comparisons per element.

`cli/src/model.rs` does the same decode in Rust, and `tinybrains conform` is what keeps the
two equal.

**`actions` takes the explicit `{m, seat, action}` form, not the positional one.** A forfeited seat
is not inferred at all, so a positional list would be shorter than the view list and every action
after the first forfeit would land in the wrong chair. Omission *is* the no-op: the engine plays it
for any seat it is given nothing for.

### 4.5 Strikes and the forfeit

- A seat whose inference produced an action gets it; its count is unchanged.
- A seat whose `model_infer` failed — a timeout, an adapter over budget, a run failure — gets the
  **no-op** and takes a **strike**. `timeout_ms` is the task's, so Kalam does no timing of its own.
- **`strike_ceiling` strikes forfeit the seat**, and the ceiling is **read off the match row**
  (decision 54), not from `[vars]`. Pair stamps `matches.strike_ceiling` from the season, so a trial
  is judged by the rule it was played under even if the deploy's number moved in between. It rides
  in the ref, because a var read inside a map body is null.
- Counted **cumulatively, not consecutively**: five missed clocks in a match is five missed clocks,
  and a model that misses every fourth turn is not better behaved than one that misses five in a
  row. It is the stricter reading and the one a competitor cannot game.
- **A forfeited seat plays the no-op until the engine ends the match** — no resign value. Its
  `forfeited` flag rides in the ref, so the following turns cost nothing. That flag must be carried
  by hand.
- **At finish, forfeited seats rank last** — `engine_rank + seat_count`. Kalam overrides the
  engine's ranks and keeps the engine's own in the replay envelope, so a timed-out model cannot win
  on points and an audit can still see what the game thought happened.

**Orion's fault categories decide release from strike.** `unavailable`, `permit`, `timeout` and
`runtime_unavailable` are the platform's and release the row with no attempt spent;
`caller_input`, `adapter`, `input_size`, `output_size` and `run` are the model's and strike the
seat. That is the same two-class split the old loader's `fault` field carried, in the engine's own
vocabulary.

### 4.6 Renew, and the finish

| Task | Condition | What |
|---|---|---|
| `renew` | live and `i % renew_every_n_turns == 0` and `i > 0` | schema §4.5, one statement on the token |
| `lost` | the renew fell short | terminal |
| `results` | the engine is done | `tb.<game>.finish` — ranks, scores, an end reason |
| `pick` | done | the result, one element per seat |
| `presign` | done | `storage_presign` `PUT`, key naming the match and the token |
| `put` | done | `http_call` `PUT` — the replay envelope |
| `finish` | done | schema §4.6, one statement on the token |
| `counted` | done | `filter`, halt — the finish wrote nothing, so the token is stale |

**A partial renew halts.** A stale attempt's finish updates nothing either way (schema §4.6), so the
cost of halting is one match replayed and the cost of continuing is a match played for nobody.

**The drain is gone with the wave.** There is one match, so there is no queue of ended matches to
empty and no tail to sweep: the run finishes its row on the sweep after the engine stops returning
views, and ends.

### 4.7 The replay envelope

Written per attempt under a key naming the token, so a stale attempt's blob is an orphan rather than
a replacement for the one that counted.

```jsonc
{ "match_id": "…", "attempt_token": "…", "seed": …, "preset": "open-2",
  "map_id": "…", "map": { … },       // the board, so a replay outlives a preset re-tuning
  "seats": [ … ],                    // who sat where, by hash
  "max_turns": 1000,
  "engine_digest": "sha256:…", "orion_version": "1.8.1",
  "engine_ranks": [1, 2],            // the engine's own, before forfeits are applied
  "scores": [ … ], "reason": "…", "turns": 743,
  "deltas": [ { "t": 1, "a": ["NNE-", "S-W"] }, … ] }
```

**`orion_version` replaced `evaluator_digest` and `dialect_version`.** They named a build of a
service and a version of a dialect that no longer exist; what actually prices an adapter now is
datalogic, at the version the node links, and the Orion version is what names it. A sweep is per
Orion upgrade (R10).

The action stream, not frames; `replay-decode` re-simulates it. **A replay is self-sufficient or it
is not viewable**, which is why the board and the turn limit are in it and not merely the seed.
`tinybrains conform` rebuilds the match from this envelope alone and diffs every field and every
turn against it — which is what keeps the local runner and this workflow telling the same story
about the same seeds.

Two Orion facts the `PUT` rests on: `storage_presign` returns a **plain string**, and `http_call`
always prefixes its connector's base URL onto `path` — so the presigned URL is reduced to
`substr(url, length(blob_endpoint))`. `force_path_style` on the storage connector is what makes that
subtraction exact. An S3 `PUT` answers with an empty body, so the call is `response_format: "text"`.

### 4.8 The end

`over` fires when the row is finished. **Nothing else is written.** No rating is read, no rating is
written, no other table is touched, and the `kalam` role cannot reach one (schema §3.8, exercised in
[soma/scripts/verify/run.sh](https://github.com/Tiny-Brains/soma/blob/main/scripts/verify/run.sh)).

---

## 5. Drain on SIGTERM

1. The orchestrator sends `SIGTERM`; the entrypoint forwards it to `orion-server`.
2. Orion stops claiming new cron occurrences and lets the runs in hand finish.
3. Each match finishes its row and exits.

Three things make that work, and each was measured failing first:

- **The entrypoint must wait in a loop.** `trap 'kill -TERM $PID'; wait $PID` is the obvious shape
  and it is wrong: the trap interrupts `wait`, `wait` returns, the script falls off its end, and the
  container exits *while Orion is still draining*. Measured: `docker stop -t 300` returned in **zero
  seconds** with rows still `running`. It looks like a clean shutdown from the outside.
- **`server.shutdown_force_timeout_secs` is the outer bound**, not `cron.shutdown_timeout_secs`. The
  cron worker is a supervised task drained under the force timeout, so the cron key can only ever
  shorten a match. Both must exceed the longest match. `server.shutdown_drain_secs` is an
  unconditional sleep for a load balancer Kalam does not sit behind, and belongs **short**. The four
  numbers are
  [devops/docs/deployment.md](https://github.com/Tiny-Brains/devops/blob/main/docs/deployment.md) §6.2.
- **The orchestrator's grace period must exceed all three**, which is deployment's.

The failure mode of getting any of this wrong is the same in every case: the rows stay `running`
with a live lease, nothing reports an error, and they are unplayable until the lease lapses and the
next claim reaps them. **A drain that half works is indistinguishable from one that works** until
you look at the table.

A replica killed rather than drained loses at most four matches: their rows lapse, the next claim
reaps them, and they are played again from turn 0. The third lapse fails a row, and with drain
working, lapses come only from crashes. **The blast radius of a replica death fell from `wave_k`
matches to four**, which is the one thing the rewrite bought for free.

---

## 6. The numbers

In Kalam's own instance config
([devops/compose/orion/kalam.toml.tmpl](https://github.com/Tiny-Brains/devops/blob/main/compose/orion/kalam.toml.tmpl)),
read as `metadata.vars.*`. The `vars` task halts the run if any of them is missing.

| Var | Value | What moves it |
|---|---|---|
| `match_concurrency` | 4 | how many matches this replica plays at once, and how many channels the generator emits. Bounded by `cron.max_concurrent_runs` |
| `renew_every_n_turns` | 30 | how often a match renews its lease |
| `lease_seconds` | 300 | must exceed the renew interval by a comfortable margin |
| `turn_ms`, `max_turns` | 1000, 1000 | the cartridge's contract; they must match the registered game |
| `refusal_ceiling` | 5 | how often a row may be refused for an unserved model before it fails. Refusals are counted apart from lapses |
| `engine_digest` | the component's hash | **must equal `games.active_engine_digest`** or the clock claims nothing, for ever. The entrypoint derives it from the wasm |
| `model_prefix` | `tb.v` | **must equal Soma's and the CLI's.** A replica registers `tb.v<uuid>` and a match row names one; a mismatch is a barrier that never passes |
| `orion_version` | 1.8.1 | **must equal the soma node's.** A match recorded against one Orion and admitted against another is exactly what the re-validation sweep looks for |
| `replay_prefix`, `blob_endpoint` | `replays`, `$R2_ENDPOINT` | the attempt key, and the prefix subtracted from the presigned URL |
| `poll_secs` | 5 | one claim is 0.68 ms against the real table |

**`strike_ceiling` is deliberately NOT here.** It is pinned onto `matches.strike_ceiling` by pair
and read off the row (decision 54), so a trial is judged by the rule it was played under.
`devops/scripts/check/configs.sh` asserts that this config does **not** set it — a second copy is
what a future edit would wire back in.

`engine.ops_budget` is not a `[vars]` value either: it is an `[engine]` setting, and it **must equal
the game's `adapter_ops_max`**, because a manifest admitted under one ceiling and struck under
another is a competitor forfeiting for a rule nobody published. `configs.sh` asserts that too.

---

## 7. What Kalam must not do

Stated as a list because every one of them is a boundary something else depends on.

1. **It never joins the roster to decide who plays.** The claim reads status, engine digest and
   lease; not `model_versions`, not `ratings`. A row is played because it is `pending` on this
   engine. The roster clock reads `model_versions`, and it decides nothing — it reconciles.
2. **It never reads or writes a rating.** Counting is Soma's count clock's, in finish order, under a fence.
3. **It touches `matches` and `match_seats` and nothing else**, and its role cannot reach anything
   else. The one exception is the roster clock's column-level `SELECT` on `model_versions`, which is
   granted per column and cannot see a verdict, a reason or a rating.
4. **It does no timing of its own.** `timeout_ms` on the task is the bound; Kalam reads the error.
5. **It never interprets `wave_state`,** an observation, or a ref. The head is the one tensor it
   reads, and it reads it because a manifest cannot (R3).
6. **It never retries an inference.** A retried turn is a turn played twice.
7. **It never calls another package.** The only HTTP calls it makes are to **its own node's** admin
   API and to the object store.

---

## 8. What the match asks of its neighbours

All folded in; kept because breaking one of them breaks the match silently.

| Of | Ask |
|---|---|
| schema §4.2 | a claim that takes one row, trials first, and refuses a row wider than `MAX_SEATS` |
| schema §4.3 | the row, its seats and their version ids, by token, filtered on `running` |
| schema §4.4 | a release statement that counts refusals apart from lapses |
| soma's grants | column-level `SELECT (id, status, manifest, artifact_key, weights_hash, created_at)` on `model_versions`, for the roster clock and nothing more |
| Orion | `model_infer` with `raw: true`, `stats_output`, and a templatable `timeout_ms` |
| Orion | an admin API on this node, reachable with the whole `Authorization` header in one variable |
| the cartridge | `replay-decode`, not `replay_decode` — Orion refuses a function label with an underscore |
| the cartridge | `observe(wave_state, refs)`, refs flat and echoed |
| the cartridge | `step` accepts the explicit `{m, seat, action}` form, and plays the no-op for an absent seat |
| the cartridge | the finish result carries `map_id` and `map`, so a replay is self-sufficient |
| the cartridge | the action alphabet and its channel order, published in the competitor guide's *What your model answers* |

---

## 9. Decisions taken here

Decisions **19** (the renew interval and the lease), **54** (the strike ceiling on the match row),
and from the 1.8.1 rebuild **R3** (the platform reads the head), **R7** (one match per claim) and
**R8** (a replica registers its own roster) — with the unnumbered calls the match forced: a partial
renew halts, strikes count cumulatively, per-seat state rides in the ref, and a forfeited seat is
not inferred at all. Each is recorded with its reasoning in
[devops/docs/decisions.md](https://github.com/Tiny-Brains/devops/blob/main/docs/decisions.md).

---

## 10. What has run

The loop, the roster barrier, the turn, the head decode, the strike accumulator and the forfeit, and
the finish have all run against real rows in a real replica, driving the real `tb.ants` against
Orion's own `models` entity doing real ONNX inference under tract. Three baselines were registered,
admitted and activated by the roster clock, and matches finished `lone_survivor` and
`rank_stabilized` with zero strikes, ratings folded by count and replays in the object store.
`tinybrains conform` on a replay written by this path reports **IDENTICAL — every field, and all
150 turns of the action stream**.

**Never exercised**: a four-seat row, because every map the catalogue ships is two-player; and
mixed-engine rollout, where two replicas on different digests drain and claim past each other.
`orion-server lint` and `readyz` prove neither, and must not be reported as a working match loop.

---

## 11. Open questions

1. **Is a cumulative strike count right?** Five scattered misses over a thousand turns is a model
   that works and occasionally hiccups; five in a row is a model that has stopped. The stricter
   reading is chosen because it is the one a competitor cannot game, but it will forfeit seats that
   a consecutive count would not, and nobody has seen the distribution yet.
2. **`match_concurrency` at 4 is a guess, and the measurement is missing.** What bounds a replica is
   no longer weight memory — the session cache is bounded by `models.max_loaded_bytes` and loads on
   demand — but Orion's own per-sweep overhead at N concurrent thousand-sweep loops. A fleet and a
   full queue is what answers it.
3. **Every generation recompiles every active model's adapters**, and each replica carries the whole
   roster because each is its own Orion. Invisible at ten models; the number at which it is not is
   unknown. Upstream asked for the measurement before the branch: time a package reload against a
   staged roster of 10, 100 and 1,000 admitted models. It is a deploy-cadence cost and never a
   per-match one.
4. **Nothing here bounds how long a single turn may take end to end.** `timeout_ms` bounds the
   inference; the channel timeout bounds the run; between them a pathological engine could sit
   inside its own plugin ceiling for a long time on every turn. The plugin's `max_timeout_ms` is the
   place to say so, and the cartridge should set it.
