#!/usr/bin/env python3
"""Generate Kalam's wave channel and its workflow.

The generated files ARE the package: committed, loaded by scripts/load-package.sh, and what a
reviewer reads for the task graph. Re-run this after any edit here and commit the output with it.
The statements are soma/docs/schema.md §4; the task graph is docs/design.md §4.

Orion 1.7.0 mechanics the workflow below depends on, none of them obvious:

  * ELEMENT SCOPE DOES NOT NEST INSIDE ROOT SCOPE. Inside a `map`, `filter` or `reduce` body,
    `{"var": "data.x"}` and `{"var": "metadata.vars.x"}` are null -- and `{"==": [0, null]}` is
    TRUE under this engine's loose equality, so the mistake selects the falsy elements rather than
    failing. Compare with `===`, and carry any root value a body needs in `reduce`'s seed, the one
    expression evaluated at root that threads into the body. `sift` below is that trick.
  * JSONLogic cannot number a list, so Postgres numbers the wave (K_WAVE).
  * `{"merge": [A, B]}` concatenates two computed arrays; `{"merge": <one expression>}` does not
    flatten at all. Flattening a computed array of arrays is a reduce over merge.
  * `storage_presign` returns a plain string, and `http_call` always prefixes its connector's base
    URL onto `path`, so a presigned URL must be reduced to `substr(url, length(base))`.
    `force_path_style` on the storage connector makes that subtraction exact: the URL is
    `endpoint/bucket/key`, so the prefix is precisely the endpoint.
  * `http_call` parses the reply as JSON unless told otherwise, and an S3 PUT answers with an empty
    body -- hence `response_format: "text"`.
  * `{"now": []}` returns an ISO-8601 string.
"""

import json
import pathlib
import re

PKG = pathlib.Path(__file__).resolve().parent.parent

ENGINE = "tb.ants"


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


def loader(path: str, output: str, body=None, method: str = "POST") -> dict:
    return http("model-loader", method, path, output, body)


def unload(models: dict) -> dict:
    """A `filter` halt cannot release the models, so every exit from a wave is an unload plus a
    terminal task rather than a halt."""
    return loader("/unload", "temp_data.unheld", {"models": models})


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


# ======================================================================= the statements
#
# Every one is soma/docs/schema.md §4, and every one is conditioned on the claim token so a stale
# attempt updates nothing.

# --- 4.1 reap: its own statement rather than a CTE inside the claim, because a CTE's writes are
# invisible to the claim in the same snapshot and a reaped row would wait one more poll.
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

# --- 4.2 claim. $1 engine digest · $2 resident weights hashes · $3 K · $4 token · $5 lease seconds.
# Trial priority and affinity choose the first row; the wave is filled with rows sharing its models
# and its preset, so one inference serves the wave by construction. SKIP LOCKED is the whole of the
# coordination between replicas.
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

# --- the models the wave needs, read before the barrier and so before the wave can be numbered.
# The reply is already the exact body /load wants.
K_MODELS = """
SELECT DISTINCT s.weights_hash, s.adapter_hash
  FROM matches m
  JOIN match_seats s ON s.match_id = m.id
 WHERE m.claim_token = ($1)::uuid AND m.status = 'claimed'
 ORDER BY s.weights_hash, s.adapter_hash
"""

# --- 4.4 release. Refused for want of memory: back to the queue, NO LAPSE SPENT, under its own
# ceiling. Keyed by the refused weights HASHES, because mapping a refused model back to its rows is
# a join from element scope into root scope -- which JSONLogic cannot do and Postgres can.
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

# --- 4.4 fail, set-valued. Refused BY NAME -- a hash mismatch, a graph or adapter that will not
# build. Failed at once, with the seat it is attributed to; DISTINCT ON picks the lowest offending
# seat when a row has more than one. $1 token · $2 [{weights_hash, reason}].
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

# --- 4.4 start: everything still 'claimed' once the two refusal statements have run, so no id list
# is needed.
K_START = """
UPDATE matches SET status = 'running'
 WHERE claim_token = ($1)::uuid AND status = 'claimed'
"""

