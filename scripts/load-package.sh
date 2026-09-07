#!/usr/bin/env sh
# Load (or reload) the Kalam package into a running orion-server.
#
# The same shape as soma/scripts/load-package.sh -- the two packages are deliberately alike, and a
# difference between them should mean something. Used by the host and by docker-entrypoint.sh once
# the server answers /readyz.
#
# It is idempotent: everything tagged pkg:kalam is deleted before the package is re-created, so it
# can be re-run after an edit. An *active* workflow is immutable in Orion, so a second POST would
# otherwise be a conflict.
#
# Environment:
#   ORION_ADMIN                admin API base (default http://127.0.0.1:8080/api/v1/admin)
#   ORION_ADMIN_API_KEY        sent as a bearer token when admin_auth is enabled
#   KALAM_ALLOW_PRIVATE_URLS   1 to set allow_private_urls on the database and loader connectors
#
# The loader is ALWAYS a private address -- it is loopback by design, one beside every replica --
# so unlike Soma, where the flag is a local-development convenience for db:5432, here it is a
# deployment fact. It stays a flag rather than a committed connector field for the same reason:
# the package should not ship with the SSRF guard disabled.
set -eu

ADMIN="${ORION_ADMIN:-http://127.0.0.1:8080/api/v1/admin}"
ALLOW_PRIVATE="${KALAM_ALLOW_PRIVATE_URLS:-0}"

cd "$(dirname "$0")/.."

AUTH=""
[ -n "${ORION_ADMIN_API_KEY:-}" ] && AUTH="Authorization: Bearer ${ORION_ADMIN_API_KEY}"

curl_admin() {
  if [ -n "$AUTH" ]; then curl -sS -H "$AUTH" "$@"; else curl -sS "$@"; fi
}
req() { curl_admin --fail-with-body "$@"; }

# jq where available, python3 otherwise -- the host has python3, the image has jq.
if command -v jq > /dev/null 2>&1; then
  field() { jq -r ".$2" "$1"; }
  ids() { jq -r ".data[].$1"; }
  with_private_urls() { jq '.config.allow_private_urls = true' "$1"; }
  plugin_body() { jq -n --slurpfile m "$1" --rawfile c "$2" \
      '{plugin_id: $m[0].name, manifest: $m[0], component: ($c | rtrimstr("\n")), tags: ["pkg:kalam"]}'; }
else
  field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }
  ids() { python3 -c 'import json,sys; [print(o[sys.argv[1]]) for o in json.load(sys.stdin)["data"]]' "$1"; }
  with_private_urls() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["config"]["allow_private_urls"]=True; print(json.dumps(d))' "$1"; }
  plugin_body() { python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print(json.dumps({"plugin_id": m["name"], "manifest": m, "component": open(sys.argv[2]).read().strip(), "tags": ["pkg:kalam"]}))' "$1" "$2"; }
fi

echo "==> deleting existing pkg:kalam objects"
for kind in channels workflows connectors plugins; do
  case "$kind" in
    channels)   key=channel_id ;;
    workflows)  key=workflow_id ;;
    connectors) key=id ;;
    plugins)    key=plugin_id ;;
  esac
  for id in $(req "$ADMIN/$kind?tag=pkg:kalam&limit=500" | ids "$key"); do
    curl_admin -X DELETE "$ADMIN/$kind/$id" -o /dev/null || true
    echo "    $kind/$id"
  done
done

echo "==> connectors"
for f in connectors/*.json; do
  id=$(field "$f" id)
  case "$id" in
    kalam-db|model-loader)
      if [ "$ALLOW_PRIVATE" = "1" ]; then
        with_private_urls "$f" | req -X POST "$ADMIN/connectors" -H 'Content-Type: application/json' --data @- > /dev/null
      else
        req -X POST "$ADMIN/connectors" -H 'Content-Type: application/json' --data @"$f" > /dev/null
      fi ;;
    *)
      req -X POST "$ADMIN/connectors" -H 'Content-Type: application/json' --data @"$f" > /dev/null ;;
  esac
  echo "    $id"
done

# The engine cartridge is a plugin here, as tb.rating and tb.pairing are in Soma. Plugins before
# workflows: a workflow naming a function no plugin provides is quarantined at load.
echo "==> plugins"
for f in plugins/*/plugin.json; do
  [ -e "$f" ] || continue
  dir=$(dirname "$f")
  id=$(field "$f" name)
  component=$(field "$f" component)
  b64file=$(mktemp)
  base64 < "$dir/$component" | tr -d '\n' > "$b64file"
  plugin_body "$f" "$b64file" \
    | req -X POST "$ADMIN/plugins" -H 'Content-Type: application/json' --data @- > /dev/null
  rm -f "$b64file"
  req -X PATCH "$ADMIN/plugins/$id/status" -H 'Content-Type: application/json' \
      -d '{"status":"active"}' > /dev/null
  echo "    $id"
done

echo "==> workflows"
for f in workflows/*.json; do
  [ -e "$f" ] || continue
  id=$(field "$f" workflow_id)
  req -X POST "$ADMIN/workflows" -H 'Content-Type: application/json' --data @"$f" > /dev/null
  req -X PATCH "$ADMIN/workflows/$id/status" -H 'Content-Type: application/json' -d '{"status":"active"}' > /dev/null
  echo "    $id"
done

echo "==> channels"
for f in channels/*.json; do
  [ -e "$f" ] || continue
  id=$(field "$f" channel_id)
  req -X POST "$ADMIN/channels" -H 'Content-Type: application/json' --data @"$f" > /dev/null
  req -X PATCH "$ADMIN/channels/$id/status" -H 'Content-Type: application/json' -d '{"status":"active"}' > /dev/null
  echo "    $id"
done

echo "==> health"
curl -sS "${ADMIN%/api/v1/admin}/health" | tr ',' '\n' | grep -E 'quarantined|failed_to_load' || true
