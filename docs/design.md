# Design — the wave

Kalam claims up to K `pending` match rows in one statement and plays them as a **wave**: one engine
call and one loader call per turn serve every live match at once, and each row is finished with its
result and replay key the moment its own match ends. It knows nothing about ratings or the roster.

The row it claims, and every statement it runs, is
[soma/docs/schema.md](https://github.com/Tiny-Brains/soma/blob/main/docs/schema.md) §4. The engine is
a plugin — [ants](https://github.com/Tiny-Brains/ants). The loader it calls each turn is
[axon](https://github.com/Tiny-Brains/axon). How many replicas run, and what bounds a draining wave,
is [devops/docs/deployment.md](https://github.com/Tiny-Brains/devops/blob/main/docs/deployment.md).

## 1. What this page fixes

| Settled here | Left to |
|---|---|
| the claim channel: schedule, concurrency, timeout, tracing | 07 the poll interval at N replicas |
| the run's task list, in order, with the conditions each task carries | — |
| the residency barrier, and which of the schema §4.4's three statements each refusal takes | — |
| the turn: observe, one play call, step — and what carries between turns | — |
| strikes, the forfeit, and how a fixed task list counts anything per seat at all | — |
| renew: the interval, and the schema §4.5's open question about a partial renew | — |
| **decision 33**: the finish drain, one per turn, with a tail drain | — |
| the replay envelope's fields, and how one match's deltas are pulled from the wave's stream | 05 the envelope the viewer reads |
| drain on SIGTERM | 07 the orchestrator's grace period above it |
| K, N, the lease, and the timeouts, in one `[vars]` block, provisional | 07 the deployed values; the real loader for K |

Three things this page needs that its neighbours do not yet carry, all from the spike: the schema
§4.3's read must number the wave and put `m` on every seat (§4.1); the schema §4.4's named-fault
statement should take a set rather than one row (§4.2); and the cartridge owes `cartridge.md`'s function
set three corrections (§8).

---

## 2. What the spike settled, in one table

The numbers this page is written against, from the wave-turn spike that preceded the build.

| | Measured | Consequence for this page |
|---|---|---|
| `wave_state` at K=16, largest preset | 141.5 KiB, **14% of the 1 MiB ceiling** | the ceiling does not bound K. It starts to at about K=64 |
| a wave-turn, in Orion, loader at zero latency | **~3.0 ms per match-turn** early, ~4.6 ms late; linear in K, negligible fixed cost | Orion's task machinery is not what a turn costs. K is bounded by inference, and by blast radius |
| a full 1000-turn wave, K=16 | 73 s of engine and orchestration | the channel timeout is choosing between minutes and an hour |
| the observation over a match's life | grows **65%** as the known-water mask fragments | size a wave against a mature match, never a fresh one |
| the loop shape — whole list per turn, turn-0 tasks conditioned, a terminal task ending it | runs | the assumption the earlier design made about it holds |
| decision 33's drain, and the tail drain | runs | §4.6 |
| the strike accumulator | 107 injected timeouts → **107 strikes, 8 forfeits** | §4.5 |
| the three fault walks | pass | §4.2 |

---

## 3. The claim channel — `tb-wave`

```jsonc
{ "channel_id": "tb-wave", "channel_type": "async", "protocol": "cron",
  "workflow_id": "tb-wave-run", "tags": ["pkg:kalam"],
  "transport_config": {
      "schedule": "*/5 * * * * *",              // var: poll_interval
      "timezone": "UTC",
      "misfire_policy": "skip",                 // a missed poll is not worth catching up
      "concurrency": { "policy": "forbid", "key": "wave" } },
  "config": { "timeout_ms": 2400000,            // var: channel_timeout_ms — above the longest match
              "tracing": { "errors_only": true, "task_details": true } } }
```

- **`forbid` on key `wave`, and the key is local in effect.** Kalam's Orion runs on local SQLite
  with no shared state (the open work), so the lock is per replica, which is exactly what is
  wanted: one wave in flight per replica, N replicas in flight in parallel. This is the opposite
  of Jodi's use of the same mechanism, and worth saying because the spelling is identical.
- **`misfire_policy: "skip"`.** A poll that was missed while a wave was running has nothing to
  catch up: the queue is still there and the next tick claims from it. `catch_up` would fire a
  burst of claims at once for no gain.
- **`errors_only` tracing from day one** — architecture §7. A thousand-turn wave is ~8,000 task
  executions in one run, and tracing them all is a per-replica cost with no reader.
- **A run that claims nothing ends at its fourth task**, before the loop's first sweep does any
  work. At a 5-second poll on an empty queue that is the common case and it must be cheap.
- **The timeout is above the longest match.** Worst case is `max_turns × turn_ms` — 1000 × 1 s —
  plus the platform's own ~100 ms a turn and the drain's tail: about 19 minutes. 40 minutes is the
  bound, and the spike's measured full wave was 73 seconds, so the headroom is real rather than
  hopeful.

---

## 4. The run — `tb-wave-run`

One workflow, `loop: { "counter": "i", "max": 1100 }`. The whole task list runs once per turn;
turn-0 tasks are conditioned on the counter; a terminal task ends it.

**The loop `max` is `max_turns` plus K, not `max_turns`.** The drain finishes one match per sweep
(§4.6), so a wave whose matches all end on the last turn needs K more sweeps to empty its queue.
the open work said `[engine] max_loop_iterations` above `max_turns`; it is above `max_turns + K`.

### 4.1 Turn 0 — what this replica already holds, then the claim

| # | Task | Function | What |
|---|---|---|---|
| 1 | `resident` | `http_call` `GET /resident` | Axon §3.4. Advisory, and allowed to be stale — affinity is an optimisation of the fill, and the barrier is where correctness lives. `loading` is **not** passed on, so a wave is not filled with rows whose models are still cold |
| 2 | `reap` | `db_write` | the schema §4.1. Its own statement, not a CTE in the claim: a CTE's writes are invisible to the claim in the same snapshot. `rows_affected` is worth a metric — it counts crashes |
| 3 | `claim` | `db_write` | the schema §4.2, with `$1` the engine digest from `[vars]`, `$2` the resident weights, `$3` K, `$4` a fresh token, `$5` the lease |
| 4 | `claimed` | `filter`, `on_reject: halt` | `rows_affected > 0`. This is where an idle replica's run ends |
| 5 | `wave` | `db_read` | the schema §4.3, by token |

**One change this page asks of the schema §4.3.** The read must **number the wave** and carry that
number onto every seat:

```sql
    …, row_number() OVER (ORDER BY m.id) - 1 AS m, …
```

so a seat arrives as `{"m": 3, "seat": 1, "model_id": …, "weights_hash": …, "adapter_hash": …}`,
and the read also returns the seats as one flat list beside the rows. It is one clause, and
without it the workflow is stuck: **JSONLogic cannot number a list** — there is no index inside a
`map` — so if Postgres does not number the wave, nothing downstream can. Postgres building the
shape is what every Soma read already does.

### 4.2 The residency barrier

| # | Task | What |
|---|---|---|
| 6 | `models` | the wave's distinct models, and the refs (below) |
| 7 | `hold` | `http_call` `POST /load` — one call for the whole wave, Axon §3.1 |
| 8 | `split` | partition the reply by `state` and by `fault` |
| 9 | `release` | the schema §4.4's release statement, for `fault: "loader"` and for `loading` |
| 10 | `fail` | the schema §4.4's fail statement, for `fault: "model"` |
| 11 | `start` | the schema §4.4's start statement, for the rest |
| 12 | `playable` | `filter`, `on_reject: halt` — nothing started means unload and end |
| 13 | `world` | `tb.<game>.worldgen` from the started rows' seeds and the wave's preset |

**The split reads `fault`, not the reason word** — Axon §6. A `loader` fault returns the row to
`pending` with its refusal count raised and **no attempt spent**; a `model` fault fails it at once
with the seat. That indirection is the point: a reason word added to the loader later costs no
change here.

**One change this page asks of the schema §4.4.** Its named-fault statement takes one row —
`id = ($2)::uuid` with a single `fault_seat`. A barrier that refuses three rows for three different
seats then needs three statements out of a fixed task list, which is decision 33's problem in a
place that does not need it. Take a set instead:

```sql
UPDATE matches m
   SET status = 'failed', fault_reason = v.reason, fault_seat = v.seat,
       closed_at = now(), lease_expires_at = NULL
  FROM jsonb_to_recordset(($2)::jsonb) AS v (id uuid, reason text, seat smallint)
 WHERE m.id = v.id AND m.claim_token = ($1)::uuid AND m.status = 'claimed'
```

Same predicate, same guarantees, one round trip. The single-row form stays correct for §4.7's
mid-play fault, which really is one row at a time.

**The flatten, measured.** `{"merge": <computed>}` does **not** flatten a computed array of
arrays — it flattens only the arguments written out in the definition. The flatten is a `reduce`
over `merge`. This cost an afternoon in the spike and is invisible until it is not.

### 4.3 The refs — where per-seat state lives

The wave's models and its **refs** are built once, at turn 0, and the refs are rebuilt every turn
after that. A ref is:

```jsonc
{ "m": 3, "seat": 1,
  "weights_hash": "sha256:…", "adapter_hash": "sha256:…",
  "strikes": 0, "forfeited": false }
```

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

### 4.4 The turn

| # | Task | Condition | What |
|---|---|---|---|
| 14 | `observe` | — | `tb.<game>.observe(wave_state, refs)`; every live seat of every live match |
| 15 | `counts` | — | `n_live = length(views)`, `n_pending = length(pending)` |
| 16 | `play` | live | `http_call` `POST /play`: one row per view, `deadline_ms = turn_ms`, `budget_ops` from the game's manifest, `ref` echoed |
| 17 | `acts` | live | the actions, the strikes, and the next turn's refs — §4.5 |
| 18 | `step` | live | `tb.<game>.step(wave_state, actions)` |
| 19 | `carry` | live | the new state, the ended matches appended to `pending`, the replay deltas appended |

**Three calls a turn for the whole wave**, whatever K is: one plugin call out, one loopback HTTP
call, one plugin call back. That is finding 9–11's economics, and the spike says it costs about
3 ms per match-turn.

**`actions` is positionally aligned with the views the last `observe` emitted.** Not addressed by
`{m, seat}`, which is what a reader expects and what an audit would prefer: zipping the reply back
onto the views needs an index inside a `map`, and there is none. Positional alignment is safe
because both sides derive the order from the same `wave_state`, and a match ending mid-wave is
tested and does not shift anything.

### 4.5 Strikes, and the forfeit

One `map` over the play reply produces all three of the actions, the strike counts and the next
turn's refs — because **the reply is the only place where a row's error and the identity of its
seat are in the same object**.

- A row with an `action` gets its action; the seat's count is unchanged.
- A row with an `error` — `TIMED_OUT`, `ADAPTER_FAILED`, `INFERENCE_FAILED`, `NOT_RESIDENT` — gets
  the **no-op** and the seat takes a **strike**. The loader has already held the seat to `turn_ms`
  (Axon §5), so Kalam does no timing of its own.
- **Five strikes forfeit the seat** — the platform design §7. Counted **cumulatively, not consecutively**:
  five missed clocks in a match is five missed clocks, and a model that misses every fourth turn
  is not better behaved than one that misses five in a row. It is the stricter reading; rating and seasons
  may soften it, and the number is in `[vars]` either way.
- **A forfeited seat plays the no-op until the engine ends the match** — decision 16, no resign
  value. Its `forfeited` flag rides in the ref, so the following turns cost the loader nothing:
  the row is not sent at all.
- **At finish, forfeited seats rank last** — finding 12.5. Kalam overrides the engine's ranks and
  keeps the engine's own in the replay envelope, so a timed-out model cannot win on points and an
  audit can still see what the game thought happened.

Verified in the spike: a stub failing one row in three produced **107 strikes against 107 injected
timeouts**, and forfeited all 8 seats, with per-seat counts intact across 40 turns.

### 4.6 Renew, and the finish drain — decision 33

| # | Task | Condition | What |
|---|---|---|---|
| 20 | `renew` | live and `i % N == 0` and `i > 0` | the schema §4.5, one statement on the token |
| 21 | `held` | same | `filter`, `on_reject: halt` — see below |
| 22 | `pick` | pending | the head of the queue, and its deltas pulled from the stream |
| 23 | `presign` | pending | `storage_presign` `PUT`, key naming the match and the token |
| 24 | `put` | pending | `http_call` `PUT` — the replay envelope |
| 25 | `finish` | pending | the schema §4.6, one statement on the token |
| 26 | `pop` | pending | `pending = slice(pending, 1)`; the finished match's deltas dropped |

**The renew's halt rule, which the schema §4.5 left open.** The schema says zero rows means halt, and
asks what a count *below* the live match count means. The answer: **halt on any shortfall**, where
the expectation is the rows this wave still has `running` — claimed, minus finished, minus failed,
which the workflow knows exactly. All of a wave's rows carry one lease and expire together, so a
partial renew cannot happen in the ordinary course; if it does, this replica's grip on the wave is
not what it believes, and the safe move is the same as for zero — halt the run and release the
models. A stale attempt's finish updates nothing either way (the schema §4.6), so the cost of
halting is one wave replayed and the cost of continuing is a match played for nobody.

**The drain — decision 33, taken as recommended.** One ended match finished per sweep, from a
queue, with a **tail drain** after the last match ends: the terminal condition is "nothing live
**and** nothing pending", so the loop keeps sweeping — cheaply, since `observe` returns no views —
until the queue is empty.

The alternative was K conditioned presign-`PUT`-finish triples in the task list: 3K condition
evaluations every turn for a thing that fires once per match, against 5 tasks whatever K is. The
delay a drain adds is a few turns, which is invisible against count's ten-second tick. Watched
working in the spike: a wave whose matches ended on the same turn drained one per sweep and
stopped two sweeps later, with `pending` empty and every row finished.

**Pulling one match's deltas out of the wave's stream** is the one genuinely awkward expression in
the workflow, and it is awkward for the scope reason in §4.3 — the head match's index is a root
value and the filter runs in element scope. `reduce`'s initial accumulator is the only expression
evaluated at root that threads into the body, so it is the escape hatch:

```jsonc
{"reduce": [ {"var": "data.deltas"},
             {"if": [{"===": [{"var": "current.m"}, {"var": "accumulator.head"}]},
                     {"head": {"var": "accumulator.head"},
                      "items": {"merge": [{"var": "accumulator.items"}, [{"var": "current"}]]}},
                     {"var": "accumulator"}]},
             {"head": {"var": "data.pending.0"}, "items": []} ]}
```

The complement — the same reduce with `!==` — is what `pop` keeps, so the wave's delta stream
shrinks as matches finish and never carries a finished match's history to the end of the run.

**The replay envelope**, written per attempt under a key naming the token, so a stale attempt's
blob is an orphan rather than a replacement (finding 7d):

```jsonc
{ "match_id": "…", "attempt_token": "…", "seed": …, "preset": "standard",
  "engine_digest": "sha256:…", "evaluator_digest": "sha256:…",
  "engine_ranks": [1, 2],            // the engine's own, before forfeits are applied
  "turns": 743,
  "deltas": [ { "t": 1, "a": ["NNE-", "S-W"] }, … ] }
```

The action stream, not frames — the platform design §8. Layer 05 owns the envelope the viewer reads; what
this page fixes is that the engine's ranks are in it and that the key names the attempt.

### 4.7 A fault mid-play, release, and the end

- **A fault Kalam can attribute mid-play** — the loader answering a `model` fault on a row it was
  holding — fails that row at once with the seat, using the schema §4.4's single-row form, and the
  match leaves the wave.
- **`over`**: nothing live and nothing pending. `tb.<game>.finish` for the ranks, `POST /unload`
  for the wave's models, `terminal: true`.
- **Nothing else is written.** No rating is read, no rating is written, no other table is touched,
  and the `kalam` role cannot reach one (the schema §3.8, exercised in [soma/scripts/verify/run.sh](https://github.com/Tiny-Brains/soma/blob/main/scripts/verify/run.sh)).

---

## 5. Drain on SIGTERM

> **BUILT AND MEASURED, 8 September 2026, and it needed three fixes this section does not
> mention.** The mechanism as written — "Orion stops claiming and lets the wave in hand finish
> inside `cron.shutdown_timeout_secs`" — is right and was not enough on its own:
>
> 1. **The entrypoint's `wait` returns when the trap fires.** `trap 'kill -TERM $PID'; wait $PID` is
>    the obvious shape and it is wrong: the trap interrupts `wait`, `wait` returns, the script falls
>    off its end, and the container exits *while Orion is still draining*. Measured: `docker stop -t
>    300` returned in **zero seconds** with two rows still `running`. It looks like a clean shutdown
>    from the outside, and it is a lost wave. The fix is to wait again, in a loop, until the process
>    is actually gone.
> 2. **`cron.shutdown_timeout_secs` is not the only bound.** `[server] shutdown_drain_secs` and
>    `[server] shutdown_force_timeout_secs` default to about 30 seconds, and **the smaller number
>    wins**: with the cron key at 2700 and the server keys at their defaults, the replica drained
>    for 30 s and abandoned the wave. All three have to exceed the longest match.
> 3. **The orchestrator's grace period must exceed all three**, which is deployment's and unchanged.
>
> **Correction, deployment §6.1.** Point 2's rule — "all three have to exceed the longest match" — is
> not what the code does. The cron worker is a supervised task drained under
> `server.shutdown_force_timeout_secs`, so that key is the *outer* bound on a wave and
> `cron.shutdown_timeout_secs` only ever shortens it; `shutdown_drain_secs` is an unconditional
> sleep for a load balancer Kalam does not sit behind, and belongs *short*. §6's table gives it as
> 2 700 for the wrong key. Layer deployment §6.2 has the four numbers.
>
> The failure mode of getting any of this wrong is the same in every case and is worth naming: the
> rows stay `running` with a live lease, nothing reports an error, and they are unplayable until the
> lease lapses and the next claim reaps them. A drain that half works is indistinguishable from one
> that works until you look at the table.

Finding 8b, and the whole of Kalam's scale-down story:

1. The orchestrator sends `SIGTERM`; `kalam/docker-entrypoint.sh` forwards it to `orion-server`.
2. Orion stops claiming new cron occurrences and lets the run in hand finish inside
   `cron.shutdown_timeout_secs`.
3. The wave finishes its matches, drains its queue, releases its models and exits.
4. The orchestrator's grace period must be **above** `cron.shutdown_timeout_secs`, which must be
   above the longest match — deployment.

A replica killed rather than drained loses its wave and nothing else: its rows lapse, the next
claim reaps them, and they are played again from turn 0. The third lapse fails a row, and with
drain working, lapses come only from crashes.

**Not yet verified.** It needs a cron channel and a real shutdown, and the spike used a `sync`
channel so it could be driven and timed. It is the first thing to test when the workflow moves
into `kalam/`.

---

## 6. The numbers

In Kalam's own instance config (`devops/orion/kalam.toml.tmpl`), read as `metadata.vars.*`.

| Var | Value | What moves it |
|---|---|---|
| `K` | 16 | **a residency budget**, not a throughput guess: `K x seats x max_class_bytes <= the replica's weight memory`. Decision 5 |
| `renew_every_turns` | 30 | how often the wave renews its lease |
| `lease_secs` | 300 | must exceed the renew interval by a comfortable margin |
| `strike_ceiling` | 5 | **must equal Jodi's `forfeit_strikes`** — Kalam applies it, count reads its consequences off the row. `devops/scripts/check-configs.sh` asserts the equality |
| `engine_digest` | the vendored component's hash | **must equal `games.active_engine_digest`** or the wave claims nothing, for ever. The entrypoint derives it from the wasm |
| `poll_interval` | 5 s | closed by measurement: one claim is 0.68 ms against the real table, so claim load does not bound it at any contemplated fleet size. Decision 25 |

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
4. **It does no timing of its own.** `turn_ms` is the loader's (Axon §5); Kalam reads
   `timed_out` off the reply.
5. **It never interprets `wave_state`,** an observation, an action, or a ref.
6. **It never retries a play call.** The `model-loader` connector is `max_retries: 0`, because a
   retried `/play` replays a turn.

---

## 8. What the wave asks of its neighbours

| Of | Ask | Why |
|---|---|---|
| the schema §4.3 | number the wave — `row_number() … - 1 AS m` — and return the seats as a flat list carrying `m` and `seat` | JSONLogic cannot number a list. §4.1 |
| the schema §4.4 | a set-valued named-fault statement beside the single-row one | a barrier can refuse several rows for several seats; §4.2 |
| Axon §3.2 | `ref`, echoed verbatim on the play reply | without it Kalam cannot count strikes at all. **Already folded in** |
| the cartridge | `replay-decode`, not `replay_decode` | Orion refuses a function label with an underscore, so the five-function set does not load as written |
| the cartridge | `observe(wave_state, refs)`, refs flat and echoed | §4.3 |
| the cartridge | `actions` positionally aligned with the last `observe` | §4.4 |
| the cartridge | say whether `finish` may be called while matches are still running | the drain calls it when the queue is non-empty, which is usually before the wave is over |

---

## 9. Decisions taken here

Decisions **19** (N, the renew interval and the lease), **33** (the finish drain) and **5** (K), with
the unnumbered calls the wave forced — a partial renew halts, strikes count cumulatively, per-seat
state rides in the ref, the refs are flat and matched on `(m, seat)`, and a forfeited seat is not
sent to the loader at all — are recorded with their reasoning in
[devops/docs/decisions.md](https://github.com/Tiny-Brains/devops/blob/main/docs/decisions.md) §3,
under *The wave*.

---

## 10. What has run, and what has not

**Run** in the wave-turn spike, against Orion 1.7.0: the loop shape; the residency barrier and
its two-class split; the turn — observe, one play call, step; the strike accumulator and the
forfeit; decision 33's drain and its tail; and the three fault walks — a timed-out row, a memory
refusal, a named refusal.

**Also run since, 8 September 2026**: the spike's workflow drove the **real** `tb.ants` against the
**real** `axon` doing real ONNX inference — four matches, the finish drain firing, ranks and scores
returned. So §4's loop has now run against both real artifacts, not only against stubs. What it has
never run against is a row.

**BUILT AND RUN, 8 September 2026.** All six of the items below have run, against real rows, in a
real replica. What each one taught is in the banner at the top of this document and in
`kalam/scripts/gen-kalam.py`'s docstring; the list is kept because it was the right list.

*The six, as they stood before the build:*

- the real statements. The spike writes no rows: it is driven over HTTP with a hand-fed wave, so
  the schema §4.1–§4.6 are proved against Postgres ([soma/scripts/verify/run.sh](https://github.com/Tiny-Brains/soma/blob/main/scripts/verify/run.sh)) and not yet against Orion.
  **This is the largest of the six**, because it is where the claim's affinity ordering, the token
  and the lease first meet a real table
- the claim channel as **cron**. The spike's channel is `sync`/REST so it could be driven and timed
- **drain on SIGTERM** — §5
- the replay `PUT` — the spike drains a queue and writes nothing. `kalam-blobs` is configured with
  `presign_put` and has never been called
- the engine plugin **as part of the package**: `tb.ants` is loaded in the running server by hand
  and under no tag, so `kalam/plugins/` is empty, nothing installs it under `pkg:kalam`, and the
  `[vars]` digest equality check at boot has never fired. Signing waits on the trust keys (the build);
  the equality check does not (the open work)
- **residency under real memory.** Axon's holds, LRU and memory budget are tested, but never with a
  wave's worth of real weights against a real budget — which is the one place §4.2's *memory*
  refusal, as opposed to its named one, has ever been exercised. **Still true**: two Micro-class
  models against a 1 GiB budget never approach it, so the memory branch of the barrier has been
  written and never taken

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
   is where the ceiling starts to bind. The argument for staying low is blast radius: a replica
   death replays K matches from turn 0.
4. **The refs travel four times per turn** — into `observe`, onto the view, onto the play row, back
   on the reply. At K=16 that is 32 small objects, a few kilobytes. It is only worth noticing if
   the strike counter is ever the only thing in them, which would mean the hashes could be dropped
   from the round trip.
5. **Nothing here bounds how long a single turn may take end to end.** `turn_ms` bounds the
   loader; the channel timeout bounds the run; between them a pathological engine could sit inside
   its own plugin ceiling for a long time on every turn. The plugin's `max_timeout_ms` is the place
   to say so, and the cartridge should set it.
