# kalam

Kalam is the TinyBrains match runner. It is an Orion 1.8.1 package (cron channels, generated
workflows, connectors) shipped inside a runnable image, `ghcr.io/tiny-brains/kalam`: orion-server,
the package, and the Ants engine from one ants release. A runner claims queued matches through
[Soma](https://github.com/Tiny-Brains/soma)'s runner gate, plays them turn by turn on Orion's own
model runtime, and posts the result and the replay back. It holds no database credential and binds
no public port. Soma owns the schema, pairing and ratings, [ants](https://github.com/Tiny-Brains/ants)
owns the rules, and Orion runs the models.

*Kalam* (களம்) is Tamil for the field of contest.

## Quick start

```sh
cp .env.example .env            # fill it in: see Configuration, or Run a runner
docker compose up -d            # ghcr.io/tiny-brains/kalam:latest; add --build to run this checkout
docker compose logs -f runner
```

A healthy boot logs the architecture and engine it derived, then the self-load, and ends with:

```text
==> arch arm64
==> engine sha256:…
==> loading the kalam package into this node
==> loaded: tb.ants is live and <n> channels are active, this node can claim
```

If it ends with `self-load: …` instead, the container stops on purpose: see
[Troubleshooting](#troubleshooting).

**Against web's local stack** (bring it up first, as [web](https://github.com/Tiny-Brains/web)'s
README says):

1. In `.env`, uncomment the `host.docker.internal` block at the bottom. It points Soma, the models
   bucket and the replay endpoint at this machine, sets `RUNNER_SIG_DIR=../web/keys/signatures`, and
   sets `RUNNER_ALLOW_PRIVATE_URLS=1`.
2. Copy `TB_TRUST_PUBLIC_KEY`, `MODELS_READ_ACCESS_KEY` and `MODELS_READ_SECRET_KEY` from web's `.env`.
3. `RUNNER_KEY`: mint one with web's `scripts/dev/runner-key.sh`.
4. `ORION_ADMIN_KEY`: `openssl rand -hex 32`.
5. `docker compose up -d --build`.

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
| `KALAM_IMAGE` | `ghcr.io/tiny-brains/kalam:latest` | The image, and so the engine. **Pin a version under a live season** |
| `MODELS_BUCKET` | `tinybrains-models` | The models bucket's name |
| `RUNNER_CRON_WORKERS` | `2` | Matches at once, across all lanes (Orion `cron.workers`) |
| `RUNNER_MAX_CACHE_BYTES` | 4 GiB | The on-disk model cache (the `runner-models` volume) |
| `RUNNER_MAX_LOADED_BYTES` | 2 GiB | Model sessions held in memory at once |
| `RUNNER_ALLOW_PRIVATE_URLS` | unset | `1` only against a local stack: lets the connectors reach private addresses |
| `RUNNER_ADMIN_PORT` | `8090` | Loopback port for this node's `/health` and `/metrics` |
| `RUNNER_ARCH` | from `uname -m` | Reported on the Runners screen. Leave it unset |
| `RUNNER_NODE_VERSION` | `dev` | Reported on the Runners screen |
| `ORION_VERSION` | `1.8.1` | Recorded on every match. Must equal the Soma node's `orion_version` |
| `RUNNER_SHUTDOWN_DRAIN_SECS` | `5` | Orion `server.shutdown_drain_secs` |
| `RUNNER_SHUTDOWN_FORCE_SECS` | `2700` | Orion `server.shutdown_force_timeout_secs`: the real bound on a draining match |
| `RUNNER_CRON_SHUTDOWN_SECS` | `2700` | Orion `cron.shutdown_timeout_secs` |
| `RUNNER_STOP_GRACE` | `2760s` | Docker's stop grace. Must exceed drain + force |
| `RUNNER_CPUS`, `RUNNER_MEMORY` | `0` (no limit) | Container limits |

Compose also sets values you should not override: `KALAM_DB_URL`, `R2_BUCKET`, `R2_ACCESS_KEY` and
`R2_SECRET_KEY` are set to the empty string, so the `db`-mode connectors resolve and are never used;
`ORION_ADMIN_BEARER` is `Bearer ${ORION_ADMIN_KEY}`; `R2_ENDPOINT` comes from `RUNNER_BLOB_ENDPOINT`.

The node's Orion config is [`docker/runner.toml.tmpl`](docker/runner.toml.tmpl): api mode, the
engine digest (derived by the entrypoint from the component), the model cache, `engine.ops_budget`
and `models.max_timeout_ms`. The terms a match is played under (`turn_ms`, `max_turns`, the lease
and renew interval, the refusal and strike ceilings, the replay and model prefixes) arrive on each
claim from the match's season, and are not configured here.

Build args: `ANTS_RELEASE` (empty means the latest ants release) and `ORION_VERSION` (`1.8.1`).

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

### The machine

- **Pin the image.** Set `KALAM_IMAGE=ghcr.io/tiny-brains/kalam:<version>`. The image carries the
  engine, and a runner whose engine digest is not `games.active_engine_digest` claims nothing, for
  ever, with no error anywhere. `latest` moves with every release.
- **The architecture doesn't matter.** The cartridge is wasm32, so an arm64 Mac derives the same
  digest as an amd64 deployment. Images are published for linux/amd64 and linux/arm64.
- **Disable sleep.** Use Energy Saver, or run the stack under `caffeinate -dimsu`. A sleeping host
  stops renewing its leases. The matches lapse and are replayed by another runner, so no work is
  lost, but the machine keeps claiming matches it won't finish.
- **Size the Docker VM** above `RUNNER_MAX_CACHE_BYTES + RUNNER_MAX_LOADED_BYTES` plus the runtime:
  about 8 GiB at the defaults, more if you raise `RUNNER_CRON_WORKERS`. Below that the model cache
  thrashes. Every eviction re-fetches an artifact over the WAN, and it shows only as slowness.
- **Capacity is `RUNNER_CRON_WORKERS`.** It is shared by every channel, and the package's four
  match lanes cap matches at once at four whatever it says. Start at 2 and watch lease renewals
  before raising it: each match in flight keeps its seats' model sessions in memory. The engine is
  wasm and the models are ONNX on CPU, so cores matter more than clock speed.
- **Bandwidth is small and bursty.** One replay PUT per match (about 58 KiB for a 549-turn match),
  one artifact GET per cache miss (at most 64 MiB, `max_artifact_bytes`), and the idle poll: a token
  exchange and a claim per lane every 5 s.
- **No inbound ports.** 8080 is published on loopback only (`RUNNER_ADMIN_PORT`), for `/health` and
  `/metrics`. Publishing it would expose this node's admin API.

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
| `self-load: tb.ants=… active-channels=…` and the container stops | A workflow could not activate, so `package apply` left every channel after it as a draft (a draft cron channel never fires) | Read the error above it. Usually a connector names an `env://` variable that is **absent**: it must be set, even to the empty string. A missing or wrong signature (`RUNNER_SIG_DIR`, `TB_TRUST_PUBLIC_KEY`) quarantines the engine the same way |
| `self-load: the package did not load` | `load-package.sh` failed | Read its output above: signatures, the trust key, or a private address refused |
| Replay PUT 403 `SignatureDoesNotMatch` | `RUNNER_BLOB_ENDPOINT` differs from Soma's | Make them the same string |
| Shown as **quiet** or **key or owner**, or never appears | The token exchange is refused: 401 (the key is revoked or unknown, or its owner is no longer an admin) or 429 (too many runners behind one address) | Mint a new key or re-grant the owner. For 429, raise Soma's `runner_token_rate` or spread the machines across addresses |
| Matches are claimed and handed back; rows eventually fail `MODEL_UNAVAILABLE` | This node can't serve a seat's model, because its roster clock hasn't registered and activated it | Check `MODELS_ENDPOINT`, `MODELS_BUCKET` and the read key. Against a local stack, also check `RUNNER_ALLOW_PRIVATE_URLS=1` |
| Calls to this node's admin API get 401 | `ORION_ADMIN_BEARER` must be the whole `Bearer <key>` header | Leave it as compose sets it |
| Rows stay `running` after a restart | The drain was cut short: Docker's grace period ran out before Orion's | Keep `RUNNER_STOP_GRACE` above drain + force. The rows are reaped when their lease lapses |

## Developing the package

[`scripts/gen-kalam.py`](scripts/gen-kalam.py) is the source of `workflows/` and `channels/`, which
are gitignored build output: the SQL and JSONLogic are readable there and inlined into JSON. The
Dockerfile regenerates them into the image. `connectors/` and `shared/kalam.json` are authored JSON.
[CLAUDE.md](CLAUDE.md) has the rules and the Orion gotchas.

| Command | What it does | Needs |
|---|---|---|
| `python3 scripts/gen-kalam.py` | Writes `workflows/` and `channels/` | Python 3 |
| `python3 scripts/gen-kalam.py --check` | Fails if the files on disk drifted from the generator | Python 3 |
| `./scripts/check-defs.sh` | `--check`, then `orion-server lint` and `clippy` (both `--deny-warnings`), then `fmt --check` on `connectors/` and `shared/` | `orion-server` 1.8.x on `PATH` |
| `./scripts/check-sql.sh` | PREPAREs the generated SQL against a scratch database built from Soma's migrations, and asserts the `kalam` role's grants | Docker; web's running `tinybrains-db-1`; `../soma/migrations` (override with `MIGRATIONS`, `DB_CONTAINER`, `DB_USER`) |
| `docker compose up -d --build` | Builds this checkout and runs it as a runner | Docker and a Soma |
| `ORION_ADMIN=… ORION_ADMIN_API_KEY=… ./scripts/load-package.sh` | Compiles and applies the package into a running 1.8.1 node, and retires objects it no longer ships. The entrypoint runs it at boot | `orion-server`, curl, Python 3 |

- If the host's `orion-server` is older than 1.8, lint with the image's copy:
  `docker run --rm --entrypoint orion-server ghcr.io/tiny-brains/kalam clippy /pkg/kalam --deny-warnings`.
- Lint checks the engine calls too when the engine is present in the gitignored `plugins/tb-ants/`:
  copy `tb-ants.wasm`, `plugin.toml`, `plugin.json` and `cartridge.json` there from an ants release
  archive or from `../ants/dist/`.
- There are no unit tests. Lint and `check-sql.sh` are the compiler, and neither exercises leases or
  turns. A real match needs a Soma: web's stack plus this runner. `tinybrains conform` re-runs a
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
  the new signatures. Otherwise its self-load stops on a quarantined engine.

## Layout

```text
connectors/                    authored connector definitions
  kalam-api.json               Soma's runner gate; URL from KALAM_API_URL at load
  kalam-orion.json             this node's own admin API, where its model set lives
  kalam-models.json            the models bucket, read-only
  kalam-blobs-put.json         the replay PUT to a presigned URL; base URL from R2_ENDPOINT
  kalam-db.json                db mode only: Postgres as the kalam role
  kalam-blobs.json             db mode only: signs replay PUTs itself
shared/kalam.json              shared constants: clock tracing, the match lanes' config, the token call
scripts/
  gen-kalam.py                 source of workflows/ and channels/: the SQL, the task lists, the lanes
  check-defs.sh                no-stack gate: drift, lint, clippy, fmt
  check-sql.sh                 PREPARE the generated SQL; assert the kalam role's grants
  load-package.sh              compile and apply the package into a node
  stage-set.py                 stage connectors with a deployment's URLs and private-address flags
docker/
  entrypoint.sh                derive arch and engine digest, migrate, self-load, exec orion-server
  runner.toml.tmpl             the runner's Orion config
  replica-db.toml.tmpl         db-mode config: run by nothing, kept for the rollback and web's check
Dockerfile                     the runner image: orion-server, the package, the engine from an ants release
docker-compose.yml             one runner service
.env.example                   a runner's settings
.github/workflows/release.yml  v* tag → ghcr.io/tiny-brains/kalam
workflows/, channels/          generated (gitignored)
plugins/tb-ants/               optional local engine for lint (gitignored)
```

## Invariants

- **Kalam writes execution columns only.** Soma's migrations grant the `kalam` role claim, lease,
  result and replay columns. `check-sql.sh` fails if the role can write `matches.rated_at`, read
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
  back with a refusal spent, and after `refusal_ceiling` refusals the row fails `MODEL_UNAVAILABLE`.
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
- The same loop max caps a match at about 1000 turns. A season may set `max_turns` up to 100000,
  and a match that runs past the cap can't finish.
- Orion's `Message.audit_trail` keeps old and new values for every task execution, and there is no
  setting to turn it off.
- Every cron run mints a ten-minute token and uses it once. A longer-lived token would halve an idle
  runner's calls and lift the per-address runner limit.
- `check-sql.sh` does not descend into task groups, so the grouped `db`-mode statements (reap, claim,
  row) are not prepared.
- `match_concurrency` in the runner config is read by nothing, and in api mode neither is
  `refusal_ceiling`.
- The `db`-mode branch is still in the package as a rollback. CLAUDE.md has the removal checklist.

## License

Apache-2.0. See [LICENSE](LICENSE).
