# kalam

Kalam plays TinyBrains matches one at a time: it claims a queued row, advances its turns, and
records the result and the replay. It ships an Orion 1.8.1 package with five cron channels, two
generated workflows, five connectors, and the Ants plugin; it ships no server.
[DevOps](https://github.com/Tiny-Brains/devops) chooses the Orion instances that run its definitions.

## The name

**Kalam** (களம்) means the arena or field of contest in Tamil. It is where scheduled competitors
meet; eligibility, matchmaking, and rankings are decided elsewhere.

## Scope

**It owns**

- Atomic claims, lease renewal, and recovery of expired claims.
- Keeping this node's model set in step with the ladder, and the observe / infer / step loop.
- Per-seat strike accounting and attempt failure handling.
- Result persistence and replay uploads identified by match attempt.

**It does not**

- Select opponents, rate results, or promote versions; [Jodi](https://github.com/Tiny-Brains/jodi) owns those decisions.
- Read the roster or expose public routes; [Soma](https://github.com/Tiny-Brains/soma) owns the API and schema.
- Interpret game state; [Ants](https://github.com/Tiny-Brains/ants) implements the cartridge.
- Implement inference; Orion's own `models` entity fetches each artifact by digest and runs it
  under `tract`, and a competitor's adapter is JSONLogic the same engine evaluates.
- Checkpoint turns; recovered matches restart from their seeds.

## Where it sits

```text
[Postgres matches + seats] <-- SQL --> [Kalam replica] --> [Ants plugin]
[Postgres model_versions]  --  SQL -->       |     |
                                      admin API   replay PUT
                                             v     v
                                  [this node's]  [object store]
                                  [models entity] --> [models bucket]
```

| Direction | Party | Over | What moves |
|---|---|---|---|
| reads | Postgres | kalam-db SQL | Queued matches, leases, engine identity, and seat hashes |
| writes | Postgres | Restricted SQL role | Claim, execution, result, and replay columns |
| reads | Postgres | Six columns of model_versions | What the ladder says this node should be able to play |
| calls | This node's admin API | kalam-orion HTTP | Register, admit and activate a model here; ask whether a seat's model is servable |
| calls | The models bucket | kalam-models storage | The node fetches each artifact by connector and digest, and re-hashes it |
| calls | Ants | In-process plugin ABI | Opaque state, seat observations, actions, and scores |
| calls | Replay store | Presign followed by HTTP PUT | One replay object per finished attempt |

The [system map](https://github.com/Tiny-Brains/devops#where-it-sits) shows how replicas scale.
The match database is the coordination boundary with Jodi; the packages do not call each other.

## Interface

Kalam exposes no public API. `channels/tb-match-{1..4}.json` and `channels/tb-roster.json` define
its triggers; `workflows/tb-match-run.json` and `workflows/tb-roster-run.json` are the installed
execution graphs.

| Channel | Singleton key | Configurable controls | One occurrence writes |
|---|---|---|---|
| tb-match-1..4 | match-N, within this replica | transport_config.schedule, config.timeout_ms; forbid concurrency and skip misfires | One claim, its renewed leases, its terminal result, its replay key and its seat scores |
| tb-roster | roster, within this replica | schedule, timeout_ms | Nothing in the platform schema — it writes only to this node's own model registry |

A match run claims ONE row, checks this node can serve both seats' models, generates the world, and
repeats observe → one `model_infer` per live seat → step until the engine stops returning views.
Four channels means four matches at once per replica; the claim's `FOR UPDATE SKIP LOCKED` is what
keeps them off each other's rows.

A roster run takes one step per version per tick — register, then activate what has passed — so a
node that restarts mid-admission simply catches up.

| Plugin id | Export | Role in the package | Purity |
|---|---|---|---|
| tb.ants | tb.ants.worldgen | Initialize a seeded world | Pure, seeded |
| tb.ants | tb.ants.observe | Build live-seat views | Pure |
| tb.ants | tb.ants.step | Advance the match | Pure |
| tb.ants | tb.ants.finish | Extract results | Pure |
| tb.ants | tb.ants.replay-decode | Available in the plugin; not used by the match clock | Pure replay reconstruction |

[plugins/tb-ants/plugin.json](plugins/tb-ants/plugin.json) declares the ABI inputs.
[Soma's migrations](https://github.com/Tiny-Brains/soma/tree/main/migrations) declare allowed columns
and the restricted kalam role. Check the SQL/role seam against a development database:

```sh
./scripts/check-sql.sh
```

## Run it, test it

Kalam cannot run alone. Provision the [DevOps stack](https://github.com/Tiny-Brains/devops#run-it-test-it)
or an Orion instance with the migrated database, `models.enabled = true`, a readable models bucket and a writable replay store.
Run commands from this repository's root.

- Orion server 1.8.1 with plugins enabled and the variables below supplied.
- curl plus jq or Python 3 for loading; Python 3 and Docker for SQL checks.
- No Rust toolchain is needed to load the committed engine component.

Point ORION_ADMIN at the replica, enable private connections for its loopback sidecar, and load:

```sh
KALAM_ALLOW_PRIVATE_URLS=1 R2_ENDPOINT=... ./scripts/load-package.sh
```

Validate the definitions independently of the running wave:

```sh
orion-server lint . --deny-warnings
```

The SQL check prepares the nine shipped statements and checks execution-column grants, including
denial of rated_at updates. It recreates and drops `kalam_sqlcheck`; use a development DB_CONTAINER
and DB_USER. Set MIGRATIONS to the path of Soma's migrations when the two repositories are not
adjacent. There is no standalone wave test suite: SQL preparation does not exercise leases or turn
execution. Package lint does not check Ants inputs from the vendored JSON manifest; use --plugin-dir
with a checkout containing Ants' plugin.toml for that check, or validate against the active plugin
at load.

Edit scripts/gen-kalam.py and regenerate with `python3 scripts/gen-kalam.py`; commit the workflow
and channel output. To update the engine, move ANTS_REF: the component comes from the cartridge's own artifact image, so this repository keeps no copy of it.
That script copies committed artifacts and prints their digest; it does not compile Ants.

Use the pinned Orion 1.8.1 toolchain for these checks. Older binaries do not understand this
package's cron, plugin, or authentication definitions and can report misleading schema errors.

## What a deployment owes it

| Setting | Purpose | Missing or inconsistent value |
|---|---|---|
| KALAM_DB_URL | Secret-bearing database URL using the kalam role | The match clock cannot reach its execution rows |
| R2_ENDPOINT, R2_BUCKET | Replay store location | Uploads fail and successful results cannot finish |
| R2_ACCESS_KEY, R2_SECRET_KEY | Secret replay-store credentials | Replay signing or upload fails |
| ORION_ADMIN, ORION_ADMIN_API_KEY | Loader destination and optional secret admin token | Defaults target the local admin API; protected APIs reject missing credentials |
| KALAM_ALLOW_PRIVATE_URLS | Set to 1 for private database, bucket and admin addresses | Orion blocks private connections, including the models bucket at the `head` stage |
| MODELS_BUCKET, MODELS_ENDPOINT | The models bucket, at the address a NODE dials | The roster registers nothing; every match is released as unready |
| ORION_ADMIN_BEARER | The whole `Bearer <key>` header value | Every admin call is 401 — a connector resolves `env://` only when the reference is the entire string |
| KALAM_ORION_ADMIN | Loader-time override for this node's own admin API | Defaults to 127.0.0.1:8080, which is right on a node |
| lease_seconds, renew_every_n_turns | Claim timing | Poor sizing causes lease loss mid-match |
| turn_ms, max_turns | Cartridge execution limits, and the per-seat `model_infer` deadline | Must match the registered game contract |
| model_prefix, orion_version | The model id a version gets, and what ran the adapters | Must equal Soma's; `configs.sh` asserts both |
| refusal_ceiling | How often a row may be refused for an unserved model before it fails | Counted apart from lapses. The STRIKE ceiling is not here: it is read off `matches.strike_ceiling` |
| engine_digest | Identity used to filter claims | A mismatch can leave a healthy replica idle |
| replay_prefix, blob_endpoint | Attempt-object naming and signed URL handling | Incorrect paths or endpoint subtraction break uploads |

The `[vars]` rows are checked once, loudly, at the match clock's `vars` task, which halts if any is
missing. The
[replica template](https://github.com/Tiny-Brains/devops/blob/main/compose/orion/kalam.toml.tmpl)
contains deployment values; capacity and timing are tuning choices, not game-independent constants.
Derive engine_digest from the vendored bytes and align it with games.active_engine_digest and
season identity. blob_endpoint must match the replay endpoint used to construct signed paths.

Give each replica independent Orion state with cluster mode disabled, so its concurrency keys stay
local. Reload the package after replacing that state. A readiness probe must establish that the
engine and the tb-match channels are loaded; Orion's readyz alone does not prove playing capacity.
A replica also needs `[models] enabled = true` and a cache directory, because its roster clock is
what makes a version playable here.
The outer drain limit is server.shutdown_force_timeout_secs; configure the cron timeout and
container stop grace to allow the intended drain before leases become the recovery mechanism.

## Layout

```text
channels/tb-match-{1..4}.json  four cron schedules, one singleton key each
channels/tb-roster.json      the reconciler's schedule
workflows/tb-match-run.json  generated one-match execution graph
workflows/tb-roster-run.json generated model reconciler
connectors/kalam-db.json     restricted platform database connection
connectors/kalam-orion.json  this node's own admin API, where its model set lives
connectors/kalam-models.json the models bucket the node fetches artifacts from
connectors/kalam-blobs.json  replay URL signing
connectors/kalam-blobs-put.json replay upload connection
plugins/tb-ants/             vendored component and manifests
scripts/gen-kalam.py         readable SQL and workflow generator
scripts/check-sql.sh         SQL preparation and grant assertions
scripts/load-package.sh      replacement of objects tagged pkg:kalam
```

## What must stay true

- **Kalam cannot write the ladder.** Soma's migrations restrict its role to match execution columns, and the SQL check rejects rated_at access.
- **Only the current claim may finish a row.** Finish SQL checks the claim token, while replay keys distinguish attempts.
- **Leases make lost matches recoverable.** Claim SQL reaps expired work and bounds repeated failures rather than relying on process memory.
- **A seat this node cannot serve is released, never played.** The barrier asks the local admin API per seat; a model the roster has not caught up with costs a refusal, not a blind seat.
- **Engine identity controls claims.** A replica must only play rows matching its loaded component digest.
- **Game state remains opaque to workflows.** This boundary is a review requirement; cartridge functions own its interpretation.
- **The generated files are the package.** A change to scripts/gen-kalam.py that is not regenerated and committed ships a stale workflow, and nothing at runtime notices.

## Status

**15 September 2026 — the definitions say each thing once.** `shared/kalam.json` holds the clock
tracing block the five channels copied and the `config` all four `tb-match-N` lanes share — the
lanes now differ in `channel_id` and `concurrency.key` and nothing else, which is the whole of what
makes them separate lanes. The generator gained `group_runs()`, which collapses each run of
consecutive tasks sharing one condition into a task group carrying it once, and the no-op
`terminal` on the last step is gone. `orion-server clippy` went from 9 findings to 0.

`scripts/load-package.sh` is now `orion-server compile` + `orion-server package apply`; its sweep
skips any kind the artifact has none of, **which this package is the reason for** — the cartridge
comes from ants' image, so a checkout with no `plugins/` compiles to an artifact with no plugins,
and an unguarded sweep would delete `tb.ants`. New: `scripts/check-defs.sh`, the no-stack gate.
Verified on both live replicas: `tb.ants` loaded, 5 cron channels, 0 quarantined, and the engine
digest agreeing across `games.active_engine_digest`, the live season and both nodes.

**14 September 2026 — the wave is gone, and so is Axon.** This package is two clocks now:
`tb-match` claims one row and plays it with one `model_infer` per seat, and `tb-roster` keeps this
node's model set in step with `model_versions`. Orion 1.8.1's `models` entity is what Axon was — it
fetches the artifact from the bucket by digest, re-hashes it, reads the graph and runs it under
`tract`, and a competitor's adapter is JSONLogic evaluated by the same engine that evaluates this
workflow. The wave existed to amortise one batched inference call across many seats, and every Ants
map is two-player; the batching it bought was measured at 1.11x. Verified on a live stack: three
baselines registered, admitted and activated by the roster clock, matches finishing `lone_survivor`
and `rank_stabilized` with zero strikes, and Jodi folding the results into ratings.
devops/docs/decisions.md, the R-series.

**11 September 2026 — the strike ceiling survives the first turn.** The change below put
`strike_ceiling` on the turn-0 refs, but `acts` rebuilds the refs every turn and did not carry it:
from turn 1 the forfeit test read a null ceiling, `{">=": [n, null]}` is true, every seat forfeited,
and on turn 2 an empty `/play` reached `step` as `BAD_ACTION: 0 actions for N live seats`. Every
wave on a fresh stack died that way, and the rows it had claimed sat `running` until their leases
lapsed. The rebuilt ref carries the ceiling now; waves play to `lone_survivor`, `rank_stabilized`
and `idle_food`, and fold.

**10 September 2026 — the strike ceiling comes off the match row.** `matches.strike_ceiling` is
stamped by pair from the season's rules and rides the refs to every seat, so the wave that applies
it and the clock that judges its result read one value from one place. Kalam's own
`[vars] strike_ceiling` is deleted, and with it `configs.sh`'s equality assertion: the documented
cross-repo footgun is gone because Kalam stops keeping a second copy, not because Jodi stopped
keeping the first. The replay envelope carries it too, so `tinybrains conform` replays a match at
the ceiling it was played under.

Nothing else changed. Kalam still never joins the roster, and it never learns that
`match_seats.version_id` names a version rather than an entry.

**10 September 2026 — the package ships as an image, and the engine is no longer vendored.**
`channels/`, `workflows/` and `plugins/` are gitignored; `Dockerfile` builds the package and devops
copies it into a volume, which both the loader and every replica mount where they used to mount this
checkout. `connectors/` is the authored part and is copied through. Both generated declarations are
byte-identical to the ones that were committed.

**`scripts/vendor-engine.sh` is deleted, and with it the drift it made possible.** The component
used to be copied into `plugins/tb-ants/` and committed, so two copies of one engine existed and
nothing errored when they diverged — the ladder played a component ants does not ship, with a viewer
built against the other. The image now takes the component straight from the cartridge's own
artifact image, named by `ANTS_REF`, so there is one copy and the two cannot disagree.

**The engine digest moved from `sha256:f17b51b6…` to `sha256:0807b641…`**, declared as a *patch*
rather than a release: `ants`' `cartridge.json` and `reference/observations.json` are byte-identical
across the change, so no rule moved and the live season kept its ratings and took the new digest.
(It has moved again since — R5 put the visibility mask in `observe`, which **is** a protocol change,
and the engine the ladder plays today is `sha256:185a2845…`. Nothing here names a digest; read it
from `games.active_engine_digest` or from `tinybrains games`, and treat any digest written in prose
as the date it was written.)
Verified on the running stack — `games.active_engine_digest`, the live season, and both replicas'
`[vars] engine_digest` all read the same value, no channel quarantined. The loader also registers
the engine's own 10-observation reference set now, rather than a single worst-case fixture
standing in for it.

**Per-seat cost, 10 September 2026.** The wave accumulates the loader's `infer_us` per seat across
the match — three counters on the `refs` element, exactly where `strikes` lives and for the same
reason — and writes them to `match_seats` at finish. The replay envelope's `seats` gets them for
nothing, since it is `hrefs.items`. Verified end to end on the running stack, and a
platform-written replay still conforms IDENTICAL against a local re-play over all 150 turns.

**Engine re-vendored, 10 September 2026.** The committed `tb-ants.wasm` was `sha256:254549b4` while
ants shipped `sha256:1555f081`, so `devops/scripts/check/configs.sh` was failing on the committed
tree and local play and the fleet were running different engines. Re-vendored and re-signed.

**10 September 2026.** The wave, replay upload, claim recovery, drain and the vendored engine are
implemented and have run against real rows in a real replica. Orion 1.8.1 package lint passes and
check-sql.sh checks the database contract; a running DevOps stack is still required to validate
play, drain and recovery. Open: mixed-engine rollout verification, and the memory branch of the
residency barrier, which no deployed model has yet been large enough to take. Lint or readyz alone
must not be reported as a working match loop.

## More

- Local references: [wave generator](scripts/gen-kalam.py), [engine ABI](plugins/tb-ants/plugin.json), and [SQL check](scripts/check-sql.sh).
- Design docs: [`docs/design.md`](docs/design.md) — the wave, the lease, the finish, and drain.
- [The competitor guide](https://github.com/Tiny-Brains/docs) — the reader-facing half: the rules, the model format, the adapter dialect, submitting, ranking and seasons. The platform section is the high-level design for someone new to the codebase.
- Related repositories: [Soma](https://github.com/Tiny-Brains/soma), [Jodi](https://github.com/Tiny-Brains/jodi), [Ants](https://github.com/Tiny-Brains/ants), [DevOps](https://github.com/Tiny-Brains/devops).
- Apache-2.0: see [LICENSE](LICENSE).
