#!/usr/bin/env python3
"""Generate Kalam's claim channel and its wave workflow.

    kalam/scripts/gen-kalam.py

Kalam is ONE cron channel and ONE workflow, and that workflow is a long JSON document whose
interesting content is SQL and JSONLogic. Both are unreadable inline in JSON and perfectly readable
here, so they live here and this script inlines them -- the same argument, and the same shape, as
`jodi/scripts/gen-jodi.py`.

The generated files ARE the package: committed, loaded by scripts/load-package.sh, and what a
reviewer reads for the task graph. Re-run this after changing a statement and commit the result.

The specification is `design/v2/03-kalam.md` (layer 03), whose statements are
`design/v2/01-match-table.md` §4. `design/v2/03-spike/orion/gen-spike.py` is the draft this grew
from: the spike drove the same loop against a stub engine and a stub loader and measured it, so
the loop shape, decision 33's drain and the strike accumulator are transcribed from something
watched working rather than argued.

=========================================================================================
WHAT THE BUILD FOUND THAT LAYER 03 DOES NOT SAY. Each of these is a thing the workflow could
not have been written correctly without, and none was discoverable from the documents.

  1. THE WAVE IS READ TWICE, and it has to be. Layer 03 §4.1 reads the claim once and numbers it,
     then the barrier drops some rows, then §4.2 builds worlds from "the started rows' seeds".
     Those two numberings disagree the moment the barrier refuses anything: the engine indexes its
     matches 0..n_started-1 while the refs still carry 0..n_claimed-1, so `observe` -- which
     matches refs on (m, seat) -- silently attaches no ref to any view, and the wave plays a
     thousand turns naming a model that does not exist. That is the exact failure the spike found
     once already. JSONLogic cannot renumber a list, so Postgres has to: the models are read
     BEFORE the barrier (unnumbered, for the hold) and the wave is read AFTER `start`, numbered,
     filtered on status = 'running'.

  2. THE BARRIER'S UNIT IS A MODEL, NOT A ROW. Layer 01 §4.4 takes a uuid[] of match ids, but the
     loader answers about (weights_hash, adapter_hash) pairs, and mapping a refused model back to
     the rows that seat it is a join from element scope into root scope -- the one thing JSONLogic
     here cannot do. So release and fail take the refused HASHES and let Postgres do the join.
     Fewer moving parts, and one round trip either way.

  3. `step` TAKES THE EXPLICIT {m, seat, action} FORM, not the positional one. Layer 03 §4.4
     specifies positional alignment with the last `observe`. That is correct only if every live
     seat is played, and §4.5 says a forfeited seat is not sent to the loader at all -- so the
     reply is shorter than the view list and every action after the first forfeit lands in the
     wrong chair. The engine accepts both forms; the explicit one is immune, and it is also the
     one an audit can read.

  4. A FORFEITED SEAT'S REF HAS TO BE CARRIED FORWARD BY HAND. Refs are rebuilt each turn from the
     play reply, and a seat that is not sent does not appear in it -- so rebuilding from the reply
     alone loses the `forfeited` flag and the seat is sent to the loader again next turn.

  5. STRIKES OUTLIVE THE MATCH THEY WERE EARNED IN. The drain finishes one match per sweep, and by
     the second sweep that match's seats are gone from the refs (`observe` returns nothing for a
     finished match). So `carry` snapshots the ending matches' refs into `data.done_refs` on the
     turn they end, which is the only turn they still exist.

  6. `/play`'s ROW REPLY HAS NO `fault` FIELD (layer 04 §3.2 vs. the built api.rs), so layer 03
     §4.7's "a fault Kalam can attribute mid-play" has no signal to branch on. Every per-row error
     is therefore a strike, which is §4.5's rule and covers the case.

Six mechanics of Orion 1.7.0, each measured on the running server (`design/v2/03-spike/FINDINGS.md`
has the ones the spike found; these are new):

  * `storage_presign` returns the URL as a PLAIN STRING, not an object.
  * `http_call` ALWAYS prefixes the connector's base URL onto `path`. An absolute URL in `path`
    produces `base + url`. So a presigned URL has to be reduced to its path and query, which is
    `substr(url, length(base))` -- and `length` does work on a string.
  * `force_path_style` on the storage connector is what makes that subtraction well defined: the
    presigned URL becomes `endpoint/bucket/key`, so the prefix to strip is exactly the endpoint.
    Without it the URL is virtual-hosted (`bucket.host/key`) and the prefix is a different string
    from anything either side is configured with.
  * `http_call` parses the response as JSON unless told otherwise, and an S3 PUT answers with an
    empty body -- `response_format: "text"`.
  * `{"now": []}` exists and returns an ISO-8601 string, which is where `played_ms` comes from.
  * `{"merge": [A, B]}` CONCATENATES two computed arrays (it does not flatten deeper), while
    `{"merge": <one computed expression>}` does not flatten at all. Both are used below.
=========================================================================================
"""

import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent

ENGINE = "tb.ants"


# ======================================================================= helpers


