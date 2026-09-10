# Design — the wave

Kalam claims up to `wave_k` `pending` match rows in one statement and plays them as a **wave**: one
engine call and one loader call per turn serve every live match at once, and each row is finished
with its result and replay key the moment its own match ends. It knows nothing about ratings or the
roster.

The row it claims, and every statement it runs, is
[soma/docs/schema.md](https://github.com/Tiny-Brains/soma/blob/main/docs/schema.md) §4. The engine is
a plugin — [ants](https://github.com/Tiny-Brains/ants). The loader it calls each turn is
[axon](https://github.com/Tiny-Brains/axon). How many replicas run, and what bounds a draining wave,
is [devops/docs/deployment.md](https://github.com/Tiny-Brains/devops/blob/main/docs/deployment.md).

The package is generated. [`scripts/gen-kalam.py`](../scripts/gen-kalam.py) holds the SQL and the
task graph readably and inlines both into `workflows/tb-wave-run.json` and `channels/tb-wave.json`.
Those two files are the package; this page is why they are shaped as they are.

## 1. What this page fixes

- the claim channel: schedule, concurrency, timeout, tracing
- the run's task list, in order, with the condition each task carries
- the residency barrier, and which of schema §4.4's three statements each refusal takes
- the turn — observe, one play call, step — and what carries between turns
- strikes, the forfeit, and how a fixed task list counts anything per seat at all
- renew: the interval, and what a partial renew means
- the finish drain, one match per sweep, with a tail drain
- the replay envelope's fields, and how one match's deltas are pulled from the wave's stream
- drain on SIGTERM, below the orchestrator's grace period
- `[vars]`: capacity, timing and the two equalities that are correctness rather than tuning

---

## 2. The measurements the sizing rests on

From the wave-turn spike that preceded the build, and unchanged by it.

| | Measured | Consequence |
|---|---|---|
| `wave_state` at K=16, largest preset | 141.5 KiB, **14% of the 1 MiB ceiling** | the ceiling does not bound K. It starts to at about K=64 |
| a wave-turn, loader at zero latency | **~3.0 ms per match-turn** early, ~4.6 ms late; linear in K | Orion's task machinery is not what a turn costs. K is bounded by inference, and by blast radius |
| a full 1000-turn wave, K=16 | 73 s of engine and orchestration | the channel timeout is choosing between minutes and an hour |
| the observation over a match's life | grows **65%** as the known-water mask fragments | size a wave against a mature match, never a fresh one |
| the strike accumulator | 107 injected timeouts → **107 strikes, 8 forfeits** | §4.5 |

---

## 3. The claim channel — `tb-wave`

```jsonc
{ "channel_id": "tb-wave", "channel_type": "async", "protocol": "cron",
  "workflow_id": "tb-wave-run", "tags": ["pkg:kalam"],
  "transport_config": {
      "schedule": "*/5 * * * * *",
      "timezone": "UTC",
      "misfire_policy": "skip",                 // a missed poll is not worth catching up
      "concurrency": { "policy": "forbid", "key": "wave" } },
  "config": { "timeout_ms": 2400000,            // above the longest match
              "tracing": { "errors_only": true, "task_details": true } } }
```

- **`forbid` on key `wave`, and the key is local in effect.** Kalam's Orion runs on local SQLite
  with no shared state, so the lock is per replica, which is exactly what is wanted: one wave in
  flight per replica, N replicas in flight in parallel. This is the opposite of Jodi's use of the
  same mechanism, and worth saying because the spelling is identical.
- **`misfire_policy: "skip"`.** A poll missed while a wave was running has nothing to catch up: the
  queue is still there and the next tick claims from it. `catch_up` would fire a burst of claims at
  once for no gain.
- **`errors_only` tracing from day one.** A thousand-turn wave is thousands of task executions in
  one occurrence, and tracing a clean one writes more trace than match.
- **A run that claims nothing ends at its sixth task**, before the loop's first sweep does any work.
  At a 5-second poll on an empty queue that is the common case and it must be cheap.
- **The timeout is above the longest match.** Worst case is `max_turns × turn_ms` — 1000 × 1 s —
  plus the platform's own time and the drain's tail: about 19 minutes. 40 minutes is the bound.

---

## 4. The run — `tb-wave-run`

One workflow, `loop: { "counter": "i", "max": 1100 }`, 36 tasks. The whole task list runs once per
turn; turn-0 tasks are conditioned on the counter; a terminal task ends it.

**The loop `max` is `max_turns` plus K, not `max_turns`.** The drain finishes one match per sweep
(§4.6), so a wave whose matches all end on the last turn needs K more sweeps to empty its queue.
`[engine] max_loop_iterations` must sit above it.

### 4.1 Turn 0 — configure, then claim

| Task | Function | What |
|---|---|---|
| `token` | `map` | one token for the whole wave, minted before the claim and carried in `data`, plus `opened_at` for the played duration |
| `vars` | `filter`, halt | every `[vars]` value resolves. A missing one is silent and catastrophic: `{">=": [1, null]}` is true here, so an unresolved `strike_ceiling` forfeits every seat on turn 0 and the wave dies two turns later at `step`, naming neither the variable nor the cause |
| `resident` | `http_call` `GET /resident` | Axon §3.4. Advisory, and allowed to be stale — affinity is an optimisation of the fill, and the barrier is where correctness lives. `loading` is **not** passed on, so a wave is not filled with rows whose models are still cold |
| `reap` | `db_write` | schema §4.1. Its own statement, not a CTE in the claim: a CTE's writes are invisible to the claim in the same snapshot. `rows_affected` is worth a metric — it counts crashes |
| `claim` | `db_write` | schema §4.2, with `$1` the engine digest from `[vars]`, `$2` the resident weights, `$3` K, `$4` the token, `$5` the lease |
| `claimed` | `filter`, halt | `rows_affected > 0`. This is where an idle replica's run ends |

### 4.2 The residency barrier

| Task | Condition | What |
|---|---|---|
| `models` | turn 0 | the wave's distinct `(weights_hash, adapter_hash)` pairs, `DISTINCT` in SQL because the reply is already the body `/load` wants |
| `hold` | turn 0 | `http_call` `POST /load` — one call for the whole wave, Axon §3.1 |
| `split` | turn 0 | partition the reply by `fault` and by `state` |
| `release` | any memory refusal | schema §4.4's release statement, for `fault: "loader"` and for `loading` |
| `fail` | any named refusal | schema §4.4's fail statement, for `fault: "model"` |
| `start` | turn 0 | schema §4.4's start statement: everything still `claimed` |
| `idle-unload`, `idle` | nothing started | release the held models, then a terminal task |

**The split reads `fault`, not the reason word** — Axon §6. A `loader` fault returns the row to
`pending` with its refusal count raised and **no attempt spent**; a `model` fault fails it at once
with the seat. That indirection is the point: a reason word added to the loader later costs no
change here.

**The barrier's unit is a model, not a row.** The loader answers about `(weights_hash,
adapter_hash)` pairs, and mapping a refused model back to the rows that seat it is a join from
element scope into root scope — the one thing JSONLogic here cannot do. So both statements take the
refused **hashes** and let Postgres do the join, which also makes the named-fault statement
set-valued: a barrier can refuse several models at once, and `DISTINCT ON` picks the lowest
offending seat when a row has more than one.

```sql
UPDATE matches m
   SET status = 'failed', fault_reason = x.reason, fault_seat = x.seat,
       closed_at = now(), lease_expires_at = NULL
  FROM (SELECT DISTINCT ON (s.match_id) s.match_id, s.seat, v.reason
          FROM jsonb_to_recordset(($2)::jsonb) AS v (weights_hash text, reason text)
          JOIN match_seats s ON s.weights_hash = v.weights_hash
         ORDER BY s.match_id, s.seat) AS x
 WHERE m.id = x.match_id AND m.claim_token = ($1)::uuid AND m.status = 'claimed'
```

**Nothing started means unload and end, not halt.** A `filter` halt cannot release the models, so
every exit from a wave is an `/unload` followed by a terminal task.

**The empty wave exits before `start` can lie.** `start` returning zero rows is the signal, because
the two refusal statements have already moved everything they refused.

### 4.3 Opening the wave — and why it is read twice

| Task | What |
|---|---|
| `wave` | schema §4.3, by token, **numbered**, filtered on `status = 'running'` |
| `open` | rows, the flat seat list, the refs, and the counters the run carries |
| `world` | `tb.<game>.worldgen` from the started rows' seeds and the wave's preset |
| `init` | the opening `wave_state` |

The models are read **before** the barrier, for the hold. The wave is read **after** `start`, and
that is not a convenience. The barrier drops rows, so a numbering taken at claim time and a
numbering taken after the barrier disagree the moment anything is refused: the engine indexes its
matches `0..n_started-1` while the refs still carry `0..n_claimed-1`, `observe` — which matches refs
on `(m, seat)` — attaches no ref to any view, and the wave plays a thousand turns naming a model
that does not exist. Nothing errors.

**JSONLogic cannot number a list**, so Postgres does it, and repeats `m` onto every seat:

```sql
    …, (row_number() OVER (ORDER BY m.id) - 1)::int AS m, …
```

A seat arrives as `{"m": 3, "seat": 1, "model_id": …, "weights_hash": …, "adapter_hash": …}`, and
the read returns the seats as one flat list beside the rows.

### 4.4 The refs — where per-seat state lives

The wave's models and its **refs** are built once, at turn 0, and the refs are rebuilt every turn
after that. A ref is:

```jsonc
{ "m": 3, "seat": 1,
  "weights_hash": "sha256:…", "adapter_hash": "sha256:…",
  "strikes": 0, "forfeited": false,
  "infer_us_total": 78360, "infer_us_max": 1001, "infer_turns": 150 }
```

The three `infer_*` counters are the seat's **cost**, accumulated exactly as `strikes` is and for the
same reason — a fixed task list has nowhere else to keep a per-seat number across turns. Each turn
adds the loader's `infer_us` for that row: its share of its own group's inference, *not* its
`elapsed_ms`, which runs from a row entering the call to leaving it and so reports roughly the whole
call for every row alike. **All three are seeded at `0` rather than left absent**, because
`{"+": [null, x]}` on the first write is the silent-null failure this document keeps warning about.
A forfeited seat is absent from the next turn's play call, so its counters freeze at the last turn it
was played — which is why `infer_turns` is carried instead of reusing `matches.turns` as a divisor.

They land in two places at finish: `match_seats` (three columns, granted to the Kalam role) and the
replay envelope's `seats`, which is `hrefs.items` and therefore gets them for nothing. `tinybrains
conform` compares an explicit field allowlist that excludes `seats`, so carrying a
non-reproducible number in a replay cannot make a deterministic one fail to conform.

It is passed to `observe`, echoed onto every view, copied onto the play row, echoed back by the
loader on the reply, and rebuilt from that reply into the next turn's refs. **The engine and the
loader both carry it and neither looks inside it.**

This is not decoration. It is the only way a fixed task list can hold anything per seat across
turns, and the reason is one measured fact: **element scope does not nest inside root scope.**
Inside a `map` or `filter` body, `{"var": "data.anything"}` is `null` — as is `{"val": [...]}`,
computed segments or not. So the workflow can never join "this row timed out", which is positional
in the loader's reply, to "this is the seat it belongs to", which is positional in the engine's
views. The counter has to travel with the seat it counts.

Two properties that are easy to get wrong, and were:

- **The list is flat and the engine matches on `(m, seat)`.** A nested `refs[m][seat]` is rebuilt
  each turn from the play reply, a rebuild only ever sees the **live** seats, and so the moment one
  match in the wave ends every later index shifts and the wave seats one competitor's model in
  another's chair. Silently.
- **Compare with `===`, or compare against something that resolves.** `{"==": [0, null]}` is
  **true** under this engine's loose equality, so a join written against a path that does not
  resolve does not fail and does not return nothing — it selects the falsy elements and looks
  perfect for exactly as long as the value it is compared against is zero.

### 4.5 The turn, the strikes, and the forfeit

| Task | Condition | What |
|---|---|---|
| `observe` | — | `tb.<game>.observe(wave_state, refs)`; every live seat of every live match |
| `counts` | — | `n_live`, `n_pending`, and the seats actually being played |
| `play` | live | `http_call` `POST /play`: one row per view, `deadline_ms = turn_ms`, `budget_ops` from the game's manifest, `ref` echoed |
| `acts` | live | the actions, the strikes, and the next turn's refs |
| `step` | live | `tb.<game>.step(wave_state, actions)` |
| `carry` | live | the new state, the ended matches appended to `pending`, the replay deltas appended, the ending matches' refs snapshotted |

**Three calls a turn for the whole wave**, whatever K is: one plugin call out, one loopback HTTP
call, one plugin call back. About 3 ms per match-turn.

**`actions` takes the explicit `{m, seat, action}` form, not the positional one.** Positional
alignment with the last `observe` is correct only if every live seat is played, and a forfeited seat
is not sent to the loader at all — so the reply is shorter than the view list and every action after
the first forfeit lands in the wrong chair. The engine accepts both forms; the explicit one is
immune, and it is the one an audit can read. Omission *is* the no-op: the engine plays it for any
seat it is given nothing for, so a forfeited seat needs no empty row.

One walk over the play reply produces all three of the actions, the strike counts and the next
turn's refs — because **the reply is the only place where a row's error and the identity of its seat
are in the same object**.

- A row with an `action` gets its action; the seat's count is unchanged.
- A row with an `error` — `TIMED_OUT`, `ADAPTER_FAILED`, `INFERENCE_FAILED`, `NOT_RESIDENT` — gets
  the **no-op** and the seat takes a **strike**. The loader has already held the seat to `turn_ms`
  (Axon §5), so Kalam does no timing of its own. `/play`'s row reply has no `fault` field, so every
  per-row error is a strike; there is no mid-play fault to attribute separately.
- **`strike_ceiling` strikes forfeit the seat.** Counted **cumulatively, not consecutively**: five
  missed clocks in a match is five missed clocks, and a model that misses every fourth turn is not
  better behaved than one that misses five in a row. It is the stricter reading and the one a
  competitor cannot game.
- **A forfeited seat plays the no-op until the engine ends the match** — no resign value. Its
  `forfeited` flag rides in the ref, so the following turns cost the loader nothing. That flag must
  be **carried by hand**: a seat that is not sent does not appear in the reply, so rebuilding the
  refs from the reply alone loses it and the seat is sent again next turn.
- **At finish, forfeited seats rank last.** Kalam overrides the engine's ranks and keeps the
  engine's own in the replay envelope, so a timed-out model cannot win on points and an audit can
  still see what the game thought happened.

**Strikes outlive the match they were earned in.** The drain finishes one match per sweep, and by
the second sweep that match's seats are gone from the refs — `observe` returns nothing for a
finished match. So `carry` snapshots the ending matches' refs into `data.done_refs` on the turn they
end, which is the only turn they still exist.

### 4.6 Renew, and the finish drain

| Task | Condition | What |
|---|---|---|
| `renew` | live and `i % renew_every_n_turns == 0` and `i > 0` | schema §4.5, one statement on the token |
| `lost-unload`, `lost` | the renew fell short | release the models, then a terminal task |
| `results` | pending | `tb.<game>.finish` — safe while matches are still running: the engine reports `done` per match and the drain reads only the head's entry |
| `pick` | pending | the head of the queue, its row, its result, its seats and its deltas |
| `presign` | pending | `storage_presign` `PUT`, key naming the match and the token |
| `put` | pending | `http_call` `PUT` — the replay envelope |
| `finish` | pending | schema §4.6, one statement on the token |
| `counted` | pending | `filter`, halt — the finish wrote nothing, so the token is stale |
| `pop` | pending | `pending = slice(pending, 1)`; the finished match's deltas dropped |

**A partial renew halts.** The expectation is the rows this wave still has `running` — claimed,
minus finished, minus failed, which the workflow knows exactly. All of a wave's rows carry one lease
and expire together, so a shortfall cannot happen in the ordinary course; if it does, this replica's
grip on the wave is not what it believes, and the safe move is the same as for zero rows. A stale
attempt's finish updates nothing either way (schema §4.6), so the cost of halting is one wave
replayed and the cost of continuing is a match played for nobody.

**The drain finishes one ended match per sweep, from a queue, with a tail drain** after the last
match ends: the terminal condition is "nothing live **and** nothing pending", so the loop keeps
sweeping — cheaply, since `observe` returns no views — until the queue is empty. The alternative was
K conditioned presign-`PUT`-finish triples in the task list: 3K condition evaluations every turn for
a thing that fires once per match, against seven tasks whatever K is. The delay a drain adds is a
few turns, invisible against count's ten-second tick.

**Pulling one match's deltas out of the wave's stream** is the one genuinely awkward expression in
the workflow, and it is awkward for the scope reason in §4.4 — the head match's index is a root
value and the filter runs in element scope. `reduce`'s initial accumulator is the only expression
evaluated at root that threads into the body, so it is the escape hatch, and `gen-kalam.py`'s `sift`
is the one place it is written:

```jsonc
{"reduce": [ {"var": "data.deltas"},
             {"if": [{"===": [{"var": "current.m"}, {"var": "accumulator.head"}]},
                     {"head": {"var": "accumulator.head"},
                      "items": {"merge": [{"var": "accumulator.items"}, [{"var": "current"}]]}},
                     {"var": "accumulator"}]},
             {"head": {"var": "temp_data.head"}, "items": []} ]}
```

Unwrap the result's `.items`: reducing over the accumulator **object** instead iterates its keys and
produces rows of nulls, which the finish statement then correctly refuses because they name no seat.
The complement — the same reduce with the arms swapped — is what `pop` keeps, so the wave's delta
stream shrinks as matches finish and never carries a finished match's history to the end of the run.

### 4.7 The replay envelope

Written per attempt under a key naming the token, so a stale attempt's blob is an orphan rather than
a replacement for the one that counted.

```jsonc
{ "match_id": "…", "attempt_token": "…", "seed": …, "preset": "standard",
  "map_id": "…", "map": { … },       // the board, so a replay outlives a preset re-tuning
  "seats": [ … ],                    // who sat where, by hash
  "max_turns": 1000,
  "engine_digest": "sha256:…", "evaluator_digest": "sha256:…", "dialect_version": "…",
  "engine_ranks": [1, 2],            // the engine's own, before forfeits are applied
  "scores": [ … ], "reason": "…", "turns": 743,
  "deltas": [ { "t": 1, "a": ["NNE-", "S-W"] }, … ] }
```

The action stream, not frames; `replay-decode` re-simulates it. **A replay is self-sufficient or it
is not viewable**, which is why the board and the turn limit are in it and not merely the seed: the
decoder rebuilds the match from `map`, so a replay stays viewable when the preset table has been
re-tuned or the catalogue has moved on. The seats are hashes, not identities, but enough that
`tinybrains conform` can rebuild the match from this envelope alone and diff the result against it
— which is what keeps the local runner and this workflow telling the same story about the same
seeds.

Two Orion facts the `PUT` rests on: `storage_presign` returns a **plain string**, and `http_call`
always prefixes its connector's base URL onto `path` — so the presigned URL is reduced to
`substr(url, length(blob_endpoint))`. `force_path_style` on the storage connector is what makes that
subtraction exact: the URL is `endpoint/bucket/key`, so the prefix is precisely the endpoint.
Without it the URL is virtual-hosted (`bucket.host/key`) and the prefix is a different string from
anything either side is configured with. An S3 `PUT` answers with an empty body, so the call is
`response_format: "text"`.

### 4.8 The end

`over` fires on nothing live and nothing pending: `POST /unload` for the wave's models, then a
terminal task. **Nothing else is written.** No rating is read, no rating is written, no other table
is touched, and the `kalam` role cannot reach one (schema §3.8, exercised in
[soma/scripts/verify/run.sh](https://github.com/Tiny-Brains/soma/blob/main/scripts/verify/run.sh)).

---

## 5. Drain on SIGTERM

1. The orchestrator sends `SIGTERM`; the entrypoint forwards it to `orion-server`.
2. Orion stops claiming new cron occurrences and lets the run in hand finish.
3. The wave finishes its matches, drains its queue, releases its models and exits.

Three things make that work, and each was measured failing first:

- **The entrypoint must wait in a loop.** `trap 'kill -TERM $PID'; wait $PID` is the obvious shape
  and it is wrong: the trap interrupts `wait`, `wait` returns, the script falls off its end, and the
  container exits *while Orion is still draining*. Measured: `docker stop -t 300` returned in **zero
  seconds** with two rows still `running`. It looks like a clean shutdown from the outside, and it
  is a lost wave.
- **`server.shutdown_force_timeout_secs` is the outer bound**, not
  `cron.shutdown_timeout_secs`. The cron worker is a supervised task drained under the force
  timeout, so the cron key can only ever shorten the wave. Both must exceed the longest match.
  `server.shutdown_drain_secs` is an unconditional sleep for a load balancer Kalam does not sit
  behind, and belongs **short**. The four numbers are
  [devops/docs/deployment.md](https://github.com/Tiny-Brains/devops/blob/main/docs/deployment.md) §6.2.
- **The orchestrator's grace period must exceed all three**, which is deployment's.

The failure mode of getting any of this wrong is the same in every case and is worth naming: the
rows stay `running` with a live lease, nothing reports an error, and they are unplayable until the
lease lapses and the next claim reaps them. A drain that half works is indistinguishable from one
that works until you look at the table.

A replica killed rather than drained loses its wave and nothing else: its rows lapse, the next claim
reaps them, and they are played again from turn 0. The third lapse fails a row, and with drain
working, lapses come only from crashes.

---

## 6. The numbers

In Kalam's own instance config
([devops/compose/orion/kalam.toml.tmpl](https://github.com/Tiny-Brains/devops/blob/main/compose/orion/kalam.toml.tmpl)),
read as `metadata.vars.*`. The `vars` task halts the run if any of them is missing.

| Var | Value | What moves it |
|---|---|---|
| `wave_k` | 16 | **a residency budget**, not a throughput guess: `K × seats × max_class_bytes ≤ the replica's weight memory`. Conservative on purpose — a replica death replays K matches from turn 0 |
| `renew_every_n_turns` | 30 | how often the wave renews its lease |
| `lease_seconds` | 300 | must exceed the renew interval by a comfortable margin |
| `turn_ms`, `max_turns`, `budget_ops` | 1000, 1000, 1000000 | the cartridge's contract; they must match the registered game |
| `strike_ceiling` | 5 | **must equal Jodi's `forfeit_strikes`** — Kalam applies it, count reads its consequences off the row. `devops/scripts/check/configs.sh` asserts the equality |
| `refusal_ceiling` | 5 | how often a row may be refused for memory before it fails. Refusals are counted apart from lapses |
| `engine_digest` | the vendored component's hash | **must equal `games.active_engine_digest`** or the wave claims nothing, for ever. The entrypoint derives it from the wasm |
| `replay_prefix`, `blob_endpoint` | `replays`, `$R2_ENDPOINT` | the attempt key, and the prefix subtracted from the presigned URL. `blob_endpoint` must equal the `kalam-blobs-put` connector's base URL |
| `poll_secs` | 5 | one claim is 0.68 ms against the real table, so claim load does not bound it at any contemplated fleet size |

Every one is provisional except the two equalities, which are correctness rather than tuning.

---

## 7. What Kalam must not do

Stated as a list because every one of them is a boundary something else depends on.

1. **It never joins the roster.** The claim reads status, engine digest and lease; not `models`,
   not `ratings`. A row is played because it is `pending` on this engine, not because anyone is
   still contesting — that is withdraw's job, and by the time Kalam sees a row the question is
   settled.
2. **It never reads or writes a rating.** Counting is Jodi's, in finish order, under a fence.
3. **It touches `matches` and `match_seats` and nothing else**, and its role cannot reach anything
   else.
4. **It does no timing of its own.** `turn_ms` is the loader's (Axon §5); Kalam reads the error off
   the reply.
5. **It never interprets `wave_state`,** an observation, an action, or a ref.
6. **It never retries a play call.** The `model-loader` connector is `max_retries: 0`, because a
   retried `/play` replays a turn.

---

## 8. What the wave asks of its neighbours

All folded in; kept because breaking one of them breaks the wave silently.

| Of | Ask |
|---|---|
| schema §4.3 | number the wave — `row_number() … - 1 AS m` — and return the seats as a flat list carrying `m` and `seat` |
| schema §4.4 | a set-valued named-fault statement keyed by weights hash |
| Axon §3.2 | `ref`, echoed verbatim on the play reply |
| the cartridge | `replay-decode`, not `replay_decode` — Orion refuses a function label with an underscore |
| the cartridge | `observe(wave_state, refs)`, refs flat and echoed |
| the cartridge | `step` accepts the explicit `{m, seat, action}` form, and plays the no-op for an absent seat |
| the cartridge | `finish` may be called while matches are still running |
| the cartridge | the finish result of an ended match carries `map_id` and `map`, so a replay is self-sufficient |

---

## 9. Decisions taken here

Decisions **19** (N, the renew interval and the lease), **33** (the finish drain) and **5** (K), with
the unnumbered calls the wave forced — a partial renew halts, strikes count cumulatively, per-seat
state rides in the ref, the refs are flat and matched on `(m, seat)`, and a forfeited seat is not
sent to the loader at all — are recorded with their reasoning in
[devops/docs/decisions.md](https://github.com/Tiny-Brains/devops/blob/main/docs/decisions.md) §3,
under *The wave*.

---

## 10. What has run

The loop, the barrier and its two-class split, the turn, the strike accumulator and the forfeit, the
drain and its tail, and the three fault walks — a timed-out row, a memory refusal, a named refusal —
have all run against real rows in a real replica, driving the real `tb.ants` against a real `axon`
doing real ONNX inference.

**Never exercised**: the memory branch of the barrier, because two Micro-class models against a
1 GiB budget never approach it; and mixed-engine rollout, where two replicas on different digests
drain and claim past each other. `orion-server lint` and `readyz` prove neither, and must not be
reported as a working match loop.

---

## 11. Open questions

1. **Is a cumulative strike count right?** Five scattered misses over a thousand turns is a model
   that works and occasionally hiccups; five in a row is a model that has stopped. The stricter
   reading is chosen because it is the one a competitor cannot game, but it will forfeit seats that
   a consecutive count would not, and nobody has seen the distribution yet.
2. **The drain finishes one match per sweep even when the wave is over.** A wave whose K matches
   all end together spends K quiet sweeps draining. It is cheap — `observe` returns nothing — but a
   "drain up to J per sweep" is one number away if it ever matters.
3. **`wave_k` at 16 is conservative on purpose.** Everything measured says 32 is comfortable and 64
   is where the ceiling starts to bind. The argument for staying low is blast radius.
4. **The refs travel four times per turn** — into `observe`, onto the view, onto the play row, back
   on the reply. At K=16 that is 32 small objects, a few kilobytes. It is only worth noticing if
   the strike counter is ever the only thing in them, which would mean the hashes could be dropped
   from the round trip.
5. **Nothing here bounds how long a single turn may take end to end.** `turn_ms` bounds the loader;
   the channel timeout bounds the run; between them a pathological engine could sit inside its own
   plugin ceiling for a long time on every turn. The plugin's `max_timeout_ms` is the place to say
   so, and the cartridge should set it.
