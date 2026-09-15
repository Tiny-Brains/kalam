#!/usr/bin/env python3
"""Generate Kalam's channels and workflows.

The SQL and the JSONLogic are unreadable inline in JSON and readable here, so this file is the
source and `workflows/*.json` + `channels/*.json` are build output. `Dockerfile` regenerates them
into the artifact image and runs `--check` straight after, so a hand-edited file is a failed build.

Two clocks since the 1.8.1 rebuild (devops/docs/decisions.md, the R-series):

  tb-roster   reconciles this node's model set with the shared schema. Every version the ladder
              says is verified or active is registered here, admitted here, and activated here.
              Jodi never calls a replica: the database is the only channel (R8).

  tb-match    claims ONE queued row and plays it turn by turn -- observe, one `model_infer` per
              seat, step -- and finishes it in place. The wave is gone: every Ants map is
              two-player, so K rows per claim existed to amortise one batched inference call over
              a number that is two (R7).

Run with no arguments to write the files; `--check` fails if what is on disk has drifted.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

PKG = pathlib.Path(__file__).resolve().parent.parent

ENGINE = "tb.ants"

# How many seats a match may have and still be claimed here. The task list is fixed, so a seat is
# a task: four is every board the catalogue ships (all 2-player) with room for a 4-player preset,
# and the claim refuses anything wider rather than playing it short a seat.
MAX_SEATS = 4

# The action alphabet, and the ONE place the platform knows it. It is the cartridge's, read off
# `docs/protocol.md` §1 -- a per-cell head's channel order is part of the game's contract, not the
# competitor's (R3).
DIRS = ["N", "E", "S", "W", "-"]


# ======================================================================= helpers


def sql(text: str) -> str:
    """Collapse a readable statement to the single line a JSON field holds.

    Line comments go FIRST: collapsing whitespace would put everything after a surviving `--` on
    the same line, silently, because the truncated text is often still valid SQL.
    """
    out = []
    for line in text.split("\n"):
        i = line.find("--")
        while i != -1:
            if line[:i].count("'") % 2 == 0:
                line = line[:i]
                break
            i = line.find("--", i + 2)
        out.append(line)
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def var(path: str) -> dict:
    return {"var": path}


def vars_(name: str) -> dict:
    """An instance value from [vars] in the replica's orion.toml."""
    return {"var": f"metadata.vars.{name}"}


def root(*segments) -> dict:
    """A ROOT value read from inside one iterator body.

    `{"val": [[1], …]}` leaves the innermost frame; at or above the number of enclosing iterators
    it resolves against the root. Inside a NESTED iterator the enclosing element is not reachable
    at all -- verified, not assumed -- which is why every cross-product in this file is carried in
    a reduce's accumulator instead.
    """
    return {"val": [[1], *segments]}


def task(tid: str, name: str, fn: dict, cond=None, terminal: bool = False,
         soft: bool = False) -> dict:
    t = {"id": tid, "name": name}
    if cond is not None:
        t["condition"] = cond
    t["function"] = fn
    if soft:
        t["continue_on_error"] = True
    if terminal:
        t["terminal"] = True
    return t


def mapping(*pairs) -> dict:
    """A `map` task. Mappings are applied IN ORDER and later ones see earlier ones.

    A pair whose logic is `None` means CLEAR THIS SLOT, and it is emitted as `False`, not as JSON
    null. dataflow-rs SKIPS a mapping whose logic evaluates to null -- `map.rs`:

        if matches!(transformed_value, OwnedDataValue::Null) { ... continue; }

    -- so `{"logic": null}` writes nothing at all and the slot keeps the PREVIOUS sweep's value.
    That is the opposite of what a clear is for, and it is silent. `False` is falsy to every
    condition that tests the slot, and reading a path through it yields null exactly as an unset
    slot does, so the intent survives and the write actually happens. Found 15 September 2026, when
    tb-roster stopped registering any model on a node that already had one."""
    return {"name": "map", "input": {
        "mappings": [{"path": p, "logic": False if l is None else l} for p, l in pairs]}}


def plugin(fn: str, inp: dict) -> dict:
    return {"name": fn, "input": inp}


def db(fn: str, query: str, params: list, output: str) -> dict:
    return {"name": fn, "input": {
        "connector": "kalam-db", "query": sql(query), "params": params, "output": output}}


def http(connector: str, method: str, path, output: str, body=None, **extra) -> dict:
    inp = {"connector": connector, "method": method, "path": path}
    if body is not None:
        inp["body"] = body
    inp.update(extra)
    inp["output"] = output
    return {"name": "http_call", "input": inp}


def admin(method: str, path, output: str, body=None, **extra) -> dict:
    """A call to THIS node's own admin API, which is where its model set lives."""
    return http("kalam-orion", method, path, output, body, **extra)


def halt_unless(condition: dict) -> dict:
    return {"name": "filter", "input": {"condition": condition, "on_reject": "halt"}}


def wrote(path: str) -> dict:
    """A fenced statement learns its fate from rows_affected: db_write returns nothing else."""
    return {">": [var(f"{path}.rows_affected"), 0]}


def sift(source: dict, carried: dict, test=None, element=None, items=None,
         keep: bool = True) -> dict:
    """Build a list in a `reduce`, because element scope cannot see root scope: every root value the
    body needs rides in the accumulator beside the list. `test` filters and `element` transforms;
    both run in element scope. Unwrap the result's `.items` -- reducing over the accumulator OBJECT
    instead iterates its keys and yields rows of nulls, which the finish statement then correctly
    refuses because they name no seat."""
    grow = dict({k: var(f"accumulator.{k}") for k in carried},
                items={"merge": [var("accumulator.items"),
                                 [var("current") if element is None else element]]})
    body = grow if test is None else {
        "if": [test, *([grow, var("accumulator")] if keep else [var("accumulator"), grow])]}
    return {"reduce": [source, body, dict(carried, items=[] if items is None else items)]}


