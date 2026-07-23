#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run the uk-aq ingest prune Cloud Run service with safe auth defaults.

Usage:
  scripts/uk_aq_run_ingestdb_prune.sh [options]

Options:
  --project <id>                        GCP project (default: GCP_PROJECT_ID or astute-lyceum-484111-k5)
  --region <region>                     Cloud Run region (default: GCP_REGION or europe-west2)
  --service <name>                      Cloud Run service name (default: uk-aq-ingestdb-prune-service)
  --dry-run                             Run in dry-run mode (default)
  --live                                Run in standard delete mode (dryRun=false)
  --start-date <YYYY-MM-DD>             Convenience mode: compute retentionDays from start date + maxHours
  --retention-days <n>                  INGESTDB_RETENTION_DAYS override for this call
  --max-hours <n>                       MAX_HOURS_PER_RUN override for this call
  --window-start <YYYY-MM-DD>           Convenience mode: compute retention/maxHours from date window start (UTC)
  --window-end <YYYY-MM-DD>             Convenience mode: compute retention/maxHours from date window end (UTC, inclusive)
  --auth-mode <proxy|impersonate|direct>
                                        proxy (default): use `gcloud run services proxy`
                                        impersonate: identity token via --impersonate-service-account
                                        direct: identity token from active gcloud account
  --impersonate-service-account <email> Service account for auth-mode=impersonate
  --proxy-port <port>                   Local port for auth-mode=proxy (default: 8080)
  --proxy-timeout-seconds <n>           Max wait for proxy readiness (default: 60)
  --extra-query <k=v&k2=v2>             Extra query args appended to /run request
  -h, --help                            Show this help

Examples:
  scripts/uk_aq_run_ingestdb_prune.sh --dry-run --start-date 2026-02-10 --max-hours 48
  scripts/uk_aq_run_ingestdb_prune.sh --dry-run --retention-days 9 --max-hours 48
  scripts/uk_aq_run_ingestdb_prune.sh --live --window-start 2026-02-10 --window-end 2026-02-12
  scripts/uk_aq_run_ingestdb_prune.sh --auth-mode impersonate \
    --impersonate-service-account uk-aq-ops-job@astute-lyceum-484111-k5.iam.gserviceaccount.com
EOF
}

require_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Missing required command: ${cmd}" >&2
    exit 1
  fi
}

is_positive_int() {
  [[ "$1" =~ ^[0-9]+$ ]] && [[ "$1" -gt 0 ]]
}

PROJECT_ID="${GCP_PROJECT_ID:-astute-lyceum-484111-k5}"
REGION="${GCP_REGION:-europe-west2}"
SERVICE_NAME="${INGESTDB_PRUNE_SERVICE_NAME:-uk-aq-ingestdb-prune-service}"
DRY_RUN="true"
RETENTION_DAYS=""
MAX_HOURS=""
START_DATE=""
WINDOW_START=""
WINDOW_END=""
AUTH_MODE="${INGESTDB_PRUNE_AUTH_MODE:-proxy}"
IMPERSONATE_SERVICE_ACCOUNT="${INGESTDB_PRUNE_IMPERSONATE_SERVICE_ACCOUNT:-}"
PROXY_PORT="${INGESTDB_PRUNE_PROXY_PORT:-8080}"
PROXY_TIMEOUT_SECONDS="${INGESTDB_PRUNE_PROXY_TIMEOUT_SECONDS:-60}"
EXTRA_QUERY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      PROJECT_ID="${2:-}"
      shift 2
      ;;
    --region)
      REGION="${2:-}"
      shift 2
      ;;
    --service)
      SERVICE_NAME="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    --live)
      DRY_RUN="false"
      shift
      ;;
    --retention-days)
      RETENTION_DAYS="${2:-}"
      shift 2
      ;;
    --start-date)
      START_DATE="${2:-}"
      shift 2
      ;;
    --max-hours)
      MAX_HOURS="${2:-}"
      shift 2
      ;;
    --window-start)
      WINDOW_START="${2:-}"
      shift 2
      ;;
    --window-end)
      WINDOW_END="${2:-}"
      shift 2
      ;;
    --auth-mode)
      AUTH_MODE="${2:-}"
      shift 2
      ;;
    --impersonate-service-account)
      IMPERSONATE_SERVICE_ACCOUNT="${2:-}"
      shift 2
      ;;
    --proxy-port)
      PROXY_PORT="${2:-}"
      shift 2
      ;;
    --proxy-timeout-seconds)
      PROXY_TIMEOUT_SECONDS="${2:-}"
      shift 2
      ;;
    --extra-query)
      EXTRA_QUERY="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

