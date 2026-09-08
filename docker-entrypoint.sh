#!/usr/bin/env sh
# Check the environment, wait for Postgres, start orion-server, load the Kalam package.
#
# Same shape as Soma's, with three differences that matter:
#
#   1. Orion's own state is a LOCAL SQLITE FILE, not Postgres and not shared. A replica coordinates
#      with nothing: it takes work by claiming rows in Soma's database, and its Orion state is
#      private scratch it is free to lose. That is what makes a replica disposable.
#   2. It waits on the MATCH database rather than on its own state, since the state file cannot
#      fail to be reachable.
#   3. It checks the Model Loader is answering before loading the package. A replica with no loader
#      can claim matches it then cannot play, and every one of those costs a lease and two lapses
#      before the row goes back to the queue.
set -eu

: "${KALAM_DB_URL:?KALAM_DB_URL is required -- the match database, on the kalam role}"
: "${MODEL_LOADER_URL:?MODEL_LOADER_URL is required -- the loader beside this replica}"
: "${KALAM_ENGINE_DIGEST:?KALAM_ENGINE_DIGEST is required -- the engine this replica plays}"

CFG="${ORION_CONFIG_TEMPLATE:-/etc/kalam/orion.toml.tmpl}"
if [ ! -r "$CFG" ]; then
  echo "instance config not readable at $CFG -- mount it, or set ORION_CONFIG_TEMPLATE" >&2
  exit 1
fi

echo "==> migrating local state"
# SQLite, so this is creating a file rather than reaching a server; it still has to happen before
# the server starts.
orion-server -c "$CFG" migrate

echo "==> starting orion-server"
orion-server -c "$CFG" &
ORION_PID=$!
# SIGTERM is the drain signal (finding 8b): Orion stops claiming new occurrences and lets the wave
# in hand finish inside cron.shutdown_timeout_secs, which the config sets above the longest match.
# Forwarding it rather than killing the container is the whole of Kalam's scale-down story.
trap 'kill -TERM "$ORION_PID" 2>/dev/null || true' TERM INT

i=0
until curl -fsS http://127.0.0.1:8080/readyz > /dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 30 ]; then echo "orion-server did not become ready" >&2; exit 1; fi
  kill -0 "$ORION_PID" 2>/dev/null || { echo "orion-server exited during startup" >&2; wait "$ORION_PID"; }
  sleep 1
done

echo "==> waiting for the model loader at $MODEL_LOADER_URL"
i=0
until curl -fsS "${MODEL_LOADER_URL}/healthz" > /dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 30 ]; then
    echo "the model loader did not answer; refusing to claim matches this replica cannot play" >&2
    exit 1
  fi
  sleep 2
done

# The match database and the loader are both private addresses by design -- the loader is loopback,
# and the database is a compose service name or a VPC host. See scripts/load-package.sh.
KALAM_ALLOW_PRIVATE_URLS="${KALAM_ALLOW_PRIVATE_URLS:-1}" /app/scripts/load-package.sh

echo "==> kalam is up on :8080, playing engine $KALAM_ENGINE_DIGEST"

# THE DRAIN, and it needs two waits rather than one. A `wait` that is interrupted by a trap
# RETURNS -- so the obvious `trap ...; wait $PID` runs the trap, returns immediately, falls off the
# end of the script, and the container exits while Orion is still finishing the wave. Measured: a
# `docker stop -t 300` came back in ZERO seconds with two rows still `running`, which then sat
# unplayable until their leases lapsed. That is the exact failure drain exists to prevent, and it
# looks like a successful shutdown from the outside.
#
# So: wait once for the signal, then wait again -- in a loop, because every wait is interruptible --
# until the process is actually gone. Orion holds the occurrence for up to cron.shutdown_timeout_secs
# while it finishes the wave in hand and claims nothing new; the orchestrator's grace period must be
# longer still (layer 07), or it SIGKILLs the container mid-match and the rows go back to the queue
# to be replayed from turn zero.
wait "$ORION_PID" 2>/dev/null || true
while kill -0 "$ORION_PID" 2>/dev/null; do
  wait "$ORION_PID" 2>/dev/null || sleep 1
done
echo "==> orion-server exited; the wave in hand is finished"
