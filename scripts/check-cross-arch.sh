#!/usr/bin/env bash
# THE SAME OFFLINE CASES, ON THE OTHER ARCHITECTURE. Images ship for amd64 and arm64, and a model's
# memory is fed back into it every turn -- so if tract's CPU kernels disagreed by a bit anywhere,
# the difference would compound turn over turn and the two architectures would play different
# moves from the same board. Nothing about the platform makes that impossible, and for a season
# that allows memory it is the question worth answering before the season opens.
#
#   ./scripts/check-cross-arch.sh
#
# WHY THE OFFLINE CASES ARE THE RIGHT INSTRUMENT, rather than playing a match on two machines and
# diffing the replays: `tests/match/m8.case.json` already pins seat 0's ENDING MEMORY TENSOR byte
# for byte after ten turns of real tract inference with the memory handed back each turn, and
# `tests/admit/a4.case.json` pins the admission probe's own memory chain. A tensor that came out a
# bit different anywhere in those ten turns is a failed assertion naming the tensor. So the check is
# just: run the committed cases under the other architecture and see that they still pass. It needs
# no stack, no key and no second machine.
#
# IT RUNS THE RELEASE BINARY, NOT A BUILD. orion-server comes from the same GitHub release the
# Dockerfile installs (ORION_VERSION below is read out of it, so there is one pin), and the
# Dockerfile's own note is what makes that sufficient: every stage that BUILDS runs on the build
# platform, so the amd64 and arm64 images carry identical packages and identical cartridge bytes,
# and only the runtime stage differs. The binary is the whole of the difference, and this runs it.
#
# Needs: Docker with emulation for the other platform (Docker Desktop has it), and the network, the
# first time. It is SLOW under emulation -- minutes, not the seconds check-tests.sh takes -- which
# is why it is its own script and not a flag on that one. Run it when the Orion pin moves, when a
# model fixture changes, and before a season that allows memory opens.
set -euo pipefail
cd "$(dirname "$0")/.."

# The platform to check AGAINST: the one this machine is not.
HOST_ARCH=$(uname -m)
case "$HOST_ARCH" in
  arm64 | aarch64) OTHER=linux/amd64; TRIPLE=x86_64-unknown-linux-gnu ;;
  x86_64 | amd64)  OTHER=linux/arm64; TRIPLE=aarch64-unknown-linux-gnu ;;
  *) echo "no other platform known for $HOST_ARCH" >&2; exit 1 ;;
esac

# ONE PIN, and it is the Dockerfile's: the version in the image is the version a runner runs.
ORION_VERSION=$(sed -n 's/^ARG ORION_VERSION=\(.*\)$/\1/p' Dockerfile)
[ -n "$ORION_VERSION" ] || { echo "Dockerfile declares no ARG ORION_VERSION" >&2; exit 1; }

[ -f plugins/tb-ants/tb-ants.wasm ] || {
  echo "plugins/tb-ants is missing the cartridge -- ./scripts/check-tests.sh says how to fill it" >&2
  exit 1
}
for m in tests/models/*/; do
  [ -f "$m/model.onnx" ] || {
    echo "$(basename "$m") has no model.onnx -- run ./scripts/check-tests.sh once to copy the fixtures" >&2
    exit 1
  }
done

CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/tinybrains/orion-$ORION_VERSION-$TRIPLE"
BIN="$CACHE/orion-server"
if [ ! -x "$BIN" ]; then
  echo "==> fetching orion-server $ORION_VERSION for $TRIPLE"
  mkdir -p "$CACHE"
  base="https://github.com/GoPlasmatic/Orion/releases/download/v${ORION_VERSION}"
  curl -fsSL --retry 5 -o "$CACHE/orion.tar.xz"    "${base}/orion-server-${TRIPLE}.tar.xz"
  curl -fsSL --retry 5 -o "$CACHE/orion.sha256"    "${base}/orion-server-${TRIPLE}.tar.xz.sha256"
  # The release's own sum, checked here rather than trusted: this binary decides what "identical"
  # means for the whole answer below.
  ( cd "$CACHE" && awk '{print $1 "  orion.tar.xz"}' orion.sha256 | shasum -a 256 -c - > /dev/null )
  tar xf "$CACHE/orion.tar.xz" -C "$CACHE"
  install -m 0755 "$CACHE/orion-server-${TRIPLE}/orion-server" "$BIN"
fi

echo "==> $OTHER (this machine is $HOST_ARCH), orion-server $ORION_VERSION"
# ca-certificates: orion-server builds an HTTP client at startup whatever the command, and a
# bookworm-slim with no CA store panics before it reads a case. Nothing here reaches the network.
docker run --rm --platform "$OTHER" \
  -v "$BIN:/usr/local/bin/orion-server:ro" \
  -v "$PWD:/pkg:ro" -w /pkg \
  debian:bookworm-slim sh -c '
    apt-get -qq update > /dev/null 2>&1 && apt-get -qq install -y ca-certificates > /dev/null 2>&1
    fail=0
    for g in tests/match tests/admit tests/roster; do
      orion-server test "$g" --definitions shared --plugin-dir plugins/tb-ants --model-dir tests/models || fail=1
    done
    exit $fail'

echo "==> the cases pass on $OTHER as they do here, M8 and A4 among them: tract answers the same"
echo "    bytes on both, so a memory carried turn to turn cannot make the two play different moves."
