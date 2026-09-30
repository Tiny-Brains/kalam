#!/usr/bin/env python3
"""Write the *.case.json files `orion-server test tests` runs, from one place.

Every case stubs the gate (kalam-api), this node's admin API (kalam-orion), the replay bucket
(kalam-blobs-put) and the token cache (kalam-cache); the engine (tb.ants) and the models run for
real from plugins/tb-ants and tests/models. A stub is one value per connector, so every kalam-api
route in a run answers the same object: that works because the fate fields the routes are read
by are disjoint (`token`, `match`/`claim`/`contract`, `started`, `applied`, `url`/`endpoint`/`key`,
`state`/`mine`, `job`, `n`/`items`).

Each case is written twice over: the properties the case is FOR go into `expect`/`expect_calls`
by hand below, and the branch that ran (`expect_tasks`) is recorded from a dry-run of the same
stubs, after the properties were checked against that run -- so a case is never written with an
expectation the workflow does not meet today, and a later change to which tasks run fails it.

    python3 tests/make-cases.py            # rewrite every case (needs ants/dist for the map and
                                           # the reference observations: ANTS_DIST=<dist>)
"""
import json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
KALAM = os.path.dirname(HERE)
ANTS = os.environ.get("ANTS_DIST", os.path.join(os.path.dirname(KALAM), "ants", "dist"))
MAP = json.load(open(f"{ANTS}/maps/basic-tiny-2p.json"))
_OBS_ALL = json.load(open(f"{ANTS}/reference/observations.json"))["observations"]
OBS = _OBS_ALL[:3]
# FOUR OBSERVATIONS THAT CROSS A BOARD SIZE CHANGE, for the memory round trip: the reference set
# changes board at index 18, so this window is two observations on one size and two on the next.
# The chain must restart at that boundary -- a memory is only fed forward when the size matches --
# so exactly two of the four are fed, which is what A4 pins.
OBS_SPAN = _OBS_ALL[16:20]
MANIFEST = json.load(open(f"{HERE}/models/tb.nano-bc/manifest.json"))
REGISTRATION = {k: v for k, v in MANIFEST.items() if k != "artifact"}
MEMO = json.load(open(f"{HERE}/models/tb.nano-bc-max-r/manifest.json"))
MEMO_REGISTRATION = {k: v for k, v in MEMO.items() if k != "artifact"}

VARS = {"engine_digest": "sha256:" + "0" * 64, "orion_version": "1.11.1", "runner_key": "k",
        "runner_label": "h", "arch": "arm64", "node_version": "dev", "match_slots": 2,
        "ops_budget": 1000000, "match_timeout_ms": 2400000, "seat_concurrency": 2, "models_bucket_connector": "kalam-models"}
TRIGGER = {"scheduled_for": "2026-09-26T03:00:00Z", "attempt": 1,
           "occurrence_id": "01a0db56-0000-7000-8000-000000000001", "singleton_slot": 0}
CACHE = {"cache_read": {"kalam-cache": None}, "cache_write": {"kalam-cache": {}},
         "cache_delete": {"kalam-cache": {"deleted": 1}}}


def row(models, ceiling=5):
    return {"id": "m-1", "seed": 7, "map": MAP, "map_id": MAP["id"], "seat_count": len(models),
            "strike_ceiling": ceiling,
            "seats": [{"seat": i, "model": m, "weights_hash": "sha256:w", "manifest_hash": "sha256:m",
                       "strike_ceiling": ceiling} for i, m in enumerate(models)]}


