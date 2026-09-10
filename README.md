# kalam

Kalam plays TinyBrains matches in waves: it claims queued rows, advances their turns together,
and records each result and replay. It ships an Orion 1.7.0 package with one cron channel, one
generated workflow, four connectors, and a vendored Ants plugin; it ships no server.
[DevOps](https://github.com/Tiny-Brains/devops) chooses the Orion instances that run its definitions.

## The name

**Kalam** (களம்) means the arena or field of contest in Tamil. It is where scheduled competitors
meet; eligibility, matchmaking, and rankings are decided elsewhere.

## Scope

**It owns**

- Atomic wave claims, lease renewal, and recovery of expired claims.
- Model residency coordination and the observe/play/step loop.
- Per-seat strike accounting and attempt failure handling.
- Result persistence and replay uploads identified by match attempt.

**It does not**

- Select opponents, rate results, or promote versions; [Jodi](https://github.com/Tiny-Brains/jodi) owns those decisions.
- Read the roster or expose public routes; [Soma](https://github.com/Tiny-Brains/soma) owns the API and schema.
- Interpret game state; [Ants](https://github.com/Tiny-Brains/ants) implements the cartridge.
- Run ONNX graphs; its [Axon](https://github.com/Tiny-Brains/axon) sidecar performs inference.
- Checkpoint turns; recovered matches restart from their seeds.

## Where it sits

```text
[Postgres matches + seats] <-- SQL --> [Kalam replica] --> [Ants plugin]
                                            |      |
                                      wave HTTP   replay PUT
                                            v      v
                                         [Axon]  [object store]
```

| Direction | Party | Over | What moves |
|---|---|---|---|
| reads | Postgres | kalam-db SQL | Queued matches, leases, engine identity, and seat hashes |
| writes | Postgres | Restricted SQL role | Claim, execution, result, and replay columns |
| calls | Replica's Axon | model-loader HTTP | Residency, one play request per wave turn, and hold release |
| calls | Ants | In-process plugin ABI | Opaque state, seat observations, actions, and scores |
| calls | Replay store | Presign followed by HTTP PUT | One replay object per finished attempt |

The [system map](https://github.com/Tiny-Brains/devops#where-it-sits) shows how replicas scale.
The match database is the coordination boundary with Jodi; the packages do not call each other.

## Interface

Kalam exposes no public API. [channels/tb-wave.json](channels/tb-wave.json) defines its trigger;
[workflows/tb-wave-run.json](workflows/tb-wave-run.json) is the installed execution graph.

| Channel | Singleton key | Configurable controls | One occurrence writes |
|---|---|---|---|
| tb-wave | wave, within this replica | transport_config.schedule, config.timeout_ms; forbid concurrency and skip misfires | Claims, renewed leases, terminal results, replay keys, and seat scores |

A run claims up to `wave_k` compatible rows, prioritizing trials and useful resident models.
It establishes residency, releases refused rows, generates worlds, and repeats observe → play → step.
Finished matches are persisted during the wave; remaining holds are released when the run ends.

| Plugin id | Export | Role in the package | Purity |
|---|---|---|---|
| tb.ants | tb.ants.worldgen | Initialize a seeded wave | Pure, seeded |
| tb.ants | tb.ants.observe | Build live-seat views | Pure |
| tb.ants | tb.ants.step | Advance the wave | Pure |
| tb.ants | tb.ants.finish | Extract results | Pure |
| tb.ants | tb.ants.replay-decode | Available in the vendored plugin; not used by the wave | Pure replay reconstruction |

[plugins/tb-ants/plugin.json](plugins/tb-ants/plugin.json) declares the ABI inputs.
[Soma's migrations](https://github.com/Tiny-Brains/soma/tree/main/migrations) declare allowed columns
and the restricted kalam role. Check the SQL/role seam against a development database:

```sh
./scripts/check-sql.sh
```

## Run it, test it

Kalam cannot run alone. Provision the [DevOps stack](https://github.com/Tiny-Brains/devops#run-it-test-it)
or an Orion instance with the migrated database, a replica Axon, and a writable replay store.
Run commands from this repository's root.

- Orion server 1.7.0 with plugins enabled and the variables below supplied.
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
and channel output. To update the engine, pass a cartridge checkout path to scripts/vendor-engine.sh.
That script copies committed artifacts and prints their digest; it does not compile Ants.

Use the pinned Orion 1.7.0 toolchain for these checks. Older binaries do not understand this
package's cron, plugin, or authentication definitions and can report misleading schema errors.

## What a deployment owes it

| Setting | Purpose | Missing or inconsistent value |
|---|---|---|
| KALAM_DB_URL | Secret-bearing database URL using the kalam role | The wave cannot access its execution rows |
| R2_ENDPOINT, R2_BUCKET | Replay store location | Uploads fail and successful results cannot finish |
| R2_ACCESS_KEY, R2_SECRET_KEY | Secret replay-store credentials | Replay signing or upload fails |
| ORION_ADMIN, ORION_ADMIN_API_KEY | Loader destination and optional secret admin token | Defaults target the local admin API; protected APIs reject missing credentials |
| KALAM_ALLOW_PRIVATE_URLS | Set to 1 for private database and sidecar addresses | Orion blocks private connections |
| MODEL_LOADER_URL | Loader-time sidecar URL override | Uses the committed loopback endpoint |
| wave_k, lease_seconds, renew_every_n_turns | Wave capacity and claim timing | Poor sizing causes refusals or lease loss |
| turn_ms, max_turns, budget_ops | Cartridge execution limits | Must match the registered game contract |
| strike_ceiling, refusal_ceiling | Failure accounting | strike_ceiling must equal Jodi's forfeit_strikes |
| engine_digest | Identity used to filter claims | A mismatch can leave a healthy replica idle |
| replay_prefix, blob_endpoint | Attempt-object naming and signed URL handling | Incorrect paths or endpoint subtraction break uploads |

The final five rows are Orion `[vars]`, and the wave halts at its `vars` task if any is missing. The
[replica template](https://github.com/Tiny-Brains/devops/blob/main/compose/orion/kalam.toml.tmpl)
contains deployment values; capacity and timing are tuning choices, not game-independent constants.
Derive engine_digest from the vendored bytes and align it with games.active_engine_digest and
season identity. blob_endpoint must match the replay endpoint used to construct signed paths.

Give each replica independent Orion state with cluster mode disabled, so its wave singleton stays
local. Reload the package after replacing that state. A readiness probe must establish that the
engine and tb-wave channel are loaded; Orion's readyz alone does not prove playing capacity.
The outer drain limit is server.shutdown_force_timeout_secs; configure the cron timeout and
container stop grace to allow the intended drain before leases become the recovery mechanism.

## Layout

```text
channels/tb-wave.json        cron schedule, singleton policy, and timeout
workflows/tb-wave-run.json   generated wave execution graph
connectors/kalam-db.json     restricted platform database connection
connectors/model-loader.json replica Axon connection with retries disabled
connectors/kalam-blobs.json  replay URL signing
connectors/kalam-blobs-put.json replay upload connection
plugins/tb-ants/             vendored component and manifests
scripts/gen-kalam.py         readable SQL and workflow generator
scripts/check-sql.sh         SQL preparation and grant assertions
scripts/vendor-engine.sh     artifact copying and digest calculation
scripts/load-package.sh      replacement of objects tagged pkg:kalam
```

## What must stay true

- **Kalam cannot write the ladder.** Soma's migrations restrict its role to match execution columns, and the SQL check rejects rated_at access.
- **Only the current claim may finish a row.** Finish SQL checks the claim token, while replay keys distinguish attempts.
- **Leases make lost waves recoverable.** Claim SQL reaps expired work and bounds repeated failures rather than relying on process memory.
- **Play calls are never retried by the connector.** model-loader.json sets max_retries to zero to avoid duplicate turn execution.
- **Engine identity controls claims.** A replica must only play rows matching its loaded component digest.
- **Game state remains opaque to workflows.** This boundary is a review requirement; cartridge functions own its interpretation.
- **The generated files are the package.** A change to scripts/gen-kalam.py that is not regenerated and committed ships a stale workflow, and nothing at runtime notices.

## Status

**Per-seat cost, 10 September 2026.** The wave accumulates the loader's `infer_us` per seat across
the match — three counters on the `refs` element, exactly where `strikes` lives and for the same
reason — and writes them to `match_seats` at finish. The replay envelope's `seats` gets them for
nothing, since it is `hrefs.items`. Verified end to end on the running stack, and a
platform-written replay still conforms IDENTICAL against a local re-play over all 150 turns.

**Engine re-vendored, 10 September 2026.** The committed `tb-ants.wasm` was `sha256:254549b4` while
ants shipped `sha256:1555f081`, so `devops/scripts/check/configs.sh` was failing on the committed
tree and local play and the fleet were running different engines. Re-vendored and re-signed.

**10 September 2026.** The wave, replay upload, claim recovery, drain and the vendored engine are
implemented and have run against real rows in a real replica. Orion 1.7.0 package lint passes and
check-sql.sh checks the database contract; a running DevOps stack is still required to validate
play, drain and recovery. Open: mixed-engine rollout verification, and the memory branch of the
residency barrier, which no deployed model has yet been large enough to take. Lint or readyz alone
must not be reported as a working match loop.

## More

- Local references: [wave generator](scripts/gen-kalam.py), [engine ABI](plugins/tb-ants/plugin.json), and [SQL check](scripts/check-sql.sh).
- Design docs: [`docs/design.md`](docs/design.md) — the wave, the lease, the finish, and drain.
- [The competitor guide](https://github.com/Tiny-Brains/docs) — the reader-facing half: the rules, the model format, the adapter dialect, submitting, ranking and seasons. The platform section is the high-level design for someone new to the codebase.
- Related repositories: [Soma](https://github.com/Tiny-Brains/soma), [Jodi](https://github.com/Tiny-Brains/jodi), [Axon](https://github.com/Tiny-Brains/axon), [Ants](https://github.com/Tiny-Brains/ants), [DevOps](https://github.com/Tiny-Brains/devops).
- Apache-2.0: see [LICENSE](LICENSE).
