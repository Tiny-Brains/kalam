#!/usr/bin/env sh
# Copy the built cartridge into this package, and print the digest it will load under.
#
#   kalam/scripts/vendor-engine.sh [path-to-cartridge-repo]     # default ../ants
#
# The engine is a plugin here, exactly as tb.rating and tb.pairing are in Jodi. It is *vendored*
# rather than built: `ants/` is a repository of its own with its own toolchain (wasm32 target,
# wasm-tools) and its own gate (`deny.sh`, 44 rule tests), and a replica image must not need any
# of that. What ships here is the artifact that repository already commits.
#
# THE DIGEST IS THE POINT. Orion identifies a plugin by sha256 over the component bytes, which is
# what this prints, and three things must agree on it or the system silently plays nothing:
#
#   * the plugin Orion loads               -- this file's sha256
#   * [vars] engine_digest on the replica  -- KALAM_ENGINE_DIGEST in the environment
#   * games.active_engine_digest           -- what pair stamps on every row it inserts
#
# The claim statement filters `engine_digest = $1`, so a mismatch is not an error anywhere: the
# replica simply claims nothing, for ever, and the queue grows. Hence `devops/scripts/engine-digest.sh`,
# which writes the third from the first.
set -eu

SRC="${1:-$(cd "$(dirname "$0")/../../ants" 2>/dev/null && pwd || true)}"
[ -n "$SRC" ] && [ -d "$SRC" ] || { echo "cartridge repo not found; pass its path" >&2; exit 1; }

cd "$(dirname "$0")/.."
DST=plugins/tb-ants
mkdir -p "$DST"

for f in tb-ants.wasm plugin.json; do
  [ -r "$SRC/$f" ] || { echo "$SRC/$f is missing -- run $SRC/build.sh first" >&2; exit 1; }
  cp "$SRC/$f" "$DST/$f"
done
# cartridge.json is not Orion's; it is the registration manifest the platform reads for presets,
# limits and budgets. The wave workflow needs turn_ms, max_turns and adapter_ops_max out of it, so
# it travels with the component rather than being retyped into [vars].
cp "$SRC/cartridge.json" "$DST/cartridge.json"

if command -v sha256sum > /dev/null 2>&1; then D=$(sha256sum "$DST/tb-ants.wasm" | cut -d' ' -f1)
else D=$(shasum -a 256 "$DST/tb-ants.wasm" | cut -d' ' -f1); fi

echo "==> vendored $(cat "$DST/cartridge.json" | tr -d ' \n' | sed 's/.*"game":"\([^"]*\)".*/\1/') from $SRC"
echo "    plugins/tb-ants/tb-ants.wasm   $(wc -c < "$DST/tb-ants.wasm" | tr -d ' ') bytes"
echo
echo "KALAM_ENGINE_DIGEST=sha256:$D"