def model_id(version_expr: dict) -> dict:
    """The Orion model id of a version (R9). Derived, never stored: an Orion label may not begin
    with a digit, so a bare uuid is refused and `tb.v` is the prefix that fixes it."""
    return {"cat": [vars_("model_prefix"), version_expr]}


# ======================================================================= the statements
#
# Every one is soma/docs/schema.md §4, and every one is conditioned on the claim token so a stale
# attempt updates nothing.

# --- 4.1 reap: its own statement rather than a CTE inside the claim, because a CTE's writes are
# invisible to the claim in the same snapshot and a reaped row would wait one more poll.
K_REAP = """
UPDATE matches
   SET status = CASE WHEN lapses + 1 >= 3 THEN 'failed' ELSE 'pending' END::match_status,
       lapses = lapses + 1, claim_token = NULL, lease_expires_at = NULL,
       fault_reason = CASE WHEN lapses + 1 >= 3 THEN 'LEASE_LAPSED' END,
       closed_at = CASE WHEN lapses + 1 >= 3 THEN now() END
 WHERE status IN ('claimed', 'running') AND lease_expires_at < now()
"""

# --- 4.2 claim, and it takes ONE row. $1 engine digest · $2 token · $3 lease seconds · $4 seats.
#
# What went with the wave: the resident-weights affinity ordering (an optimisation of a residency
# model that no longer exists -- the session cache loads on demand) and the preset grouping (which
# existed so one `observe` call could serve a whole wave of one board). What stays: trials first,
# then oldest, and `FOR UPDATE SKIP LOCKED` so N replicas and N match channels take disjoint rows
# rather than queueing behind each other.
#
# `seat_count <= $4` is new and deliberate. A seat is a task and the task list is fixed, so a
# 6-player preset must not be claimed by a replica that can only play four: refusing to claim is
# visible in the queue, playing it short a seat would be a match nobody could explain.
K_CLAIM = """
WITH pick AS MATERIALIZED (
    SELECT m.id FROM matches m
     WHERE m.status = 'pending' AND m.engine_digest = ($1)::text
       AND m.seat_count <= ($4)::int
     ORDER BY (m.trial_version_id IS NOT NULL) DESC, m.created_at, m.id
     LIMIT 1 FOR UPDATE SKIP LOCKED)
UPDATE matches m
   SET status = 'claimed', claim_token = ($2)::uuid,
       lease_expires_at = now() + ($3)::int * interval '1 second'
  FROM pick WHERE m.id = pick.id
"""

# --- 4.3 read the claimed row and its seats. One row, so no `row_number()`: the engine's wave
# still has an `m`, and it is 0 for the whole run.
#
# `model` is DERIVED here rather than stored: the version id is the model id (R9), so a row and a
# node cannot disagree about what to call a model. `weights_hash` and `manifest_hash` are carried
# for the replay envelope -- the record of what was paired, not what the version row says today.
K_ROW = """
SELECT json_build_object(
         'id', m.id, 'seed', m.seed, 'preset', m.preset, 'seat_count', m.seat_count,
         'trial_model_id', m.trial_version_id, 'strike_ceiling', m.strike_ceiling,
         'seats', (SELECT json_agg(json_build_object(
                     'm', 0, 'seat', s.seat, 'version_id', s.version_id,
                     'model', ($2)::text || s.version_id::text,
                     'strike_ceiling', m.strike_ceiling,
                     'weights_hash', s.weights_hash,
                     'manifest_hash', s.manifest_hash) ORDER BY s.seat)
                    FROM match_seats s WHERE s.match_id = m.id)) AS row
  FROM matches m
 WHERE m.claim_token = ($1)::uuid AND m.status = 'claimed'
"""

# --- 4.4 release: the models this replica has not caught up with yet. NOT a fault and NOT a lapse
# -- the row goes back to the queue with `refusals` spent, so a replica that is permanently behind
# eventually fails the row rather than passing it round the fleet for ever. The roster clock is
# what makes this rare; without the statement it would be a match played with one seat blind.
K_RELEASE = """
UPDATE matches
   SET status = CASE WHEN refusals + 1 >= ($3)::int THEN 'failed' ELSE 'pending' END::match_status,
       refusals = refusals + 1, claim_token = NULL, lease_expires_at = NULL,
       fault_reason = CASE WHEN refusals + 1 >= ($3)::int THEN 'MODEL_UNAVAILABLE' END,
       closed_at = CASE WHEN refusals + 1 >= ($3)::int THEN now() END
 WHERE claim_token = ($1)::uuid AND status = 'claimed' AND ($2)::boolean
"""

# --- 4.4 start.
K_START = """
UPDATE matches SET status = 'running'
 WHERE claim_token = ($1)::uuid AND status = 'claimed'
"""

# --- 4.5 renew. No indexed column changes, so it stays heap-only -- the statement the table's
# fillfactor exists for.
K_RENEW = """
UPDATE matches SET lease_expires_at = now() + ($2)::int * interval '1 second'
 WHERE claim_token = ($1)::uuid AND status = 'running'
"""