# THE RENEW IS PACED BY THE RUNNER'S OWN CLOCK, which no case can hold still -- so a case pins the
# pace by its extremes instead: `renew_after_ms` 0 is a renew due on every turn, and the default
# (a third of the 300 s lease, as Soma sends it) is a renew never due in a match a case plays in
# well under a second. `renew_after_ms=None` is a contract from a Soma older than the time rule,
# which the runner renews by turns, every `renew`.
def gate(models=("tb.nano-bc", "tb.nano-bc"), max_turns=10, renew=4, ceiling=5,
         renew_after_ms=100000, retry_after_ms=20000, lease_seconds=300, **over):
    contract = {"turn_ms": 1000, "max_turns": max_turns, "renew_every_n_turns": renew,
                "lease_seconds": lease_seconds, "refusal_ceiling": 5}
    if renew_after_ms is not None:
        contract.update(renew_after_ms=renew_after_ms, retry_after_ms=retry_after_ms)
    g = {"token": "tok", "expires_in": 600, "match": row(list(models), ceiling),
         "claim": {"token": "ct-1"},
         "contract": contract,
         "started": True, "applied": True, "state": "finished", "mine": True,
         "url": "http://blobs:9000/tinybrains-replays/replays/m-1/ct-1.json",
         "endpoint": "http://blobs:9000", "key": "replays/m-1/ct-1.json"}
    g.update(over)
    return g


def admission(state="passed", model="tb.nano-bc", registration=None, observations=None):
    """The admission claim's answer, and the node's admission record, for one submission."""
    job = {"token": "tok", "claim": {"token": "ct-a"},
           "admission": {"model": model, "version_id": "v-1",
                         "registration": REGISTRATION if registration is None else registration,
                         "artifact": {"key": "models/v-1/model.onnx", "digest": "sha256:" + "1" * 64},
                         "budget_ops": 1000000, "infer_ms": 5000,
                         "observations": OBS if observations is None else observations}}
    node = {"data": {"model_id": model, "status": "active" if state == "passed" else "draft",
                     "admission": {"state": state, **({"stage": "probe", "reason": "x"} if state != "passed" else {})},
                     "stats": {"parameters": 1000, "artifact_bytes": 11350, "opset": 17, "operators": ["Conv"]}}}
    return job, node


def stubs(api, orion, blobs="", cache=None):
    s = {"http_call": {"kalam-api": api, "kalam-orion": orion, "kalam-blobs-put": blobs}}
    s.update(CACHE)
    if cache is not None:
        s["cache_read"] = {"kalam-cache": cache}
    return s


def dry_run(workflow, case_stubs, model_dir=f"{HERE}/models"):
    with tempfile.TemporaryDirectory() as d:
        json.dump(case_stubs, open(f"{d}/stubs.json", "w"))
        json.dump({"vars": VARS, "trigger": TRIGGER}, open(f"{d}/meta.json", "w"))
        json.dump({}, open(f"{d}/in.json", "w"))
        out = subprocess.run(["orion-server", "dry-run", "--definitions", f"{KALAM}/shared",
                              "-w", f"{KALAM}/workflows/{workflow}", "-i", f"{d}/in.json",
                              "-m", f"{d}/meta.json", "--stubs", f"{d}/stubs.json",
                              "--plugin-dir", f"{KALAM}/plugins/tb-ants",
                              "--model-dir", model_dir, "--trace", "none"],
                             capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"dry-run of {workflow} failed:\n{out.stderr[-1500:]}")
    return json.loads(out.stdout)


def paths(run):
    return [c["input"].get("path") for c in run.get("calls", {}).get("http_call", [])]


def write(group, name, workflow, case_stubs, expect, expect_calls=None, expect_errors=None,
          check=None, tasks=True):
    run = dry_run(workflow, case_stubs)
    if check:
        check(run)
    for path, want in expect.items():
        got = lookup(run, path)
        assert got == want, f"{name}: {path} is {got!r}, the case expects {want!r}"
    if expect_calls:
        for fn, calls in expect_calls.items():
            got = run.get("calls", {}).get(fn, [])
            assert len(got) == len(calls), f"{name}: {fn} made {len(got)} calls, the case expects {len(calls)}"
            for g, w in zip(got, calls):
                assert subset(w, g["input"]), f"{name}: {fn} call {g['task_id']} is not {w}"
    errors = sorted(e["code"] for e in run.get("errors", []))
    assert errors == sorted(expect_errors or []), f"{name}: errors {errors}, the case expects {expect_errors}"
    case = {"name": name, "workflow": f"../../workflows/{workflow}", "input": {},
            "metadata": {"vars": VARS, "trigger": TRIGGER}, "stubs": case_stubs, "expect": expect}
    if expect_calls:
        case["expect_calls"] = expect_calls
    if expect_errors:
        case["expect_errors"] = expect_errors
    if tasks:
        case["expect_tasks"] = run["tasks"]
    slug = name.split(":")[0].strip().lower().replace(" ", "-")
    with open(f"{HERE}/{group}/{slug}.case.json", "w") as f:
        json.dump(case, f, indent=1)
        f.write("\n")
    print(f"  {group}/{slug}: {len(run['tasks'])} tasks, {len(paths(run))} calls")
    return run


