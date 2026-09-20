#!/usr/bin/env sh
# A Kalam RUNNER: derive what this machine is and which role it plays, migrate its disposable state,
# load the package this image carries, and exec orion-server.
#
# THE ONLY SETTINGS A RUNNER NEEDS ARE THE PLATFORM'S ADDRESSES AND ITS KEY. It holds no database
# credential and no bucket secret: every match statement is a call to Soma's gate, every model is a
# GET on a public-read bucket, and every replay is a PUT to a URL the gate presigned. docker-compose.yml
# at the repository root is the one service that runs this.
#
# THE PACKAGE IS LOADED BY THE NODE ITSELF, because there is nothing else on a runner's machine to do
# it: forked BEFORE the exec, so the exec still happens and SIGTERM still reaches Orion directly. The
# fork outlives the shell it came from and is reaped by whatever is PID 1 -- the compose file sets
# `init: true` so that is tini. Without it the fork lingers as one zombie: harmless, but explicable.
set -eu

CFG="${ORION_CONFIG_TEMPLATE:-/etc/orion/runner.toml.tmpl}"
PKG="${KALAM_PKG_DIR:-/pkg/kalam}"
[ -r "$CFG" ] || { echo "instance config not readable at $CFG" >&2; exit 1; }
: "${ORION_ADMIN_KEY:?ORION_ADMIN_KEY is required -- this node's own admin key; the package is loaded over it}"

# ---------------------------------------------------------------- the state database
# A SQLite file holding the loaded package and nothing else, so a container-local path with no
# volume behind it is correct: it is rebuilt on every boot.
state_dir=$(dirname "${ORION_STATE_PATH:-/var/lib/orion/state.db}")
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

# ---------------------------------------------------------------- the role
# WHAT THIS MACHINE DOES, AND A RUNNER DOES ONE OR THE OTHER. `match` (the default) plays matches:
# its lanes and the roster clock. `admit` admits submissions for Soma's admit clock: the tb-admit
# lane and nothing else, so an admission never takes CPU from a match on the same node, and its
# probe is not timed under a match's load. load-package.sh leaves the other role's channels out.
KALAM_ROLE="${RUNNER_ROLE:-match}"
case "$KALAM_ROLE" in
  match|admit) ;;
  *) echo "RUNNER_ROLE must be match or admit, got '$KALAM_ROLE'" >&2; exit 1 ;;
esac

# ---------------------------------------------------------------- lanes and workers
# RUNNER_CRON_WORKERS IS MATCHES AT ONCE, and it is spent as LANES, not as a bigger pool. Orion has
# one cron worker pool per node and a match holds its worker for the whole match, so a pool shared
# by four lanes and the roster clock gives the roster a worker only when a lane is idle -- and its
# ticks are skipped (misfire `skip`) whenever it is not. A new version then goes unregistered while
# every lane refuses its trial. So exactly this many lanes are loaded (load-package.sh drops the
# rest before compile), each `forbid` on its own key and so never holding more than one worker, and
# Orion gets one worker more: the roster's, which no match can take. An admitting runner plays no
# match and has one worker, the admission's.
if [ "$KALAM_ROLE" = admit ]; then
  KALAM_MATCH_LANES=0
  KALAM_CRON_WORKERS=1
  channels=1
  echo "==> an admitting runner: the tb-admit lane, 1 cron worker, no match lanes"
else
  lanes="${RUNNER_CRON_WORKERS:-2}"
  case "$lanes" in
    ''|*[!0-9]*) echo "RUNNER_CRON_WORKERS must be a whole number of matches, got '$lanes'" >&2; exit 1 ;;
  esac
  [ "$lanes" -ge 1 ] || { echo "RUNNER_CRON_WORKERS must be at least 1" >&2; exit 1; }
  shipped=$(ls "$PKG"/channels/tb-match-*.json 2>/dev/null | wc -l | tr -d ' ')
  [ "$shipped" -ge 1 ] || { echo "no match lanes under $PKG/channels" >&2; exit 1; }
  if [ "$lanes" -gt "$shipped" ]; then
    echo "==> RUNNER_CRON_WORKERS=$lanes, but the package ships $shipped match lanes: playing $shipped"
    lanes=$shipped
  fi
  KALAM_MATCH_LANES=$lanes
  KALAM_CRON_WORKERS=$((lanes + 1))
  channels=$KALAM_CRON_WORKERS
  echo "==> $KALAM_MATCH_LANES match lane(s), $KALAM_CRON_WORKERS cron workers (one is the roster's)"
fi
export KALAM_ROLE KALAM_MATCH_LANES KALAM_CRON_WORKERS

echo "==> migrating state"
orion-server -c "$CFG" migrate > /dev/null

# ---------------------------------------------------------------- load the package
# A FAILED LOAD IS LOUD AND FATAL. A node whose package did not load answers /readyz 200, carries no
# match channel, claims nothing, and is counted as capacity by everything that looks -- the
# invisible-capacity failure, which on a machine nobody is watching is the worst shape there is.
(
  i=0
  until curl -fsS http://127.0.0.1:8080/readyz > /dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
      echo "self-load: orion did not become ready in 120s" >&2
      kill -TERM 1 2>/dev/null
      exit 1
    fi
    sleep 2
  done
  echo "==> loading the kalam package into this node"
  if ORION_ADMIN=http://127.0.0.1:8080/api/v1/admin \
     ORION_ADMIN_API_KEY="$ORION_ADMIN_KEY" \
     sh "$PKG/scripts/load-package.sh"; then
    # WHAT "LOADED" HAS TO MEAN. /readyz says the server is up; the plugin list says the engine
    # arrived. NEITHER says this node will ever claim a row -- `package apply` activates in
    # dependency order and STOPS at the first workflow it cannot activate, leaving the rest as
    # DRAFTS, and a draft cron channel holds no schedule. So the assertion is the channels, in the
    # state that makes them fire.
    active=$(curl -fsS -H "Authorization: Bearer $ORION_ADMIN_KEY" \
               "http://127.0.0.1:8080/api/v1/admin/channels?tag=pkg:kalam&limit=100" \
             | tr '{' '\n' | grep -c '"status":"active"' || true)
    plugin=$(curl -fsS -H "Authorization: Bearer $ORION_ADMIN_KEY" http://127.0.0.1:8080/health \
             | grep -c '"tb.ants"' || true)
    if [ "$plugin" -ge 1 ] && [ "$active" -ge "$channels" ]; then
      echo "==> loaded: tb.ants is live and $active channels are active, this node can claim"
    else
      echo "self-load: tb.ants=$plugin active-channels=$active of $channels -- this node is invisible" >&2
      echo "           capacity, not a runner. A channel left in draft means package apply" >&2
      echo "           could not activate a workflow: read the error above it." >&2
      kill -TERM 1 2>/dev/null
      exit 1
    fi
  else
    echo "self-load: the package did not load -- this node would claim nothing, for ever" >&2
    kill -TERM 1 2>/dev/null
    exit 1
  fi
) &

echo "==> starting orion-server with $CFG"
exec orion-server -c "$CFG"
