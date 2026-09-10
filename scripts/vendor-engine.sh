#!/usr/bin/env sh
# Copy the built cartridge into this package, and print the digest it will load under.
#
#   ./scripts/vendor-engine.sh [path-to-cartridge-repo]     # default ../ants
#
# The engine is *vendored* rather than built: ants/ is a repository of its own with its own
# toolchain and its own gate, and a replica image must not need any of that. What ships here is the
# artifact that repository already commits.
#
# THE DIGEST IS THE POINT. Orion identifies a plugin by sha256 over the component bytes, and three
# things must agree on it or the system silently plays nothing: the plugin Orion loads, [vars]
# engine_digest on the replica, and games.active_engine_digest, which pair stamps on every row it
# inserts. The claim filters `engine_digest = $1`, so a mismatch is not an error anywhere -- the
# replica simply claims nothing, for ever, and the queue grows. devops/scripts/deploy/declare-engine.sh
# writes the third from the first.
set -eu

SRC="${1:-$(cd "$(dirname "$0")/../../ants" 2>/dev/null && pwd || true)}"
[ -n "$SRC" ] && [ -d "$SRC" ] || { echo "cartridge repo not found; pass its path" >&2; exit 1; }

cd "$(dirname "$0")/.."
DST=plugins/tb-ants
mkdir -p "$DST"

# cartridge.json is not Orion's: it is the registration manifest the platform reads for presets,
# limits and budgets, and it travels with the component rather than being retyped into [vars].
for f in tb-ants.wasm plugin.json cartridge.json; do
  [ -r "$SRC/$f" ] || { echo "$SRC/$f is missing -- run $SRC/build.sh first" >&2; exit 1; }
  cp "$SRC/$f" "$DST/$f"
done

if command -v sha256sum > /dev/null 2>&1; then D=$(sha256sum "$DST/tb-ants.wasm" | cut -d' ' -f1)
else D=$(shasum -a 256 "$DST/tb-ants.wasm" | cut -d' ' -f1); fi

echo "==> vendored $DST from $SRC"
echo "    tb-ants.wasm   $(wc -c < "$DST/tb-ants.wasm" | tr -d ' ') bytes"
echo
echo "KALAM_ENGINE_DIGEST=sha256:$D"
