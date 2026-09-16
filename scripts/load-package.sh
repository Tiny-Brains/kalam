#!/usr/bin/env sh
# Load (or reload) the Kalam package into a running orion-server.
#
#   kalam/scripts/load-package.sh          # ORION_ADMIN selects the replica
#
# The same shape as soma/scripts/load-package.sh -- the two are deliberately alike, and
# a difference between them should mean something. `orion-server compile` resolves the set --
# including the `$from` constants in shared/kalam.json, which the admin API does not accept -- into
# ONE promotion artifact, and `orion-server package apply` stages it, activates in dependency order
# and reloads the engine once. That is idempotent by content: an unchanged package is a no-op, and
# unlike the delete-by-tag-then-POST loop it replaces, nothing is ever briefly absent.
#
# ONE REPLICA PER RUN. Each replica is its own Orion with its own state database and there is no
# epoch bus between them, so devops' run.sh calls this once per entry in KALAM_ORION_ADMINS.
#
# Environment:
#   ORION_ADMIN                admin API base (default http://127.0.0.1:8080/api/v1/admin)
#   ORION_ADMIN_API_KEY        admin credential, when admin_auth is enabled
#   KALAM_ALLOW_PRIVATE_URLS   1 to set allow_private_urls on the database and loader connectors
#   KALAM_ORION_ADMIN          this node's own admin API, for the kalam-orion connector
#   R2_ENDPOINT                the replay store, for the kalam-blobs-put connector
#   PLUGIN_SIG_DIR             detached Ed25519 signatures, named <component>.sig
set -eu

ADMIN="${ORION_ADMIN:-http://127.0.0.1:8080/api/v1/admin}"
SERVER="${ADMIN%/api/v1/admin}"
STAGE="${TMPDIR:-/tmp}/kalam-pkg.$$"
ARTIFACT="$STAGE.json"
trap 'rm -rf "$STAGE" "$ARTIFACT" "$STAGE.keep"' EXIT

cd "$(dirname "$0")/.."

AUTH=""
[ -n "${ORION_ADMIN_API_KEY:-}" ] && AUTH="Authorization: Bearer ${ORION_ADMIN_API_KEY}"
curl_admin() {
  if [ -n "$AUTH" ]; then curl -sS -H "$AUTH" "$@"; else curl -sS "$@"; fi
}

# THE DEPLOYMENT'S CONNECTOR SETTINGS, applied to a staged copy rather than committed. An http
# connector's `url` is SCHEME-CHECKED and `allow_private_urls` is a BOOLEAN, and both are validated
# by every offline gate -- lint, clippy, compile, package lint -- BEFORE a `var://` or `env://`
# reference would resolve. That is why neither can be a reference and both are applied here: the
# committed package stays lintable and carries no deployment's addresses. A storage connector's
# `endpoint` takes env:// happily, which is why kalam-blobs can and these cannot.
PRIVATE=$([ "${KALAM_ALLOW_PRIVATE_URLS:-0}" = "1" ] && echo true || echo false)

# `kalam-api` IS THE ONE THAT MATTERS OFF-SITE. The others address things inside the deployment, so
# a private address is the normal case for them; this one addresses the platform from wherever the
# runner is, and a runner that will follow a redirect into a private network is the wrong side of
# Orion's S6 posture to be on. Leave KALAM_ALLOW_PRIVATE_URLS unset anywhere the runner is not on
# the compose bridge -- devops/scripts/check/configs.sh asserts exactly that.

# EXCEPT `kalam-orion`, WHICH IS ALWAYS PRIVATE AND MUST BE. It is THIS NODE'S OWN admin API -- the
# roster clock registering and activating a model on the machine it is already running on -- and on
# a standalone runner that address is `127.0.0.1:8080`, which Orion's guard counts as private
# (127/8 is loopback, ssrf.rs). So one variable for all six cannot express a runner's posture: it
# needs `kalam-api` refusing private addresses AND `kalam-orion` permitted one, at the same time.
# Driving this off KALAM_ALLOW_PRIVATE_URLS would mean an off-site runner whose roster clock cannot
# reach itself, and a roster that never catches up is a runner that refuses every seat.
#
# The guard is about egress to somewhere else. A node calling itself is not that.

