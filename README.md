# kalam

Kalam is the TinyBrains match runner. It is an Orion 1.9.0 package (cron channels, workflows,
connectors) shipped inside a runnable image, `ghcr.io/tiny-brains/kalam`: orion-server,
the package, and the Ants engine from one ants release. A runner claims queued matches through
[Soma](https://github.com/Tiny-Brains/soma)'s runner gate, plays them turn by turn on Orion's own
model runtime, and posts the result and the replay back. The same image in its other role, an
**admitting runner** (`RUNNER_ROLE=admit`), admits submissions: Soma runs no model, so every
submission is registered, admitted and played over the game's reference observations on one of
these, and Soma judges the report. Either way it holds no database credential and binds no public
port. Soma owns the schema, admission's verdicts, pairing and ratings,
[ants](https://github.com/Tiny-Brains/ants) owns the rules, and Orion runs the models.

*Kalam* (களம்) is Tamil for the field of contest.

## Quick start

```sh
cp .env.example .env            # fill it in: see Configuration, or Run a runner
docker compose up -d            # KALAM_IMAGE, a release; or tinybrains/kalam:dev and --build
docker compose logs -f runner
docker compose --profile admit up -d   # the deployment's admitting runner, on one machine
```

A healthy boot logs what it derived, generates the package for its role, and hands the applying to
the server:

```text
==> arch arm64
==> engine sha256:…
==> private addresses: false
==> 2 match slot(s), 3 cron workers (one is the roster's)
==> generating the match package
==> compiling the kalam package
==> starting orion-server with /etc/orion/runner.toml.tmpl
```

**`/readyz` answers 503 until the package is serving**, and any failure applying it stops the
container non-zero — a runner that cannot claim is never reported as capacity. If it stops, the
reason is the last error in the log: see [Troubleshooting](#troubleshooting). The admitting runner
(`docker compose logs -f admit`) logs `==> an admitting runner: the tb-admit channel, 1 cron worker,
no match lanes`.

**Against web's local stack** (bring it up first, as [web](https://github.com/Tiny-Brains/web)'s
README says):

1. In `.env`, uncomment the `host.docker.internal` block at the bottom. It points Soma, the models
   bucket and the replay endpoint at this machine, sets `RUNNER_SIG_DIR=../web/keys/signatures`, and
   sets `RUNNER_ALLOW_PRIVATE_URLS=1`.
2. Copy `TB_TRUST_PUBLIC_KEY`, `MODELS_READ_ACCESS_KEY` and `MODELS_READ_SECRET_KEY` from web's `.env`.
3. `RUNNER_KEY`: mint one on the admin Runners page.
4. `ORION_ADMIN_KEY`: `openssl rand -hex 32`.
5. `KALAM_IMAGE`: the release to test (web's `init.sh` prints the newest), then `docker compose up -d`;
   or `KALAM_IMAGE=tinybrains/kalam:dev` and `docker compose up -d --build` for this checkout.

**To run an unreleased engine**, build against an ants checkout's `dist/` with a machine-local
`docker-compose.override.yml` (gitignored), and set `KALAM_IMAGE=tinybrains/kalam:dev` in `.env`:

```yaml
services:
  runner:
    build:
      additional_contexts:
        ants: ../ants/dist
```

## Configuration

Set in `.env`. [`docker-compose.yml`](docker-compose.yml) refuses to start without the required ones.

| Variable | Default | What it is |
|---|---|---|
| `RUNNER_KEY` | required | This machine's credential, minted on the admin Runners screen (`/admin/runners`). Shown once |
| `SOMA_URL` | required | The Soma this runner plays for. Every match statement is a call to its `/v1/runner/*` |
| `MODELS_ENDPOINT` | required | The models bucket, as reached from this machine |
| `MODELS_READ_ACCESS_KEY`, `MODELS_READ_SECRET_KEY` | required | The deployment's read-only key for `models/*`. Every byte fetched is re-hashed against the roster's digest |
| `RUNNER_BLOB_ENDPOINT` | required | The replay bucket's base URL as reached from here. **Must equal Soma's `RUNNER_BLOB_ENDPOINT`** |
| `TB_TRUST_PUBLIC_KEY` | required | The deployment's plugin trust key |
| `ORION_ADMIN_KEY` | required | This node's own admin key (`openssl rand -hex 32`). Never the deployment's |
| `RUNNER_LABEL` | `runner` | How this machine appears on the Runners screen. Two machines on one key are told apart by it |
| `RUNNER_SIG_DIR` | `./keys/signatures` | The deployment's plugin signatures, mounted read-only |
| `KALAM_IMAGE` | required | The image, and so the engine: a release (`ghcr.io/tiny-brains/kalam:<version>`), or `tinybrains/kalam:dev` built from this checkout |
| `MODELS_BUCKET` | `tinybrains-models` | The models bucket's name |
| `RUNNER_CRON_WORKERS` | `2` | Matches at once: the match channel's `concurrency.slots`, at most the four the package ships. Orion's `cron.workers` is this plus one, for the roster |
| `RUNNER_MAX_CACHE_BYTES` | 4 GiB | The on-disk model cache (the `runner-models` volume) |
| `RUNNER_MAX_LOADED_BYTES` | 2 GiB | Model sessions held in memory at once |
| `RUNNER_ALLOW_PRIVATE_URLS` | unset | `1` only against a local stack: lets the connectors reach private addresses |
| `RUNNER_ADMIN_PORT` | `8090` | Loopback port for this node's `/health` and `/metrics` |
| `RUNNER_ARCH` | from `uname -m` | Reported on the Runners screen. Leave it unset |
| `RUNNER_NODE_VERSION` | `dev` | Reported on the Runners screen |
| `ORION_VERSION` | `1.9.0` | Recorded on every match. Must equal the Soma node's `orion_version` |
| `RUNNER_SHUTDOWN_DRAIN_SECS` | `5` | Orion `server.shutdown_drain_secs` |
| `RUNNER_SHUTDOWN_FORCE_SECS` | `2700` | Orion `server.shutdown_force_timeout_secs`: the real bound on a draining match |
| `RUNNER_CRON_SHUTDOWN_SECS` | `2700` | Orion `cron.shutdown_timeout_secs` |
| `RUNNER_STOP_GRACE` | `2760s` | Docker's stop grace. Must exceed drain + force |
| `RUNNER_CPUS`, `RUNNER_MEMORY` | `0` (no limit) | Container limits |
| `ADMIT_CPUS`, `ADMIT_MEMORY` | `2`, `0` | The admitting runner's limits (`--profile admit`). The CPU ceiling keeps it off the cores the runner beside it plays on |
| `ADMIT_ADMIN_PORT` | `8091` | The admitting runner's loopback port |

Compose also sets one value you should not override: `ORION_ADMIN_BEARER` is
`Bearer ${ORION_ADMIN_KEY}`. This machine holds no bucket credential and no database credential.

The node's Orion config is [`docker/runner.toml.tmpl`](docker/runner.toml.tmpl): api mode, the
engine digest (derived by the entrypoint from the component), the model cache, `engine.ops_budget`,
`models.max_timeout_ms` and `models.max_probe_ms`, admission's one timing gate, pinned so every
runner admits a model under the same number. Compose sets `RUNNER_ROLE=admit` on the `admit`
service and nothing on `runner`, whose role is `match`. The terms a match is played under (`turn_ms`, `max_turns`, the lease
and renew interval, the refusal and strike ceilings, the replay and model prefixes) arrive on each
claim from the match's season, and are not configured here.

Build args: `ANTS_RELEASE` (empty means the latest ants release) and `ORION_VERSION` (`1.9.0`).

## Run a runner

A runner is this image on any machine you control: a desk, a Mac mini, a cloud VM. It plays the
same queue by the same claim as every other runner, through Soma's gate. What it holds is a key, a
label and the engine it plays: no connection string, no bucket write credential, and no admin
token for anything but its own loopback API.

### What to copy from the deployment

| What | From | Why |
|---|---|---|
| `RUNNER_KEY` | the admin **Runners** screen, `/admin/runners` | Shown once: Soma keeps only its hash and a display prefix. Minting another is free |
| `TB_TRUST_PUBLIC_KEY` and `keys/signatures/` | the deployment (web's `keys/signatures/`) | A plugin signature belongs to whoever holds the trust key, so neither the image nor this repository carries one |
| `MODELS_READ_ACCESS_KEY`, `MODELS_READ_SECRET_KEY` | the deployment | A key that can only GET `models/*` |
| `SOMA_URL`, `MODELS_ENDPOINT`, `RUNNER_BLOB_ENDPOINT` | the deployment | As reached from this machine |
| `ORION_ADMIN_KEY` | generate it here | `openssl rand -hex 32`. It authorises only this node's own package load and roster clock |

```sh
cp .env.example .env                                    # fill in the table above
mkdir -p keys/signatures
scp <deployment>:web/keys/signatures/* ./keys/signatures/
docker compose up -d
docker compose logs -f runner                           # wait for "==> loaded: …"
```

**For a production deployment** (web's `docker-compose.prod.yml`, R2 behind it), use
[`docker-compose.prod.yml`](docker-compose.prod.yml) with `.env.prod.example` instead:

```sh
cp .env.prod.example .env.prod                          # the same values; one R2_S3_ENDPOINT for both buckets
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

That file never builds, never lets `RUNNER_ALLOW_PRIVATE_URLS` through, runs Orion in production mode
(which refuses an admin key shorter than 32 characters), logs JSON and rotates the log.

### The machine

- **Pin the image.** Set `KALAM_IMAGE=ghcr.io/tiny-brains/kalam:<version>`; compose refuses to start
  without it. The image carries the engine, and a runner whose engine digest is not
  `games.active_engine_digest` claims nothing, for ever, with no error anywhere.
- **The architecture doesn't matter.** The cartridge is wasm32, so an arm64 Mac derives the same
  digest as an amd64 deployment. Images are published for linux/amd64 and linux/arm64.
- **Disable sleep.** Use Energy Saver, or run the stack under `caffeinate -dimsu`. A sleeping host
  stops renewing its leases. The matches lapse and are replayed by another runner, so no work is
  lost, but the machine keeps claiming matches it won't finish.
- **Size the Docker VM** above `RUNNER_MAX_CACHE_BYTES + RUNNER_MAX_LOADED_BYTES` plus the runtime:
  about 8 GiB at the defaults, more if you raise `RUNNER_CRON_WORKERS`. Below that the model cache
  thrashes. Every eviction re-fetches an artifact over the WAN, and it shows only as slowness.
- **Capacity is `RUNNER_CRON_WORKERS`.** That many slots on the one match channel, up to the four the package
  ships, and Orion's pool is one worker larger so the roster clock always has one: with a shared
  pool, long matches skip its ticks, new versions go unregistered, and every lane refuses their
  trials. Start at 2 and watch lease renewals before raising it: each match in flight keeps its
  seats' model sessions in memory. The engine is
  wasm and the models are ONNX on CPU, so cores matter more than clock speed.
- **Bandwidth is small and bursty.** One replay PUT per match (about 58 KiB for a 549-turn match),
  one artifact GET per cache miss (at most 64 MiB, `max_artifact_bytes`), and the idle poll: a token
  exchange and a claim per lane every 5 s.
- **No inbound ports.** 8080 is published on loopback only (`RUNNER_ADMIN_PORT`), for `/health` and
  `/metrics`. Publishing it would expose this node's admin API.

### The admitting runner

`docker compose --profile admit up -d` starts `admit` beside `runner`: the same image, key and
addresses, labelled `<RUNNER_LABEL>-admit` on the Runners screen, with its own model cache. With
`RUNNER_ROLE=admit` it loads `tb-admit` and nothing else. Every 10 s it claims one submission Soma
prepared (`POST /v1/runner/admissions/claim`), registers it on its own node from the registration
Soma rebuilt, lets Orion admit it, plays it over up to 64 reference observations, deletes it, and
reports (`POST /v1/runner/admissions/{id}/report`). Soma decides.

- **One per deployment is enough**, and nothing is admitted while none is up: submissions wait in
  `testing` without spending an attempt. A second one only shares the queue.
- **Put it where matches are fewest.** A probe measured over `max_probe_ms` on a busy machine is sent
  back to be tried again, and three of those expire the submission `TIMED_OUT`. It plays no match,
  so an admission never takes time from one on its own node, and `ADMIT_CPUS` keeps it off the
  runner beside it.
- **Its Orion must be the one Soma's `orion_version` names**: the claim answers 409
  `orion_version_differs` otherwise, on every poll.

### The replay endpoint

One bucket has three addresses, and two of them must be the same string, because SigV4 signs the host:

| Setting | Set on | What it is |
|---|---|---|
| `R2_ENDPOINT` | Soma | Where Soma itself reaches the bucket |
| `RUNNER_BLOB_ENDPOINT` | Soma | The host the gate signs a runner's replay PUT for |
| `RUNNER_BLOB_ENDPOINT` | the runner's `.env` | The base the runner PUTs to. **Must equal the row above** |

If these two differ, every replay PUT fails with `SignatureDoesNotMatch`, a 403 that names neither
setting.

### Reading the Runners screen

| State | Meaning | What to do |
|---|---|---|
| **live** | Authorised and calling in | Nothing |
| **quiet** | Authorised, and silent for longer than a lease | The machine is off, can't reach the gate, or is being rate-limited on the token route (see below). Its in-flight matches are already lapsing |
| **wedged** | Quiet and still holding matches | Revoke the runner so it takes no more. Its rows are reaped on their own |
| **key or owner** | The runner row is fine, but its key is revoked or the key's owner is no longer an admin | Re-grant the owner, or mint a new key |
| **revoked** | Stopped on purpose | Nothing. It stays listed because `matches.played_by` points at it |

The screen also warns when live runners disagree about the engine digest or the Orion version.
Both disagreements are silent everywhere else.

**Revoking a key** stops every machine that uses it. **Revoking a runner** stops only that machine.
Both take effect within one token lifetime (ten minutes), because every `/v1/runner/*` call carries
a short-lived token rather than the key.

### Several runners behind one address

Each cron run exchanges the key for a fresh token: measured at about 0.89 exchanges/s per idle runner.
`POST /v1/runner/token` is rate-limited per source address, and a NAT is one address:

```text
runners behind one address  ≈  Soma's runner_token_rate / 0.89   (about 33 at 30 rps)
```

Past that, the gate answers 429, the run ends without an error trace, and the machine shows **quiet**
while it is plainly switched on.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Boots healthy, never claims a match | The engine digest (the `==> engine` boot line) is not the one Soma declared, because the image was built from another ants release | Pin `KALAM_IMAGE` to an image built from the deployment's ants release. Its `dev.tinybrains.ants.engine` label names the digest |
| `failed to apply at startup: … is not serving on this node` and the container stops | The reload quarantined something the package carries, so the node would have served without it | Read the WARN lines above: each names the member and why. A missing or wrong signature (`RUNNER_SIG_DIR`, `TB_TRUST_PUBLIC_KEY`) quarantines the engine this way |
| `activation stopped at workflows '…': connector(s) … not found` | A connector was skipped at load, so no workflow naming it could activate | A connector needs **every** `env://` it names to resolve to something well-formed. Since Orion 1.9.0 an EMPTY value is refused too ("uses no scheme"), so the `db`-mode placeholders are URLs that route nowhere, not empty strings |
| Replay PUT 403 `SignatureDoesNotMatch` | `RUNNER_BLOB_ENDPOINT` differs from Soma's | Make them the same string |
| Shown as **quiet** or **key or owner**, or never appears | The token exchange is refused: 401 (the key is revoked or unknown, or its owner is no longer an admin) or 429 (too many runners behind one address) | Mint a new key or re-grant the owner. For 429, raise Soma's `runner_token_rate` or spread the machines across addresses |
| Matches are claimed and handed back; rows eventually fail `MODEL_UNAVAILABLE` | This node can't serve a seat's model, because its roster clock hasn't registered and activated it | Check `MODELS_ENDPOINT`, `MODELS_BUCKET` and the read key. Against a local stack, also check `RUNNER_ALLOW_PRIVATE_URLS=1` |
| Calls to this node's admin API get 401 | `ORION_ADMIN_BEARER` must be the whole `Bearer <key>` header | Leave it as compose sets it |
| Rows stay `running` after a restart | The drain was cut short: Docker's grace period ran out before Orion's | Keep `RUNNER_STOP_GRACE` above drain + force. The rows are reaped when their lease lapses |
| Submissions stay `testing` (phase `queued`) | No admitting runner is up, or its claim is refused | Start one with `--profile admit`. A 409 `orion_version_differs` in its log means its image is not on Soma's Orion |
| A submission expires `TIMED_OUT` | Every attempt's report decided nothing: the runner could not fetch it, ran out of time, measured the probe over `max_probe_ms`, or an inference failed outright | `admissions.requeued_for` names the last reason |

## Developing the package

Everything is authored JSON and committed: `workflows/`, `channels/`, `connectors/` and `shared/`.
There is no generator and no build step — `orion-server compile` resolves `$from`, `$use` and
`$each` into what the admin API accepts. The package ships no SQL: every statement a runner needs
is a call to Soma's gate. The per-seat tasks are written ONCE, over
`constants.seats`. [CLAUDE.md](CLAUDE.md) has the rules and the Orion gotchas.

| Command | What it does | Needs |
|---|---|---|
| `./scripts/check-defs.sh` | `orion-server lint`, `clippy`, `fmt --check` over the whole set, and `clippy -c docker/runner.toml.tmpl` (all `--deny-warnings`) | `orion-server` in `shared/package.json`'s range, on `PATH` |
| `docker compose up -d --build` | Builds this checkout and runs it as a runner | Docker and a Soma |
| `ORION_ADMIN=… ORION_ADMIN_API_KEY=… ./scripts/load-package.sh [--prune]` | Shapes this role's package, compiles it and applies it into a running node; `--prune` retires what the applied version carried and this one does not. `--compile-only -o <file>` stops after compiling, which is what `entrypoint.sh` calls at boot | `orion-server` |

- If the host's `orion-server` is older than 1.8, lint with the image's copy:
  `docker run --rm --entrypoint orion-server ghcr.io/tiny-brains/kalam clippy /pkg/kalam --deny-warnings`.
- Lint checks the engine calls too when the engine is present in the gitignored `plugins/tb-ants/`:
  copy `tb-ants.wasm`, `plugin.toml`, `plugin.json` and `cartridge.json` there from an ants release
  archive or from `../ants/dist/`.
- There are no unit tests. Lint is the compiler, and it does not exercise leases or turns. A real match needs a Soma: web's stack plus this runner. `tinybrains conform` re-runs a
  replay locally and diffs every field and every turn.
- After any change, rebuild (`docker compose up -d --build`). A runner started from an older image
  is still running the old package.

## Releasing

```sh
gh workflow run release.yml                   # rehearsal: builds both platforms, pushes nothing
git tag vX.Y.Z && git push origin vX.Y.Z      # from main: publishes the image
```

- A `v*` tag on main publishes `ghcr.io/tiny-brains/kalam:X.Y.Z`, `:X.Y` and `:latest` for
  linux/amd64 and linux/arm64 ([`.github/workflows/release.yml`](.github/workflows/release.yml)).
  The workflow checks that both platforms carry the same package and engine.
- The ants release is chosen once per build: the latest, or the repository variable `ANTS_RELEASE`.
  The image is labelled `dev.tinybrains.ants.release` and `dev.tinybrains.ants.engine`. **An image
  on a new engine is an engine cutover.** It claims nothing until Soma declares that engine, so pin
  `ANTS_RELEASE` under a live season.
- Never re-cut a tag. Runners pin versions.
- When the engine changes, re-sign with web's `scripts/setup/sign-plugins.sh` and give every runner
  the new signatures. Otherwise the boot apply stops it on a quarantined engine.

## Layout

```text
connectors/                    authored connector definitions
  kalam-api.json               Soma's runner gate; URL from KALAM_API_URL at load
  kalam-orion.json             this node's own admin API, where its model set lives
  kalam-models.json            the models bucket, read-only
  kalam-blobs-put.json         the replay PUT to a presigned URL; base URL from RUNNER_BLOB_ENDPOINT
shared/kalam.json              shared constants: clock tracing, the channels' configs, the token call
shared/package.json            the package's name and the Orion range it needs
workflows/                     tb-match-run, tb-roster-run, tb-admit-run -- authored, with the
                               per-seat tasks written once over constants.seats with $each
channels/                      tb-match (its slots are the matches at once), tb-roster, tb-admit
scripts/
  check-defs.sh                no-stack gate: clippy (which gates on lint), fmt, clippy -c
  load-package.sh              shape this role's package (which channels, how many slots),
                               compile it, apply it; --prune retires what a version dropped.
                               entrypoint.sh calls it with --compile-only at boot
docker/
  entrypoint.sh                derive arch, engine digest and role, migrate, compile this
                               node's package via load-package.sh, exec orion-server (which
                               applies it and holds /readyz until it serves)
  runner.toml.tmpl             the runner's Orion config
Dockerfile                     the runner image: orion-server, the package, the engine from an ants release
docker-compose.yml             one runner service, and `admit` under --profile admit
docker-compose.prod.yml        the same runner for a production deployment
.env.example                   a runner's settings
.env.prod.example              a production runner's settings
.github/workflows/release.yml  v* tag → ghcr.io/tiny-brains/kalam
plugins/tb-ants/               optional local engine for lint (gitignored)
```

## Invariants

- **Kalam writes execution columns only, and no longer holds a credential to write them with.**
  Every write is a gate call, and the gate's own role (`runner_gate`) is granted claim, lease,
  result and replay columns and nothing else. Soma's `scripts/check-sql.sh` fails if that role --
  or the now-unused `kalam` role the migrations still create -- can write `matches.rated_at`, read
  `ratings`, or read a verdict column of `model_versions`. A wider grant would let a runner move
  the ladder.
- **Every write is fenced on the claim token.** Start, renew, release and finish all carry
  `claim_token`, so a runner whose lease was reaped writes nothing. The replay key names the attempt
  (`<replay_prefix>/<match>/<claim_token>.json`), so a stale attempt's blob is an orphan, not a
  replacement.
- **The engine digest decides what is claimed.** It is derived from the component in the image and
  must equal `games.active_engine_digest`. A mismatch claims nothing, silently.
- **One Orion state per runner, never cluster mode.** Each lane's `forbid` key is local. Shared state
  would make each lane a fleet-wide singleton: one runner plays and the rest idle, all looking
  healthy.
- **A seat this node can't serve is released, never played.** Before a match starts, each seat's
  model is checked against this node's admin API. A seat whose model is not `active` sends the row
  back with a refusal spent. The row fails `MODEL_UNAVAILABLE` only when the ceiling is spent AND it
  has waited Soma's `refusal_grace_secs` since it was paired, since a count of claims alone is spent
  in seconds by the lanes on a new trial. Trials are claimed before ranked matches, and within each
  kind a refused row after the fresh ones.
- **The roster always has a worker.** Orion's pool is the match slots plus one, and only
  `RUNNER_CRON_WORKERS` lanes load, so no number of long matches can skip a roster tick.
- **A runner has one role.** A match runner loads no `tb-admit`, and an admitting runner loads
  nothing else, so an admission never shares a node with a match.
- **An admitting runner reports and never decides.** It registers what Soma rebuilt, never the
  competitor's manifest, deletes what it registered, and sends Orion's record as it answered.
- **Game state stays opaque.** No workflow interprets `wave_state`, an observation, an action or a
  ref. The policy head is the one tensor the platform reads, because a manifest cannot decode it.
- **A match's terms come from its season, on the claim.** That includes the strike ceiling, which is
  read off the match row. A copy in runner config is dead config that a later edit would wire back in.
- **`engine.ops_budget` equals Soma's `adapter_ops_max`.** Otherwise a model is admitted under one
  ceiling and struck under another.
- **`models.max_timeout_ms` is at least the season ceiling for `turn_ms` (60000).** Orion silently
  clamps `model_infer`'s deadline to it.
- **The drain order holds.** Keep `stop_grace_period` (2760 s) above drain + force (5 + 2700 s), and
  force at or above `cron.shutdown_timeout_secs`. Both must be above the match channel timeout
  (2400 s). If this breaks, rows stay `running` with a live lease until the reap frees them.

## Known gaps

- A runner on another network is untested. Every rehearsal reached the gate through
  `host.docker.internal`, a private address, so `kalam-api` refusing private addresses has never
  refused anything for real.
- Mixed-engine rollout, where runners on two digests drain and claim past each other, has never been
  exercised.
- `tb-match-run` keeps sweeping to its loop max (1010) after the match finishes, with every task
  skipped.
- A new version's first registration logs an ERROR: the roster's existence check is a GET that
  404s, and so is the barrier's check while a claimed trial waits for it. Orion's `http_call`
  has no accepted-status option, and the model list is paginated, so both stay per-model GETs.
- Orion's `Message.audit_trail` keeps old and new values for every task execution, and there is no
  setting to turn it off.
- Every cron run mints a ten-minute token and uses it once. A longer-lived token would halve an idle
  runner's calls and lift the per-address runner limit.
- `match_concurrency` in the runner config is read by nothing, and in api mode neither is
  `refusal_ceiling`.
- An admitting runner's idle poll is a token exchange and a claim every 10 s, like a lane's.
- Every admission runs twice on the admitting runner: Orion queues one when a model is
  registered and `admit?wait=true` runs another inline, and registration has no way to skip the
  queued one. When the inline one fails fast, `tb-admit` deletes the model before the queued one
  finishes, which logs `Model admission could not be recorded` at ERROR.
- `tb-admit-run` plays one observation a sweep, so a claim carrying more than `ADMIT_LOOP_MAX` (64)
  would stop at the loop's end without a report. Soma's `admit_observations` is held to it by web's
  `configs.sh`.
- The `db`-mode branch is still in the package as a rollback. CLAUDE.md has the removal checklist.

## License

Apache-2.0. See [LICENSE](LICENSE).