require_cmd gcloud
require_cmd curl
require_cmd python3

case "${AUTH_MODE}" in
  proxy|impersonate|direct)
    ;;
  *)
    echo "Invalid --auth-mode: ${AUTH_MODE}" >&2
    exit 1
    ;;
esac

if [[ -n "${START_DATE}" ]]; then
  if [[ -n "${WINDOW_START}" || -n "${WINDOW_END}" ]]; then
    echo "Do not combine --start-date with --window-start/--window-end." >&2
    exit 1
  fi
  if [[ -n "${RETENTION_DAYS}" ]]; then
    echo "Do not combine --start-date with --retention-days." >&2
    exit 1
  fi
  MAX_HOURS="${MAX_HOURS:-${MAX_HOURS_PER_RUN:-48}}"
  read -r RETENTION_DAYS < <(
    python3 - "${START_DATE}" "${MAX_HOURS}" <<'PY'
from datetime import date, datetime, timedelta, timezone
import sys

start_date = date.fromisoformat(sys.argv[1])
max_hours = int(sys.argv[2])
if max_hours <= 0:
    raise SystemExit("max_hours must be > 0")

start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
end_dt = start_dt + timedelta(hours=max_hours)

# Current service window_end is midnight-based via retentionDays.
if end_dt.time().isoformat() != "00:00:00":
    raise SystemExit(
        "start-date + max-hours must land on 00:00 UTC (use hours divisible by 24)"
    )

today = date.today()
retention_days = (today - end_dt.date()).days
if retention_days < 0:
    raise SystemExit("computed window end is in the future relative to UTC today")
print(retention_days)
PY
  )
fi

if [[ -n "${WINDOW_START}" || -n "${WINDOW_END}" ]]; then
  if [[ -z "${WINDOW_START}" || -z "${WINDOW_END}" ]]; then
    echo "--window-start and --window-end must be provided together." >&2
    exit 1
  fi
  if [[ -n "${RETENTION_DAYS}" || -n "${START_DATE}" ]]; then
    echo "Do not combine --window-start/--window-end with --retention-days/--start-date." >&2
    exit 1
  fi
  read -r RETENTION_DAYS MAX_HOURS < <(
    python3 - "${WINDOW_START}" "${WINDOW_END}" <<'PY'
from datetime import date, timedelta
import sys

start = date.fromisoformat(sys.argv[1])
end = date.fromisoformat(sys.argv[2])
today = date.today()

if end < start:
    raise SystemExit("window_end must be on or after window_start")
end_exclusive = end + timedelta(days=1)
retention_days = (today - end_exclusive).days
if retention_days < 0:
    raise SystemExit("window_end is in the future relative to UTC today")
max_hours = (end_exclusive - start).days * 24
if max_hours <= 0:
    raise SystemExit("computed max_hours must be > 0")
print(retention_days, max_hours)
PY
  )
fi

RETENTION_DAYS="${RETENTION_DAYS:-${INGESTDB_RETENTION_DAYS:-5}}"
MAX_HOURS="${MAX_HOURS:-${MAX_HOURS_PER_RUN:-48}}"

if ! is_positive_int "${RETENTION_DAYS}"; then
  echo "Invalid retention days: ${RETENTION_DAYS}" >&2
  exit 1
fi
if ! is_positive_int "${MAX_HOURS}"; then
  echo "Invalid max hours: ${MAX_HOURS}" >&2
  exit 1