# --- 4.6 finish. $1 token · $2 match · $3 the result, one element per seat · $4 the engine's end
# reason · $5 turns · $6 opened at · $7 engine digest · $8 the Orion that ran the adapters · $9 key.
#
# One statement: the row and its seats move together or not at all. The seat count check is what
# refuses a partial result -- a result naming three of four seats leaves the row running and the
# lease reaps it, which is recoverable, where a half-written match is not.
K_FINISH = """
WITH m AS (
    UPDATE matches
       SET status = 'finished', reason = ($4)::text, turns = ($5)::int,
           played_ms = GREATEST(0, (EXTRACT(EPOCH FROM (now() - ($6)::timestamptz)) * 1000)::int),
           engine_digest_played = ($7)::text, orion_version = ($8)::text,
           replay_key = ($9)::text, played_at = now(), lease_expires_at = NULL
     WHERE id = ($2)::uuid AND claim_token = ($1)::uuid AND status = 'running'
       AND (SELECT count(DISTINCT v.seat) FROM jsonb_to_recordset(($3)::jsonb) AS v (seat smallint)
             WHERE v.seat BETWEEN 0 AND seat_count - 1) = seat_count
 RETURNING id)
UPDATE match_seats s
   SET rank = v.rank, score = v.score, strikes = v.strikes,
       infer_us_total = v.infer_us_total, infer_us_max = v.infer_us_max,
       infer_turns = v.infer_turns
  FROM m, jsonb_to_recordset(($3)::jsonb)
       AS v (seat smallint, rank smallint, score int, strikes smallint,
             infer_us_total bigint, infer_us_max int, infer_turns int)
 WHERE s.match_id = m.id AND s.seat = v.seat
"""

# --- the roster read. Every version this node should be able to play, with the manifest it was
# admitted under and where its bytes are. `verified` as well as `active`: a verified version's
# trial match is a real match and it is paired before promotion, so a replica that waits for
# `active` cannot play the trial that produces it.
#
# Ordered oldest-first so a backlog is worked in submission order, and LIMITed because the clock
# does one registration step per item per tick -- it is a reconciler, not a batch job.
R_ROSTER = """
SELECT json_build_object(
         'n', count(*),
         'items', coalesce(json_agg(json_build_object(
                    'model', ($1)::text || v.id::text,
                    'version_id', v.id,
                    'digest', v.weights_hash,
                    'key', v.artifact_key,
                    -- WHAT IS REGISTERED IS NOT WHAT WAS UPLOADED, and the difference is two
                    -- things. `name` becomes the platform's model id, because Orion takes a
                    -- model's id from the manifest and a competitor's name is not the platform's
                    -- (R9). And the document is rebuilt FIELD BY FIELD rather than passed through,
                    -- so a `reference` naming somebody else's bucket key -- the one field that
                    -- could reach outside this version -- has nowhere to survive. The stored text
                    -- stays the competitor's exact bytes, because that is what the hash is over.
                    'manifest', jsonb_build_object(
                        'abi',         v.manifest::jsonb -> 'abi',
                        'name',        to_jsonb(($1)::text || v.id::text),
                        'version',     coalesce(v.manifest::jsonb -> 'version', '"1"'::jsonb),
                        'format',      coalesce(v.manifest::jsonb -> 'format', '"onnx"'::jsonb),
                        'description', coalesce(v.manifest::jsonb -> 'description', '""'::jsonb),
                        'inputs',      v.manifest::jsonb -> 'inputs',
                        'outputs',     v.manifest::jsonb -> 'outputs',
                        'probe_dims',  coalesce(v.manifest::jsonb -> 'probe_dims', '{}'::jsonb)),
                    'status', v.status) ORDER BY v.created_at), '[]'::json)) AS body
  FROM model_versions v
 WHERE v.status IN ('verified', 'active')
   AND v.manifest IS NOT NULL AND v.artifact_key IS NOT NULL AND v.weights_hash IS NOT NULL
   AND EXISTS (SELECT 1 FROM match_seats s WHERE s.version_id = v.id)
"""


# ======================================================================= shared conditions

TURN0 = {"==": [var("temp_data.i"), 0]}
LIVE = {">": [var("temp_data.n_live"), 0]}
ENDED = {"and": [{"==": [var("temp_data.n_live"), 0]},
                 {"!": var("data.finished")}]}
OVER = var("data.finished")

NOT_CLAIMED = {"and": [TURN0, {"!": wrote("temp_data.claim")}]}
NOT_READY = {"and": [TURN0, {">": [var("temp_data.n_missing"), 0]}]}

DO_RENEW = {"and": [LIVE,
                    {">": [var("temp_data.i"), 0]},
                    {"==": [{"%": [var("temp_data.i"), vars_("renew_every_n_turns")]}, 0]}]}
RENEW_LOST = {"and": [DO_RENEW, {"!": wrote("temp_data.renewed")}]}


def seat(i: int) -> dict:
    """The i-th seat of the claimed row, as `open` wrote it."""
    return {"val": ["data", "seats", i]}


def seat_exists(i: int) -> dict:
    return {">": [var("data.row.seat_count"), i]}


def seat_plays(i: int) -> dict:
    """A seat is asked for a move while the match is live, it exists, and it has not forfeited.
    A forfeited seat is never inferred: it plays the no-op by construction, which is the same
    rule the wave had and the reason a forfeit costs nothing after it is taken."""
    return {"and": [LIVE, seat_exists(i), {"!": var(f"data.f{i}")}]}


def view_of(i: int) -> dict:
    """The i-th seat's view out of `observe`, selected by the ref it echoed rather than by
    position: a match that has ended returns no view at all, and position would then hand seat 1's
    observation to seat 0."""
    return {"reduce": [var("temp_data.obs.views"),
                       {"if": [{"===": [var("current.ref.seat"), i]},
                               var("current.view"), var("accumulator")]},
                       None]}