def lookup(run, path):
    root, _, rest = path.partition(".")
    node = run.get(root)
    for seg in rest.replace("[", ".").replace("]", "").split(".") if rest else []:
        if isinstance(node, list):
            node = node[int(seg)] if int(seg) < len(node) else None
        elif isinstance(node, dict):
            node = node.get(seg)
        else:
            return None
    return node


def subset(want, got):
    if isinstance(want, dict):
        return isinstance(got, dict) and all(k in got and subset(v, got[k]) for k, v in want.items())
    if isinstance(want, list):
        return isinstance(got, list) and len(want) == len(got) and all(subset(a, b) for a, b in zip(want, got))
    return want == got


FENCED = {"claim_token": "ct-1"}
OPEN_CALLS = [{"path": "/v1/runner/token"}, {"path": "/v1/runner/claim"},
              {"path": "/models/tb.nano-bc"}, {"path": "/models/tb.nano-bc"},
              {"path": "/v1/runner/matches/m-1/start", "body": FENCED}]
RENEW_CALLS = [{"path": "/v1/runner/token"}, {"path": "/v1/runner/matches/m-1/renew", "body": FENCED}]
CLOSE_CALLS = [{"path": "/v1/runner/token"}, {"path": "/v1/runner/matches/m-1/replay-url", "body": FENCED},
               {"path": "/tinybrains-replays/replays/m-1/ct-1.json"},
               {"path": "/v1/runner/matches/m-1/finish", "body": {**FENCED, "turns": 10, "reason": "turn_limit"}}]
# A ten-turn match renews NOTHING: its lease is a third of 300 s from running out when it finishes.
# It still reads the token before it closes (`when_closing`), which is a mint here only because the
# stubbed cache always misses.
MATCH_CALLS = OPEN_CALLS + CLOSE_CALLS
# With the cache stubbed (one answer for every read), a run that starts on a miss misses at each
# renew too, so it mints and keeps once more per renew; a run that starts on a hit never exchanges.
CACHED_CALLS = [c for c in MATCH_CALLS if c["path"] != "/v1/runner/token"]


def frame_present(run):
    finish = [c for c in run["calls"]["http_call"] if c["task_id"] == "finish"]
    assert finish and finish[0]["input"]["body"].get("frame"), "the finish body carries no frame"
    assert run["tasks"][-1] == "stop" and run["tasks"].count("stop") == 11, "the loop did not halt at the finishing sweep"


print("match")
M = "kalam-match-run.json"
write("match", "M1: a ten-turn match completes, finishes with its frame, and the loop halts", M,
      stubs(gate(), {"data": {"status": "active"}}),
      {"data.outcome": "complete", "data.finished": True, "data.stopped_at_turn": 10, "data.struck": 0,
       "data.refs[0].strikes": 0, "data.refs[1].strikes": 0, "data.refs[0].infer_turns": 10,
       "data.lease.wait": 100000,
       "calls.http_call[8].input.body.frame.turn": 10, "calls.http_call[8].input.body.replay_key": "replays/m-1/ct-1.json"},
      {"http_call": MATCH_CALLS, "cache_write": [{"key": "runner_token"}] * 2, "cache_delete": []},
      check=frame_present)

write("match", "M2: a seat whose adapter yields no tensor is struck every turn and forfeits at the ceiling", M,
      stubs(gate(models=("tb.nano-bc", "tb.nano-bad"), ceiling=3), {"data": {"status": "active"}}),
      {"data.outcome": "complete", "data.refs[1].strikes": 3, "data.refs[1].forfeited": True,
       "data.refs[0].strikes": 0, "data.refs[0].forfeited": False, "data.struck": 3},
      expect_errors=["VALIDATION_ERROR"] * 3)

