#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/uk_aq_int4_migration_all_clear.sh [options]

Options:
  --env-file PATH         Env file to source first (default: .env in ingest repo)
  --main-db-url URL       MAIN DB Postgres URL (overrides SUPABASE_DB_URL)
  --obs-aqidb-db-url URL  OBS_AQIDB Postgres URL (overrides OBS_AQIDB_SUPABASE_DB_URL)
  --main-only             Run MAIN DB checks only
  --obs-aqidb-only        Run OBS_AQIDB checks only
  -h, --help              Show this help

Environment fallback:
  MAIN DB:    SUPABASE_DB_URL
  OBS_AQIDB: OBS_AQIDB_SUPABASE_DB_URL
EOF
}

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INGEST_REPO="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

ENV_FILE="${INGEST_REPO}/.env"
MAIN_DB_URL="${MAIN_DB_URL:-${SUPABASE_DB_URL:-}}"
OBS_AQIDB_DB_URL="${OBS_AQIDB_DB_URL:-${OBS_AQIDB_SUPABASE_DB_URL:-}}"
RUN_MAIN=1
RUN_OBS_AQIDB=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --main-db-url)
      MAIN_DB_URL="${2:-}"
      shift 2
      ;;
    --obs-aqidb-db-url)
      OBS_AQIDB_DB_URL="${2:-}"
      shift 2
      ;;
    --main-only)
      RUN_MAIN=1
      RUN_OBS_AQIDB=0
      shift
      ;;
    --obs-aqidb-only)
      RUN_MAIN=0
      RUN_OBS_AQIDB=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -n "${ENV_FILE}" && -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
  set +a
  # Re-resolve in case env file populated them.
  [[ -z "${MAIN_DB_URL}" ]] && MAIN_DB_URL="${SUPABASE_DB_URL:-}"
  [[ -z "${OBS_AQIDB_DB_URL}" ]] && OBS_AQIDB_DB_URL="${OBS_AQIDB_SUPABASE_DB_URL:-}"
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "psql is required but not found in PATH." >&2
  exit 1
fi

if [[ "${RUN_MAIN}" -eq 1 && -z "${MAIN_DB_URL}" ]]; then
  echo "MAIN DB URL missing. Set SUPABASE_DB_URL or pass --main-db-url." >&2
  exit 1
fi

if [[ "${RUN_OBS_AQIDB}" -eq 1 && -z "${OBS_AQIDB_DB_URL}" ]]; then
  echo "OBS_AQIDB URL missing. Set OBS_AQIDB_SUPABASE_DB_URL or pass --obs-aqidb-db-url." >&2
  exit 1
fi

if [[ -z "${PGOPTIONS:-}" ]]; then
  export PGOPTIONS='-c statement_timeout=0 -c lock_timeout=0 -c idle_in_transaction_session_timeout=0'
fi

run_main_checks() {
  echo "=== MAIN DB all-clear ==="
  psql "${MAIN_DB_URL}" -v ON_ERROR_STOP=1 <<'SQL'
\pset pager off

-- 1) Column types must be integer
with target_cols as (
  select table_schema, table_name, column_name, data_type
  from information_schema.columns
  where
    (table_schema = 'uk_aq_core' and table_name in ('connectors','timeseries') and column_name = 'id')
    or (table_schema in ('uk_aq_core','uk_aq_raw','uk_aq_observs') and column_name in ('connector_id','timeseries_id'))
)
select * from target_cols
where data_type <> 'integer'
order by table_schema, table_name, column_name;

do $$
declare v_bad int;
begin
  select count(*) into v_bad
  from information_schema.columns
  where
    (
      table_schema = 'uk_aq_core' and table_name in ('connectors','timeseries') and column_name = 'id'
      and data_type <> 'integer'
    )
    or (
      table_schema in ('uk_aq_core','uk_aq_raw','uk_aq_observs')
      and column_name in ('connector_id','timeseries_id')
      and data_type <> 'integer'
    );
  if v_bad > 0 then
    raise exception 'FAIL: non-integer connector/timeseries id columns remain (% rows)', v_bad;
  end if;
end $$;

-- 2) FK child/parent type parity
with fk_pairs as (
  select
    con.conname,
    format_type(a1.atttypid, a1.atttypmod) as child_type,
    format_type(a2.atttypid, a2.atttypmod) as parent_type
  from pg_constraint con
  join pg_class c1 on c1.oid = con.conrelid
  join pg_namespace n1 on n1.oid = c1.relnamespace
  join pg_class c2 on c2.oid = con.confrelid
  join pg_namespace n2 on n2.oid = c2.relnamespace
  join unnest(con.conkey) with ordinality as ck(attnum, ord) on true
  join unnest(con.confkey) with ordinality as fk(attnum, ord) on fk.ord = ck.ord
  join pg_attribute a1 on a1.attrelid = c1.oid and a1.attnum = ck.attnum
  join pg_attribute a2 on a2.attrelid = c2.oid and a2.attnum = fk.attnum
  where con.contype = 'f'
    and (
      a1.attname in ('connector_id','timeseries_id')
      or (n2.nspname='uk_aq_core' and c2.relname in ('connectors','timeseries') and a2.attname='id')
    )
)
select * from fk_pairs where child_type <> parent_type;

