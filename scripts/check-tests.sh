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

# THE MODEL BYTES ARE THE STARTER'S, AND THEY ARE PINNED. Every fixture is a starter graph under a
# manifest of its own (tests/models/*/manifest.json, committed); the artifact beside each is a copy
# of the starter's, which no repository but ants-starter commits. tb.nano-bc-max-r is the starter's
# model that remembers (M8's memory carry); every other fixture is the nano-bc graph.
#
# EVERY COPY IS CHECKED AGAINST tests/models/SHA256SUMS, because M8 pins seat 0's ending memory
# tensor byte for byte -- the only way to prove the runner hands a memory back rather than dropping
# it. A retrained starter model is a red build either way; the digest is what makes it a red build
# that says WHICH MODEL MOVED instead of a tensor diff naming nothing.
sha256() {
  if command -v sha256sum > /dev/null; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

starter="${ANTS_STARTER:-../ants-starter}"
for m in tests/models/*/; do
  name=$(basename "$m")
  case "$name" in
    tb.nano-bc-max-r) src="$starter/models/nano-bc-max-r/model.onnx" ;;
    *) src="$starter/models/nano-bc/model.onnx" ;;
  esac
  if [ ! -f "$m/model.onnx" ]; then
    [ -f "$src" ] || {
      echo "$name has no model.onnx and $src is not there to copy (ANTS_STARTER=<checkout>)." >&2
      # nano-bc-max-r IS NEWER THAN THE STARTER'S DEFAULT BRANCH. A clone of ants-starter main has
      # nano-bc, micro-bc and micro-percell only, so this is the one fixture a fresh clone lacks --
      # and it is the one M8 needs. The starter has to release the memory baseline first.
      case "$name" in
        tb.nano-bc-max-r) echo "  tb.nano-bc-max-r is the memory baseline: it must be on the ants-starter branch this checkout (or CI's ANTS_STARTER_REF) points at." >&2 ;;
      esac
      exit 1
    }
    cp "$src" "$m/model.onnx"
  fi
  want=$(grep "  $name/model.onnx\$" tests/models/SHA256SUMS | cut -d' ' -f1)
  [ -n "$want" ] || { echo "tests/models/SHA256SUMS has no line for $name/model.onnx" >&2; exit 1; }
  got=$(sha256 "$m/model.onnx")
  [ "$got" = "$want" ] || {
    echo "$name/model.onnx is not the pinned model:" >&2
    echo "  pinned $want" >&2
    echo "  found  $got" >&2
    echo "The starter's model changed. Re-run \`python3 tests/make-cases.py\` to replay every case" >&2
    echo "against the new bytes, then update tests/models/SHA256SUMS." >&2
    exit 1
  }
done

# `test` reads one directory flat, so the groups run one at a time: the match loop, the admission
# walk, the roster. A group with no case is a mistake, not an empty pass.
for group in tests/match tests/admit tests/roster; do
  echo "--- $group"
  orion-server test "$group" --definitions shared --plugin-dir plugins/tb-ants --model-dir tests/models
done
