#!/usr/bin/env sh
# Apply the Kalam package into a running orion-server, from this checkout.
#
#   kalam/scripts/load-package.sh                      # ORION_ADMIN selects the replica
#   KALAM_ROLE=admit kalam/scripts/load-package.sh     # an admitting runner's channels
#   kalam/scripts/load-package.sh --prune              # ...and retire what this version dropped
#   kalam/scripts/load-package.sh --compile-only -o F  # shape and compile; apply nothing
#
# `--compile-only` is what docker/entrypoint.sh calls at boot. THE ROLE'S SHAPE IS ONE RULE AND THIS
# IS WHERE IT LIVES: which channels a role gets, and how many slots the match channel has. A node
# and an operator must shape a package identically, and the way to guarantee that is not to write
# the rule twice.
#
# A RUNNER DOES NOT NEED THIS TO BOOT. Its own `[packages] apply` compiles and applies the package
# in the image (docker/runner.toml.tmpl, entrypoint.sh), holding /readyz until it is serving. This
# script is the two things that path is not: applying a WORKING COPY into a node already up, and
# `--prune`, which the boot path deliberately does not do.
#
# The same shape as soma/scripts/load-package.sh -- the two are deliberately alike, and a
# difference between them should mean something.
#
# ONE REPLICA PER RUN. Each replica is its own Orion with its own state database and there is no
# epoch bus between them, so each node applies against itself.
#
# WHICH CHANNELS: a node's role decides. `match` gets the match channel and the roster, `admit`
# gets tb-admit alone, and `KALAM_MATCH_LANES` is the match channel's `slots` -- one channel bounded
# at N, where before Orion 1.9.0 it was N cloned channels a generator wrote.
#
# WHAT THIS SCRIPT NO LONGER DOES, because Orion 1.9.0 does it: a staged copy of the set (connector
# URLs and booleans are references in the committed definitions now, #338), a content-derived
# version (`--version content`, #339), patching in signatures (`--signatures`, #340), a sweep that
# deleted what the artifact did not carry (`--prune`, #341), and reading /health back to see
# whether the apply really served (`apply` fails on a quarantined member itself, #342).
#
# Environment:
#   ORION_ADMIN             admin API base (default http://127.0.0.1:8080/api/v1/admin)
#   ORION_ADMIN_API_KEY     admin credential, when admin_auth is enabled
#   PLUGIN_SIG_DIR          detached Ed25519 signatures, named <component>.sig or <plugin id>.sig
#   KALAM_ROLE              match (default) or admit
#   KALAM_MATCH_LANES       matches at once, as the match channel's slots (unset keeps the shipped max)
#
# Everything a deployment varies is read by the definitions and the instance config: the platform's
# URL, this node's admin API, the replay endpoint and `allow_private_urls` as references on the
# connectors, and the runner key and engine digest as [vars].
set -eu

ADMIN="${ORION_ADMIN:-http://127.0.0.1:8080/api/v1/admin}"
SERVER="${ADMIN%/api/v1/admin}"
ARTIFACT="${TMPDIR:-/tmp}/kalam-pkg.$$.json"

cd "$(dirname "$0")/.."

PRUNE=""
COMPILE_ONLY=0
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --prune)        PRUNE="--prune" ;;
    --prune=delete) PRUNE="--prune=delete" ;;
    --compile-only) COMPILE_ONLY=1 ;;
    -o)             shift; OUT="${1:-}" ;;
    *) echo "usage: load-package.sh [--prune | --prune=delete] [--compile-only -o <file>]" >&2; exit 2 ;;
  esac
  shift
done
[ "$COMPILE_ONLY" = 1 ] && [ -z "$OUT" ] && { echo "--compile-only needs -o <file>" >&2; exit 2; }
[ -n "$OUT" ] && ARTIFACT="$OUT"

# THIS ROLE'S CHANNELS, shaped into a copy -- the checkout is never written to. The same two facts
# the entrypoint applies: which channels this node runs, and how many matches at once.
ROLE="${KALAM_ROLE:-match}"
SET="${TMPDIR:-/tmp}/kalam-set.$$"
# The artifact survives when the caller named it; the working copy never does.
if [ "$COMPILE_ONLY" = 1 ]; then
  trap 'rm -rf "$SET"' EXIT
else
  trap 'rm -rf "$SET"; rm -f "$ARTIFACT"' EXIT
fi
mkdir -p "$SET"
cp -R . "$SET"/
rm -rf "$SET/scripts" "$SET/docker"

echo "==> shaping the package for a $ROLE runner"
if [ "$ROLE" = admit ]; then
  rm -f "$SET/channels/tb-match.json" "$SET/channels/tb-roster.json"
else
  rm -f "$SET/channels/tb-admit.json"
  if [ -n "${KALAM_MATCH_LANES:-}" ]; then
    # `slots` is a literal in the channel, because Orion takes lock cardinality as an authoring
    # decision. The shipped value is the maximum; this is the number THIS node plays.
    ch="$SET/channels/tb-match.json"
    sed "s/\"slots\": [0-9][0-9]*/\"slots\": $KALAM_MATCH_LANES/" "$ch" > "$ch.tmp" && mv "$ch.tmp" "$ch"
    grep -q "\"slots\": $KALAM_MATCH_LANES" "$ch" || {
      echo "could not set the match channel to $KALAM_MATCH_LANES slot(s)" >&2; exit 1; }
  fi
fi

echo "==> compiling"
orion-server compile "$SET" --version content -o "$ARTIFACT" | tail -1
[ "$COMPILE_ONLY" = 1 ] && exit 0

echo "==> applying${PRUNE:+ (with $PRUNE)}"
ORION_ADMIN_TOKEN="${ORION_ADMIN_API_KEY:-}" \
  orion-server package apply -s "$SERVER" -f "$ARTIFACT" \
    ${PLUGIN_SIG_DIR:+--signatures "$PLUGIN_SIG_DIR"} \
    ${PRUNE:+$PRUNE}
