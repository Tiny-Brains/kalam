#!/usr/bin/env sh
# A Kalam RUNNER: derive what this machine is and which role it plays, migrate its disposable state,
# load the package this image carries, and exec orion-server.
#
# THE ONLY SETTINGS A RUNNER NEEDS ARE THE PLATFORM'S ADDRESSES AND ITS KEY. It holds no database
# credential and no bucket secret: every match statement is a call to Soma's gate, every model is a
# GET on a public-read bucket, and every replay is a PUT to a URL the gate presigned. docker-compose.yml
# at the repository root is the one service that runs this.
#
# THE PACKAGE IS APPLIED BY THE NODE ITSELF, because there is nothing else on a runner's machine to
# do it -- and by the SERVER, not by a fork of this script: `[packages] apply` in runner.toml.tmpl
# holds /readyz at 503 until the package is serving and stops the node if it cannot be. What this
# script does is shape the package for this machine's role and slot count, and compile it.
set -eu

CFG="${ORION_CONFIG_TEMPLATE:-/etc/orion/runner.toml.tmpl}"
PKG="${KALAM_PKG_DIR:-/pkg/kalam}"
[ -r "$CFG" ] || { echo "instance config not readable at $CFG" >&2; exit 1; }
: "${ORION_ADMIN_KEY:?ORION_ADMIN_KEY is required -- the admin key of this node itself; the package is applied over it}"

# ---------------------------------------------------------------- the state database
# A SQLite file holding the loaded package and a few hours of run records, so it belongs on the
# tmpfs docker-compose.yml mounts here: it is rebuilt on every boot, a crash's included.
state_dir=$(dirname "${ORION_STATE_PATH:-/var/lib/orion/state/state.db}")
mkdir -p "$state_dir" 2>/dev/null || true
[ -w "$state_dir" ] || { echo "state directory $state_dir is not writable by $(id -un)" >&2; exit 1; }

# ---------------------------------------------------------------- the architecture
# Reported at token exchange and never enforced -- it answers "what is that machine" on the Runners
# screen. DERIVED, because a hand-typed `amd64` on an arm64 Mac is a label that lies in exactly the
# situation that screen exists for. Docker's spelling, not uname's.
if [ -z "${RUNNER_ARCH:-}" ]; then
  case "$(uname -m)" in
    x86_64|amd64)  RUNNER_ARCH=amd64 ;;
    aarch64|arm64) RUNNER_ARCH=arm64 ;;
    *)             RUNNER_ARCH="$(uname -m)" ;;
  esac
fi
export RUNNER_ARCH
echo "==> arch $RUNNER_ARCH"

# ---------------------------------------------------------------- the engine digest
# The engine this runner plays, and the only rows its match clock may claim. DERIVED from the
# component in this image rather than typed, so the plugin key and the claim filter agree by
# construction, and Soma's declared digest agrees when both images were built from one ants release.
# A digest that agrees with nothing is silent everywhere: the clock claims nothing, for ever.
if [ -z "${KALAM_ENGINE_DIGEST:-}" ]; then
  wasm="$PKG/plugins/tb-ants/tb-ants.wasm"
  [ -r "$wasm" ] || { echo "engine component not readable at $wasm" >&2; exit 1; }
  KALAM_ENGINE_DIGEST="sha256:$(sha256sum "$wasm" | cut -d' ' -f1)"
fi
export KALAM_ENGINE_DIGEST
echo "==> engine $KALAM_ENGINE_DIGEST"

# ---------------------------------------------------------------- private addresses
# [vars] allow_private_urls must substitute to a bare TOML BOOLEAN, and `${X:-false}` falls back
# only when X is UNSET -- docker-compose.yml passes it through as EMPTY when the deployment leaves
# RUNNER_ALLOW_PRIVATE_URLS out, which would substitute as empty and stop the line being TOML.
# Normalised here, so `1`, `yes`, empty and absent all mean something.
case "${KALAM_ALLOW_PRIVATE_URLS:-}" in
  1|true|yes|on) KALAM_ALLOW_PRIVATE_URLS=true ;;
  *)             KALAM_ALLOW_PRIVATE_URLS=false ;;
esac
export KALAM_ALLOW_PRIVATE_URLS
echo "==> private addresses: $KALAM_ALLOW_PRIVATE_URLS"

# ---------------------------------------------------------------- the role
# WHAT THIS MACHINE DOES, AND A RUNNER DOES ONE OR THE OTHER. `match` (the default) plays matches:
# its lanes and the roster clock. `admit` admits submissions for Soma's admit clock: the kalam-admit
# lane and nothing else, so an admission never takes CPU from a match on the same node, and its
# probe is not timed under a match's load. load-package.sh leaves the other role's channels out.
KALAM_ROLE="${RUNNER_ROLE:-match}"
case "$KALAM_ROLE" in
  match|admit) ;;
  *) echo "RUNNER_ROLE must be match or admit, got '$KALAM_ROLE'" >&2; exit 1 ;;
esac