def decode(i: int) -> dict:
    """One seat's policy tensor to one action per ant, positionally aligned with `mine` (R3).

    The platform does this, not the manifest, and it is not a preference: a `result` expression's
    root is the output tensors alone, so it cannot see the observation and cannot gather at the
    ants' cells. Both head shapes are legal and the rank tells them apart --

      [1, 5, H, W]  per-cell: reshape to [5, H*W], gather the ants' flat indices, transpose to
                    [n, 5], argmax the channel axis.
      [n, 5]        per-ant: already in `mine` order, because the adapter fed the coordinates in
                    that order. Argmax and nothing else.

    Verified against numpy on every reference observation before it was written here.
    """
    policy = var(f"temp_data.p{i}.policy")
    per_cell = {"transpose": [
        {"gather": [{"reshape": [policy, [5, var("temp_data.cells")]]},
                    var("temp_data.idx"), 1]},
        [1, 0]]}
    chosen = {"argmax": [{"if": [{"==": [{"length": [{"shape": [policy]}]}, 4]},
                                 per_cell, policy]}, 1]}
    return {"if": [
        {"!": policy}, None,
        {"map": [chosen, {"val": [[1], "data", "dirs", var("")]}]}]}


# ======================================================================= tb-match

MATCH_TASKS = [
    # ------------------------------------------------------------------ turn 0: claim
    task("token", "Mint this attempt's claim token", mapping(
        ("data.token", {"random": ["uuid"]}),
        ("data.opened_at", {"now": []}),
        ("data.dirs", DIRS),
        ("data.finished", False),
    ), cond=TURN0),

    # A MISSING [vars] VALUE IS SILENT AND CATASTROPHIC: `{">=": [1, null]}` is true, so a
    # comparison against an unresolved ceiling passes and the match dies two turns later at `step`,
    # naming neither the variable nor the cause. So: checked once, loudly, up front.
    task("vars", "Halt unless this replica is configured", halt_unless({"and": [
        {"!=": [vars_("engine_digest"), None]},
        {">": [vars_("lease_seconds"), 0]},
        {">": [vars_("renew_every_n_turns"), 0]},
        {">": [vars_("turn_ms"), 0]},
        {">": [vars_("max_turns"), 0]},
        {">": [vars_("refusal_ceiling"), 0]},
        {"!=": [vars_("replay_prefix"), None]},
        {"!=": [vars_("blob_endpoint"), None]},
        {"!=": [vars_("model_prefix"), None]},
        {"!=": [vars_("orion_version"), None]},
    ]}), cond=TURN0),

    task("reap", "Return lapsed leases to the queue", db(
        "db_write", K_REAP, [], "temp_data.reaped",
    ), cond=TURN0),

    task("claim", "Claim one queued match", db(
        "db_write", K_CLAIM,
        [vars_("engine_digest"), var("data.token"), vars_("lease_seconds"), MAX_SEATS],
        "temp_data.claim",
    ), cond=TURN0),

    task("idle", "Nothing queued for this engine: end the run", mapping(
        ("data.outcome", "idle"),
    ), cond=NOT_CLAIMED, terminal=True),

    task("row", "The row, its seats and their model ids", db(
        "db_read", K_ROW, [var("data.token"), vars_("model_prefix")], "temp_data.rows",
    ), cond=TURN0),

    task("open", "What the run rides on", mapping(
        ("data.row", var("temp_data.rows.0.row")),
        ("data.seats", var("temp_data.rows.0.row.seats")),
        # THE REFS. A flat list, each entry carrying its own m and seat, which the engine matches
        # on rather than indexes -- and where the per-seat counters live, because a fixed task list
        # has no other way to accumulate anything per seat across turns. Seeded at 0, not left
        # absent: `{"+": [null, x]}` on a first write is exactly the silent-null class of bug this
        # file keeps warning about.
        ("data.refs", {"map": [var("data.seats"),
                               {"m": 0, "seat": var("seat"),
                                "model": var("model"),
                                "weights_hash": var("weights_hash"),
                                "manifest_hash": var("manifest_hash"),
                                "strike_ceiling": var("strike_ceiling"),
                                "strikes": 0, "forfeited": False,
                                "infer_us_total": 0, "infer_us_max": 0, "infer_turns": 0}]}),
        ("data.deltas", []),
        ("data.struck", 0),
    ), cond=TURN0),
] + [
    # ------------------------------------------------------------------ turn 0: the barrier
    #
    # The residency barrier is gone with the sidecar, but one thing it did still has to happen:
    # a seat whose model this node cannot serve must not play. The roster clock registers and
    # activates from the shared schema and is normally ahead of the pairing, so this is the lag
    # case -- a GET per seat against this node's own admin API, on localhost, once per match,
    # against a thousand turns.
    task(f"has{i}", f"Can this node serve seat {i}'s model?", admin(
        "GET", {"cat": ["/models/", {"val": ["data", "seats", i, "model"]}]},
        f"temp_data.m{i}",
    ), cond={"and": [TURN0, seat_exists(i)]}, soft=True)
    for i in range(MAX_SEATS)
] + [
    task("barrier", "How many seats this node cannot play", mapping(
        # Through the admin API's `data` envelope, which is what every reply carries. Reading
        # `temp_data.m{i}.status` instead finds null, `null != "active"` is true, and every match
        # is released as unready for ever -- with both sides looking healthy.
        ("temp_data.n_missing", {"+": [
            *[{"if": [{"and": [seat_exists(i),
                               {"!==": [var(f"temp_data.m{i}.data.status"), "active"]}]}, 1, 0]}
              for i in range(MAX_SEATS)]]}),
    ), cond=TURN0),

    task("release", "A model this node cannot serve: back to the queue", db(
        "db_write", K_RELEASE,
        [var("data.token"), True, vars_("refusal_ceiling")], "temp_data.released",
    ), cond=NOT_READY),

    task("unready", "End the run: the roster has not caught up", mapping(
        ("data.outcome", "model_unavailable"),
    ), cond=NOT_READY, terminal=True),

    # ------------------------------------------------------------------ turn 0: open the match
    task("start", "Claimed becomes running", db(
        "db_write", K_START, [var("data.token")], "temp_data.started",
    ), cond=TURN0),

    task("started", "Halt if the start wrote nothing: the token is stale",
         halt_unless(wrote("temp_data.started")), cond=TURN0),

    task("world", "Build the world", plugin(f"{ENGINE}.worldgen", {
        "seeds": [var("data.row.seed")],
        "preset": var("data.row.preset"),
        # The preset carries the seat count and the engine refuses a caller that disagrees, so
        # passing it is a free check rather than a parameter.
        "players": var("data.row.seat_count"),
        "max_turns": vars_("max_turns"),
        "output": "temp_data.w0",
    }), cond=TURN0),

    task("init", "Open the match", mapping(
        ("data.state", var("temp_data.w0.wave_state")),
    ), cond=TURN0),

    # ------------------------------------------------------------------ every turn
    task("observe", "Every live seat's view", plugin(f"{ENGINE}.observe", {
        "wave_state": var("data.state"),
        "refs": var("data.refs"),
        "output": "temp_data.obs",
    }), cond={"!": OVER}),

    task("turn", "This turn's views, and the board they are on", mapping(
        ("temp_data.n_live", {"length": [var("temp_data.obs.views")]}),
        *[(f"temp_data.v{i}", view_of(i)) for i in range(MAX_SEATS)],
        *[(f"data.f{i}", {"val": ["data", "refs", i, "forfeited"]}) for i in range(MAX_SEATS)],
        # The board's shape, read off any live view. Every seat of a match sees the same board, so
        # seat 0's is the match's -- and seat 0 exists for as long as the match does.
        ("temp_data.w", {"val": ["temp_data", "obs", "views", 0, "view", "size", 1]}),
        ("temp_data.cells", {"*": [{"val": ["temp_data", "obs", "views", 0, "view", "size", 0]},
                                   {"val": ["temp_data", "obs", "views", 0, "view", "size", 1]}]}),
    ), cond={"!": OVER}),
] + [
    # ONE INFERENCE PER SEAT, and the whole reason the wave could go. `model` is computed, so this
    # task is invisible to `models.preload` -- the roster clock tags every registration `ladder`
    # and the replica's `models.preload_tags` warms them at boot instead.
    #
    # `continue_on_error` is what makes a competitor's failure THEIR failure: an adapter that
    # throws, a graph that will not run, a call that outlives its deadline -- all of them leave
    # `temp_data.p{i}` absent, which the decode below reads as "no action", which the engine plays
    # as the no-op and this workflow counts as a strike.
    task(f"infer{i}", f"Seat {i}'s move", {
        "name": "model_infer",
        "input": {
            "model": {"val": ["data", "seats", i, "model"]},
            "input": var(f"temp_data.v{i}"),
            "output": f"temp_data.p{i}",
            "raw": True,
            "stats_output": f"temp_data.s{i}",
            "timeout_ms": vars_("turn_ms"),
        },
    }, cond=seat_plays(i), soft=True)
    for i in range(MAX_SEATS)
] + [
    task("acts", "Decode each head, accumulate strikes and cost", mapping(
        # The ants' flat indices, shared by every seat of this match because `mine` is per seat.
        # Computed per seat, immediately before its decode, so the two cannot disagree.
        *[(f"temp_data.a{i}", {"if": [
            {"!": seat_plays(i)}, None,
            {"map": [{"reduce": [
                {"if": [var(f"temp_data.v{i}.mine"), var(f"temp_data.v{i}.mine"), []]},
                {"merge": [var("accumulator"),
                           [{"+": [{"*": [root("temp_data", "w"), var("current.0")]},
                                   var("current.1")]}]]}, []]},
                     var("")]}]})
          for i in range(MAX_SEATS)],
        *[(f"temp_data.idx", None)],
    ), cond=LIVE),
]