# --- 4.3 read the wave, numbered. It runs AFTER `start` and filters on 'running', so the engine's
# match indices and the refs agree by construction -- number the claim instead and the barrier's
# refusals shift every index, silently seating one competitor's model in another's chair. `m` is
# repeated onto every seat because the refs are a FLAT list the engine matches on (m, seat).
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

# --- 4.5 renew. No indexed column changes, so it stays heap-only -- the statement the table's fill
# factor exists for. Zero rows means the lease was reaped and the wave belongs to someone else.
K_RENEW = """
UPDATE matches
   SET lease_expires_at = now() + ($2)::int * interval '1 second'
 WHERE claim_token = ($1)::uuid AND status = 'running'
"""

# --- 4.6 finish. $1 token · $2 match · $3 the result, one element per seat · $4 the engine's end
# reason · $5 turns · $6 when the wave opened · $7, $8 the digests that played it · $9 replay key.
#
# `played_ms` is subtracted here because Postgres holds one instant and the workflow carries the
# other. rows_affected is seat_count; zero means the token is stale OR the result did not name
# every seat once, and in both cases nothing was written.
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
   SET rank = v.rank, score = v.score, strikes = v.strikes,
       infer_us_total = v.infer_us_total, infer_us_max = v.infer_us_max,
       infer_turns = v.infer_turns
  FROM m,
       jsonb_to_recordset(($3)::jsonb) AS v (seat smallint, rank smallint, score int,
                                             strikes smallint, infer_us_total bigint,
                                             infer_us_max int, infer_turns int)
 WHERE s.match_id = m.id AND s.seat = v.seat
