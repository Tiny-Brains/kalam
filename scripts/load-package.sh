#!/usr/bin/env sh
# Load (or reload) the Kalam package into a running orion-server.
#
# The same shape as soma/ and jodi/scripts/load-package.sh -- the three are deliberately alike, and a
# difference between them should mean something. It is idempotent: everything tagged pkg:kalam is
# deleted before the package is re-created, because an *active* workflow is immutable in Orion and a
# second POST would be a conflict. The sweep is by tag, so it touches nothing of Soma's or Jodi's.
#
# Environment:
#   ORION_ADMIN                admin API base (default http://127.0.0.1:8080/api/v1/admin)
#   ORION_ADMIN_API_KEY        sent as a bearer token when admin_auth is enabled
#   KALAM_ALLOW_PRIVATE_URLS   1 to set allow_private_urls on the database and loader connectors
#   MODEL_LOADER_URL           the loader's address, substituted into the model-loader connector
#   R2_ENDPOINT                the replay store, substituted into the kalam-blobs-put connector
#
# THE TWO HTTP URLS ARE SUBSTITUTED HERE rather than written in the connectors, because Orion
# validates an http connector's `url` and refuses an env:// reference outright ("Connector URL must
# use http or https scheme, got 'env'"). A storage connector's `endpoint` takes env:// happily,
# which is why kalam-blobs can and these cannot. allow_private_urls stays a flag for a different
# reason: the loader is loopback by design, but the package should not ship with the SSRF guard off.
set -eu

ADMIN="${ORION_ADMIN:-http://127.0.0.1:8080/api/v1/admin}"
export ALLOW_PRIVATE="${KALAM_ALLOW_PRIVATE_URLS:-0}"
JSON='Content-Type: application/json'

cd "$(dirname "$0")/.."

# No apostrophe in that message: inside ${VAR:?word} the shell still applies quote removal to
# `word`, so a lone ' opens a quote that never closes and the whole file fails to parse.
: "${R2_ENDPOINT:?is required -- the replay store endpoint, which is also kalam-blobs-put base URL}"

AUTH=""
[ -n "${ORION_ADMIN_API_KEY:-}" ] && AUTH="Authorization: Bearer ${ORION_ADMIN_API_KEY}"

curl_admin() {
  if [ -n "$AUTH" ]; then curl -sS -H "$AUTH" "$@"; else curl -sS "$@"; fi
}

# --fail-with-body prints the server's explanation on stdout, and every call site here redirects
# stdout to /dev/null -- so a 400 would otherwise surface as a bare `curl: (22) ... error: 400` with
# the reason discarded. Keep the body, on stderr, where the redirect cannot reach it.
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

post()   { req -X POST "$ADMIN/$1" -H "$JSON" --data @- > /dev/null; }
status() { req -X PATCH "$ADMIN/$1/$2/status" -H "$JSON" -d "{\"status\":\"$3\"}" > /dev/null; }

# jq where available, python3 otherwise -- the host has python3, the loader image has jq.
if command -v jq > /dev/null 2>&1; then
  field() { jq -r ".$2" "$1"; }
  ids() { jq -r ".data[].$1"; }
  with_private_urls() { jq '.config.allow_private_urls = true' "$1"; }
  with_url() { jq --arg u "$SUB_URL" --arg private "$ALLOW_PRIVATE" \
      '.config.url = $u | if $private == "1" then .config.allow_private_urls = true else . end' "$1"; }
  plugin_body() { jq -n --slurpfile m "$1" --rawfile c "$2" --arg s "$3" \
      '{plugin_id: $m[0].name, manifest: $m[0], component: ($c | rtrimstr("\n")), tags: ["pkg:kalam"]}
       + (if $s == "" then {} else {signature: $s} end)'; }
else
  field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }
  ids() { python3 -c 'import json,sys; [print(o[sys.argv[1]]) for o in json.load(sys.stdin)["data"]]' "$1"; }
  with_private_urls() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["config"]["allow_private_urls"]=True; print(json.dumps(d))' "$1"; }
  with_url() { python3 -c 'import json,os,sys; d=json.load(open(sys.argv[1])); d["config"]["url"]=os.environ["SUB_URL"]; d["config"]["allow_private_urls"]=(os.environ["ALLOW_PRIVATE"]=="1"); print(json.dumps(d))' "$1"; }
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
    # silent no-op and the re-create is a 409 that only appears on the SECOND load of a package.
    [ "$kind" != plugins ] || curl_admin -X PATCH "$ADMIN/plugins/$id/status" -H "$JSON" \
        -d '{"status":"archived"}' -o /dev/null || true
    curl_admin -X DELETE "$ADMIN/$kind/$id" -o /dev/null || true
    echo "    $kind/$id"
  done