# The decode reads `temp_data.idx`, which is per seat: fold it into one task per seat so the index
# list and the gather that consumes it are never a turn apart.
MATCH_TASKS += [
    task(f"act{i}", f"Seat {i}: the head, decoded", mapping(
        ("temp_data.idx", var(f"temp_data.a{i}")),
        (f"temp_data.act{i}", decode(i)),
    ), cond=seat_plays(i))
    for i in range(MAX_SEATS)
]

MATCH_TASKS += [
    task("moves", "The turn's actions, and what they cost", mapping(
        # The EXPLICIT {m, seat, action} form, never the positional one: a forfeited seat sends
        # nothing at all, so a positional list would land every action after the first forfeit in
        # the wrong chair. Omission IS the no-op -- the engine plays it for any seat it is given
        # nothing for.
        ("temp_data.acts", {"reduce": [
            [*[{"seat": i, "action": var(f"temp_data.act{i}"),
                "live": seat_plays(i)} for i in range(MAX_SEATS)]],
            {"if": [{"and": [var("current.live"), var("current.action")]},
                    {"merge": [var("accumulator"),
                               [{"m": 0, "seat": var("current.seat"),
                                 "action": var("current.action")}]]},
                    var("accumulator")]},
            []]}),
        # A seat that was asked and answered nothing takes a strike. Counted CUMULATIVELY -- five
        # missed clocks in a match, not five in a row -- which is the reading a competitor cannot
        # game by failing every other turn.
        ("temp_data.miss", [*[{"if": [{"and": [seat_plays(i), {"!": var(f"temp_data.act{i}")}]},
                                      1, 0]} for i in range(MAX_SEATS)]]),
        # FLOORED HERE, not at the finish: `inference_ms` is a float, `infer_us_total` and
        # `infer_us_max` are a bigint and an int, and `jsonb_to_recordset` refuses `10051.542` for
        # either. One conversion, at the one place milliseconds become microseconds.
        ("temp_data.cost", [*[{"floor": [{"*": [1000, {"??": [var(f"temp_data.s{i}.inference_ms"),
                                                             0]}]}]}
                              for i in range(MAX_SEATS)]]),
        ("temp_data.asked", [*[{"if": [seat_plays(i), 1, 0]} for i in range(MAX_SEATS)]]),
        # Every root value the body needs rides in the accumulator: element scope cannot see root,
        # and `{"==": [0, null]}` is TRUE under this engine's loose equality, so the mistake is
        # silent rather than loud.
        ("temp_data.nr", sift(
            var("data.refs"),
            {"miss": var("temp_data.miss"), "cost": var("temp_data.cost"),
             "asked": var("temp_data.asked")},
            element={
                "m": 0, "seat": var("current.seat"),
                "model": var("current.model"),
                "weights_hash": var("current.weights_hash"),
                "manifest_hash": var("current.manifest_hash"),
                "strike_ceiling": var("current.strike_ceiling"),
                "strikes": {"+": [var("current.strikes"),
                                  {"val": ["accumulator", "miss", {"val": ["current", "seat"]}]}]},
                "forfeited": {"or": [
                    var("current.forfeited"),
                    {">=": [{"+": [var("current.strikes"),
                                   {"val": ["accumulator", "miss",
                                            {"val": ["current", "seat"]}]}]},
                            var("current.strike_ceiling")]}]},
                # Microseconds, from the milliseconds `stats_output` reports. A seat that was not
                # asked adds nothing, which is why `infer_turns` is carried rather than the match's
                # turn count being reused as the divisor.
                "infer_us_total": {"+": [
                    var("current.infer_us_total"),
                    {"val": ["accumulator", "cost", {"val": ["current", "seat"]}]}]},
                "infer_us_max": {"max": [
                    var("current.infer_us_max"),
                    {"val": ["accumulator", "cost", {"val": ["current", "seat"]}]}]},
                "infer_turns": {"+": [var("current.infer_turns"),
                                      {"val": ["accumulator", "asked",
                                               {"val": ["current", "seat"]}]}]},
            })),
        ("data.refs", var("temp_data.nr.items")),
        ("data.struck", {"+": [var("data.struck"),
                               {"reduce": [var("temp_data.miss"),
                                           {"+": [var("accumulator"), var("current")]}, 0]}]}),
    ), cond=LIVE),

    task("step", "Advance the match by one turn", plugin(f"{ENGINE}.step", {
        "wave_state": var("data.state"),
        "actions": var("temp_data.acts"),
        "output": "temp_data.st",
    }), cond=LIVE),

    task("carry", "Carry the state and the replay stream", mapping(
        ("data.state", var("temp_data.st.wave_state")),
        ("data.deltas", {"merge": [var("data.deltas"), var("temp_data.st.replay_delta")]}),
    ), cond=LIVE),

    # ------------------------------------------------------------------ renew
    task("renew", "Extend the lease", db(
        "db_write", K_RENEW, [var("data.token"), vars_("lease_seconds")], "temp_data.renewed",
    ), cond=DO_RENEW),

    task("lost", "Halt: the lease was reaped under us", mapping(
        ("data.outcome", "lease_lost"),
    ), cond=RENEW_LOST, terminal=True),

    # ------------------------------------------------------------------ the finish
    #
    # `observe` returns no view for a match that has ended, so the turn the view list empties is
    # the turn the match is over. One match, so there is no drain and no queue: the finish runs
    # once, in the sweep after the last turn.
    task("results", "Ranks, scores and an end reason", plugin(f"{ENGINE}.finish", {
        "wave_state": var("data.state"), "output": "temp_data.fin",
    }), cond=ENDED),

    task("pick", "The result, one element per seat", mapping(
        ("temp_data.res", {"val": ["temp_data", "fin", "results", 0]}),
        # Forfeited seats rank last, and the rule is `engine_rank + seat_count` rather than "set
        # them all to last", because two forfeited seats must not tie with a seat that played --
        # ranks need not be dense, and any order-preserving relabelling gives identical TrueSkill
        # output. The engine's OWN ranks go in the replay envelope untouched, so an audit can still
        # see what the game thought happened.
        ("temp_data.seatrows", {"reduce": [
            var("data.refs"),
            {"r": var("accumulator.r"),
             "n": var("accumulator.n"),
             "items": {"merge": [var("accumulator.items"), [{
                 "seat": var("current.seat"),
                 "rank": {"+": [{"val": ["accumulator", "r", "ranks", {"val": ["current", "seat"]}]},
                                {"if": [var("current.forfeited"), var("accumulator.n"), 0]}]},
                 "score": {"val": ["accumulator", "r", "scores", {"val": ["current", "seat"]}]},
                 "strikes": var("current.strikes"),
                 "infer_us_total": var("current.infer_us_total"),
                 "infer_us_max": var("current.infer_us_max"),
                 "infer_turns": var("current.infer_turns"),
             }]]}},
            {"r": var("temp_data.res"), "n": var("data.row.seat_count"), "items": []}]}),
        # The key names the ATTEMPT, so a stale attempt's blob is an orphan under its own key
        # rather than a replacement for the one that counted.
        ("temp_data.key", {"cat": [vars_("replay_prefix"), "/",
                                   var("data.row.id"), "/",
                                   var("data.token"), ".json"]}),
    ), cond=ENDED),

    task("presign", "Sign a PUT for this attempt's replay", {
        "name": "storage_presign",
        "input": {"connector": "kalam-blobs", "method": "PUT",
                  "key": var("temp_data.key"), "expires_in": "15m",
                  "output": "temp_data.signed"},
    }, cond=ENDED),

    task("put", "Write the replay envelope", http(
        "kalam-blobs-put", "PUT",
        {"substr": [var("temp_data.signed"), {"length": [vars_("blob_endpoint")]}]},
        "temp_data.putres", {
            "match_id": var("data.row.id"),
            "attempt_token": var("data.token"),
            "seed": var("data.row.seed"),
            "preset": var("data.row.preset"),
            # THE BOARD. A replay is self-sufficient or it is not viewable: `replay-decode`
            # rebuilds the match from `map`, so a replay stays viewable when the preset table has
            # been re-tuned or the catalogue has moved on.
            "map_id": var("temp_data.res.map_id"),
            "map": var("temp_data.res.map"),
            # WHO SAT WHERE, by hash -- not identity, but enough that a replay can be RE-RUN and
            # not merely watched: `tinybrains conform` rebuilds the match from this envelope alone
            # and diffs the result against it.
            "seats": var("data.refs"),
            "max_turns": vars_("max_turns"),
            "strike_ceiling": var("data.row.strike_ceiling"),
            "engine_digest": vars_("engine_digest"),
            # What ran the adapters. `evaluator_digest` named an axon build; this names the Orion
            # whose expression engine and tract this match was played on, which is what a
            # re-validation sweep is per (R10).
            "orion_version": vars_("orion_version"),
            "engine_ranks": var("temp_data.res.ranks"),   # before forfeits are applied
            "scores": var("temp_data.res.scores"),
            "reason": var("temp_data.res.reason"),
            "turns": var("temp_data.res.turns"),
            "deltas": var("data.deltas"),                 # the action stream, not frames
        },
        response_format="text",
    ), cond=ENDED),

    task("finish", "Finish the row -- one statement, on the token", db(
        "db_write", K_FINISH,
        [var("data.token"), var("data.row.id"), var("temp_data.seatrows.items"),
         var("temp_data.res.reason"), var("temp_data.res.turns"),
         var("data.opened_at"), vars_("engine_digest"),
         vars_("orion_version"), var("temp_data.key")],
        "temp_data.wrote",
    ), cond=ENDED),

    task("counted", "Halt if the finish wrote nothing: the token is stale",
         halt_unless(wrote("temp_data.wrote")), cond=ENDED),

    task("done", "Mark the run complete", mapping(
        ("data.finished", True),
    ), cond=ENDED),

    # No `terminal`: this is the last step, so the flag would do nothing but suggest the list is
    # guarded when it is not. `orion-server clippy` reports one as style.terminal_on_last_step.
    task("over", "Played and finished", mapping(
        ("data.outcome", "complete"),
        ("data.stopped_at_turn", var("temp_data.i")),
    ), cond=OVER),
]


