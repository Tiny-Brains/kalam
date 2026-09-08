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
#   MODEL_LOADER_URL           the loader's address, substituted into the model-loader connector
#
# WHY THE LOADER'S URL IS SUBSTITUTED HERE and not written in the connector: Orion validates an
# http connector's `url` as a URL, and refuses an `env://` reference with
#   VALIDATION_ERROR: Connector URL must use http or https scheme, got 'env'
# A storage connector's `endpoint` takes env:// happily, which is why kalam-blobs can and this
# cannot. So the committed file carries the loopback default -- which is the deployed topology,
# one loader beside every replica -- and a deployment that puts the loader elsewhere says so
# through the environment, exactly as it does for the database.
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
# --fail-with-body prints the server's explanation on stdout, and every call site here redirects
# stdout to /dev/null -- so a 400 used to surface as a bare `curl: (22) ... error: 400` with the
# reason discarded. That cost an hour on a VALIDATION_ERROR that says exactly what is wrong. Keep
# the body, on stderr, where the redirect cannot reach it.
req() {
  _out=$(mktemp)
  if curl_admin --fail-with-body -o "$_out" "$@"; then
    cat "$_out"; rm -f "$_out"
  else
    _rc=$?
    echo "    !! admin API refused:" >&2; cat "$_out" >&2; echo >&2
    rm -f "$_out"; return $_rc
  fi
}

# jq where available, python3 otherwise -- the host has python3, the image has jq.
if command -v jq > /dev/null 2>&1; then
  field() { jq -r ".$2" "$1"; }
  ids() { jq -r ".data[].$1"; }
  with_private_urls() { jq '.config.allow_private_urls = true' "$1"; }
  with_url() { jq --arg u "$SUB_URL" \
      '.config.url = $u | if $private == "1" then .config.allow_private_urls = true else . end' \
      --arg private "$ALLOW_PRIVATE" "$1"; }
  plugin_body() { jq -n --slurpfile m "$1" --rawfile c "$2" --arg s "$3" \
      '{plugin_id: $m[0].name, manifest: $m[0], component: ($c | rtrimstr("\n")), tags: ["pkg:kalam"]}
       + (if $s == "" then {} else {signature: $s} end)'; }
else
  field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }
  ids() { python3 -c 'import json,sys; [print(o[sys.argv[1]]) for o in json.load(sys.stdin)["data"]]' "$1"; }
  with_private_urls() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["config"]["allow_private_urls"]=True; print(json.dumps(d))' "$1"; }
  with_url() { python3 -c 'import json,os,sys; d=json.load(open(sys.argv[1])); d["config"]["url"]=os.environ["SUB_URL"]; d["config"]["allow_private_urls"]=(os.environ.get("KALAM_ALLOW_PRIVATE_URLS")=="1"); print(json.dumps(d))' "$1"; }
  plugin_body() { python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); b={"plugin_id": m["name"], "manifest": m, "component": open(sys.argv[2]).read().strip(), "tags": ["pkg:kalam"]}; s=sys.argv[3].strip(); s and b.update({"signature": s}); print(json.dumps(b))' "$1" "$2" "$3"; }
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
    # A plugin is ARCHIVED, not deleted, and it cannot be archived while an active workflow calls
    # its functions -- which is why workflows are swept first. Without the archive the DELETE is a
    # silent no-op and the re-create is a 409 that only shows up on the SECOND load of a package.
    # (The first load of a fresh server works either way, so this is a bug that hides until the
    # first redeploy.)
    if [ "$kind" = plugins ]; then
      curl_admin -X PATCH "$ADMIN/plugins/$id/status" -H 'Content-Type: application/json' \
          -d '{"status":"archived"}' -o /dev/null || true
    fi
    curl_admin -X DELETE "$ADMIN/$kind/$id" -o /dev/null || true
    echo "    $kind/$id"
  done
done

echo "==> connectors"
for f in connectors/*.json; do
  id=$(field "$f" id)
  case "$id" in
    model-loader)
      SUB_URL="${MODEL_LOADER_URL:-http://127.0.0.1:9090}" \
      KALAM_ALLOW_PRIVATE_URLS="$ALLOW_PRIVATE" \
        with_url "$f" | req -X POST "$ADMIN/connectors" -H 'Content-Type: application/json' --data @- > /dev/null ;;
    # The replay PUT goes through an ordinary http connector, because Orion carries no bytes:
    # `storage_presign` and `storage_head` are its only storage task functions. Its base URL must
    # be EXACTLY the storage endpoint, because the workflow reduces the presigned URL to a path by
    # subtracting `[vars] blob_endpoint` from it -- so one value feeds both, from the environment.
    kalam-blobs-put)
      # No apostrophe in that message: inside ${VAR:?word} the shell still applies quote removal
      # to `word`, so a lone ' opens a quote that never closes and the whole file fails to parse.
      SUB_URL="${R2_ENDPOINT:?R2_ENDPOINT is required: the replay store endpoint}" \
      KALAM_ALLOW_PRIVATE_URLS="$ALLOW_PRIVATE" \
        with_url "$f" | req -X POST "$ADMIN/connectors" -H 'Content-Type: application/json' --data @- > /dev/null ;;
    kalam-db)
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
  # The detached signature, if this machine made one. Orion verifies it over the DIGEST STRING --
  # `sha256:<64 hex>`, the ASCII, not the bytes -- when `[plugins.trust] public_keys` is non-empty,
  # both here at the upload and again on every node that loads the version. No `.sig` file means no
  # field, which a node with keys refuses by name and a node without keys accepts.
  sigfile="$dir/$component.sig"
  sig=""
  [ -r "$sigfile" ] && sig=$(tr -d '\n' < "$sigfile")
  plugin_body "$f" "$b64file" "$sig" \
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
# Through curl_admin: /health's detail -- the plugin list, the quarantined channels -- is gated on
# `show_detail = !admin_auth.enabled || a valid admin key`. Unauthenticated it still answers 200 and
# simply omits them, so this check would quietly report nothing wrong on a node where something is.
curl_admin "${ADMIN%/api/v1/admin}/health" | tr ',' '\n' | grep -E 'quarantined|failed_to_load' || true