write("match", "M3: a renew the gate refuses ends the run as lease_lost, before the next turn is played", M,
      stubs(gate(applied=False, renew_after_ms=0), {"data": {"status": "active"}}),
      {"data.outcome": "lease_lost", "data.finished": False, "data.stopped_at_turn": None},
      {"http_call": OPEN_CALLS + RENEW_CALLS})

write("match", "M4: a claim lost by the time of finish leaves the match unfinished", M,
      stubs(gate(state="running", mine=False), {"data": {"status": "active"}}),
      {"data.outcome": None, "data.finished": False},
      {"http_call": MATCH_CALLS})

write("match", "M5: a seat's model this node does not serve releases the row as model_unavailable", M,
      stubs(gate(), {"data": {"status": "draft"}}),
      {"data.outcome": "model_unavailable", "data.finished": False},
      {"http_call": MATCH_CALLS[:4] + [{"path": "/v1/runner/matches/m-1/release", "body": FENCED}]})

write("match", "M6: a token the cache holds is used without an exchange", M,
      stubs(gate(), {"data": {"status": "active"}}, cache={"token": "cached", "expires_in": 480}),
      {"calls.http_call[0].input.path": "/v1/runner/claim", "data.outcome": "complete"},
      {"http_call": CACHED_CALLS, "cache_write": []})

write("match", "M7: no token from the gate ends the run as no_token without a claim", M,
      stubs(gate(token=None), {"data": {"status": "active"}}),
      {"data.outcome": "no_token", "data.claimed": None},
      {"http_call": [{"path": "/v1/runner/token"}], "cache_write": []})


write("match", "M9: a contract from a Soma older than the time rule renews by turns, every renew_every_n_turns", M,
      stubs(gate(renew_after_ms=None), {"data": {"status": "active"}}),
      {"data.outcome": "complete", "data.lease.wait": None},
      {"http_call": OPEN_CALLS + RENEW_CALLS * 2 + CLOSE_CALLS, "cache_write": [{"key": "runner_token"}] * 4})

write("match", "M10: a renew that never arrived is retried after retry_after_ms, and the match plays on", M,
      # No `applied` in the answer is a renew that never arrived: neither the claim nor its loss.
      # Due on the first turn (renew_after_ms 0), then not again inside retry_after_ms.
      stubs(gate(applied=None, renew_after_ms=0, retry_after_ms=100000), {"data": {"status": "active"}}),
      {"data.outcome": "complete", "data.lease.wait": 100000},
      {"http_call": OPEN_CALLS + RENEW_CALLS + CLOSE_CALLS})

write("match", "M11: a whole lease of the runner's own time with no renew applied ends the run as lease_lapsed", M,
      # A zero lease is lapsed the moment the clock starts; the renew that never arrived renews nothing.
      stubs(gate(applied=None, renew_after_ms=0, lease_seconds=0), {"data": {"status": "active"}}),
      {"data.outcome": "lease_lapsed", "data.finished": False},
      {"http_call": OPEN_CALLS + RENEW_CALLS})


def forgetful_twin():
    """tests/models plus tb.nano-bc-max-f: the same graph as tb.nano-bc-max-r under a manifest whose
    memory input is zeros on every turn, whatever the view carries -- so it plays exactly as
    max-r would if the runner never handed a memory back."""
    d = tempfile.mkdtemp()
    # The DIRECTORIES only: models/ also holds SHA256SUMS, which pins the fixtures' bytes.
    for m in os.listdir(f"{HERE}/models"):
        if os.path.isdir(f"{HERE}/models/{m}"):
            shutil.copytree(f"{HERE}/models/{m}", f"{d}/{m}")
    twin = json.load(open(f"{HERE}/models/tb.nano-bc-max-r/manifest.json"))
    twin["name"] = "tb.nano-bc-max-f"
    for i in twin["inputs"]:
        if i["name"] == "memory_in":
            i["adapter"] = {"zeros": [{"merge": [[1, 2], {"var": "size"}]}, "i8"]}
    os.makedirs(f"{d}/tb.nano-bc-max-f")
    shutil.copy(f"{HERE}/models/tb.nano-bc-max-r/model.onnx", f"{d}/tb.nano-bc-max-f/model.onnx")
    json.dump(twin, open(f"{d}/tb.nano-bc-max-f/manifest.json", "w"))
    return d