# ======================================================================= tb-roster

ROSTER_TASKS = [
    task("roster", "What the ladder says this node should be able to play", db(
        "db_read", R_ROSTER, [vars_("model_prefix")], "temp_data.rst",
    ), cond=TURN0),

    task("more", "Stop when the roster runs out",
         halt_unless({"<": [var("temp_data.i"), var("temp_data.rst.0.body.n")]})),

    task("item", "Take version i, and clear the last one", mapping(
        ("temp_data.it", {"val": ["temp_data", "rst", 0, "body", "items",
                                  {"val": ["temp_data", "i"]}]}),
        # temp_data survives a sweep, so every per-item slot is cleared here: a task skipped this
        # time round would otherwise be read at the PREVIOUS item's value, and the one that matters
        # decides whether this item is registered at all.
        ("temp_data.have", None),
        ("temp_data.made", None),
        ("temp_data.activated", None),
    )),

    task("have", "Does this node know it already?", admin(
        "GET", {"cat": ["/models/", var("temp_data.it.model")]}, "temp_data.have",
    ), soft=True),

    # A registration carries the manifest, the reference and the digest -- never the bytes. The
    # node fetches the object through the connector and re-hashes it, so a row that lies about its
    # digest fails admission here rather than playing something else.
    #
    # `tags` is what makes a computed `model` warmable: `models.preload` reads the LITERAL model of
    # each active workflow's tasks and tb-match names its model with a var, so `preload_tags` is
    # the only mode that fits this shape (Orion 1.8.1, #329).
    task("register", "Register it here", admin(
        "POST", "/models", "temp_data.made", {
            "manifest": var("temp_data.it.manifest"),
            "artifact": {"connector": vars_("models_bucket_connector"),
                         "key": var("temp_data.it.key"),
                         "digest": var("temp_data.it.digest")},
            "tags": ["ladder"],
        },
    ), cond={"!": var("temp_data.have.data.model_id")}, soft=True),

    # Admission is asynchronous and this clock is a reconciler: it does ONE step per item per tick,
    # so a model registered this tick is activated on a later one. There is no poll loop, no
    # timeout to tune, and a node that restarts mid-admission simply catches up.
    task("activate", "Activate what has passed", admin(
        "PATCH", {"cat": ["/models/", var("temp_data.it.model"), "/status"]},
        "temp_data.activated", {"status": "active"},
    ), cond={"and": [{"===": [var("temp_data.have.data.admission.state"), "passed"]},
                     {"!==": [var("temp_data.have.data.status"), "active"]}]}, soft=True),
]


