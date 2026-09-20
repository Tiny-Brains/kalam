#!/usr/bin/env bash
# THE NAMES ARE THE ORGANISING SYSTEM, so they are checked rather than remembered.
#
#   ./scripts/check-names.sh          (scripts/check-defs.sh runs it too)
#
# The long version of why is in soma/scripts/check-names.sh, which has 59 channels across five
# surfaces to organise. This package has three, so it is flat -- `kalam-<name>`, with no surface
# segment -- but it shares the vocabulary, because `?tag=` is one exact string and `?tag=matches`
# has to mean the same thing whichever node is asked. web's scripts/check/configs.sh compares the
# two DOMAINS lists; keep this one formatted as it is, or that comparison stops seeing them.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 - <<'PY'
import json, pathlib, sys

PACKAGE = json.loads(pathlib.Path("shared/package.json").read_text())["package"]["name"]
SURFACES = ("clock",)   # every channel here is a cron; the gate is Soma's side of the call
CONN = "conn"
DOMAINS = {"platform", "auth", "profile", "notifications", "seasons", "maps", "baselines",
           "ladder", "matches", "models", "admission", "runners", "users"}

errors = []
def bad(p, msg): errors.append(f"{p}: {msg}")

def check_tags(p, doc, surface):
    t = doc.get("tags")
    if not isinstance(t, list) or len(t) != 3 or not all(isinstance(x, str) for x in t):
        bad(p, f"tags must be exactly three strings [package, surface, domain], not {json.dumps(t)}")
        return
    if t[0] != PACKAGE:  bad(p, f"tags[0] must be {PACKAGE!r}, not {t[0]!r}")
    if t[1] != surface:  bad(p, f"tags[1] must be {surface!r}, not {t[1]!r}")
    if t[2] not in DOMAINS:
        bad(p, f"tags[2] {t[2]!r} is not in the domain vocabulary: {sorted(DOMAINS)}")

def load(kind):
    return {p: json.loads(p.read_text()) for p in sorted(pathlib.Path(kind).glob("*.json"))}

channels, workflows, connectors = load("channels"), load("workflows"), load("connectors")
wf_by_id = {d["workflow_id"]: (p, d) for p, d in workflows.items()}

for p, ch in channels.items():
    cid = ch["channel_id"]
    if p.name != cid + ".json":  bad(p, f"the file name must be the id: {cid}.json")
    if ch.get("name") != cid:    bad(p, f"name must equal the id ({cid!r}), not {ch.get('name')!r}")
    if not cid.startswith(PACKAGE + "-"):
        bad(p, f"an id is {PACKAGE}-<name>")
    if ch.get("protocol") != "cron":
        bad(p, f"protocol is {ch.get('protocol')!r}; every channel here is a cron, which is what "
               f"makes `clock` the derived surface rather than a literal")
        continue
    check_tags(p, ch, "clock")
    want = cid + "-run"
    if ch["workflow_id"] != want:
        bad(p, f"workflow_id should be {want!r}, not {ch['workflow_id']!r}")
    e = wf_by_id.get(ch["workflow_id"])
    if e is None:
        bad(p, f"names workflow {ch['workflow_id']!r}, which is not in workflows/")
    else:
        wp, wf = e
        if wp.name != wf["workflow_id"] + ".json":
            bad(wp, f"the file name must be the id: {wf['workflow_id']}.json")
        if wf.get("name") == wf["workflow_id"]:
            bad(wp, "name repeats the id; a workflow's name is prose")
        if wf.get("tags") != ch.get("tags"):
            bad(wp, f"tags must equal its channel's {json.dumps(ch.get('tags'))}, "
                    f"not {json.dumps(wf.get('tags'))}")

routed = {ch["workflow_id"] for ch in channels.values()}
for wid in sorted(set(wf_by_id) - routed):
    bad(wf_by_id[wid][0], "no channel routes to it")

for p, cn in connectors.items():
    if p.name != cn["id"] + ".json":
        bad(p, f"the file name must be the id: {cn['id']}.json")
    if cn.get("name") != cn["id"]:
        bad(p, f"name must equal the id ({cn['id']!r}), not {cn.get('name')!r}")
    if not cn["id"].startswith(PACKAGE + "-"):
        bad(p, f"a connector id starts {PACKAGE}-")
    check_tags(p, cn, CONN)

# This package ships no SQL: every statement a runner needs is a call to Soma's gate.
if pathlib.Path("sql").exists():
    bad(pathlib.Path("sql"), "this package ships no SQL -- a statement here is a second copy of "
                             "the gate's, and nothing would compare them")

for e in errors:
    print(f"  {e}", file=sys.stderr)
if errors:
    sys.exit(f"{len(errors)} naming error(s)")
print(f"  {len(channels)} clocks, {len(connectors)} connectors, no statements")
PY
