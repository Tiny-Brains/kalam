#!/usr/bin/env bash
# PREPARE every statement the package actually ships, against a schema built from Soma's migrations.
#
#   ./scripts/check-sql.sh            # needs the db container up
#
# Kalam's statements live twice: readably in scripts/gen-kalam.py, and inlined as single-line JSON
# strings in the workflow it generates. This checks the SHIPPED copy -- it pulls each `query` out of
# workflows/*.json and asks Postgres to parse and plan it, so a statement hand-edited in the JSON,
# or a generator run that was never committed, fails here rather than on a cron tick. What each
# statement DOES is soma/scripts/verify/run.sh's walk, and what the WAVE does is a real wave.
#
# Kalam ships no migrations: the schema is Soma's and Kalam holds a narrow role on it. MIGRATIONS is
# relative because the repos sit beside each other; override it when they do not.
set -euo pipefail
cd "$(dirname "$0")/.."

DB_CONTAINER="${DB_CONTAINER:-tinybrains-db-1}"
DB_USER="${DB_USER:-$(docker exec "$DB_CONTAINER" printenv POSTGRES_USER)}"
MIGRATIONS="${MIGRATIONS:-../soma/migrations}"
SCRATCH=kalam_sqlcheck

psql() { docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" "$@"; }
drop() { psql -d postgres -q -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $SCRATCH" 2>/dev/null; }

trap drop EXIT
drop
psql -d postgres -q -v ON_ERROR_STOP=1 -c "CREATE DATABASE $SCRATCH"
cat "$MIGRATIONS"/0001_init.sql "$MIGRATIONS"/0002_sessions.sql \
    | psql -d "$SCRATCH" -q -v ON_ERROR_STOP=1

# Parameter types are left to Postgres. Every statement writes its placeholders as ($1)::type, so
# inference has everything it needs -- and one that stopped doing that would be ambiguous to the
# server too, which is worth failing on.
echo "==> preparing every query in workflows/*.json"
python3 - workflows/*.json <<'PY' | psql -d "$SCRATCH" -q -v ON_ERROR_STOP=1
import json, sys

for path in sys.argv[1:]:
    doc = json.load(open(path))
    for task in doc.get("tasks", []):
        query = task.get("function", {}).get("input", {}).get("query")
        if not query:
            continue
        name = f"chk_{doc['workflow_id']}_{task['id']}".replace("-", "_")
        print(rf"\echo '  {doc['workflow_id']} / {task['id']}'")
        print(f"PREPARE {name} AS {query};")
PY

# The other half of "this statement will work": a role can PREPARE a statement it would be refused
# at execution time, so check that the columns the wave writes are the columns it is granted.
echo "==> the kalam role's grants cover what the wave writes"
psql -d "$SCRATCH" -q -v ON_ERROR_STOP=1 <<'SQL'
DO $$
DECLARE missing text;
BEGIN
    SELECT string_agg(c, ', ') INTO missing FROM unnest(ARRAY[
        'status','claim_token','lease_expires_at','lapses','refusals','reason','turns',
        'played_ms','engine_digest_played','evaluator_digest','replay_key','played_at',
        'fault_reason','fault_seat','closed_at']) AS c
     WHERE NOT has_column_privilege('kalam', 'matches', c, 'UPDATE');
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'the kalam role cannot UPDATE matches.%', missing;
    END IF;
    SELECT string_agg(c, ', ') INTO missing FROM unnest(ARRAY['rank','score','strikes']) AS c
     WHERE NOT has_column_privilege('kalam', 'match_seats', c, 'UPDATE');
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'the kalam role cannot UPDATE match_seats.%', missing;
    END IF;
    IF has_column_privilege('kalam', 'matches', 'rated_at', 'UPDATE') THEN
        RAISE EXCEPTION 'the kalam role can write matches.rated_at -- counting is Jodi''s';
    END IF;
END $$;
SQL

echo "==> all shipped SQL parses and plans, and the role can write what it writes"