# ======================================================================= the documents

MATCH = {
    "workflow_id": "tb-match-run",
    "name": "Kalam: one match, turn by turn",
    "description": (
        "Claim ONE queued row on this replica's engine digest and play it: observe, one "
        "`model_infer` per live seat, step, until the engine stops returning views. The row is "
        "finished in place, with its replay under a key naming the attempt. Every statement is "
        "conditioned on the claim token, so a stale attempt updates nothing. It reads no rating "
        "and writes no rating, and its database role cannot reach one. The wave it replaces "
        "existed to amortise one batched inference call across many seats -- a number that is two "
        "on every Ants map (decision R7). On SIGTERM Orion stops claiming and lets the match in "
        "hand finish inside cron.shutdown_timeout_secs. docs/design.md; the statements are "
        "soma/docs/schema.md §4."
    ),
    "tags": ["pkg:kalam"],
    "condition": True,
    # One sweep per turn, plus the sweep that finishes. `max_turns` is the game's bound and this is
    # the runaway bound above it.
    "loop": {"counter": "i", "max": 1010},
    "tasks": MATCH_TASKS,
}

ROSTER = {
    "workflow_id": "tb-roster-run",
    "name": "Kalam: reconcile this node's model set",
    "description": (
        "Every version the ladder says is verified or active, registered, admitted and activated "
        "ON THIS NODE. Models are a state-database entity and each replica is its own Orion with "
        "its own state database (decision 41), so a roster is per node -- and a clock that "
        "reconciles from the shared schema needs no replica list anywhere, which is what keeps "
        "Jodi from ever calling a replica (decision R8). One step per item per tick: registration "
        "queues admission, and a later tick activates what passed. Nothing here writes to the "
        "platform schema; its database role has SELECT and nothing else on model_versions."
    ),
    "tags": ["pkg:kalam"],
    "condition": True,
    "loop": {"counter": "i", "max": 256},
    "tasks": ROSTER_TASKS,
}

