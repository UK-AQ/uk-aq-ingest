#!/usr/bin/env bash
set -euo pipefail

# Move observations older than a cutoff to the observs DB in batches.
#
# Defaults:
#   CUTOFF_DAYS=14
#   BATCH_SIZE=50000
#
# Environment:
#   SUPABASE_DB_URL
#   OBS_AQIDB_SUPABASE_DB_URL
# Optional:
#   CUTOFF_DAYS
#   BATCH_SIZE
#
# Example:
#   CUTOFF_DAYS=21 BATCH_SIZE=20000 ./scripts/uk_aq_move_observations.sh

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

CUTOFF_DAYS=${CUTOFF_DAYS:-14}
BATCH_SIZE=${BATCH_SIZE:-50000}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --days)
      CUTOFF_DAYS="$2"
      shift 2
      ;;
    --batch-size)
      BATCH_SIZE="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ -z "${SUPABASE_DB_URL:-}" ]; then
  echo "SUPABASE_DB_URL is missing." >&2
  exit 1
fi
if [ -z "${OBS_AQIDB_SUPABASE_DB_URL:-}" ]; then
  echo "OBS_AQIDB_SUPABASE_DB_URL is missing." >&2
  exit 1
fi

TMP_DIR=$(mktemp -d)
trap "rm -rf \"$TMP_DIR\"" EXIT

TOTAL_SELECTED=0
TOTAL_INSERTED=0
TOTAL_DELETED=0
BATCH=0

while true; do
  BATCH=$((BATCH + 1))
  CSV_HIST="$TMP_DIR/observs_${BATCH}.csv"
  CSV_KEYS="$TMP_DIR/keys_${BATCH}.csv"
  rm -f "$CSV_HIST" "$CSV_KEYS"

  echo "Batch $BATCH: selecting up to $BATCH_SIZE rows older than ${CUTOFF_DAYS} days"

  psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 <<SQL
DROP TABLE IF EXISTS tmp_obs;
CREATE TEMP TABLE tmp_obs AS
SELECT
  o.connector_id,
  o.timeseries_id,
  o.observed_at,
  o.value,
  o.status
FROM observations o
WHERE o.observed_at < (now() - interval '${CUTOFF_DAYS} days')
ORDER BY o.observed_at, o.connector_id, o.timeseries_id
LIMIT ${BATCH_SIZE};

\copy (SELECT connector_id, timeseries_id, observed_at, value, status FROM tmp_obs) TO '${CSV_HIST}' WITH (FORMAT csv);
\copy (SELECT connector_id, timeseries_id, observed_at FROM tmp_obs) TO '${CSV_KEYS}' WITH (FORMAT csv);
SQL

  if [ ! -s "$CSV_KEYS" ]; then
    echo "Batch $BATCH: no rows found. Done."
    break
  fi

  SELECTED=$(wc -l < "$CSV_KEYS" | tr -d ' ')
  TOTAL_SELECTED=$((TOTAL_SELECTED + SELECTED))

  INSERTED=$(psql "$OBS_AQIDB_SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -q -t -A <<SQL
DROP TABLE IF EXISTS tmp_hist;
CREATE TEMP TABLE tmp_hist (
  connector_id integer,
  timeseries_id integer,
  observed_at timestamptz,
  value double precision,
  status text
);
\copy tmp_hist FROM '${CSV_HIST}' WITH (FORMAT csv);
WITH ins AS (
  INSERT INTO uk_aq_observs.observations (
    connector_id,
    timeseries_id,
    observed_at,
    value,
    status
  )
  SELECT connector_id, timeseries_id, observed_at, value, status
  FROM tmp_hist
  ON CONFLICT DO NOTHING
  RETURNING 1
)
SELECT count(*) FROM ins;
SQL
  )

  INSERTED=$(printf '%s\n' "$INSERTED" | tail -n 1 | tr -d '[:space:]')
  INSERTED=${INSERTED:-0}
  TOTAL_INSERTED=$((TOTAL_INSERTED + INSERTED))

  DELETED=$(psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -q -t -A <<SQL
DROP TABLE IF EXISTS tmp_del;
CREATE TEMP TABLE tmp_del (
  connector_id integer,
  timeseries_id integer,
  observed_at timestamptz
);
\copy tmp_del FROM '${CSV_KEYS}' WITH (FORMAT csv);
WITH del AS (
  DELETE FROM observations o
  USING tmp_del d
  WHERE o.connector_id = d.connector_id
    AND o.timeseries_id = d.timeseries_id
    AND o.observed_at = d.observed_at
  RETURNING 1
)
SELECT count(*) FROM del;
SQL
  )

  DELETED=$(printf '%s\n' "$DELETED" | tail -n 1 | tr -d '[:space:]')
  DELETED=${DELETED:-0}
  TOTAL_DELETED=$((TOTAL_DELETED + DELETED))

  echo "Batch $BATCH: selected=${SELECTED} inserted=${INSERTED} deleted=${DELETED}"

  if [ "$SELECTED" -lt "$BATCH_SIZE" ]; then
    echo "Batch $BATCH: final batch (fewer than batch size)."
    break
  fi
done

echo "Done. Total selected=${TOTAL_SELECTED} inserted=${TOTAL_INSERTED} deleted=${TOTAL_DELETED}"