# ---------------------------------------------------------------- lanes and workers
# RUNNER_CRON_WORKERS IS MATCHES AT ONCE, and it is the match channel's `slots`. Until Orion 1.9.0
# `forbid` meant exactly one occurrence per key, so this was N cloned channels differing only in
# their key and the loader dropped the ones above the count; `slots` is the bound itself.
#
# ORION GETS ONE WORKER MORE, and it is the roster's. There is one cron worker pool per node and a
# match holds its worker for the whole match, so a pool sized to the lanes gives the roster a worker
# only when a lane is idle -- and its ticks are skipped (misfire `skip`) whenever it is not. A new
# version then goes unregistered while every lane refuses its trial. An admitting runner plays no
# match and has one worker, the admission's.
if [ "$KALAM_ROLE" = admit ]; then
  KALAM_MATCH_LANES=0
  KALAM_CRON_WORKERS=1
  KALAM_SEAT_CONCURRENCY=
  echo "==> an admitting runner: the kalam-admit channel, 1 cron worker, no match lanes"
else
  lanes="${RUNNER_CRON_WORKERS:-2}"
  case "$lanes" in
    ''|*[!0-9]*) echo "RUNNER_CRON_WORKERS must be a whole number of matches, got '$lanes'" >&2; exit 1 ;;
  esac
  [ "$lanes" -ge 1 ] || { echo "RUNNER_CRON_WORKERS must be at least 1" >&2; exit 1; }
  # A DEPLOYMENT SETTING, NOT A PACKAGE ONE: load-package.sh writes this into the match channel's
  # `slots`, whatever the committed number is. Orion's own bound on `slots` is 64; what a machine
  # can afford is its memory and cores, which RUNNER_SEAT_CONCURRENCY below divides between slots.
  [ "$lanes" -le 64 ] || { echo "RUNNER_CRON_WORKERS must be at most 64 (Orion's bound on slots), got $lanes" >&2; exit 1; }
  KALAM_MATCH_LANES=$lanes
  KALAM_CRON_WORKERS=$((lanes + 1))
  echo "==> $KALAM_MATCH_LANES match slot(s), $KALAM_CRON_WORKERS cron workers (one is the roster's)"

  # SEATS ONE MATCH ASKS AT ONCE, and never more than the cores leave room for. A turn's inferences
  # run in parallel up to this many, and every one is timed against the turn's deadline from the
  # moment it asks -- WAITING FOR ORION'S INFERENCE PERMIT INCLUDED, and the permits are one per core
  # (`models.max_concurrent_inferences` defaults to the cores this container may use). So slots x
  # seats must stay within the cores, or a seat is struck for the node's load, not its model.
  # Derived from the cgroup's CPU quota when one is set (docker's `cpus`), else the cores present.
  cpus=$(nproc)
  if [ -r /sys/fs/cgroup/cpu.max ]; then
    read -r quota period < /sys/fs/cgroup/cpu.max || true
    if [ "${quota:-max}" != max ] && [ "${period:-0}" -gt 0 ]; then
      cpus=$((quota / period)); [ "$cpus" -ge 1 ] || cpus=1
    fi
  fi
  seats="${RUNNER_SEAT_CONCURRENCY:-$((cpus / lanes))}"
  case "$seats" in
    ''|*[!0-9]*) echo "RUNNER_SEAT_CONCURRENCY must be a whole number of seats, got '$seats'" >&2; exit 1 ;;
  esac
  [ "$seats" -ge 1 ] || seats=1
  [ "$seats" -le 8 ] || seats=8
  if [ $((seats * lanes)) -gt "$cpus" ]; then
    echo "==> WARNING: $lanes slot(s) x $seats seat(s) at once exceeds $cpus core(s); seats will be struck for this node's load" >&2
  fi
  KALAM_SEAT_CONCURRENCY=$seats
  echo "==> $KALAM_SEAT_CONCURRENCY seat(s) of a match asked at once ($cpus core(s))"
fi
export KALAM_ROLE KALAM_MATCH_LANES KALAM_CRON_WORKERS KALAM_SEAT_CONCURRENCY

echo "==> migrating state"
orion-server -c "$CFG" migrate --wait 30s > /dev/null

# ---------------------------------------------------------------- the package for this node
# WHAT A NODE SHAPES, AND IT IS NOT AUTHORING. The package in this image is complete and authored:
# every workflow, every channel and every statement. Two things about it are this MACHINE's -- which
# role it plays, and how many matches it plays at once -- and `scripts/load-package.sh` is where
# that rule lives, for a node and for an operator alike. It shapes a COPY, so the image's own tree
# is never written to and a restart shapes it the same way whatever the last boot did.
#
# `--compile-only` stops before applying: applying is the SERVER's job here (`[packages] apply` in
# runner.toml.tmpl), which is what holds /readyz until the package is serving.
ARTIFACT="${KALAM_ARTIFACT:-/var/lib/orion/kalam.package.json}"
export KALAM_ARTIFACT="$ARTIFACT"

KALAM_ROLE="$KALAM_ROLE" KALAM_MATCH_LANES="$KALAM_MATCH_LANES" \
  KALAM_SEAT_CONCURRENCY="${KALAM_SEAT_CONCURRENCY:-}" \
  sh "$PKG/scripts/load-package.sh" --compile-only -o "$ARTIFACT" > /dev/null

echo "==> starting orion-server with $CFG"
exec orion-server -c "$CFG"