def sql(text: str) -> str:
    """Collapse a readable statement to the single line a JSON field holds.

    Line comments are stripped FIRST: collapsing whitespace turns a multi-line statement into one
    line, so a surviving `--` would comment out everything after it -- silently, because the
    truncated text is often still valid SQL. Lifted from jodi/scripts/gen-jodi.py, which learned it
    the hard way.
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


def task(tid: str, name: str, fn: dict, cond=None, terminal: bool = False) -> dict:
    t = {"id": tid, "name": name}
    if cond is not None:
        t["condition"] = cond
    t["function"] = fn
    if terminal:
        t["terminal"] = True
    return t


def mapping(*pairs) -> dict:
    """A `map` task. Mappings are applied IN ORDER and later ones see earlier ones."""
    return {"name": "map", "input": {"mappings": [{"path": p, "logic": l} for p, l in pairs]}}


def plugin(fn: str, inp: dict) -> dict:
    return {"name": fn, "input": inp}


def db_read(query: str, params: list, output: str) -> dict:
    return {"name": "db_read", "input": {
        "connector": "kalam-db", "query": sql(query), "params": params, "output": output}}


def db_write(query: str, params: list, output: str) -> dict:
    return {"name": "db_write", "input": {
        "connector": "kalam-db", "query": sql(query), "params": params, "output": output}}


def loader(path: str, body, output: str, method: str = "POST") -> dict:
    inp = {"connector": "model-loader", "method": method, "path": path, "output": output}
    if body is not None:
        inp["body"] = body
    return {"name": "http_call", "input": inp}


def halt_unless(condition: dict) -> dict:
    return {"name": "filter", "input": {"condition": condition, "on_reject": "halt"}}


def wrote(path: str) -> dict:
    """A fenced statement learns its fate from rows_affected: db_write returns nothing else."""
    return {">": [var(f"{path}.rows_affected"), 0]}


# ======================================================================= the statements
#
# Every one is layer 01 §4, and every one is conditioned on the claim token so a stale attempt
# updates nothing (principle 4). Where this file departs from 01 §4 as written, the reason is in
# the module docstring and repeated at the statement.

# --- 4.1 reap ---------------------------------------------------------------------------
# Its own statement rather than a CTE inside the claim: a CTE's writes are invisible to the claim
# in the same snapshot, so a folded-in reap would leave a reaped row waiting one more poll.
# `rows_affected` counts crashes and is worth a metric.
K_REAP = """
UPDATE matches
   SET status           = CASE WHEN lapses + 1 >= 3 THEN 'failed' ELSE 'pending' END::match_status,
       lapses           = lapses + 1,
       claim_token      = NULL,
       lease_expires_at = NULL,
       fault_reason     = CASE WHEN lapses + 1 >= 3 THEN 'LEASE_LAPSED' END,
       closed_at        = CASE WHEN lapses + 1 >= 3 THEN now() END
 WHERE status IN ('claimed', 'running')
   AND lease_expires_at < now()
"""

# --- 4.2 claim --------------------------------------------------------------------------
# $1 engine digest · $2 resident weights hashes · $3 K · $4 token · $5 lease seconds.
# Trial priority and affinity choose the first row; the wave is filled with rows sharing its models
# and its preset, so one inference serves the wave by construction (finding 9-11). SKIP LOCKED is
# the whole of the coordination between replicas.
K_CLAIM = """
WITH first AS MATERIALIZED (
    SELECT m.id, m.preset
      FROM matches m
     WHERE m.status = 'pending' AND m.engine_digest = ($1)::text
     ORDER BY (m.trial_model_id IS NOT NULL) DESC,
              EXISTS (SELECT 1 FROM match_seats s
                       WHERE s.match_id = m.id
                         AND s.weights_hash = ANY (($2)::text[])) DESC,
              m.created_at, m.id
     LIMIT 1
       FOR UPDATE SKIP LOCKED
), wave AS MATERIALIZED (
    SELECT m.id
      FROM matches m, first f
     WHERE m.status = 'pending' AND m.engine_digest = ($1)::text
       AND m.preset = f.preset
       AND (m.id = f.id
            OR EXISTS (SELECT 1 FROM match_seats a
                         JOIN match_seats b ON b.weights_hash = a.weights_hash
                        WHERE a.match_id = f.id AND b.match_id = m.id))
     ORDER BY (m.id = f.id) DESC, (m.trial_model_id IS NOT NULL) DESC, m.created_at, m.id
     LIMIT ($3)::int
       FOR UPDATE OF m SKIP LOCKED
)
UPDATE matches m
   SET status           = 'claimed',
       claim_token      = ($4)::uuid,
       lease_expires_at = now() + ($5)::int * interval '1 second'
  FROM wave
 WHERE m.id = wave.id
"""

# --- the models the wave needs ----------------------------------------------------------
# NOT in layer 01. It exists because the barrier runs before the wave can be numbered (docstring
# 1), and because deduplicating in SQL is one `DISTINCT` against a `distinct`-plus-`map` in
# JSONLogic. The reply is already the exact body /load wants.
K_MODELS = """
SELECT DISTINCT s.weights_hash, s.adapter_hash
  FROM matches m
  JOIN match_seats s ON s.match_id = m.id
 WHERE m.claim_token = ($1)::uuid AND m.status = 'claimed'
 ORDER BY s.weights_hash, s.adapter_hash
