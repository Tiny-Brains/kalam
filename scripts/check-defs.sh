#!/usr/bin/env bash
# Every check that reads the definitions and nothing else. No database, no stack, no Docker --
# so this is the one to run on every change and the one a CI job runs first.
#
#   ./scripts/check-defs.sh
#
# Four gates, and between them they are everything:
#
#   clippy   RUNS LINT'S GATE FIRST -- the set resolves, every reference, every function input
#            schema, every declared env var -- and stops on it ("N lint error(s) -- fix those
#            first; clippy's rules did not run"). So `lint` is not run separately: it would be the
#            same work twice. Then clippy's own rules: what lint cannot prove but can be certain
#            of -- a run of steps repeating one condition, an object copied across the set, an
#            input key the function ignores. An ignored input key is silent at run time and a
#            whole feature stops working, which is why it is --deny-warnings.
#   fmt      the house style, so a diff is the change and not a reformat.
#   names    the ids and the three tags; Orion checks that a reference RESOLVES and has no opinion
#            about what anything is CALLED, and `?tag=` is the only navigation there is.
#   tests    the offline cases: a whole workflow run with the gate stubbed and the engine and the
#            models real. Lint proves the set RESOLVES; this is what proves a lost lease halts and
#            a struck seat forfeits.
#
# It runs clippy TWICE, and the second run is not a repeat: three rules say nothing without the
# serving config, and "said nothing" reads exactly like "found nothing".
#
# There is no SQL check here and no sql/ directory: every statement a runner needs is a call to
# Soma's gate, so the one copy -- and the grant boundary it rests on -- is soma's to check
# (scripts/check-sql.sh there). check-names.sh FAILS if an sql/ appears in this repository.
#
# What this does NOT check is anything that needs a stack: a real match is `docker compose up -d
# --build` against web's local platform.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v orion-server > /dev/null || {
  echo "orion-server is not on PATH -- this repo is an Orion package and the binary is its compiler" >&2
  exit 1
}

# WHICH orion-server is not asked here. shared/package.json declares `requires.orion`, and lint,
# clippy, fmt and compile each check the running binary against it before anything else -- one
# declaration instead of a version test per script, and it travels with the package to `apply`.

echo "==> clippy (lint's gate, then the rules that need no config)"
orion-server clippy . --deny-warnings

# Over everything: every file here is authored and committed.
echo "==> fmt"
orion-server fmt --check .

# ---------------------------------------------------------------- the names and the tags
# Orion checks that every reference RESOLVES; it has no opinion about what anything is CALLED, and
# `?tag=` is the only way to navigate a list. So the naming rules are checked here or nowhere.
echo "==> names and tags"
./scripts/check-names.sh

# ---------------------------------------------------------------- the offline cases
# Every *.case.json under tests/, through `orion-server test`: the gate stubbed, the engine and the
# models real. Lint proves the set resolves; this is what proves a lost lease halts, a struck seat
# forfeits, a missing version is registered. Since Orion 1.10.0 a case runs at a node's cost.
echo "==> offline test cases"
./scripts/check-tests.sh

# ---------------------------------------------------------------- the serving config's rules
# Three clippy rules need the config this node will actually serve with, because what they prove is
# a definition against a setting: a `[vars]` name nothing declares, a `secret` nothing supplies, and
# a `model_infer` deadline `[models]` would silently clamp. That last one matters here -- the match
# lane's deadline is the season's `turn_ms` and `models.max_timeout_ms` clamps a longer one WITHOUT
# SAYING SO, giving the model less time than the match is scored against. Without -c the rules are
# SKIPPED, which reads as a pass.
#
# AGAINST runner.toml.tmpl, which is now the only config this repository ships and the one every
# image copies. It used to run against replica-db.toml.tmpl, whose `[vars]` was a superset, because
# the package carried a `db`-mode branch reading `lease_seconds`, `turn_ms`, `model_prefix` and the
# rest of the execution contract. That branch is gone -- the contract rides the claim -- so the set
# reads exactly the ten vars a runner declares, and the two-template coupling went with it.
#
# STAND-IN VALUES FOR WHAT THE TEMPLATE REQUIRES: `${NAME:?message}` stops a boot when the variable
# is unset or empty, which is what it is for; this is a static check, not a boot, so it supplies
# something shaped right and obviously fake. A variable named only in a COMMENT is not required --
# Orion skips the file's comments when it substitutes.
echo "==> clippy against the shipped instance config"
RUNNER_KEY=check-defs-not-a-key \
ORION_ADMIN_KEY=check-defs-not-a-key \
KALAM_ENGINE_DIGEST=sha256:0000000000000000000000000000000000000000000000000000000000000000 \
TB_TRUST_PUBLIC_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAI= \
RUNNER_ARCH=arm64 \
  orion-server clippy . -c docker/runner.toml.tmpl --deny-warnings

echo "==> Kalam's definitions are clean"
