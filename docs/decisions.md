# Decisions — Kalam

Why Kalam is shaped the way it is: the part of TinyBrains' decision record about this
repository. One line per decision, with the reasoning kept and the cost of flipping it named where
that was worked out.

> **The record was one file until 17 September 2026**, `devops/docs/decisions.md`. When devops
> stopped running anything (N25) it was split, so each decision lives in the repository it is
> about. **The numbers are the record's, not this file's**: they were assigned once across the
> platform and are never reused, so a citation of `41` or `N24` names one decision wherever it now
> lives, and a section number below is the one the whole record gave it.
>
> **Four numbering series.** The **A-series** is the twenty-one architectural decisions taken
> before anything was built. The **plain series** is the build decisions the layers took, numbering
> from 1 again, so `A5` and `5` are different decisions and a bare number in a code comment means
> the plain series. The **R-series** is the Orion 1.8.1 rebuild and the **N-series** the runner, the
> submission path and where each repository's artifacts come from.

## Where the rest of the record is

| Decisions | Where they live |
|---|---|
| **A1–A21**, §2's review findings and the Orion changes asked for | [soma](https://github.com/Tiny-Brains/soma/blob/main/docs/decisions.md) |
| Plain series: the match table (2, 3, 7, 7c, 7d, 18, 21, 22), the clocks (1, 7–13, 23, 24, 28, 51–59), admission (20, 35–40), the retired loader (6, 34, 46, the adapter cap) and deployment 43, 44, 48 | [soma](https://github.com/Tiny-Brains/soma/blob/main/docs/decisions.md) |
| Plain series: the wave (19, 33) and deployment 5, 25, 41, 42, 45 | [kalam](https://github.com/Tiny-Brains/kalam/blob/main/docs/decisions.md) |
| Plain series: the game and the protocol (4, 14–16), the baselines (49, 50 of the loader's) | [ants](https://github.com/Tiny-Brains/ants/blob/main/DECISIONS.md) |
| Plain series: the training environment (47, 48 of the loader's) | [cli](https://github.com/Tiny-Brains/cli/blob/main/DECISIONS.md) |
| Plain series: deployment 47 and 49 (the compose file's) | [web](https://github.com/Tiny-Brains/web/blob/main/DECISIONS.md) |
| **R1, R2, R4, R6, R9, R10, R11** · **R3, R7, R8** · **R5** | soma · kalam · ants |
| **N3, N6–N8, N12, N13, N15–N19** · **N1, N2, N4, N5, N9** · **N20–N22, N24** · **N23** · **N25** | soma · kalam · ants · cli · web |
| Still open | the repository each is forced in: 30, 31, N14 and three unnumbered in soma; N10 and a runner on another network in kalam; 32 in ants; 26, 27, N11 and the orchestrator in web |

The plain series collides with itself once: the retired loader's **47, 48, 49** and deployment's
**47, 48, 49** are different decisions, told apart above by where each lives.

---

## 3. Build decisions

Numbered as the build numbered them. Each names the repository it now lives in.

### The wave — [`kalam`](https://github.com/Tiny-Brains/kalam)

| # | Decision | Taken as |
|---|---|---|
| 19 | N, the renew interval, and the lease | 30 turns and 300 s |
| 33 | The finish drain | one per sweep from a queue, tail drain after the last match ends, terminal on "nothing live and nothing pending" |
| — | A partial renew halts | all of a wave's rows share one lease, so a shortfall means this replica's grip is not what it believes |
| — | Strikes are counted cumulatively | five missed clocks in a match, not five in a row — the stricter reading |
| — | Per-seat state rides in the ref | the only mechanism a fixed task list has, since element scope does not nest inside root scope |
| — | The refs are flat, matched on `(m, seat)` | a nested array shifts the moment a match ends, silently |
| — | The forfeited seat is not sent to the loader | it plays the no-op by construction; the loader is not asked to run a model whose action is discarded |

### Deployment — decided in `devops`, which runs nothing since N25

| # | Decision | Taken as | Why |
|---|---|---|---|
| 5 | K, rows per wave per replica | **a residency budget**: `K × seats × max_class_bytes ≤ the replica's weight memory`. K = 16 is the largest that fits 2 GiB at a 64 MiB class | every measured ceiling was checked and none binds; memory at the largest class does, and it moves K *down* on small replicas |
| 25 | The poll interval at N replicas | **5 s, closed by measurement.** One claim is 0.68 ms against the real table, unchanged by queue depth; 100 replicas at 5 s is 1.4% of the single-client floor | the lower bound was supposed to be claim load, and it is not one at any contemplated fleet size — what bounds the interval is trial latency, which argues for keeping it small |
| 41 | Kalam in cluster mode | **no** — one Orion per replica on local SQLite | a shared `forbid` row would make the wave a fleet-wide singleton: one replica plays, N−1 idle, all healthy |
| 42 | What bounds a draining wave | **`server.shutdown_force_timeout_secs`**, with `cron.shutdown_timeout_secs` as the inner deadline | the folk rule raised the number that costs and left the one that helps |
| 45 | Replica pools per weight class | **one pool**, K sized for the largest class the fleet serves | a wave can contain any mix and the claim does not select by class |
| — | Where the package lives on a replica | **loaded into each replica's own state at init**, not baked into the image | the load script is the same one a developer runs |

---

## 4. The 1.8.1 rebuild — the R-series

Taken 14 September 2026, when Orion 1.8.1 made its `models` entity a strict superset of what `axon`
does. Each is **measured where it could be measured**, and the numbers are in the rows themselves —
taken with `orion-server dry-run --model-dir` against the real baselines, not estimated. A third
numbering series, because these overturn A-series decisions rather than extending the build series.
The study they came out of is in this repo's git history (`docs/migratingv18.md`, deleted
15 September 2026); what survived it is here and in [`orion-notes.md`](https://github.com/Tiny-Brains/soma/blob/main/docs/orion-notes.md) §0, with the
three measurements still owed named in this repo's `README.md` Status block.

| # | Question | Decision | What it overturns, and what it cost to check |
|---|---|---|---|
| R3 | Who reads the policy head? | **The platform.** `model_infer` is called with `raw: true`, so the manifest has no `result` expression; the workflow decodes by rank — `[1, 5, H, W]` is per-cell and gathered at the ants' flat indices, `[N, 5]` is per-ant and already in `mine` order | **A2** in part. Forced by one fact, not by shapes: a `result` expression's root is the output tensors alone (`handler.rs:808-816`), so it cannot see the observation and cannot gather. Both head shapes stay legal — all six shipped adapters emit per-cell and `drill/models/ragged.onnx` emits per-ant |
| R7 | One match per run, or a wave? | **One match.** `tb-match` claims a row, plays it turn by turn, finishes it | **A18** and **A3**'s wave half. Every Ants map is two-player, so the wave, the batcher and the affinity ordering served a number that is two. Concurrency moves to concurrent match runs |
| R8 | Where does a replica's model roster come from? | **The shared schema, read by a Kalam-side `tb-roster` clock** that registers, admits and activates on its own node whatever `model_versions` says is verified or active | Nothing, but it is the decision that keeps **41** intact: each replica stays its own Orion with its own state database, so models are per-node entities, and a clock that reconciles from Postgres needs no replica list anywhere. Jodi never calls a replica; the database stays the only channel |

---

## 4b. The N-series — a runner leaves the deployment, and GitHub leaves the submission path

Taken and built 16 September 2026, as two tracks decided together because each removed a dependency
that was not earning its place. They shared one thread — the models bucket, which lets a runner read
artifacts without a secret and is the submission path's audit trail — and no file. The proposal and
its phased plan (`docs/design.md`, `docs/design-plan.md`) were deleted once they were all record;
what they argued is here and in [`architecture.md`](https://github.com/Tiny-Brains/soma/blob/main/docs/architecture.md) §3a, the statements and routes
are `soma/docs/schema.md` §3.8a, §4 and §4a, the operator's page is [`deployment.md`](deployment.md)
§11, and what each phase turned up is in the Status blocks of `soma`, `kalam`, `devops` and `web`.
N10, N11 and N14 are still open, in §5.

### The runner

| # | Question | Decision | What it overturns, and what it cost |
|---|---|---|---|
| N1 | Who operates a runner? | **Admins, on platform hardware that is somewhere else.** Not competitors | Nothing — it is a change of *reach*, not of trust, and every other N-decision leans on it. An admin can already close a season or promote a version through routes that exist, so a runner grants no authority its operator lacked. That is why the design builds misconfiguration checks and blast-radius bounds and not fraud detection |
| N2 | Do off-site runners play rated matches? | **Yes, everything.** No pools, no verification clock, no quarantine | What competitor-operated runners would need, which is substantial: a `pool` column on `matches` and `runners` with the claim filtering on it, engine-only recomputation from a replay's `deltas` as a gate in count, sampled conformance on a trusted host (`tinybrains conform` is the algorithm), a quarantine flag, and a statement returning a quarantined runner's unrated results to the queue. **`matches.pool` is deliberately not added** against that maybe: a pre-release schema is rewritten in place, and a column for a future that may not arrive has to be explained to everyone who reads the migration |
| N4 | How does a runner read artifacts? | **A GET-only key scoped to `models/*`**, minted by `scripts/setup/models-read-key.sh`. On a public-read bucket it is not sensitive; Orion re-hashes every byte against `weights_hash`, so the key buys read access and no authority | The only option 1.8.1 leaves: `ArtifactRef` takes `{connector, key, digest}` and no URL, so the gate cannot broker a signed link for weights the way it does for replays. Measured on a credential-free replica: anonymous GET of a model `200`, of a replay `403`, listing `403`; the runner's key GET `200`, PUT `403`. The clean end state is an Orion change, listed in §2 and **not yet filed** |
| N5 | Do runners fetch model bytes from GitHub? | **No.** Bytes come from the bucket, by `GENERATED` key, verified by digest | Fetching from a release would add a third-party dependency on the hot path of every match that the data path does not have (R11), Orion's `ArtifactRef` could not address it, and a release is mutable where a generated key is not — a deleted release must not make a rated version unplayable or a replay unverifiable |
| N9 | Should the idle poll be longer, or back off? | **Neither — the question was aimed at the wrong resource.** The claim runs at ~1,200/s on real data, so a thousand runners at `*/5` use ~16% of it. What binds is that a cron run carries no state between occurrences, so every `api` run mints a ten-minute token and uses it **once** (214 exchanges against 214 authenticated calls in 120 s, 0.89/s per idle runner), and the token route must be rate limited on the **caller's address** because it is the route that establishes the principal | The 5 rps limit it shipped with refused the sixth machine behind one NAT — **silently**: the call is soft, the run ends at `noauth` with outcome `no_token`, and the channel traces `errors_only`. Now `runner_token_rate`, 30 rps. **The real repair is to stop minting a ten-minute credential for one call**, which halves every runner's cost — a longer-lived token, or auth the connector manages — and the number is the stopgap. The idle answer is `200 {"idle": true}`, not the `204` the proposal argued for: `http_call` parses every reply as JSON, so a 204 logged a parse error 0.8 times a second on a healthy fleet |

---

## 5. Still open

| # | Decision | Forced at | Note |
|---|---|---|---|
| N10 | Does the platform keep in-cluster `db`-mode replicas once off-site runners work? | kalam, devops | **Keep both for now; delete the `db` branch once the `api` path has soaked** — and not weeks later. The dual path costs nothing functionally and is the rollback: an `api` replica sets `KALAM_DB_URL` and the R2 keys to `""`, so it carries `kalam-db` and `kalam-blobs` with no secret in them. What it costs is honesty — "declared with an empty secret" is weaker than "not declared". Deleting it takes `kalam-db.json`, `kalam-blobs.json`, the `MODE_DB` tasks and `[vars]` copies in `gen-kalam.py`, `kalam.toml.tmpl`'s execution values, and `configs.sh` §1c with them |
| — | A runner on another network | hardware | Everything else about an off-site runner is proven on arm64 in its own compose project; the rehearsal reaches the gate through `host.docker.internal`, a private address, so the one posture `configs.sh` §1e asserts statically — `kalam-api` refusing private addresses — has never refused anything for real |