def carried(run):
    """The memory a remembering seat ends on is not what the same graph ends on when it is never
    handed one: if the runner dropped the carry, the two runs would be the same run."""
    d = forgetful_twin()
    try:
        twin = dry_run(M, stubs(gate(models=("tb.nano-bc-max-f", "tb.nano-bc")), {"data": {"status": "active"}}), d)
    finally:
        shutil.rmtree(d)
    mine, theirs = run["data"]["mem0"], twin["data"]["mem0"]
    assert lookup(twin, "data.mem0.tensor.shape") == [1, 2, 24, 24], "the forgetful twin wrote no memory"
    assert mine != theirs, "seat 0's memory is what it would be if it were never handed back"


# The memory seat 0 ends on is pinned byte for byte, and `carried` proves at generation that it is
# not the value a dropped carry would give -- so the case fails if the runner stops handing the
# memory back. A retrained nano-bc-max-r in the starter moves the bytes: regenerate.
M8 = stubs(gate(models=("tb.nano-bc-max-r", "tb.nano-bc")), {"data": {"status": "active"}})
write("match", "M8: a seat whose model declares a memory is handed it back every turn, and only that seat", M, M8,
      {"data.outcome": "complete", "data.stopped_at_turn": 10, "data.struck": 0,
       "data.refs[0].strikes": 0, "data.refs[0].infer_turns": 10,
       "data.mem0.tensor.dtype": "i8", "data.mem0.tensor.shape": [1, 2, 24, 24],
       "data.mem0.tensor.data": lookup(dry_run(M, M8), "data.mem0.tensor.data"),
       "data.amem0": None, "data.mem1": None, "data.amem1": None},
      check=carried)

print("admit")
A = "kalam-admit-run.json"
job, node = admission("passed")
write("admit", "A1: an admission the node passes is probed over every observation and reported", A,
      stubs(job, node),
      {"data.probing": True, "data.checked": 3, "data.errored": False, "data.ok": True, "data.reason": False,
       "temp_data.probe.checked": 3, "temp_data.probe.round_trip": None},
      {"http_call": [{"path": "/v1/runner/token"}, {"path": "/v1/runner/admissions/claim"}, {"path": "/models"},
                     {"path": "/models/tb.nano-bc/admit?wait=true"}, {"path": "/models/tb.nano-bc/status"},
                     {"path": "/models/tb.nano-bc"},
                     {"path": "/v1/runner/admissions/v-1/report", "body": {"claim_token": "ct-a", "probe": {"checked": 3, "errored": False}}}]})

job, node = admission("failed")
write("admit", "A2: an admission the node refuses is reported without a probe", A,
      stubs(job, node),
      {"data.probing": False, "data.checked": 0},
      {"http_call": [{"path": "/v1/runner/token"}, {"path": "/v1/runner/admissions/claim"}, {"path": "/models"},
                     {"path": "/models/tb.nano-bc/admit?wait=true"}, {"path": "/models/tb.nano-bc"},
                     {"path": "/v1/runner/admissions/v-1/report", "body": {"probe": None, "admission": {"state": "failed"}}}]})

write("admit", "A3: nothing to admit ends the run at idle", A,
      stubs({"token": "tok"}, {"data": {}}),
      {"data.probing": None},
      {"http_call": [{"path": "/v1/runner/token"}, {"path": "/v1/runner/admissions/claim"}]})

# THE MEMORY ROUND TRIP, WHICH NOTHING ELSE HERE REACHES. A1-A3 probe tb.nano-bc, which declares no
# memory: `data.remembers` is false and the whole chain -- the carried view, `fed`, `lost`,
# `rt_checked`, `rt_failed` and the exclusion of a fed failure from `data.errored` -- never runs.
# This probes the model that DOES remember, over a window that crosses a board size change.
#
# WHY `checked` IS 2 AND NOT 3: a memory is fed forward only when the previous call answered AND
# the board is the same size, so the chain restarts at the boundary. Four observations, two sizes:
# the first of each pair is fed nothing, the second is fed. `failed` is 0 because every call
# answers -- and it is the field that must stay 0, since Soma reads a non-zero one as
# MEMORY_ROUND_TRIP and a submission cannot be resubmitted against it.
job, node = admission("passed", model="tb.nano-bc-max-r", registration=MEMO_REGISTRATION,
                      observations=OBS_SPAN)
