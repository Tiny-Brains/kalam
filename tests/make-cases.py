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
import json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
KALAM = os.path.dirname(HERE)
ANTS = os.environ.get("ANTS_DIST", os.path.join(os.path.dirname(KALAM), "ants", "dist"))
MAP = json.load(open(f"{ANTS}/maps/basic-tiny-2p.json"))
OBS = json.load(open(f"{ANTS}/reference/observations.json"))["observations"][:3]
MANIFEST = json.load(open(f"{HERE}/models/tb.nano-bc/manifest.json"))
REGISTRATION = {k: v for k, v in MANIFEST.items() if k != "artifact"}

VARS = {"engine_digest": "sha256:" + "0" * 64, "orion_version": "1.11.0", "runner_key": "k",
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


def gate(models=("tb.nano-bc", "tb.nano-bc"), max_turns=10, renew=4, ceiling=5, **over):
    g = {"token": "tok", "expires_in": 600, "match": row(list(models), ceiling),
         "claim": {"token": "ct-1"},
         "contract": {"turn_ms": 1000, "max_turns": max_turns, "renew_every_n_turns": renew,
                      "lease_seconds": 300, "refusal_ceiling": 5},
         "started": True, "applied": True, "state": "finished", "mine": True,
         "url": "http://blobs:9000/tinybrains-replays/replays/m-1/ct-1.json",
         "endpoint": "http://blobs:9000", "key": "replays/m-1/ct-1.json"}
    g.update(over)
    return g


def admission(state="passed"):
    """The admission claim's answer, and the node's admission record, for one submission."""
    job = {"token": "tok", "claim": {"token": "ct-a"},
           "admission": {"model": "tb.nano-bc", "version_id": "v-1", "registration": REGISTRATION,
                         "artifact": {"key": "models/v-1/model.onnx", "digest": "sha256:" + "1" * 64},
                         "budget_ops": 1000000, "infer_ms": 5000, "observations": OBS}}
    node = {"data": {"model_id": "tb.nano-bc", "status": "active" if state == "passed" else "draft",
                     "admission": {"state": state, **({"stage": "probe", "reason": "x"} if state != "passed" else {})},
                     "stats": {"parameters": 1000, "artifact_bytes": 11350, "opset": 17, "operators": ["Conv"]}}}
    return job, node


def stubs(api, orion, blobs="", cache=None):
    s = {"http_call": {"kalam-api": api, "kalam-orion": orion, "kalam-blobs-put": blobs}}
    s.update(CACHE)
    if cache is not None:
        s["cache_read"] = {"kalam-cache": cache}
    return s


def dry_run(workflow, case_stubs):
    with tempfile.TemporaryDirectory() as d:
        json.dump(case_stubs, open(f"{d}/stubs.json", "w"))
        json.dump({"vars": VARS, "trigger": TRIGGER}, open(f"{d}/meta.json", "w"))
        json.dump({}, open(f"{d}/in.json", "w"))
        out = subprocess.run(["orion-server", "dry-run", "--definitions", f"{KALAM}/shared",
                              "-w", f"{KALAM}/workflows/{workflow}", "-i", f"{d}/in.json",
                              "-m", f"{d}/meta.json", "--stubs", f"{d}/stubs.json",
                              "--plugin-dir", f"{KALAM}/plugins/tb-ants",
                              "--model-dir", f"{HERE}/models", "--trace", "none"],
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
MATCH_CALLS = [{"path": "/v1/runner/token"}, {"path": "/v1/runner/claim"},
               {"path": "/models/tb.nano-bc"}, {"path": "/models/tb.nano-bc"},
               {"path": "/v1/runner/matches/m-1/start", "body": FENCED},
               {"path": "/v1/runner/token"}, {"path": "/v1/runner/matches/m-1/renew", "body": FENCED},
               {"path": "/v1/runner/token"}, {"path": "/v1/runner/matches/m-1/renew", "body": FENCED},
               {"path": "/v1/runner/matches/m-1/replay-url", "body": FENCED},
               {"path": "/tinybrains-replays/replays/m-1/ct-1.json"},
               {"path": "/v1/runner/matches/m-1/finish", "body": {**FENCED, "turns": 10, "reason": "turn_limit"}}]
# With the cache stubbed (one answer for every read), a run that starts on a miss misses at each
# renew too, so it mints and keeps three times; a run that starts on a hit never exchanges at all.
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
       "calls.http_call[11].input.body.frame.turn": 10, "calls.http_call[11].input.body.replay_key": "replays/m-1/ct-1.json"},
      {"http_call": MATCH_CALLS, "cache_write": [{"key": "runner_token"}] * 3, "cache_delete": []},
      check=frame_present)

write("match", "M2: a seat whose adapter yields no tensor is struck every turn and forfeits at the ceiling", M,
      stubs(gate(models=("tb.nano-bc", "tb.nano-bad"), ceiling=3), {"data": {"status": "active"}}),
      {"data.outcome": "complete", "data.refs[1].strikes": 3, "data.refs[1].forfeited": True,
       "data.refs[0].strikes": 0, "data.refs[0].forfeited": False, "data.struck": 3},
      expect_errors=["VALIDATION_ERROR"] * 3)

write("match", "M3: a renew the gate refuses ends the run as lease_lost, before the next turn is played", M,
      stubs(gate(applied=False), {"data": {"status": "active"}}),
      {"data.outcome": "lease_lost", "data.finished": False},
      {"http_call": MATCH_CALLS[:7]})

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