"""

# --- 4.4 release ------------------------------------------------------------------------
# Refused for want of memory: back to the queue, NO LAPSE SPENT, under its own ceiling. Keyed by
# the refused weights hashes rather than by row ids (docstring 2).
# $1 token · $2 refused weights hashes · $3 the refusal ceiling.
K_RELEASE = """
UPDATE matches
   SET status           = CASE WHEN refusals + 1 >= ($3)::int THEN 'failed' ELSE 'pending' END::match_status,
       refusals         = refusals + 1,
       claim_token      = NULL,
       lease_expires_at = NULL,
       fault_reason     = CASE WHEN refusals + 1 >= ($3)::int THEN 'UNLOADABLE' END,
       closed_at        = CASE WHEN refusals + 1 >= ($3)::int THEN now() END
 WHERE claim_token = ($1)::uuid AND status = 'claimed'
   AND EXISTS (SELECT 1 FROM match_seats s
                WHERE s.match_id = matches.id
                  AND s.weights_hash = ANY (($2)::text[]))
"""

# --- 4.4 fail, set-valued ---------------------------------------------------------------
# Refused BY NAME -- a hash mismatch, a graph or adapter that will not build. Failed at once, with
# the seat it is attributed to. This is layer 03 §4.2's ask of layer 01 §4.4, in the shape
# docstring 2 argues for: a barrier can refuse several models at once, and DISTINCT ON picks the
# lowest offending seat when a row has more than one.
# $1 token · $2 [{weights_hash, reason}].
K_FAIL_SET = """
UPDATE matches m
   SET status           = 'failed',
       fault_reason     = x.reason,
       fault_seat       = x.seat,
       closed_at        = now(),
       lease_expires_at = NULL
  FROM (SELECT DISTINCT ON (s.match_id) s.match_id, s.seat, v.reason
          FROM jsonb_to_recordset(($2)::jsonb) AS v (weights_hash text, reason text)
          JOIN match_seats s ON s.weights_hash = v.weights_hash
         ORDER BY s.match_id, s.seat) AS x
 WHERE m.id = x.match_id AND m.claim_token = ($1)::uuid AND m.status = 'claimed'
"""

# --- 4.4 start --------------------------------------------------------------------------
# Everything still 'claimed' once the two refusal statements have run. No id list is needed, which
# is the second half of docstring 2's simplification.
K_START = """
UPDATE matches SET status = 'running'
 WHERE claim_token = ($1)::uuid AND status = 'claimed'
"""

# --- 4.3 read the wave, numbered --------------------------------------------------------
# Layer 03 §4.1's ask of layer 01 §4.3, plus docstring 1: it runs AFTER `start` and filters on
# 'running', so `m` numbers the rows that will actually be played and the engine's match indices
# and the refs agree by construction. `m` is repeated onto every seat because the refs are a FLAT
# list the engine matches on (m, seat) -- JSONLogic cannot number a list, so Postgres does it.
K_WAVE = """
WITH w AS (
    SELECT m.id, m.seed, m.preset, m.seat_count, m.trial_model_id,
           (row_number() OVER (ORDER BY m.id) - 1)::int AS m
      FROM matches m
     WHERE m.claim_token = ($1)::uuid AND m.status = 'running'
)
SELECT json_build_object(
         'm', w.m, 'id', w.id, 'seed', w.seed, 'preset', w.preset,
         'seat_count', w.seat_count, 'trial_model_id', w.trial_model_id,
         'seats', (SELECT json_agg(json_build_object(
                      'm', w.m, 'seat', s.seat, 'model_id', s.model_id,
                      'weights_hash', s.weights_hash, 'adapter_hash', s.adapter_hash)
                    ORDER BY s.seat)
                     FROM match_seats s WHERE s.match_id = w.id)
       ) AS row
  FROM w
 ORDER BY w.m
"""

# --- 4.5 renew --------------------------------------------------------------------------
# The statement the table's fill factor exists for: no indexed column changes, so it stays
# heap-only. Zero rows means the lease was reaped and the wave belongs to someone else.
K_RENEW = """
UPDATE matches
   SET lease_expires_at = now() + ($2)::int * interval '1 second'
 WHERE claim_token = ($1)::uuid AND status = 'running'
"""

# --- 4.6 finish -------------------------------------------------------------------------
# $1 token · $2 match · $3 the result, one element per seat, forfeited seats ranked last ·
# $4 the engine's end reason · $5 turns · $6 when the wave opened · $7, $8 the digests that played
# it · $9 the replay key.
#
# `played_ms` is computed here from $6 rather than passed: Orion's `{"now": []}` gives the workflow
# an instant it can carry, and Postgres has the other one, so the subtraction happens where both
# are exact. Every match in a wave opens together, so this is the match's duration.
#
# rows_affected is seat_count; zero means the token is stale OR the result did not name every seat
# once, and in both cases nothing was written.
K_FINISH = """
WITH m AS (
    UPDATE matches
       SET status               = 'finished',
           reason               = ($4)::text,
           turns                = ($5)::int,
           played_ms            = GREATEST(0, (EXTRACT(EPOCH FROM (now() - ($6)::timestamptz)) * 1000)::int),
           engine_digest_played = ($7)::text,
           evaluator_digest     = ($8)::text,
           replay_key           = ($9)::text,
           played_at            = now(),
           lease_expires_at     = NULL
     WHERE id = ($2)::uuid AND claim_token = ($1)::uuid AND status = 'running'
       AND (SELECT count(DISTINCT v.seat)
              FROM jsonb_to_recordset(($3)::jsonb) AS v (seat smallint)
             WHERE v.seat BETWEEN 0 AND seat_count - 1) = seat_count
 RETURNING id
)
UPDATE match_seats s
   SET rank = v.rank, score = v.score, strikes = v.strikes
  FROM m,
       jsonb_to_recordset(($3)::jsonb) AS v (seat smallint, rank smallint, score int, strikes smallint)
 WHERE s.match_id = m.id AND s.seat = v.seat