fi
if ! is_positive_int "${PROXY_PORT}"; then
  echo "Invalid proxy port: ${PROXY_PORT}" >&2
  exit 1
fi
if ! is_positive_int "${PROXY_TIMEOUT_SECONDS}"; then
  echo "Invalid proxy timeout seconds: ${PROXY_TIMEOUT_SECONDS}" >&2
  exit 1
fi

SERVICE_URL="$(
  gcloud run services describe "${SERVICE_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --format='value(status.url)'
)"
if [[ -z "${SERVICE_URL}" ]]; then
  echo "Failed to resolve Cloud Run service URL." >&2
  exit 1
fi

QUERY="dryRun=${DRY_RUN}&retentionDays=${RETENTION_DAYS}&maxHours=${MAX_HOURS}"
if [[ -n "${EXTRA_QUERY}" ]]; then
  QUERY="${QUERY}&${EXTRA_QUERY}"
fi

echo "Service URL: ${SERVICE_URL}"
echo "Request: /run?${QUERY}"
echo "Auth mode: ${AUTH_MODE}"

run_request() {
  local url="$1"
  shift
  local response_file http_code
  response_file="$(mktemp)"
  http_code="$(curl -sS -o "${response_file}" -w "%{http_code}" "$@" "${url}")"

  echo "HTTP ${http_code}"
  if command -v jq >/dev/null 2>&1; then
    if jq -e . >/dev/null 2>&1 < "${response_file}"; then
      jq . "${response_file}"
    else
      cat "${response_file}"
    fi
  else
    cat "${response_file}"
  fi

  rm -f "${response_file}"
  if [[ "${http_code}" -ge 400 ]]; then
    return 1
  fi
}

if [[ "${AUTH_MODE}" == "proxy" ]]; then
  proxy_log="$(mktemp)"
  gcloud run services proxy "${SERVICE_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --port="${PROXY_PORT}" >"${proxy_log}" 2>&1 &
  proxy_pid=$!
  cleanup_proxy() {
    kill "${proxy_pid}" >/dev/null 2>&1 || true
    rm -f "${proxy_log}"
  }
  trap cleanup_proxy EXIT

  ready=0
  proxy_status_code=""
  max_checks=$(( PROXY_TIMEOUT_SECONDS * 2 ))
  for _ in $(seq 1 "${max_checks}"); do
    if ! kill -0 "${proxy_pid}" >/dev/null 2>&1; then
      echo "Proxy process exited before becoming ready. Logs:" >&2
      cat "${proxy_log}" >&2
      exit 1
    fi

    proxy_status_code="$(curl -sS -o /dev/null -w "%{http_code}" \
      "http://127.0.0.1:${PROXY_PORT}/healthz" || true)"
    if [[ -n "${proxy_status_code}" && "${proxy_status_code}" != "000" ]]; then
      ready=1
      break
    fi
    sleep 0.5
  done
  if [[ "${ready}" -ne 1 ]]; then
    echo "Proxy failed to become ready within ${PROXY_TIMEOUT_SECONDS}s. Logs:" >&2
    cat "${proxy_log}" >&2
    exit 1
  fi
  echo "Proxy ready (healthz HTTP ${proxy_status_code})"

  run_request "http://127.0.0.1:${PROXY_PORT}/run?${QUERY}" -X POST
  exit $?
fi

if [[ "${AUTH_MODE}" == "impersonate" ]]; then
  if [[ -z "${IMPERSONATE_SERVICE_ACCOUNT}" ]]; then
    echo "--impersonate-service-account is required when --auth-mode=impersonate" >&2
    exit 1
  fi
  TOKEN="$(
    gcloud auth print-identity-token \
      --impersonate-service-account="${IMPERSONATE_SERVICE_ACCOUNT}" \
      --audiences="${SERVICE_URL}"
  )"
else
  TOKEN="$(
    gcloud auth print-identity-token \
      --audiences="${SERVICE_URL}"
  )"
fi

run_request "${SERVICE_URL}/run?${QUERY}" -X POST -H "Authorization: Bearer ${TOKEN}"