"""


# ======================================================================= the conditions

TURN0 = {"==": [var("temp_data.i"), 0]}
LIVE = {">": [var("temp_data.n_live"), 0]}
PENDING = {">": [var("temp_data.n_pending"), 0]}
# The wave is over when nothing is live AND the finish queue is empty. `observe` returns no views
# once every match has ended, so the tail sweeps cost almost nothing.
OVER = {"and": [{"==": [var("temp_data.n_live"), 0]},
                {"==": [var("temp_data.n_pending"), 0]}]}
NOTHING_STARTED = {"and": [TURN0, {"==": [var("temp_data.started.rows_affected"), 0]}]}
DO_RENEW = {"and": [LIVE,
                    {">": [var("temp_data.i"), 0]},
                    {"==": [{"%": [var("temp_data.i"), vars_("renew_every_n_turns")]}, 0]}]}
# Halt on ANY shortfall. All of a wave's rows carry one lease and expire together, so a partial
# renew cannot happen in the ordinary course -- if it does, this replica's grip is not what it
# believes.
RENEW_LOST = {"and": [DO_RENEW,
                      {"<": [var("temp_data.renewed.rows_affected"), var("data.n_running")]}]}

# The head of the finish queue, in element scope. `===`: a `==` against a path that does not
# resolve silently selects the falsy elements instead.
IS_HEAD = {"===": [var("current.m"), var("accumulator.head")]}
HEAD = {"head": var("temp_data.head")}


# ======================================================================= the run

# One strike per row the loader could not play. Bound to a name because the forfeit test needs the
# same expression, and the two must not drift.
NEXT_STRIKES = {"+": [var("current.ref.strikes"), {"if": [var("current.action"), 0, 1]}]}

# What the row's model COST this turn: its share of its own group's inference, from the loader. Not
# `elapsed_ms`, which runs from a row entering the call to leaving it and so reports roughly the
# whole call for every row. Defaulted because an error row carries no figure worth adding.
THIS_INFER_US = {"??": [var("current.infer_us"), 0]}
NEXT_INFER_TOTAL = {"+": [var("current.ref.infer_us_total"), THIS_INFER_US]}
NEXT_INFER_MAX = {"if": [{">": [THIS_INFER_US, var("current.ref.infer_us_max")]},
                         THIS_INFER_US, var("current.ref.infer_us_max")]}
NEXT_INFER_TURNS = {"+": [var("current.ref.infer_turns"), 1]}

TASKS = [
    # ------------------------------------------------------------------ turn 0: claim
    # One token for the whole wave, minted before the claim and carried in `data`, so the renew and
    # every finish condition on the same value: a stale replica's finish updates nothing.
    task("token", "Mint this attempt's claim token", mapping(
        ("data.token", {"random": ["uuid"]}),
        ("data.opened_at", {"now": []}),
    ), cond=TURN0),

    # A MISSING [vars] VALUE IS SILENT AND CATASTROPHIC: `{">=": [1, null]}` is true here, so an
    # unresolved `strike_ceiling` forfeits every seat on turn 0 and the wave dies two turns later
    # at `step`, naming neither the variable nor the cause. So: checked once, loudly, up front.
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
        "/resident", "temp_data.res", method="GET",
    ), cond=TURN0),

    task("reap", "Return lapsed leases to the queue", db(
        "db_write", K_REAP, [], "temp_data.reaped",
    ), cond=TURN0),

    task("claim", "Claim a wave", db(
        "db_write", K_CLAIM,
        # The resident list is advisory and allowed to be stale -- affinity is an optimisation of
        # the fill and the barrier is where correctness lives. `loading` is deliberately NOT passed
        # on, so a wave is never filled with rows whose models are still cold.
        [vars_("engine_digest"), var("temp_data.res.weights"),
         vars_("wave_k"), var("data.token"), vars_("lease_seconds")],
        "temp_data.claim",
    ), cond=TURN0),

    task("claimed", "Nothing to play: end the run here", halt_unless(wrote("temp_data.claim")),
         cond=TURN0),

    # ------------------------------------------------------------------ turn 0: the barrier
    task("models", "The wave's distinct models", db(
        "db_read", K_MODELS, [var("data.token")], "temp_data.mods",
    ), cond=TURN0),

    task("hold", "Ask the loader to hold them -- one call for the whole wave", loader(
        "/load", "temp_data.hold", {"models": var("temp_data.mods")},
    ), cond=TURN0),

    # The split reads `fault`, not the reason word (axon/docs/design.md §6), so a reason word added
    # to the loader later costs no change here.
    task("split", "Partition the reply by fault, not by reason word", mapping(
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

    task("release", "Refused for memory: back to the queue, no attempt spent", db(
        "db_write", K_RELEASE,
        [var("data.token"), var("temp_data.refused_mem"), vars_("refusal_ceiling")],
        "temp_data.released",
    ), cond={"and": [TURN0, {">": [var("temp_data.n_mem"), 0]}]}),

    task("fail", "Refused by name: failed at once, with the seat", db(
        "db_write", K_FAIL_SET, [var("data.token"), var("temp_data.refused_named")],
        "temp_data.failed",
    ), cond={"and": [TURN0, {">": [var("temp_data.n_named"), 0]}]}),

    task("start", "Everything still claimed is now running", db(
        "db_write", K_START, [var("data.token")], "temp_data.started",
    ), cond=TURN0),

    task("idle-unload", "Nothing playable: release what was held",
         unload(var("temp_data.mods")), cond=NOTHING_STARTED),
    task("idle", "Nothing playable: end the run", mapping(
        ("data.outcome", "nothing_started"),
    ), cond=NOTHING_STARTED, terminal=True),

    # ------------------------------------------------------------------ turn 0: open the wave
    task("wave", "Read the wave, numbered by Postgres", db(
        "db_read", K_WAVE, [var("data.token")], "temp_data.w",
    ), cond=TURN0),

    task("open", "Rows, seats and the refs the whole run rides on", mapping(
        ("data.rows", {"map": [var("temp_data.w"), var("row")]}),
        ("data.seats", {"reduce": [{"map": [var("data.rows"), var("seats")]},
                                   {"merge": [var("accumulator"), var("current")]}, []]}),
        # THE REFS. A flat list, each entry carrying its own m and seat, which the engine matches on
        # rather than indexes -- and where the strike counters live, because a fixed task list has
        # no other way to accumulate anything per seat across turns. The counter travels WITH the
        # seat it counts: out through `observe`, onto the play row, back on the loader's echoed
        # `ref`, and into the next turn. Neither the engine nor the loader looks inside one.
        ("data.refs", {"map": [var("data.seats"),
                               {"m": var("m"), "seat": var("seat"),
                                "weights_hash": var("weights_hash"),
                                "adapter_hash": var("adapter_hash"),
                                "strikes": 0, "forfeited": False,
                                # Seeded at 0, not left absent: the accumulators below add to these
                                # every turn, and `{"+": [null, x]}` on the first write is exactly
                                # the silent-null class of bug this file keeps warning about.
                                "infer_us_total": 0, "infer_us_max": 0, "infer_turns": 0}]}),
        ("data.models", var("temp_data.mods")),
        ("data.n_running", {"length": [var("data.rows")]}),
        ("data.pending", []),        # ended, not yet finished
        ("data.deltas", []),         # the replay stream, drained as matches finish
        ("data.done_refs", []),      # the refs of ended matches, snapshotted the turn they end
        ("data.finished", 0),
        ("data.strikes", 0),
    ), cond=TURN0),

    task("world", "Build the wave's worlds", plugin(f"{ENGINE}.worldgen", {
        "seeds": {"map": [var("data.rows"), var("seed")]},
        "preset": var("data.rows.0.preset"),
        # The preset carries the seat count and the engine refuses a caller that disagrees, so
        # passing it is a free check rather than a parameter.
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
        # A forfeited seat is not sent to the loader at all: it plays the no-op by construction.
        ("temp_data.playing", {"filter": [var("temp_data.obs.views"),
                                          {"!": [var("ref.forfeited")]}]}),
    )),

    task("play", "One play call for the whole wave", http(
        "model-loader", "POST", "/play", "temp_data.play", {
            # Element-relative throughout: joining back to `data.rows` by the view's own m and seat
            # evaluates to null inside a `map`, silently, which is what `ref` exists to avoid.
            "rows": {"map": [var("temp_data.playing"), {
                "weights_hash": var("ref.weights_hash"),
                "adapter_hash": var("ref.adapter_hash"),
                "observation": var("view"),
                "ref": var("ref"),          # echoed verbatim -- axon/docs/design.md §3.2
            }]},
            "deadline_ms": vars_("turn_ms"),
            "budget_ops": vars_("budget_ops"),
        },
    ), cond=LIVE),

    task("acts", "Errors become the no-op; strikes accumulate", mapping(
        # The explicit {m, seat, action} form, not the positional one: a forfeited seat is not sent
        # to the loader, so the reply is shorter than the view list and positional alignment would
        # land every action after the first forfeit in the wrong chair. Omission IS the no-op --
        # the engine plays it for any seat it is given nothing for.
        ("temp_data.acts", {"map": [var("temp_data.play.rows"),
                                    {"m": var("ref.m"), "seat": var("ref.seat"),
                                     "action": {"??": [var("action"), []]}}]}),
        # The strikes come out of the same walk, because the reply is the only place a row's error
        # and the identity of its seat are in the same object. Counted CUMULATIVELY: five missed
        # clocks in a match, not five in a row -- the reading a competitor cannot game.
        ("temp_data.nr", sift(
            var("temp_data.play.rows"), {"c": vars_("strike_ceiling")}, element={
                "m": var("current.ref.m"), "seat": var("current.ref.seat"),
                "weights_hash": var("current.ref.weights_hash"),
                "adapter_hash": var("current.ref.adapter_hash"),
                "strikes": NEXT_STRIKES,
                "forfeited": {"or": [var("current.ref.forfeited"),
                                     {">=": [NEXT_STRIKES, var("accumulator.c")]}]},
                # A forfeited seat is absent from the next turn's play call, so these freeze at the
                # last turn it was actually played -- which is why `infer_turns` is carried rather
                # than matches.turns being reused as the divisor.
                "infer_us_total": NEXT_INFER_TOTAL,
                "infer_us_max": NEXT_INFER_MAX,
                "infer_turns": NEXT_INFER_TURNS,
            })),
        ("temp_data.next_refs", var("temp_data.nr.items")),
        # A forfeited seat is absent from the reply, so rebuilding the refs from the reply alone
        # would drop its `forfeited` flag and send it to the loader again next turn. Carry it.
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
        # Strikes outlive the match they were earned in: the drain finishes one match per sweep, and
        # by the second sweep `observe` returns nothing for a finished match. This is the only turn
        # an ending match's seats still exist in the refs, so snapshot them now.
        ("temp_data.dr", sift(var("data.refs"),
                              {"ended": var("temp_data.st.ended")},
                              {"in": [var("current.m"), var("accumulator.ended")]},
                              items=var("data.done_refs"))),
        ("data.done_refs", var("temp_data.dr.items")),
        ("data.strikes", {"+": [var("data.strikes"), var("temp_data.struck")]}),
    ), cond=LIVE),

    # ------------------------------------------------------------------ renew
    task("renew", "Extend the wave's lease", db(
        "db_write", K_RENEW, [var("data.token"), vars_("lease_seconds")], "temp_data.renewed",
    ), cond=DO_RENEW),

    task("lost-unload", "The wave is not ours any more: release the models",
         unload(var("data.models")), cond=RENEW_LOST),
    task("lost", "Halt: the lease was reaped under us", mapping(
        ("data.outcome", "lease_lost"),
    ), cond=RENEW_LOST, terminal=True),

    # ------------------------------------------------------------------ the finish drain
    # One ended match finished per sweep, from a queue. The alternative was K conditioned
    # presign-PUT-finish triples in the task list: 3K condition evaluations every turn for a thing
    # that fires once per match, against six tasks whatever K is.
    task("results", "Ranks, scores and an end reason", plugin(f"{ENGINE}.finish", {
        # Called while matches are still running: the engine reports `done` per match and the drain
        # reads only the head's entry, so it is safe.
        "wave_state": var("data.state"), "output": "temp_data.fin",
    }), cond=PENDING),

    task("pick", "The head of the queue: its row, its result, its seats, its deltas", mapping(
        ("temp_data.head", {"val": ["data", "pending", 0]}),
        # `data.rows` is ordered by m and m is 0-based, so the index IS the match number.
        ("temp_data.hrow", {"val": ["data", "rows", {"val": ["temp_data", "head"]}]}),
        ("temp_data.hres", {"reduce": [
            var("temp_data.fin.results"),
            {"if": [IS_HEAD, {"head": var("accumulator.head"), "r": var("current")},
                    var("accumulator")]},
            {"head": var("temp_data.head"), "r": None}]}),
        ("temp_data.hrefs", sift(var("data.done_refs"), HEAD, IS_HEAD)),
        ("temp_data.hdeltas", sift(var("data.deltas"), HEAD, IS_HEAD)),
        # THE RESULT, one element per seat. Forfeited seats rank last, and the rule is
        # `engine_rank + seat_count` rather than "set them all to last", because two forfeited seats
        # must not tie with a seat that played -- ranks need not be dense, and any order-preserving
        # relabelling gives identical TrueSkill output. The engine's OWN ranks go in the replay
        # envelope untouched, so an audit can still see what the game thought happened.
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
                 "infer_us_total": var("current.infer_us_total"),
                 "infer_us_max": var("current.infer_us_max"),
                 "infer_turns": var("current.infer_turns"),
             }]]}},
            {"r": var("temp_data.hres.r"), "n": var("temp_data.hrow.seat_count"), "items": []}]}),
        # The key names the ATTEMPT, so a stale attempt's blob is an orphan under its own key rather
        # than a replacement for the one that counted.
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

    task("put", "Write the replay envelope", http(
        "kalam-blobs-put", "PUT",
        {"substr": [var("temp_data.signed"), {"length": [vars_("blob_endpoint")]}]},
        "temp_data.putres", {
            "match_id": var("temp_data.hrow.id"),
            "attempt_token": var("data.token"),
            "seed": var("temp_data.hrow.seed"),
            "preset": var("temp_data.hrow.preset"),
            # THE BOARD. A replay is self-sufficient or it is not viewable: `replay-decode` rebuilds
            # the match from `map`, so a replay stays viewable when the preset table has been
            # re-tuned or the catalogue has moved on. The engine emits it on the finish result of a
            # match that has ENDED, which is the row being written here, so it costs no carried
            # state.
            "map_id": var("temp_data.hres.r.map_id"),
            "map": var("temp_data.hres.r.map"),
            # WHO SAT WHERE, by hash -- not identity, but enough that a replay can be RE-RUN and not
            # merely watched: `tinybrains conform` rebuilds the match from this envelope alone and
            # diffs the result against it, which is what keeps the local runner and this workflow
            # telling the same story about the same seeds.
            "seats": var("temp_data.hrefs.items"),
            # The seed fixes food respawn and the map fixes the board; re-simulation needs the turn
            # limit too, and it is a var rather than a column.
            "max_turns": vars_("max_turns"),
            "engine_digest": vars_("engine_digest"),
            "evaluator_digest": var("temp_data.play.evaluator_digest"),
            "dialect_version": var("temp_data.play.dialect_version"),
            "engine_ranks": var("temp_data.hres.r.ranks"),   # before forfeits are applied
            "scores": var("temp_data.hres.r.scores"),
            "reason": var("temp_data.hres.r.reason"),
            "turns": var("temp_data.hres.r.turns"),
            "deltas": var("temp_data.hdeltas.items"),        # the action stream, not frames
        },
        response_format="text",
    ), cond=PENDING),

    task("finish", "Finish the row -- one statement, on the token", db(
        "db_write", K_FINISH,
        [var("data.token"), var("temp_data.hrow.id"), var("temp_data.seatrows.items"),
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
        ("temp_data.dk", sift(var("data.deltas"), HEAD, IS_HEAD, keep=False)),
        ("data.deltas", var("temp_data.dk.items")),
        ("data.n_running", {"-": [var("data.n_running"), 1]}),
        ("data.finished", {"+": [var("data.finished"), 1]}),
    ), cond=PENDING),

    # ------------------------------------------------------------------ the end
    task("unload", "Release the wave's models", unload(var("data.models")), cond=OVER),

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
        "Each row is finished AS ITS MATCH ENDS, one per sweep from a queue, with its replay under "
        "a key naming the attempt. Every statement is conditioned on the claim token, so a stale "
        "attempt updates nothing. It reads no rating and writes no rating, and its database role "
        "cannot reach one. On SIGTERM Orion stops claiming and lets the wave in hand finish inside "
        "cron.shutdown_timeout_secs. docs/design.md; the statements are soma/docs/schema.md §4."
    ),
    "tags": ["pkg:kalam"],
    "condition": True,
    # max_turns + K: the drain finishes one match per sweep, so a wave whose matches all end on the
    # last turn needs K more sweeps to empty its queue.
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
        # and the next tick claims from it.
        "misfire_policy": "skip",
        # `forbid` on key `wave`, and the key is LOCAL in effect: Kalam's Orion runs on local SQLite
        # with no shared state, so the lock is per replica -- one wave in flight per replica, N
        # replicas in parallel. The opposite of Jodi's use of the identical spelling.
        "concurrency": {"policy": "forbid", "key": "wave"},
    },
    "config": {
        # Above the longest match: max_turns x turn_ms plus the platform's own time and the drain's
        # tail.
        "timeout_ms": 2400000,
        # A thousand-turn wave is thousands of task executions in one occurrence, and tracing a
        # clean one writes more trace than match.
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
