#!/usr/bin/env bash
# PREPARE every statement this package ships, against a schema built from Soma's migrations, AS THE
# `kalam` ROLE -- and assert what that role must NOT be able to do.
#
#   ./scripts/check-sql.sh
#
# Two halves, and only the first is orion-server's:
#
#   sql check     every `db_read`/`db_write` in the set, prepared and planned on a scratch schema as
#                 the role `kalam-db` connects as. On PostgreSQL 16+ the plan (EXPLAIN GENERIC_PLAN)
#                 proves the role's table and column grants, so a statement naming a column `kalam`
#                 has no grant on fails HERE rather than on a cron tick.
#   the negatives the grants this role must NOT have. No schema check can express "the runner may not
#                 write `matches.rated_at`" or "may not read `ratings`" -- those are the OWNERSHIP
#                 BOUNDARY with Soma (counting is Soma's, and a runner must not see a competitive
#                 decision), and a widened grant would make every positive check pass harder.
#
# Kalam ships no migrations: the schema is Soma's and Kalam holds a narrow role on it. MIGRATIONS is
# relative because the repos sit beside each other; override it when they do not.
#
# IT NEEDS NO STACK, only a PostgreSQL 16+ server, and starts a throwaway one when SQLCHECK_DATABASE
# does not name one.
#
#   MIGRATIONS          Soma's migrations (default ../soma/migrations)
#   SQLCHECK_DATABASE   a PostgreSQL 16+ superuser URL. Unset starts and removes a container.
set -euo pipefail
cd "$(dirname "$0")/.."

MIGRATIONS="${MIGRATIONS:-../soma/migrations}"
[ -d "$MIGRATIONS" ] || { echo "no migrations at $MIGRATIONS -- set MIGRATIONS" >&2; exit 1; }

DATABASE="${SQLCHECK_DATABASE:-}"
CONTAINER=""
SCHEMA=$(mktemp -d)
cleanup() {
  rm -rf "$SCHEMA"
  [ -n "$CONTAINER" ] && docker rm -f "$CONTAINER" > /dev/null 2>&1
  return 0
}
trap cleanup EXIT

if [ -z "$DATABASE" ]; then
  command -v docker > /dev/null || {
    echo "set SQLCHECK_DATABASE to a PostgreSQL 16+ URL, or install docker to start one" >&2
    exit 1
  }
  CONTAINER="kalam-sqlcheck-$$"
  PORT=$(( 16432 + (RANDOM % 1000) ))
  echo "==> starting a throwaway postgres on :$PORT"
  docker run -d --rm --name "$CONTAINER" -p "$PORT:5432" \
    -e POSTGRES_PASSWORD=sqlcheck -e POSTGRES_DB=sqlcheck postgres:16-alpine > /dev/null
  DATABASE="postgres://postgres:sqlcheck@127.0.0.1:$PORT/sqlcheck"
  for _ in $(seq 60); do
    docker exec "$CONTAINER" pg_isready -U postgres -d sqlcheck > /dev/null 2>&1 && break
    sleep 1
  done
fi

# The migrations wrap themselves in BEGIN/COMMIT so `soma bootstrap` applies each atomically; the
# scratch schema is built inside one transaction that is always rolled back, which a COMMIT would
# end. Soma's own check-sql.sh strips them the same way, and neither touches the shipped files.
for f in "$MIGRATIONS"/*.sql; do
  grep -vxE '\s*(BEGIN|COMMIT);\s*' "$f" > "$SCHEMA/$(basename "$f")"
done

echo "==> preparing every statement in the set, as the kalam role"
orion-server sql check . \
  --schema "$SCHEMA" \
  --database "$DATABASE" \
  --role kalam-db=kalam

# ---------------------------------------------------------------- the grants this role must NOT have
echo "==> the kalam role cannot reach what is Soma's"
psql "$DATABASE" -q -v ON_ERROR_STOP=1 > /dev/null <<SQL
BEGIN;
$(cat "$SCHEMA"/*.sql)
DO \$\$
BEGIN
    IF has_column_privilege('kalam', 'matches', 'rated_at', 'UPDATE') THEN
        RAISE EXCEPTION 'the kalam role can write matches.rated_at -- counting is Soma''s';
    END IF;
    IF has_table_privilege('kalam', 'ratings', 'SELECT') THEN
        RAISE EXCEPTION 'the kalam role can read ratings';
    END IF;
    IF EXISTS (SELECT 1 FROM unnest(ARRAY['status', 'reject_reason', 'admit_token']) c
                WHERE has_column_privilege('kalam', 'model_versions', c, 'UPDATE')) THEN
        RAISE EXCEPTION 'the kalam role can write a competitive decision on model_versions';
    END IF;
END
\$\$;
ROLLBACK;
SQL

echo "==> all shipped SQL parses and plans as kalam, and that role reaches nothing of Soma's"
