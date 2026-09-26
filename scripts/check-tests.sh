#!/usr/bin/env bash
# The offline cases: every *.case.json under tests/, through `orion-server test`, with the gate,
# this node's admin API, the replay bucket and the token cache stubbed, and the engine and the
# models real. A case asserts the branch a run takes (`expect_tasks`), what it answers and what it
# sends (`expect`, `expect_calls`) -- what lint cannot exercise: a lease lost mid-match, a seat
# struck to its ceiling, a version this node lacks. tests/make-cases.py is where a case is written.
#
#   ./scripts/check-tests.sh
#
# Needs: orion-server on PATH; plugins/tb-ants (the cartridge, gitignored: a checkout takes it from
# ants/dist or an ants release, as the Dockerfile does); the two model artifacts under tests/models
# (never committed: copied from the ants-starter checkout beside this one, or from ANTS_STARTER).
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f plugins/tb-ants/plugin.toml ] && [ -f plugins/tb-ants/tb-ants.wasm ] || {
  echo "plugins/tb-ants is missing the cartridge -- copy plugin.toml, plugin.json, cartridge.json and tb-ants.wasm from an ants dist/ or release" >&2
  exit 1
}

# THE MODEL BYTES ARE THE STARTER'S. Both fixtures are the nano-bc graph under a manifest of their
# own (tests/models/*/manifest.json, committed); the artifact beside each is a copy of the starter's,
# which no repository but ants-starter commits.
starter="${ANTS_STARTER:-../ants-starter}"
for m in tests/models/*/; do
  [ -f "$m/model.onnx" ] && continue
  [ -f "$starter/models/nano-bc/model.onnx" ] || {
    echo "$m has no model.onnx and $starter/models/nano-bc/model.onnx is not there to copy (ANTS_STARTER=<checkout>)" >&2
    exit 1
  }
  cp "$starter/models/nano-bc/model.onnx" "$m/model.onnx"
done

# `test` reads one directory flat, so the groups run one at a time: the match loop, the admission
# walk, the roster. A group with no case is a mistake, not an empty pass.
for group in tests/match tests/admit tests/roster; do
  echo "--- $group"
  orion-server test "$group" --definitions shared --plugin-dir plugins/tb-ants --model-dir tests/models
done