echo "==> staging the set"
VERSION=$(python3 scripts/stage-set.py . "$STAGE" \
  "kalam-db=allow_private_urls=$PRIVATE" \
  "kalam-models=allow_private_urls=$PRIVATE" \
  "kalam-blobs=allow_private_urls=$PRIVATE" \
  "kalam-blobs-put=allow_private_urls=$PRIVATE" \
  "kalam-blobs-put=url=${R2_ENDPOINT:-}" \
  "kalam-orion=allow_private_urls=true" \
  "kalam-orion=url=${KALAM_ORION_ADMIN:-$ADMIN}" \
  "kalam-api=allow_private_urls=$PRIVATE" \
  "kalam-api=url=${KALAM_API_URL:-http://soma:8080}")

echo "==> compiling kalam@$VERSION"
if ! out=$(orion-server compile "$STAGE" --name kalam --version "$VERSION" -o "$ARTIFACT" 2>&1); then
  printf '%s\n' "$out" >&2
  exit 1
fi

# THE SIGNATURE, attached after compile: `package.content_hash` projects a plugin through its
# manifest, digest and tags only, so adding one does not invalidate the artifact. A signature
# belongs to whoever holds the trust key, never to the package -- which for the cartridge is not
# even this repository's to hold, since the component comes from ants' artifact image.
if [ -n "${PLUGIN_SIG_DIR:-}" ] || [ -d plugins ]; then
  echo "==> attaching plugin signatures"
  SIG_DIR="${PLUGIN_SIG_DIR:-}" python3 -c '
import json, os, pathlib, sys
art = pathlib.Path(sys.argv[1])
doc = json.loads(art.read_text())
sig_dir = os.environ.get("SIG_DIR") or ""
for entry in doc.get("plugins", []):
    name = entry.get("plugin_id") or ""
    manifest = entry.get("manifest") or {}
    component = manifest.get("component") if isinstance(manifest, dict) else None
    if not component:
        continue
    for cand in ([pathlib.Path(sig_dir) / f"{component}.sig"] if sig_dir else []) + \
                list(pathlib.Path("plugins").glob(f"*/{component}.sig")):
        if cand.is_file():
            entry["signature"] = cand.read_text().strip()
            print(f"    {name}  <- {cand}")
            break
    else:
        print(f"    {name}  (unsigned)")
art.write_text(json.dumps(doc, indent=2) + "\n")
' "$ARTIFACT"
fi

# A channel the package no longer ships would otherwise stay active and hold its schedule: `apply`
# adds and updates, it does not remove. This deletes only what the artifact does not carry.
echo "==> retiring objects this package no longer ships"
python3 -c '
import json, sys
a = json.load(open(sys.argv[1]))
for kind, key in (("channels", "channel_id"), ("workflows", "workflow_id"),
                  ("connectors", "id"), ("plugins", "plugin_id")):
    for e in a.get(kind, []):
        print(kind + "/" + e[key])
' "$ARTIFACT" > "$STAGE.keep"

for kind in channels workflows connectors plugins; do
  case "$kind" in
    channels)   key=channel_id ;;
    workflows)  key=workflow_id ;;
    connectors) key=id ;;
    plugins)    key=plugin_id ;;
  esac
  # An artifact with NONE of a kind was built without that kind's source, so it cannot say which of
  # them should exist and must not retire any. THIS PACKAGE IS WHY THE GUARD EXISTS: the cartridge
  # comes from ants' artifact image, so a checkout with no plugins/ compiles to an artifact with no
  # plugins -- and an unguarded sweep would delete tb.ants, which is the whole engine.
  grep -q "^$kind/" "$STAGE.keep" || continue
  for id in $(curl_admin "$ADMIN/$kind?tag=pkg:kalam&limit=500" \
      | python3 -c "import json,sys; [print(o['$key']) for o in json.load(sys.stdin)['data']]"); do
    grep -qx "$kind/$id" "$STAGE.keep" && continue
    curl_admin -X DELETE "$ADMIN/$kind/$id" -o /dev/null || true
    echo "    retired $kind/$id"
  done
done

echo "==> applying"
ORION_ADMIN_TOKEN="${ORION_ADMIN_API_KEY:-}" \
  orion-server package apply -s "$SERVER" -f "$ARTIFACT" | tail -1

echo "==> health"
# Through curl_admin: /health's detail -- the plugin list, the quarantined channels -- is gated on
# a valid admin key. Unauthenticated it still answers 200 and omits them, so this check would
# quietly report nothing wrong on a node where something is.
curl_admin "$SERVER/health" | tr ',' '\n' | grep -E 'quarantined|failed_to_load' || true