do $$
declare v_bad int;
begin
  with fk_pairs as (
    select
      format_type(a1.atttypid, a1.atttypmod) as child_type,
      format_type(a2.atttypid, a2.atttypmod) as parent_type
    from pg_constraint con
    join pg_class c1 on c1.oid = con.conrelid
    join pg_namespace n1 on n1.oid = c1.relnamespace
    join pg_class c2 on c2.oid = con.confrelid
    join pg_namespace n2 on n2.oid = c2.relnamespace
    join unnest(con.conkey) with ordinality as ck(attnum, ord) on true
    join unnest(con.confkey) with ordinality as fk(attnum, ord) on fk.ord = ck.ord
    join pg_attribute a1 on a1.attrelid = c1.oid and a1.attnum = ck.attnum
    join pg_attribute a2 on a2.attrelid = c2.oid and a2.attnum = fk.attnum
    where con.contype = 'f'
      and (
        a1.attname in ('connector_id','timeseries_id')
        or (n2.nspname='uk_aq_core' and c2.relname in ('connectors','timeseries') and a2.attname='id')
      )
  )
  select count(*) into v_bad from fk_pairs where child_type <> parent_type;

  if v_bad > 0 then
    raise exception 'FAIL: FK type mismatches remain (% rows)', v_bad;
  end if;
end $$;

-- 3) Key RPC signatures should not contain bigint for these args
with sigs as (
  select n.nspname, p.proname, pg_get_function_identity_arguments(p.oid) as args
  from pg_proc p
  join pg_namespace n on n.oid = p.pronamespace
  where n.nspname = 'uk_aq_public'
    and p.proname in ('uk_aq_latest_rpc','uk_aq_timeseries_rpc','uk_aq_stations_rpc','uk_aq_surbiton_latest_rpc')
)
select * from sigs where lower(args) like '%bigint%';

do $$
declare v_bad int;
begin
  with sigs as (
    select pg_get_function_identity_arguments(p.oid) as args
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'uk_aq_public'
      and p.proname in ('uk_aq_latest_rpc','uk_aq_timeseries_rpc','uk_aq_stations_rpc','uk_aq_surbiton_latest_rpc')
  )
  select count(*) into v_bad from sigs where lower(args) like '%bigint%';

  if v_bad > 0 then
    raise exception 'FAIL: bigint still present in key RPC argument signatures';
  end if;
end $$;

-- 4) Smoke calls
select count(*) as latest_rows
from uk_aq_public.uk_aq_latest_rpc(null,null,null,null,null,1,null,null,null,null);

select count(*) as stations_rows
from uk_aq_public.uk_aq_stations_rpc(null,null,null,1,null);

select count(*) as surbiton_rows
from uk_aq_public.uk_aq_surbiton_latest_rpc(null,null,null,null,1);

with any_ts as (
  select min(id)::integer as id
  from uk_aq_core.timeseries
)
select case
  when (select id from any_ts) is null then 0
  else (
    select count(*)
    from uk_aq_public.uk_aq_timeseries_rpc(
      (select id from any_ts),
      '24h',
      1,
      null,
      false
    )
  )
end as ts_rows;

\echo 'MAIN DB ALL-CLEAR'
SQL
}

run_obs_aqidb_checks() {
  echo "=== OBS_AQIDB all-clear ==="
  psql "${OBS_AQIDB_DB_URL}" -v ON_ERROR_STOP=1 <<'SQL'
\pset pager off

select table_schema, table_name, column_name, data_type
from information_schema.columns
where table_schema = 'uk_aq_observs'
  and table_name = 'observations'
  and column_name in ('connector_id','timeseries_id');

do $$
declare v_bad int;
begin
  select count(*) into v_bad
  from information_schema.columns
  where table_schema = 'uk_aq_observs'
    and table_name = 'observations'
    and column_name in ('connector_id','timeseries_id')
    and data_type <> 'integer';

  if v_bad > 0 then
    raise exception 'FAIL: uk_aq_observs.observations still has non-integer connector/timeseries ids';
  end if;
end $$;

select 1 from uk_aq_observs.observations limit 1;

\echo 'OBS_AQIDB ALL-CLEAR'
SQL
}

if [[ "${RUN_MAIN}" -eq 1 ]]; then
  run_main_checks
fi

if [[ "${RUN_OBS_AQIDB}" -eq 1 ]]; then
  run_obs_aqidb_checks
fi

echo "All requested checks completed."