done

# The replay PUT goes through an ordinary http connector, because Orion carries no bytes:
# `storage_presign` and `storage_head` are its only storage task functions. Its base URL must be
# EXACTLY the storage endpoint, because the workflow reduces the presigned URL to a path by
# subtracting `[vars] blob_endpoint` from it -- so one value feeds both, from the environment.
echo "==> connectors"
for f in connectors/*.json; do
  id=$(field "$f" id)
  case "$id" in
    model-loader)    SUB_URL="${MODEL_LOADER_URL:-http://127.0.0.1:9090}" with_url "$f" ;;
    kalam-blobs-put) SUB_URL="$R2_ENDPOINT" with_url "$f" ;;
    kalam-db)        if [ "$ALLOW_PRIVATE" = 1 ]; then with_private_urls "$f"; else cat "$f"; fi ;;
    *)               cat "$f" ;;
  esac | post connectors
  echo "    $id"
done

# The engine cartridge is a plugin here, as tb.rating and tb.pairing are in Jodi. Plugins before
# workflows: a workflow naming a function no plugin provides is quarantined at load.
echo "==> plugins"
for f in plugins/*/plugin.json; do
  [ -e "$f" ] || continue
  dir=$(dirname "$f")
  id=$(field "$f" name)
  component=$(field "$f" component)
  # The component reaches the helper as a FILE: base64 of a 100 KiB component is ~133 KiB of text,
  # and passing that as an argv entry is "Argument list too long" on any shell.
  b64file=$(mktemp)
  base64 < "$dir/$component" | tr -d '\n' > "$b64file"
  # The detached signature, if this machine made one. Orion verifies it over the DIGEST STRING --
  # `sha256:<64 hex>`, the ASCII, not the bytes -- when `[plugins.trust] public_keys` is non-empty.
  # No `.sig` file means no field, which a node with keys refuses by name and a node without accepts.
  sig=""
  # PLUGIN_SIG_DIR: a signature belongs to whoever holds the trust key, not to the package, so a
  # package that ships as an immutable image cannot carry one. Unset, it falls back to beside the
  # component, which is where a host-side run of devops' sign-plugins.sh still puts it.
  sigfile="${PLUGIN_SIG_DIR:-$dir}/$component.sig"
  [ -r "$sigfile" ] && sig=$(tr -d '\n' < "$sigfile")
  plugin_body "$f" "$b64file" "$sig" | post plugins
  rm -f "$b64file"
  status plugins "$id" active
  echo "    $id"
done

publish() {
  echo "==> $1"
  for f in "$1"/*.json; do
    [ -e "$f" ] || continue
    id=$(field "$f" "$2")
    post "$1" < "$f"
    status "$1" "$id" active
    echo "    $id"
  done
}
publish workflows workflow_id
publish channels channel_id

echo "==> health"
# Through curl_admin: /health's detail -- the plugin list, the quarantined channels -- is gated on
# `show_detail = !admin_auth.enabled || a valid admin key`. Unauthenticated it still answers 200 and
# omits them, so this check would quietly report nothing wrong on a node where something is.
curl_admin "${ADMIN%/api/v1/admin}/health" | tr ',' '\n' | grep -E 'quarantined|failed_to_load' || true