MATCH_CHANNELS = [
    {
        "channel_id": f"tb-match-{n}",
        "name": f"tb-match-{n}",
        "tags": ["pkg:kalam"],
        "channel_type": "async",
        "protocol": "cron",
        "workflow_id": "tb-match-run",
        "transport_config": {
            "schedule": "*/5 * * * * *",
            "timezone": "UTC",
            # A poll missed while a match was running has nothing to catch up: the queue is still
            # there and the next tick claims from it.
            "misfire_policy": "skip",
            # `forbid` on a key PER CHANNEL, and the key is local in effect: Kalam's Orion runs on
            # local SQLite with no shared state, so the lock is per replica. One match in flight
            # per channel, N channels per replica, N replicas in parallel -- and the claim's
            # `FOR UPDATE SKIP LOCKED` is what keeps them off each other's rows.
            "concurrency": {"policy": "forbid", "key": f"match-{n}"},
        },
        # Identical across all four lanes, so it is declared once. The lanes differ ONLY in
        # channel_id and concurrency.key -- that is the whole of what makes them separate lanes.
        "config": {"$from": "constants.match_channel_config"},
    }
    for n in range(1, 5)
]

ROSTER_CHANNEL = {
    "channel_id": "tb-roster",
    "name": "tb-roster",
    "tags": ["pkg:kalam"],
    "channel_type": "async",
    "protocol": "cron",
    "workflow_id": "tb-roster-run",
    "transport_config": {
        "schedule": "*/15 * * * * *",
        "timezone": "UTC",
        "misfire_policy": "skip",
        "concurrency": {"policy": "forbid", "key": "roster"},
    },
    "config": {
        "timeout_ms": 120000,
        "tracing": {"$from": "constants.clock_tracing"},
    },
}


def group_runs(tasks: list) -> list:
    """Collapse a run of consecutive tasks sharing one condition into a task group.

    The match loop is mostly runs of steps guarded by the same `first_sweep`-style condition, and
    written flat each one re-evaluates it once per member -- `orion-server clippy` reports every
    such run as perf.redundant_step_condition. A group carries the condition ONCE, on entry: a
    falsy result skips the span WITHOUT evaluating the members' conditions, which is what makes
    stripping them from the members equivalent rather than merely similar.

    A run holding a `terminal` member is left alone: terminal is about position, and a group's own
    terminal covers the whole span, so folding one in would move where the workflow ends.
    """
    out, i = [], 0
    while i < len(tasks):
        cond = tasks[i].get("condition")
        j = i
        if cond is not None and not tasks[i].get("terminal"):
            # A terminal member ends the run and is folded in, because `terminal` on a group ends
            # the workflow after the whole span -- identical when it is the last member, and only
            # then. A terminal step anywhere earlier would move where the workflow ends, so the
            # run stops before it.
            while (j + 1 < len(tasks) and tasks[j + 1].get("condition") == cond):
                j += 1
                if tasks[j].get("terminal"):
                    break
        if j > i:
            members = []
            for t in tasks[i:j + 1]:
                t = dict(t)
                t.pop("condition")
                members.append(t)
            # Canonical key order for a group: id, name, description, condition, terminal, tasks.
            group = {"id": f"when_{members[0]['id']}", "condition": cond}
            if members[-1].pop("terminal", None):
                group["terminal"] = True
            group["tasks"] = members
            out.append(group)
        else:
            out.append(tasks[i])
            j = i
        i = j + 1
    return out


def grouped(doc: dict) -> dict:
    doc = dict(doc)
    doc["tasks"] = group_runs(doc["tasks"])
    return doc


def outputs() -> list[tuple[pathlib.Path, dict]]:
    out = [(PKG / "workflows" / f"{MATCH['workflow_id']}.json", grouped(MATCH)),
           (PKG / "workflows" / f"{ROSTER['workflow_id']}.json", grouped(ROSTER)),
           (PKG / "channels" / f"{ROSTER_CHANNEL['channel_id']}.json", ROSTER_CHANNEL)]
    out += [(PKG / "channels" / f"{c['channel_id']}.json", c) for c in MATCH_CHANNELS]
    return out


def render(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def main(check: bool) -> int:
    drift = []
    for path, doc in outputs():
        text = render(doc)
        if check:
            if not path.exists() or path.read_text() != text:
                drift.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(f"    {path.relative_to(PKG)}")
    if check:
        for path in drift:
            print(f"drift: {path.relative_to(PKG)} differs from the generator", file=sys.stderr)
        if drift:
            return 1
        print("==> generated files are current")
        return 0
    print(f"    tb-match: {len(MATCH_TASKS)} tasks, loop max {MATCH['loop']['max']}, "
          f"{len(MATCH_CHANNELS)} channels")
    print(f"    tb-roster: {len(ROSTER_TASKS)} tasks")
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv[1:]))
