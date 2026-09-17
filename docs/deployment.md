# Deployment and scaling — Kalam

A Kalam replica's half of the platform's deployment page: why it keeps its own Orion state, what bounds
a draining match, the two numbers a fleet is sized by, its model roster, and a runner on a desk.

> **Moved from `devops/docs/` on 17 September 2026**, when devops stopped running anything (N25).
> The page was split by the repository each section is about, and every section keeps the number the
> whole page gave it, so a citation of *§11* still resolves — in the repository that section now
> lives in. §1–§3, §4.1, §5, §8.1, §9, §10 and §12–§17 — the fleet, Soma's cluster mode, the autoscaler, the
> models bucket, the deploy order, the roles, security and retention — are [soma's](https://github.com/Tiny-Brains/soma/blob/main/docs/deployment.md).
> It was written before N25: where it describes in-cluster Kalam replicas, a loader, an Orion image
> devops built or `docker-compose.fleet.yml`, the platform now runs a Soma node image, runners from
> kalam's compose file, and web's `docker-compose.yml`.

### 4.2 Kalam: single instance, and that is the correctness half

**A Kalam replica must have its own Orion state, and the reason is the same sentence that makes
cluster mode right for Soma's clocks.** `forbid` on a cron channel is a row in the state database. Kalam's
`tb-wave` channel is `{"policy": "forbid", "key": "wave"}` — one wave in flight per replica, which
is what K bounds and what the loader's residency budget is sized for. Put N replicas over one shared
state and that row becomes fleet-wide: **exactly one replica would ever run a wave**, and the other
N−1 would poll every five seconds and find the singleton held. The fleet would scale to zero
throughput and every replica would look healthy.

So local SQLite per replica is not a saving over a managed Postgres. It is what makes each replica
its own scheduler.

Nothing is lost by it, because **Kalam's fence is not Orion's**. Its mutual exclusion is the
database claim on `matches` — `FOR UPDATE SKIP LOCKED` over the partial index, leased, with the
lapse and reap of the match table's claim and lease ([`soma/docs/schema.md`](https://github.com/Tiny-Brains/soma/blob/main/docs/schema.md) §4) — and that is shared state of the only kind Kalam needs. Orion's
occurrence lease would recover a dead node's *occurrence*; the match lease recovers its *rows*, which
is the thing that matters, and it already works.

```toml
# kalam.toml.tmpl — no [cluster] block at all
[storage]
url = "sqlite:/var/lib/orion/state.db"
```

**The consequence that must be built for**: every replica needs the `kalam` package loaded into its
own state before it can play, so §8.2's loader is an init step on every replica and not a one-shot
for the fleet.

**The open ask this resolves.** The standing ask to Orion for a per-channel, per-node cron
concurrency cap is no longer needed for correctness — per-node is what a replica with its
own state already has. It would only be needed if a replica ever wanted *more than one* wave in
flight, which K exists to avoid. Downgrade the ask; do not withdraw it.

---

## 6. Drain, and which number actually bounds a wave

### 6.1 The correction

[`orion-notes.md`](https://github.com/Tiny-Brains/soma/blob/main/docs/orion-notes.md) §4 records, from the build: *"Drain is three numbers and
the smallest wins"*, and Kalam's drain ([`kalam/docs/design.md`](https://github.com/Tiny-Brains/kalam/blob/main/docs/design.md) §5)'s banner says all three must exceed the longest match. **The
measurement was right and the rule drawn from it is wrong**, which matters because the rule tells an
operator to raise a number that costs them and leaves the one that would have helped at its default.

Read out of `orion-server` 1.7.0's `main.rs`, the shutdown is:

1. `/readyz` flips to `503`.
2. The HTTP server **sleeps for `server.shutdown_drain_secs`** — `tokio::time::sleep(drain).await`,
   an unconditional fixed period, not a maximum.
3. Accepting stops; in-flight *requests* get up to `server.shutdown_force_timeout_secs`.
4. Then, and only then, the supervised background tasks are stopped —
   `tasks.shutdown(Duration::from_secs(config.server.shutdown_force_timeout_secs))`.
   **The cron worker is one of those tasks.**
5. Inside that, the cron worker's own drain waits for in-flight occurrences under
   `cron.shutdown_timeout_secs`.

So the bound on a draining wave is `min(cron.shutdown_timeout_secs,
server.shutdown_force_timeout_secs)` — an **inner** deadline under an **outer** one, and
`cron.shutdown_timeout_secs` can only ever make it shorter. Layer 03's config sets cron to 2 700 and
the build left the server keys at their 30 s defaults, and measured a 30 s drain. That is not three
numbers racing; it is the outer deadline doing exactly what it says.

### 6.2 What that buys Kalam

**`shutdown_drain_secs` buys a Kalam replica nothing and costs it everything.** It exists so a load
balancer's in-flight requests survive its own poll interval. Kalam binds no route and sits behind no
balancer. It is also the one number that is paid unconditionally — an idle replica pays it in full —
so setting it to the longest match, as the folk rule implies, makes **every** scale-down cost a
match duration whether or not a wave is in hand.

| Number | Kalam | Soma | Why |
|---|---|---|---|
| `server.shutdown_drain_secs` | **5** | **30** | fixed and paid always. Kalam has no rotation to leave; Soma does |
| `server.shutdown_force_timeout_secs` | **2 700** | **30** | **the real bound on the wave.** Above `channel_timeout_ms` (2 400 s) |
| `cron.shutdown_timeout_secs` | **2 700** | **60** | the inner deadline; equal to the outer on Kalam so neither surprises |
| orchestrator grace | **2 760** | **90** | above `drain + force`, as Orion's own checklist requires |

**A scale-down costs one wave, not one timeout.** The drain ends when the wave ends; the timeout is
only the cap. Measured waves are 3.8–9.1 s for ~150 turns, so the expected cost of shedding a
replica is seconds, and 2 700 is the pathological match nobody has seen. The risk register's "a
scale-down takes a match duration per replica" is the worst case, not the case.

**And a wave that does outlive the cap is not lost.** Orion cancels the attempt and *deliberately
leaves the claim and singleton rows to expire* rather than releasing them eagerly — the same safety
window Kalam's own lease uses. The rows lapse, the next claim reaps them, and they are replayed from
turn zero at the cost of one attempt. That is the design working, not a failure.

### 6.3 The entrypoint

Layer 03's banner records the trap and its fix, and it must survive into every replica image:
`trap 'kill -TERM $PID'; wait $PID` returns *when the trap fires*, so the script falls off its end
and the container exits while Orion is still draining — measured at `docker stop -t 300` returning in
zero seconds with two rows still `running`. **Wait again, in a loop, until the process is gone.**
Kalam's image has no trap to get wrong: `docker/entrypoint.sh` execs orion-server, so SIGTERM reaches it directly and `init: true` in the compose file reaps the self-load fork.

---

## 7. The two deployment numbers

### 7.1 Decision 5 — K, and what replaced it

**SUPERSEDED, 14 September 2026 (decision R7). There is no K.** A replica runs four `tb-match`
channels, each claiming ONE row, so its concurrency is a channel count rather than a claim width.
The wave existed to amortise one batched `/play` call across many seats, and every Ants map is
two-player: the batching was worth a measured 1.11x at 128x128.

What the old bound taught still holds in a new shape. It was:

```
K × seats_per_match × max_class_bytes  ≤  the replica's memory for weights
```

and the residency barrier is what enforced it. The barrier is gone; Orion's session cache is an LRU
bounded by **`models.max_loaded_bytes`** (2 GiB on the shipped template), which *evicts* rather than
refusing — so the failure mode changed from "rows go unplayable five refusals later" to "a cold load
on a match that needed an evicted model", which costs milliseconds. The number to size is therefore
the same number, and it is now a config key rather than a claim width:

```
match_channels × seats_per_match × max_class_bytes   sets how hard the LRU is worked
```

At four channels, two seats and a `large` class of 64 MiB that is 512 MiB of hot weights against a
2 GiB cache — comfortable. A fleet serving large models on small replicas lowers the channel count
or raises `max_loaded_bytes`; neither is a schema change any more.

**Blast radius is smaller too**: a replica death replays the matches actually in flight (four), not
K, and each costs one lapse of three rather than an attempt against `refusal_ceiling`.

### 7.2 Decision 25 — the poll interval

**Taken in shape, and its constant is still owed.** The interval is bounded on two sides:

- **Below, by claim load.** N replicas at interval *i* issue N/*i* claims per second, each one
  statement against the partial index on `pending`. At N = 20 and i = 5 s that is 4 per second,
  which is not a load question. It becomes one somewhere, and **where is exactly what the
  claim-under-load spike measures** — several pollers against the partial index with
  a table of finished rows behind it, at N per second, against the seats-table joins of 01 §4.2.
- **Above, by latency.** A newly paired row waits up to *i* before any replica sees it, and that
  wait is on the front of every trial — the thing a competitor is watching on the Version screen.

**5 s stands**, and the rule for moving it was: raise it only when the spike says the claim rate is a
measurable fraction of database capacity, and then raise it rather than adding replicas to absorb
it.

**The spike has now run** — `scripts/check/claim-load.sh`, against a scratch copy of the real database
so the index statistics and row widths are the real ones.

> **Re-measured 16 September 2026, and the previous numbers here were measured against a statement
> that does not ship.** `scripts/check/claim.sql` still held the pre-R7 two-CTE *wave* claim, with
> resident-weights affinity and `LIMIT 16`, months after the one-row claim replaced it — so the
> figures below replace, rather than confirm, the ones this paragraph used to carry. They are now
> the shipped claim, including the `live_runners` join and the in-flight ceiling the runner gate
> added. The harness could not run at all while the stack was up, for two further reasons the same
> pass fixed; `README.md`'s Status block has them.

| clients | idle queue | at `pair_depth_target` (64) |
|---|---|---|
| 1 | 1,729 claims/s · 0.58 ms | 1,431 claims/s · 0.70 ms |
| 8 | 7,494 · 1.07 ms | 6,614 · 1.21 ms |
| 32 | 11,585 · 2.76 ms | 7,888 · 4.06 ms |
| 64 | 12,369 · 5.17 ms | 7,306 · 8.76 ms |

**Queue depth still does not bound the claim** — a single claim costs the same against an empty
table as against a full one, which is the partial index on `pending` doing what it was shaped for.
What has changed since the wave is that depth now costs something *under concurrency*: at 64 clients
a deep queue sustains 7,300/s against 12,400/s idle, because that is where `SKIP LOCKED` actually
has rows to skip and each claimer walks past the locked head of the index. That is contention doing
its job, not a regression, and the floor it sets is the number the interval is judged against.

So the fractions, against the 1,431/s floor: 20 replicas at 5 s is 0.28% of it, 100 replicas at 5 s
is 1.4%, and even 100 replicas at a 1 s interval is 7.0%. **Claim load does not bound the interval at any fleet
size this design contemplates.** What bounds it is the other side — a newly paired row waits up to
*i* before any replica sees it, and that wait is on the front of every trial. The decision is
closed at 5 s, and the honest statement of the bound is that it is a latency choice and was never
going to be a load one. Re-run the spike if the table grows by orders of magnitude.

**One thing worth noting**: the interval is per replica, so the claim rate rises with the fleet
exactly when the queue is deepest and each claim is most likely to return rows. The pathological
case is the opposite one — a large idle fleet polling an empty queue — and the autoscaler's floor
`$6` is what keeps that fleet small.

---

### 8.2 A roster per replica, and why no clock calls one

Admission happens on the Soma node: `tb-admit` registers through the `soma-node-admin` connector,
which points at **that node's own admin API**, and archives the model again when the verdict is in —
so the admission node's active set is normally empty and a generation build recompiles no
competitor's adapter.

Every Kalam replica keeps its **own** registry, because a model is a state-database entity and each
replica has its own state database (decision 41). `tb-roster` reconciles it from `model_versions`
every fifteen seconds: register what is missing, activate what has passed, one step per version per
tick. **No clock ever calls a replica** — there is no replica list anywhere outside the loader's
`KALAM_ORION_ADMINS`, and a new replica catches up by itself.

**The package load is still an init step on each one** (§4.2): each replica has its own SQLite
state, so `kalam/scripts/load-package.sh` runs against `localhost:8080` before the replica is
useful.

**The failure mode this creates, and the health check that closes it.** Orion's `/readyz` goes green
as soon as the first generation publishes — a replica whose package load failed is *ready*, has no
`tb-match` channels, claims nothing, and is **invisible capacity**: the autoscaler counts it, the
ladder does not. Nothing errors. So a replica's readiness gate is not `/readyz`, it is
`/health` asserting `tb.ants` is loaded and the `tb-match` channels are present and not
quarantined.
Without that check the fleet can scale up into an empty ladder and every dashboard stays green.

---

## 11. A runner on a desk

A **runner** is a Kalam replica on hardware the deployment does not own. It plays the same queue as
an in-cluster replica and by the same claim, and the only difference is where the eight match
statements run: in the cluster they run on the `kalam` role over Postgres, and out here they run on
the gate over `/v1/runner/*`. That is the whole of it. There is no second protocol and no second
kind of match — `tinybrains conform` re-simulates a runner's replay to an identical action stream,
turn for turn, which is the only test that can tell a match that was *played* differently from one
that was merely *recorded* differently.

**What the machine holds.** A key, a label, and the engine it plays. No connection string, no
bucket write credential, no admin token for anything but its own loopback API. That is not a
posture statement, it is the file: `compose/orion/runner.toml.tmpl` has nowhere to put one.

### 11.1 What the operator copies

Four things, and three of them come from the deployment.

| | Where from | Why it cannot be baked in |
|---|---|---|
| `RUNNER_KEY` | the admin **Runners** screen, `/admin/runners` | Shown once: the row stores a sha256 and an eight-character display prefix. Minting another is free. |
| `TB_TRUST_PUBLIC_KEY` and `keys/signatures/` | the deployment | A plugin signature belongs to whoever holds the trust key. The package image is shared by every deployment that pulls the tag, so it cannot carry one. |
| the three addresses | the deployment | The gate, the models bucket, and **the replay endpoint the gate signs for** — see 11.3. |
| `ORION_ADMIN_KEY` | generated **here**, `openssl rand -hex 32` | It authorises this machine's own package load and its roster clock's calls to `127.0.0.1`. It is not the deployment's key and must not be. |

```sh
cp .env.runner.example .env          # then fill it in
scp <deployment>:web/keys/signatures/* ./keys/signatures/
docker compose -f docker-compose.runner.yml up -d
docker compose -f docker-compose.runner.yml logs -f
```

A healthy first boot says four things, in order: the architecture it derived, the engine digest it
derived, that it loaded its package, and **how many channels are active**. The last one is the one
that matters — see 11.5.

### 11.2 The machine itself

- **Disable sleep.** Energy Saver, or run the stack under `caffeinate -dimsu`. A sleeping host does
  not crash: it stops renewing, its lease lapses, and the reap clock hands the match to somebody
  else. The work is not lost, but the machine spends its night claiming matches it will not finish.
- **Size the Docker VM above `RUNNER_MAX_CACHE_BYTES + RUNNER_MAX_LOADED_BYTES`.** The defaults are
  4 GiB of on-disk model cache and 2 GiB resident, so **6 GiB plus the runtime** — call it 8 GiB,
  and more if `RUNNER_CRON_WORKERS` is above 2. Under that the cache thrashes: every eviction is a
  fresh fetch of an artifact over the WAN, and nothing reports it as anything but slowness.
- **No inbound ports.** A runner binds no route; it makes calls. `docker-compose.runner.yml`
  publishes 8080 on **loopback only**, and that is for reading `/health` and `/metrics` locally.
  Publishing it would publish this machine's admin plane.
- **Bandwidth is small and bursty.** One replay per match — 58 KiB for a 549-turn Ants match — plus
  one artifact fetch per cache miss, bounded by `max_artifact_bytes` at 64 MiB. Steady state is the
  poll, which is two small calls per channel per five seconds.
- **Cores, not clock.** The engine is wasm and the models are ONNX on CPU. `RUNNER_CRON_WORKERS`
  is the capacity knob; start at 2 and watch lease renewals before raising it, because each
  in-flight match holds two model sessions resident.

### 11.3 Pin `KALAM_REF`, and the three addresses

**`KALAM_REF` decides which engine this machine plays**, because the digest is derived from the
component inside that image. A `:dev` tag that moves under a live season gives the runner a digest
that matches nothing, and then it claims nothing, **for ever**, while looking perfectly healthy —
there is no error anywhere in that failure. Pin the tag to the one the deployment declared onto
`games.active_engine_digest`.

**The cartridge is `wasm32`, so the digest does not vary by architecture.** That is what lets an
arm64 Mac play in an amd64 deployment's ladder, and `scripts/check/configs.sh` §1j asserts the
component really is wasm rather than trusting it.

**One bucket has three addresses and two of them must be the same string.** SigV4 signs the host:

| | Who sets it | What it is |
|---|---|---|
| `R2_ENDPOINT` on the **gate** | deployment | where Soma itself reaches the bucket |
| `RUNNER_BLOB_ENDPOINT` on the **gate** | deployment | the host the gate **signs a runner's replay PUT for** |
| `RUNNER_BLOB_ENDPOINT` on the **runner** | operator | the base the runner PUTs to — and it must equal the line above |

Get the last two out of step and every replay PUT is `SignatureDoesNotMatch`: a 403 naming neither
setting, on a machine nobody is watching. `configs.sh` §1k checks that both read the same variable;
that the *values* agree across two hosts is the deployment's to hold.

### 11.4 Reading the Runners screen when something is wedged

`/admin/runners` is the answer to *which machine*, which is the whole reason `matches.played_by`
exists. It separates two questions that are easy to conflate:

- **live** — the runner row, its key, and the key's **owner** are all in good standing. Demote the
  owner and every machine on their keys stops at its next call. This is authorisation.
- **calling in** — `last_seen_at` has moved within the last lease period. This is presence.

So the states, and what each one means to do:

| State | What it is | What to do |
|---|---|---|
| **live** | authorised and calling in | nothing |
| **quiet** | authorised, silent past a lease | the machine is off, unreachable, or **being rate limited on the token route** (11.6). Its in-flight work is already lapsing. |
| **wedged** | authorised, silent, **and still holding matches** | the rows will be reaped on their own. Revoke the runner to stop it taking more; that does not cancel what it holds. |
| **key or owner** | the row is fine, its key or the key's owner is not | re-grant the owner, or mint a new key |
| **revoked** | stopped deliberately | nothing; it stays listed because `played_by` still points here |

The screen also warns when the machines that are calling in **disagree about the engine digest or
the Orion version**. Both disagreements are silent everywhere else: a wrong digest claims nothing,
and a wrong Orion version is a match recorded against one runtime and admitted against another,
which is exactly what a re-validation sweep looks for.

**Two revokes, and they are not the same.** Revoking a **key** stops every machine on it; revoking
a **runner** stops one and leaves the others. Both take effect within one token lifetime — ten
minutes — because every `/v1/runner/*` call carries a short-lived token rather than the key.

### 11.5 A runner that installed its package and still plays nothing

There is no loader out here, so the runner loads its own package at boot. `/readyz` answers 200 as
soon as the server is up, and the plugin list can show `tb.ants` — and the machine can still be
completely inert, because `package apply` **activates in dependency order and stops at the first
workflow it cannot activate**, leaving every channel after it a draft. A draft cron channel holds
no schedule.

That is not hypothetical; it is how this file was written. The cause is worth knowing:

> An unresolvable `env://` makes Orion **skip** that connector rather than fail the package — but a
> workflow naming a skipped connector cannot activate. So a variable that is **absent** and one
> that is the **empty string** behave completely differently: empty resolves, the connector loads
> and is simply useless, and the workflows activate. `KALAM_DB_URL`, `R2_BUCKET`, `R2_ACCESS_KEY`
> and `R2_SECRET_KEY` are set to `""` in `docker-compose.runner.yml` for exactly that reason, and
> **all four** of them, because a connector needs every `env://` it names to resolve, not most.

So the entrypoint asserts the **active channel count**, not the plugin, and kills the server rather
than leaving a node that is counted as capacity by everything that looks and claims nothing. An
earlier version asserted `tb.ants` and passed on a runner whose five channels were all drafts.

### 11.6 How many machines fit behind one address

Not a database question. `scripts/check/claim-load.sh` measures the claim statement at ~1,200/s
against a real dataset, and a fleet of a thousand at a five-second poll uses ~16% of that. The
binding constraint is somewhere else entirely.

An `api`-mode runner makes **two** calls where an in-cluster replica makes one statement: every
cron run is a fresh workflow execution carrying no state, so each one mints a ten-minute token and
uses it exactly once. Measured on a live stack over 120 seconds with two idle runners: **214 token
exchanges against 214 authenticated calls**, 0.89/s per runner.

`POST /v1/runner/token` is rate limited **on the caller's address** and cannot be limited on
anything else — it is the route that establishes the principal. So its limit is a ceiling on
machines per **source address**, and several machines in one office are one address:

```
runners behind one NAT  =  token route rps  /  0.89
```

It shipped at 5 rps, which is **fewer than six machines**, and the refusal is silent: a 429 leaves
the token unset, the run ends at `noauth` with outcome `no_token`, and the channel traces
`errors_only`. The limit is now `runner_token_rate`, equal to `runner_rate` at 30 rps, which is
about **33 machines per address**. The real repair is not minting a ten-minute credential for one
call; until that lands, this is the number to raise, and the symptom to watch for is a machine
going **quiet** on the Runners screen while its power light is on.

---
