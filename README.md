# kalam

The match player for TinyBrains: N disposable replicas that claim queued matches, play them
turn-synchronously against the game engine, and finish each row as its match ends.

Kalam is an **[Orion](https://github.com/GoPlasmatic/Orion) v1.7.0 package** — JSON definitions
plus the game engine as a signed plugin. There is no application code.

The design documents are not in this repo; they live in the private workspace this repo sits
inside, as `design/v2/` — [`00-overview.md`](../design/v2/00-overview.md) for the shape,
[`01-match-table.md`](../design/v2/01-match-table.md) for every statement Kalam runs, and
[`kalam-gaps.md`](../design/v2/kalam-gaps.md) for what is still missing, ranked.

---

## What Kalam is, in one paragraph

A replica claims up to K `pending` rows in **one statement** — trials first, then rows whose models
its loader already holds, the rest filled with rows sharing the first row's models and preset — and
stamps them with a token and a lease. It asks its Model Loader to hold the wave's models, asks the
Game Engine plugin for the wave's worlds, and plays every match turn-synchronously: observe, one
play call for the whole wave, step. Each row is finished **as its match ends**, with its result and
a replay key naming the attempt. Then it releases the models and claims again.

It reads no rating and writes no rating. Its database role can `SELECT` `matches` and `match_seats`
and `UPDATE` only its own columns of each, so that is not a convention but a grant — and
`design/v2/01-verify/run.sh` exercises it, proving Postgres refuses this role a write to `ratings`,
`models`, `clocks`, `rating_events` and `users`, and refuses it even `matches.rated_at`.

A crash loses one wave: the leases lapse, the next claim reaps them, and another replica replays
those rows from turn zero. A stale replica's finish updates nothing, because the token has moved.
SIGTERM drains — the wave in hand finishes, nothing new is claimed.

---

## Status: the package exists, the wave does not

| | |
|---|---|
| `connectors/` | **done.** `kalam-db` on the Kalam role, `kalam-blobs` with `presign_put`, `model-loader` over loopback with `max_retries: 0` — a retried play would replay a turn |
| `scripts/load-package.sh` | **done.** Same shape as Soma's, tag `pkg:kalam`, plugins before workflows |
| `Dockerfile`, `docker-entrypoint.sh` | **done.** Same upstream binary as Soma. The entrypoint refuses to load the package until the loader answers: a replica with no loader claims matches it cannot play, and each one costs a lease and two lapses |
| `../devops/kalam/orion.toml.tmpl` | **done, numbers provisional.** Local SQLite state, plugins on, `errors_only` tracing, the drain window. Validates against orion-server 1.7.0 |
| `plugins/` | **empty.** The `tb.ants` cartridge is P3's |
| `workflows/`, `channels/` | **EMPTY, AND BLOCKED.** See below |

### Why there is no wave workflow yet

This is the honest boundary, not an oversight. The wave workflow needs three things that do not
exist:

1. **Layer 03** (`design/v2/kalam-gaps.md` 0.2) is unwritten. It owes the claim's shape in a real
   workflow, the loop, the renew, and — the open question — **how a fixed task list finishes K
   matches that end on different turns.** A workflow's task list is fixed, so either it carries K
   conditioned presign-`PUT`-finish triples, or it drains one finish per turn from a queue. Layer
   03 has to choose, and the choice changes the workflow's whole shape.
2. **The Model Loader** does not exist as a binary. Its play half (`kalam-gaps` 0.3) also owes a
   call the overview's list of five does not name: *which hashes are resident now*, since the
   claim's affinity ordering takes that list as a parameter and a workflow run has nowhere else to
   learn it.
3. **The wave-turn spike** (`kalam-gaps` 0.6) has not run, so K, N and the lease in
   `[vars]` are guesses. They are written into the config anyway, labelled, so the shape is
   reviewable — but nothing should be tuned against them.

The claim, read, start, release, fail, renew and finish **statements** are all written, run and
verified on Postgres 16 (`01-match-table.md` §4, `design/v2/01-verify/run.sh`). What is missing is
the workflow that calls them in order, and it is missing because the order is layer 03's to fix.

---

## Local development

There is no compose service yet, for the same reason: a Kalam replica with no loader and no engine
has nothing to do. When layer 03 and the loader land, the service goes in `../devops/` beside
Soma's, on the same Postgres with the `kalam` role's credential, with a stub loader answering
random legal actions until the real one exists — the method `game-manager.md` §9 step 3 used.

Until then the package is checked by `orion-server -c ... validate-config` against the instance
config, and the statements it will run are checked by `design/v2/01-verify/run.sh`.

---

## What Kalam deliberately does not do

Worth stating, because every one of them was a design decision and the temptation to add them back
will recur:

- **It does not pair.** It takes what is queued. Pair is a Jodi clock in Soma's package.
- **It does not rate.** Count is the only ladder writer, and the grant makes that a fact.
- **It does not know the roster.** It cannot see who is active, only which rows name which hashes.
- **It does not talk to Soma.** The two packages share one schema and one loader artifact, and no
  Orion state, no queue and no call. They meet in rows.
- **It does not checkpoint a match.** A lost wave is replayed from turn zero. Matches are minutes.