"""


# ======================================================================= the conditions

TURN0 = {"==": [var("temp_data.i"), 0]}
LIVE = {">": [var("temp_data.n_live"), 0]}
PENDING = {">": [var("temp_data.n_pending"), 0]}
# Decision 33's tail drain: the wave is over when nothing is live AND the finish queue is empty.
# `observe` returns no views once every match has ended, so the extra sweeps cost almost nothing.
OVER = {"and": [{"==": [var("temp_data.n_live"), 0]},
                {"==": [var("temp_data.n_pending"), 0]}]}
NOTHING_STARTED = {"and": [TURN0, {"==": [var("temp_data.started.rows_affected"), 0]}]}
DO_RENEW = {"and": [LIVE,
                    {">": [var("temp_data.i"), 0]},
                    {"==": [{"%": [var("temp_data.i"), vars_("renew_every_n_turns")]}, 0]}]}
# Layer 03 §4.6: halt on ANY shortfall, where the expectation is the rows this wave still has
# running. All of a wave's rows carry one lease and expire together, so a partial renew cannot
# happen in the ordinary course -- if it does, this replica's grip is not what it believes.
RENEW_LOST = {"and": [DO_RENEW,
                      {"<": [var("temp_data.renewed.rows_affected"), var("data.n_running")]}]}


# ======================================================================= the run

TASKS = [
    # ------------------------------------------------------------------ turn 0: claim
    task("token", "Mint this attempt's claim token", mapping(
        # One token for the whole wave, minted before the claim and carried in `data` so the
        # renew and every finish condition on the same value. It is the attempt: a stale replica's
        # finish updates nothing because the token has moved (principle 4).
        ("data.token", {"random": ["uuid"]}),
        ("data.opened_at", {"now": []}),
    ), cond=TURN0),

    # A MISSING [vars] VALUE IS SILENT AND CATASTROPHIC, so it is checked once, loudly, at the
    # first task of every wave. `{">=": [1, null]}` is TRUE under this engine's loose equality --
    # so a `strike_ceiling` that did not resolve forfeits every seat on turn 0, the next turn sends
    # the loader nothing, and the wave dies at `step` with "0 actions for N live seats", which
    # names neither the variable nor the cause. Jodi's config records the same lesson as
    # "`clippy -c` makes a missing var loud"; a cron workflow has no such check, so this is it.
    task("vars", "Halt unless this replica is configured", halt_unless({"and": [
        {"!=": [vars_("engine_digest"), None]},
        {">": [vars_("wave_k"), 0]},
        {">": [vars_("lease_seconds"), 0]},
        {">": [vars_("renew_every_n_turns"), 0]},
        {">": [vars_("turn_ms"), 0]},
        {">": [vars_("max_turns"), 0]},
        {">": [vars_("budget_ops"), 0]},
        {">": [vars_("strike_ceiling"), 0]},
        {">": [vars_("refusal_ceiling"), 0]},
        {"!=": [vars_("replay_prefix"), None]},
        {"!=": [vars_("blob_endpoint"), None]},
    ]}), cond=TURN0),

    task("resident", "What this replica's loader already holds", loader(
        "/resident", None, "temp_data.res", method="GET",
    ), cond=TURN0),

    task("reap", "Return lapsed leases to the queue", db_write(
        K_REAP, [], "temp_data.reaped",
    ), cond=TURN0),

    task("claim", "Claim a wave", db_write(
        K_CLAIM, [vars_("engine_digest"),
                  # Advisory and allowed to be stale -- affinity is an optimisation of the fill and
                  # the barrier is where correctness lives. `loading` is deliberately NOT passed on,
                  # so a wave is never filled with rows whose models are still cold.
                  var("temp_data.res.weights"),
                  vars_("wave_k"), var("data.token"), vars_("lease_seconds")],
        "temp_data.claim",
    ), cond=TURN0),

    task("claimed", "Nothing to play: end the run here", halt_unless(wrote("temp_data.claim")),
         cond=TURN0),

    # ------------------------------------------------------------------ turn 0: the barrier
    task("models", "The wave's distinct models", db_read(
        K_MODELS, [var("data.token")], "temp_data.mods",
    ), cond=TURN0),

    task("hold", "Ask the loader to hold them -- one call for the whole wave", loader(
        "/load", {"models": var("temp_data.mods")}, "temp_data.hold",
    ), cond=TURN0),

    task("split", "Partition the reply by fault, not by reason word", mapping(
        # Layer 04 §6: the split reads `fault`, so a reason word added to the loader later costs no
        # change here. `===` throughout: `fault` is absent on a resident model, and loose equality
        # against a path that does not resolve silently selects the falsy elements.
        ("temp_data.refused_mem", {"map": [
            {"filter": [var("temp_data.hold.models"),
                        {"or": [{"===": [var("fault"), "loader"]},
                                {"===": [var("state"), "loading"]}]}]},
            var("weights_hash")]}),
        ("temp_data.refused_named", {"map": [
            {"filter": [var("temp_data.hold.models"), {"===": [var("fault"), "model"]}]},
            {"weights_hash": var("weights_hash"), "reason": var("reason")}]}),
        ("temp_data.n_mem", {"length": [var("temp_data.refused_mem")]}),
        ("temp_data.n_named", {"length": [var("temp_data.refused_named")]}),
    ), cond=TURN0),

    task("release", "Refused for memory: back to the queue, no attempt spent", db_write(
        K_RELEASE, [var("data.token"), var("temp_data.refused_mem"), vars_("refusal_ceiling")],
        "temp_data.released",
    ), cond={"and": [TURN0, {">": [var("temp_data.n_mem"), 0]}]}),

    task("fail", "Refused by name: failed at once, with the seat", db_write(
        K_FAIL_SET, [var("data.token"), var("temp_data.refused_named")], "temp_data.failed",
    ), cond={"and": [TURN0, {">": [var("temp_data.n_named"), 0]}]}),

    task("start", "Everything still claimed is now running", db_write(
        K_START, [var("data.token")], "temp_data.started",
    ), cond=TURN0),

    # A halt cannot release the models, so the empty-wave exit is an unload plus a terminal task
    # rather than a filter. Layer 03 §4.2: "nothing started means unload and end".
    task("idle-unload", "Nothing playable: release what was held", loader(
        "/unload", {"models": var("temp_data.mods")}, "temp_data.unheld",
    ), cond=NOTHING_STARTED),
    task("idle", "Nothing playable: end the run", mapping(
        ("data.outcome", "nothing_started"),
    ), cond=NOTHING_STARTED, terminal=True),

    # ------------------------------------------------------------------ turn 0: open the wave
    task("wave", "Read the wave, numbered by Postgres", db_read(
        K_WAVE, [var("data.token")], "temp_data.w",
    ), cond=TURN0),

    task("open", "Rows, seats and the refs the whole run rides on", mapping(
        ("data.rows", {"map": [var("temp_data.w"), var("row")]}),
        # `{"merge": <a computed array of arrays>}` does NOT flatten -- it flattens only the
        # arguments written out in the definition. The flatten is a reduce over merge.
        ("data.seats", {"reduce": [{"map": [var("data.rows"), var("seats")]},
                                   {"merge": [var("accumulator"), var("current")]}, []]}),
        # THE REFS. A flat list, each entry carrying its own m and seat, which the engine matches
        # on rather than indexes -- and where the strike counters live, because a fixed task list
        # has no other way to accumulate anything per seat across turns. Element scope does not
        # nest inside root scope, so the counter has to travel WITH the seat it counts: out through
        # `observe`, onto the play row, back on the loader's echoed `ref`, and into the next turn.
        # Neither the engine nor the loader ever looks inside one.
        ("data.refs", {"map": [var("data.seats"),
                               {"m": var("m"), "seat": var("seat"),
                                "weights_hash": var("weights_hash"),
                                "adapter_hash": var("adapter_hash"),
                                "strikes": 0, "forfeited": False}]}),
        ("data.models", var("temp_data.mods")),
        ("data.n_running", {"length": [var("data.rows")]}),
        ("data.pending", []),        # ended, not yet finished -- decision 33's queue
        ("data.deltas", []),         # the replay stream, drained as matches finish
        ("data.done_refs", []),      # the refs of ended matches, snapshotted the turn they end
        ("data.finished", 0),
        ("data.strikes", 0),
    ), cond=TURN0),

    task("world", "Build the wave's worlds", plugin(f"{ENGINE}.worldgen", {
        "seeds": {"map": [var("data.rows"), var("seed")]},
        "preset": var("data.rows.0.preset"),
        # Decision 14: the preset carries the seat count and the engine refuses a caller that
        # disagrees, so passing it is a free check rather than a parameter.
        "players": var("data.rows.0.seat_count"),
        "max_turns": vars_("max_turns"),
        "output": "temp_data.w0",
    }), cond=TURN0),

    task("init", "Open the wave", mapping(
        ("data.state", var("temp_data.w0.wave_state")),
    ), cond=TURN0),

    # ------------------------------------------------------------------ every turn
    task("observe", "Every live seat's view of every live match", plugin(f"{ENGINE}.observe", {
        "wave_state": var("data.state"),
        "refs": var("data.refs"),
        "output": "temp_data.obs",
    })),

    task("counts", "How much is left", mapping(
        ("temp_data.n_live", {"length": [var("temp_data.obs.views")]}),
        ("temp_data.n_pending", {"length": [var("data.pending")]}),
        # A forfeited seat is not sent to the loader at all -- decision 16: it plays the no-op by
        # construction, and the loader is not asked to run a model whose action is discarded.
        ("temp_data.playing", {"filter": [var("temp_data.obs.views"),
                                          {"!": [var("ref.forfeited")]}]}),
    )),

    task("play", "One play call for the whole wave", {
        "name": "http_call",
        "input": {
            "connector": "model-loader", "method": "POST", "path": "/play",
            "body": {
                # Element-relative throughout. Joining back to `data.rows` by the view's own m and
                # seat evaluates to null inside a `map` -- silently -- which is what `ref` exists
                # to avoid (03-spike FINDINGS 2.6).
                "rows": {"map": [var("temp_data.playing"), {
                    "weights_hash": var("ref.weights_hash"),
                    "adapter_hash": var("ref.adapter_hash"),
                    "observation": var("view"),
                    "ref": var("ref"),          # echoed verbatim -- layer 04 §3.2
                }]},
                "deadline_ms": vars_("turn_ms"),
                "budget_ops": vars_("budget_ops"),
            },
            "output": "temp_data.play"},
    }, cond=LIVE),

    task("acts", "Errors become the no-op; strikes accumulate", mapping(
        # The explicit {m, seat, action} form, not the positional one (docstring 3). A forfeited
        # seat is simply absent, and the engine plays the no-op for any seat it is given nothing
        # for -- so omission IS the no-op, with no empty row to keep aligned.
        ("temp_data.acts", {"map": [var("temp_data.play.rows"),
                                    {"m": var("ref.m"), "seat": var("ref.seat"),
                                     "action": {"??": [var("action"), []]}}]}),
        # The strikes come out of the same walk over the reply, because the reply is the only place
        # a row's error and the identity of its seat are in the same object. Counted CUMULATIVELY:
        # five missed clocks in a match, not five in a row. The stricter reading, and the one a
        # competitor cannot game by hiccupping every fourth turn.
        #
        # A `reduce` AND NOT A `map`, for one reason that cost an afternoon: `metadata.vars` is
        # ROOT scope, exactly as `data` is, so `{"var": "metadata.vars.strike_ceiling"}` inside a
        # map body is NULL -- and `{">=": [0, null]}` is TRUE under this engine's loose equality.
        # A map here forfeits every seat on turn 0, sends the loader nothing on turn 1, and dies at
        # `step` with "0 actions for N live seats", which names neither the variable nor the cause.
        # `reduce`'s seed is the one expression evaluated at root that threads into the body, so
        # the ceiling rides in the accumulator beside the list being built.
        ("temp_data.nr", {"reduce": [
            var("temp_data.play.rows"),
            {"c": var("accumulator.c"),
             "items": {"merge": [var("accumulator.items"), [{
                 "m": var("current.ref.m"), "seat": var("current.ref.seat"),
                 "weights_hash": var("current.ref.weights_hash"),
                 "adapter_hash": var("current.ref.adapter_hash"),
                 "strikes": {"+": [var("current.ref.strikes"),
                                   {"if": [var("current.action"), 0, 1]}]},
                 "forfeited": {"or": [var("current.ref.forfeited"),
                                      {">=": [{"+": [var("current.ref.strikes"),
                                                     {"if": [var("current.action"), 0, 1]}]},
                                              var("accumulator.c")]}]},
             }]]}},
            {"c": vars_("strike_ceiling"), "items": []}]}),
        ("temp_data.next_refs", var("temp_data.nr.items")),
        # Docstring 4: a forfeited seat is not in the reply, so rebuilding from the reply alone
        # would drop its `forfeited` flag and send it to the loader again next turn. Carry it.
        # `{"merge": [A, B]}` concatenates two computed arrays -- measured, not assumed.
        ("data.refs", {"merge": [var("temp_data.next_refs"),
                                 {"filter": [var("data.refs"), var("forfeited")]}]}),
        ("temp_data.struck", {"reduce": [
            {"map": [var("temp_data.play.rows"), {"if": [var("action"), 0, 1]}]},
            {"+": [var("accumulator"), var("current")]}, 0]}),
    ), cond=LIVE),

    task("step", "Advance every live match by one turn", plugin(f"{ENGINE}.step", {
        "wave_state": var("data.state"),
        "actions": var("temp_data.acts"),
        "output": "temp_data.st",
    }), cond=LIVE),

    task("carry", "Carry the state, queue what ended", mapping(
        ("data.state", var("temp_data.st.wave_state")),
        ("data.deltas", {"merge": [var("data.deltas"), var("temp_data.st.replay_delta")]}),
        ("data.pending", {"merge": [var("data.pending"), var("temp_data.st.ended")]}),
        # Docstring 5: the ONLY turn on which an ending match's seats still exist in the refs is
        # this one. `reduce`'s seed is the one expression evaluated at root that threads into the
        # body, so it is how `temp_data.st.ended` and the existing accumulator reach element scope.
        ("temp_data.dr", {"reduce": [
            var("data.refs"),
            {"if": [{"in": [var("current.m"), var("accumulator.ended")]},
                    {"ended": var("accumulator.ended"),
                     "items": {"merge": [var("accumulator.items"), [var("current")]]}},
                    var("accumulator")]},
            {"ended": var("temp_data.st.ended"), "items": var("data.done_refs")}]}),
        ("data.done_refs", var("temp_data.dr.items")),
        ("data.strikes", {"+": [var("data.strikes"), var("temp_data.struck")]}),
    ), cond=LIVE),

    # ------------------------------------------------------------------ renew
    task("renew", "Extend the wave's lease", db_write(
        K_RENEW, [var("data.token"), vars_("lease_seconds")], "temp_data.renewed",
    ), cond=DO_RENEW),

    task("lost-unload", "The wave is not ours any more: release the models", loader(
        "/unload", {"models": var("data.models")}, "temp_data.unheld",
    ), cond=RENEW_LOST),
    task("lost", "Halt: the lease was reaped under us", mapping(
        ("data.outcome", "lease_lost"),
    ), cond=RENEW_LOST, terminal=True),

    # ------------------------------------------------------------------ decision 33's drain
    # One ended match finished per sweep, from a queue. The alternative was K conditioned
    # presign-PUT-finish triples in the task list: 3K condition evaluations every turn for a thing
    # that fires once per match, against six tasks whatever K is. The delay a drain adds is a few
    # turns, invisible against count's ten-second tick.
    task("results", "Ranks, scores and an end reason", plugin(f"{ENGINE}.finish", {
        # Called while matches are still running, which layer 03 §8 asked layer 05 to rule on: the
        # engine reports `done` per match and the drain reads only the head's entry, so it is safe.
        "wave_state": var("data.state"), "output": "temp_data.fin",
    }), cond=PENDING),

    task("pick", "The head of the queue: its row, its result, its seats, its deltas", mapping(
        ("temp_data.head", {"val": ["data", "pending", 0]}),
        # `data.rows` is ordered by m and m is 0-based, so the index IS the match number.
        ("temp_data.hrow", {"val": ["data", "rows", {"val": ["temp_data", "head"]}]}),
        ("temp_data.hres", {"reduce": [
            var("temp_data.fin.results"),
            {"if": [{"===": [var("current.m"), var("accumulator.head")]},
                    {"head": var("accumulator.head"), "r": var("current")},
                    var("accumulator")]},
            {"head": var("temp_data.head"), "r": None}]}),
        ("temp_data.hrefs", {"reduce": [
            var("data.done_refs"),
            {"if": [{"===": [var("current.m"), var("accumulator.head")]},
                    {"head": var("accumulator.head"),
                     "items": {"merge": [var("accumulator.items"), [var("current")]]}},
                    var("accumulator")]},
            {"head": var("temp_data.head"), "items": []}]}),
        ("temp_data.hdeltas", {"reduce": [
            var("data.deltas"),
            {"if": [{"===": [var("current.m"), var("accumulator.head")]},
                    {"head": var("accumulator.head"),
                     "items": {"merge": [var("accumulator.items"), [var("current")]]}},
                    var("accumulator")]},
            {"head": var("temp_data.head"), "items": []}]}),
        # THE RESULT, one element per seat. Forfeited seats rank last -- finding 12.5 -- and the
        # rule is `engine_rank + seat_count` rather than "set them all to last", because two
        # forfeited seats must not tie with a seat that played, and ranks need not be dense
        # (decision 12: any order-preserving relabelling gives identical TrueSkill output).
        # The engine's OWN ranks go in the replay envelope untouched, so an audit can still see
        # what the game thought happened.
        # `.items`, not the accumulator: a reduce returns its accumulator, and reducing over the
        # OBJECT instead of its list iterates the object's keys and produces rows of nulls -- which
        # the finish statement then correctly refuses, because they name no seat. It fails exactly
        # where it should and says nothing about why, so: every reduce result below is unwrapped.
        ("temp_data.seatrows", {"reduce": [
            var("temp_data.hrefs.items"),
            {"r": var("accumulator.r"),
             "n": var("accumulator.n"),
             "items": {"merge": [var("accumulator.items"), [{
                 "seat": var("current.seat"),
                 "rank": {"+": [{"val": ["accumulator", "r", "ranks", {"val": ["current", "seat"]}]},
                                {"if": [var("current.forfeited"), var("accumulator.n"), 0]}]},
                 "score": {"val": ["accumulator", "r", "scores", {"val": ["current", "seat"]}]},
                 "strikes": var("current.strikes"),
             }]]}},
            {"r": var("temp_data.hres.r"), "n": var("temp_data.hrow.seat_count"), "items": []}]}),
        # The key names the ATTEMPT, so a stale attempt's blob is an orphan under its own key
        # rather than a replacement for the one that counted (finding 7d).
        ("temp_data.key", {"cat": [vars_("replay_prefix"), "/",
                                   var("temp_data.hrow.id"), "/",
                                   var("data.token"), ".json"]}),
    ), cond=PENDING),

    task("presign", "Sign a PUT for this attempt's replay", {
        "name": "storage_presign",
        "input": {"connector": "kalam-blobs", "method": "PUT",
                  "key": var("temp_data.key"), "expires_in": "15m",
                  "output": "temp_data.signed"},
    }, cond=PENDING),

    task("put", "Write the replay envelope", {
        "name": "http_call",
        "input": {
            "connector": "kalam-blobs-put", "method": "PUT",
            # `storage_presign` returns a plain string, and `http_call` always prefixes the
            # connector's base -- so the presigned URL has to be reduced to its path and query.
            # `force_path_style` on the storage connector is what makes this subtraction exact:
            # the URL is `endpoint/bucket/key?sig`, so the prefix is precisely the endpoint.
            "path": {"substr": [var("temp_data.signed"), {"length": [vars_("blob_endpoint")]}]},
            "body": {
                "match_id": var("temp_data.hrow.id"),
                "attempt_token": var("data.token"),
                "seed": var("temp_data.hrow.seed"),
                "preset": var("temp_data.hrow.preset"),
                "engine_digest": vars_("engine_digest"),
                "evaluator_digest": var("temp_data.play.evaluator_digest"),
                "dialect_version": var("temp_data.play.dialect_version"),
                # The engine's own ranks, BEFORE forfeits are applied -- finding 12.5.
                "engine_ranks": var("temp_data.hres.r.ranks"),
                "scores": var("temp_data.hres.r.scores"),
                "reason": var("temp_data.hres.r.reason"),
                "turns": var("temp_data.hres.r.turns"),
                # The action stream, not frames -- DESIGN.md §8. `replay-decode` re-simulates it.
                "deltas": var("temp_data.hdeltas.items"),
            },
            # An S3 PUT answers with an empty body, and http_call parses JSON unless told not to.
            "response_format": "text",
            "output": "temp_data.putres"},
    }, cond=PENDING),

    task("finish", "Finish the row -- one statement, on the token", db_write(
        K_FINISH, [var("data.token"), var("temp_data.hrow.id"), var("temp_data.seatrows.items"),
                   var("temp_data.hres.r.reason"), var("temp_data.hres.r.turns"),
                   var("data.opened_at"), vars_("engine_digest"),
                   var("temp_data.play.evaluator_digest"), var("temp_data.key")],
        "temp_data.wrote",
    ), cond=PENDING),

    task("counted", "Halt if the finish wrote nothing: the token is stale",
         halt_unless(wrote("temp_data.wrote")), cond=PENDING),

    task("pop", "Drop the head, and its deltas with it", mapping(
        ("data.pending", {"slice": [var("data.pending"), 1]}),
        # The complement of `pick`'s filter, so the wave's delta stream shrinks as matches finish
        # and never carries a finished match's history to the end of the run.
        ("temp_data.dk", {"reduce": [
            var("data.deltas"),
            {"if": [{"===": [var("current.m"), var("accumulator.head")]},
                    var("accumulator"),
                    {"head": var("accumulator.head"),
                     "items": {"merge": [var("accumulator.items"), [var("current")]]}}]},
            {"head": var("temp_data.head"), "items": []}]}),
        ("data.deltas", var("temp_data.dk.items")),
        ("data.n_running", {"-": [var("data.n_running"), 1]}),
        ("data.finished", {"+": [var("data.finished"), 1]}),
    ), cond=PENDING),

    # ------------------------------------------------------------------ the end
    task("unload", "Release the wave's models", loader(
        "/unload", {"models": var("data.models")}, "temp_data.unheld",
    ), cond=OVER),

    task("over", "Every match played and every finish drained", mapping(
        ("data.outcome", "complete"),
        ("data.stopped_at_turn", var("temp_data.i")),
    ), cond=OVER, terminal=True),
]


# ======================================================================= the documents

WAVE = {
    "workflow_id": "tb-wave-run",
    "name": "Kalam: one wave, turn by turn",
    "description": (
        "Claim up to K pending rows on this replica's engine digest, hold their models in the "
        "loader beside it, and play them turn-synchronously: observe -> one play call -> step. "
        "Each row is finished AS ITS MATCH ENDS, one per sweep from a queue (decision 33), with "
        "its replay under a key naming the attempt. Every statement is conditioned on the claim "
        "token, so a stale attempt updates nothing. It reads no rating and writes no rating, and "
        "its database role cannot reach one. On SIGTERM Orion stops claiming and lets the wave in "
        "hand finish inside cron.shutdown_timeout_secs. 03-kalam.md; the statements are "
        "01-match-table.md §4."
    ),
    "tags": ["pkg:kalam"],
    "condition": True,
    # max_turns + K: the drain finishes one match per sweep, so a wave whose matches all end on the
    # last turn needs K more sweeps to empty its queue (layer 03 §4).
    "loop": {"counter": "i", "max": 1100},
    "tasks": TASKS,
}

CHANNEL = {
    "channel_id": "tb-wave",
    "name": "tb-wave",
    "tags": ["pkg:kalam"],
    "channel_type": "async",
    "protocol": "cron",
    "workflow_id": "tb-wave-run",
    "transport_config": {
        "schedule": "*/5 * * * * *",
        "timezone": "UTC",
        # A poll missed while a wave was running has nothing to catch up: the queue is still there
        # and the next tick claims from it. `catch_up` would fire a burst of claims for no gain.
        "misfire_policy": "skip",
        # `forbid` on key `wave`, and the key is LOCAL in effect: Kalam's Orion runs on local
        # SQLite with no shared state, so the lock is per replica -- one wave in flight per
        # replica, N replicas in parallel. The opposite of Jodi's use of the identical spelling.
        "concurrency": {"policy": "forbid", "key": "wave"},
    },
    "config": {
        # Above the longest match: max_turns x turn_ms plus the platform's own time and the
        # drain's tail. The spike's measured full wave was 73 seconds.
        "timeout_ms": 2400000,
        # errors_only from day one (overview §7). A thousand-turn wave is thousands of task
        # executions in one occurrence, and tracing a clean one writes more trace than match.
        "tracing": {"errors_only": True, "task_details": True},
    },
}


def write(path: pathlib.Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"    {path.relative_to(PKG)}")


def main() -> None:
    print("==> workflows")
    write(PKG / "workflows" / f"{WAVE['workflow_id']}.json", WAVE)
    print("==> channels")
    write(PKG / "channels" / f"{CHANNEL['channel_id']}.json", CHANNEL)
    print(f"    {len(TASKS)} tasks, loop max {WAVE['loop']['max']}")


if __name__ == "__main__":
    main()