write("admit", "A4: a model that declares a memory is fed its own last output, and the chain restarts on a new board", A,
      stubs(job, node),
      {"data.probing": True, "data.remembers": True, "data.checked": 4, "data.errored": False,
       "data.ok": True, "data.reason": False,
       "temp_data.probe.checked": 4, "temp_data.probe.round_trip": {"checked": 2, "failed": 0}},
      {"http_call": [{"path": "/v1/runner/token"}, {"path": "/v1/runner/admissions/claim"},
                     {"path": "/models"},
                     {"path": "/models/tb.nano-bc-max-r/admit?wait=true"},
                     {"path": "/models/tb.nano-bc-max-r/status"},
                     {"path": "/models/tb.nano-bc-max-r"},
                     {"path": "/v1/runner/admissions/v-1/report",
                      "body": {"claim_token": "ct-a",
                               "probe": {"checked": 4, "errored": False,
                                         "round_trip": {"checked": 2, "failed": 0}}}}]})

def overflowed(run):
    assert run["tasks"].index("overflow") == 4, "the warn did not run right after the roster read"
    assert run["tasks"].count("have") == 4096, "the loop did not run to its bound"


print("roster")
R = "kalam-roster-run.json"
item = {"model": "tb.nano-bc", "version_id": "v-1", "digest": "sha256:" + "1" * 64, "key": "models/v-1/model.onnx",
        "manifest": REGISTRATION, "status": "verified"}
write("roster", "R1: a version this node lacks is registered", R,
      stubs({"token": "tok", "n": 1, "items": [item]}, {"data": {}}),
      {"data.rb.n": 1},
      {"http_call": [{"path": "/v1/runner/token"}, {"path": "/v1/runner/roster"}, {"path": "/models/tb.nano-bc"},
                     {"path": "/models", "body": {"tags": ["ladder"], "artifact": {"key": "models/v-1/model.onnx"}}}]})
write("roster", "R2: a version already active here is left alone", R,
      stubs({"token": "tok", "n": 1, "items": [item]},
            {"data": {"model_id": "tb.nano-bc", "status": "active", "admission": {"state": "passed"}}}),
      {"data.rb.n": 1},
      {"http_call": [{"path": "/v1/runner/token"}, {"path": "/v1/runner/roster"}, {"path": "/models/tb.nano-bc"}]})
write("roster", "R3: a version that passed admission but is not active is activated", R,
      stubs({"token": "tok", "n": 1, "items": [item]},
            {"data": {"model_id": "tb.nano-bc", "status": "inactive", "admission": {"state": "passed"}}}),
      {"data.rb.n": 1},
      {"http_call": [{"path": "/v1/runner/token"}, {"path": "/v1/runner/roster"}, {"path": "/models/tb.nano-bc"},
                     {"path": "/models/tb.nano-bc/status", "body": {"status": "active"}}]})
write("roster", "R4: an empty roster ends the run after the read", R,
      stubs({"token": "tok", "n": 0, "items": []}, {"data": {}}),
      {"data.rb.n": 0},
      {"http_call": [{"path": "/v1/runner/token"}, {"path": "/v1/runner/roster"}]})
write("roster", "R5: a roster larger than the loop registers in one tick is warned about", R,
      # 4097 real rows: the loop iterates the array itself (loop.over), so its bound is the array's
      # length, and the warn is what says the last one was never reached.
      stubs({"token": "tok", "n": 4097, "items": [{"model": "tb.nano-bc"}] * 4097},
            {"data": {"model_id": "tb.nano-bc", "status": "active", "admission": {"state": "passed"}}}),
      {"data.rb.n": 4097},
      check=overflowed)
# House style: fmt knows a case file, and check-defs.sh runs `fmt --check .` over the tree.
subprocess.run(["orion-server", "fmt", HERE], check=True, capture_output=True)
print("done")
