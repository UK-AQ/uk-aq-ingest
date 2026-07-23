#!/usr/bin/env bash
set -euo pipefail

ENV_FILE=".env"
NO_NETWORK=0

usage() {
  cat <<'EOF'
Usage:
  ./scripts/uk_aq_check_env.sh [--env-file <path>] [--no-network]

Options:
  --env-file <path>  Load variables from this env file (default: .env)
  --no-network       Skip live HTTP checks and run local validation only
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --env-file" >&2
        exit 2
      fi
      ENV_FILE="$2"
      shift 2
      ;;
    --no-network)
      NO_NETWORK=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Env file not found: $ENV_FILE" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

main_vars=(
  SUPABASE_URL
  SUPABASE_PROJECT_REF
  SUPABASE_DB_URL
  SB_PUBLISHABLE_DEFAULT_KEY
  SB_SECRET_KEY
  SUPABASE_ACCESS_TOKEN
  UK_AIR_RAW_DROPBOX_ALLOWED_SUPABASE_URL
  SB_UK_AQ_CRON_SECRET
  UK_AQ_CORE_SCHEMA
  SUPABASE_SECRETS_ENV
)

obs_aqi_vars=(
  OBS_AQIDB_SUPABASE_URL
  OBS_AQIDB_SUPABASE_PROJECT_REF
  OBS_AQIDB_RPC_SCHEMA
)

edge_api_vars=(
  UK_AQ_EDGE_UPSTREAM_SECRET
  UK_AQ_OBSERVS_HISTORY_R2_API_URL
)

failures=0
warnings=0

ok() {
  printf "OK    %s\n" "$1"
}

warn() {
  printf "WARN  %s\n" "$1"
  warnings=$((warnings + 1))
}

fail() {
  printf "FAIL  %s\n" "$1"
  failures=$((failures + 1))
}

mask_value() {
  local value="$1"
  local len="${#value}"
  if (( len == 0 )); then
    printf "<empty>"
    return
  fi
  if (( len <= 14 )); then
    printf "%s" "$value"
    return
  fi
  local head="${value:0:8}"
  local tail="${value: -6}"
  printf "%s...%s" "$head" "$tail"
}

extract_ref_from_url() {
  local url="${1:-}"
  printf "%s" "$url" | sed -E 's#https?://([^.]+)\.supabase\.co/?#\1#'
}

extract_ref_from_db_url() {
  python3 - <<'PY'
import os
import urllib.parse
db_url = os.environ.get("SUPABASE_DB_URL", "")
parsed = urllib.parse.urlparse(db_url)
username = parsed.username or ""
if username.startswith("postgres."):
    print(username.split(".", 1)[1])
else:
    print("")
PY
}

http_code() {
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' "$@")"
  printf "%s" "$code"
}

check_presence_group() {
  local group="$1"
  shift
  local var
  echo
  echo "[$group] Presence"
  for var in "$@"; do
    if [[ -n "${!var:-}" ]]; then
      ok "$var is set"
    else
      fail "$var is missing"
    fi
  done
}

echo "UK-AQ env check"
echo "Env file: $ENV_FILE"

check_presence_group "Main" "${main_vars[@]}"
check_presence_group "Obs AQI DB" "${obs_aqi_vars[@]}"
check_presence_group "Edge API routing" "${edge_api_vars[@]}"
if [[ -z "${SB_SECRET_KEY:-}" ]]; then
  fail "Main key check: set SB_SECRET_KEY"
fi
if [[ -z "${OBS_AQIDB_SECRET_KEY:-}" ]]; then
  fail "Obs AQI DB key check: set OBS_AQIDB_SECRET_KEY"
fi

OBS_AQI_PRIV_KEY="${OBS_AQIDB_SECRET_KEY:-}"
OBS_AQI_PRIV_KEY_NAME="OBS_AQIDB_SECRET_KEY"

echo
echo "[Secrets] Masked preview"
for var in \
  SB_PUBLISHABLE_DEFAULT_KEY \
  SB_SECRET_KEY \
  SUPABASE_ACCESS_TOKEN \
  SB_UK_AQ_CRON_SECRET \
  OBS_AQIDB_SECRET_KEY; do
  value="${!var:-}"
  printf "%-32s len=%-4s value=%s\n" "$var" "${#value}" "$(mask_value "$value")"
done

echo
echo "[Refs] Project alignment"
main_ref_url="$(extract_ref_from_url "${SUPABASE_URL:-}")"
main_ref_env="${SUPABASE_PROJECT_REF:-}"
main_ref_db="$(extract_ref_from_db_url)"
obs_aqi_ref_url="$(extract_ref_from_url "${OBS_AQIDB_SUPABASE_URL:-}")"
obs_aqi_ref_env="${OBS_AQIDB_SUPABASE_PROJECT_REF:-}"

if [[ -n "$main_ref_url" && -n "$main_ref_env" && "$main_ref_url" == "$main_ref_env" ]]; then
  ok "SUPABASE_URL ref matches SUPABASE_PROJECT_REF ($main_ref_env)"
else
  fail "SUPABASE_URL ref ($main_ref_url) does not match SUPABASE_PROJECT_REF ($main_ref_env)"
fi

if [[ -n "$main_ref_db" && -n "$main_ref_env" && "$main_ref_db" == "$main_ref_env" ]]; then
  ok "SUPABASE_DB_URL ref matches SUPABASE_PROJECT_REF ($main_ref_env)"
else
  fail "SUPABASE_DB_URL ref ($main_ref_db) does not match SUPABASE_PROJECT_REF ($main_ref_env)"
fi

if [[ -n "$obs_aqi_ref_url" && -n "$obs_aqi_ref_env" && "$obs_aqi_ref_url" == "$obs_aqi_ref_env" ]]; then
  ok "OBS_AQIDB_SUPABASE_URL ref matches OBS_AQIDB_SUPABASE_PROJECT_REF ($obs_aqi_ref_env)"
else
  fail "OBS_AQIDB_SUPABASE_URL ref ($obs_aqi_ref_url) does not match OBS_AQIDB_SUPABASE_PROJECT_REF ($obs_aqi_ref_env)"
fi

if [[ "${UK_AIR_RAW_DROPBOX_ALLOWED_SUPABASE_URL:-}" == "${SUPABASE_URL:-}" ]]; then
  ok "UK_AIR_RAW_DROPBOX_ALLOWED_SUPABASE_URL matches SUPABASE_URL"
else
  warn "UK_AIR_RAW_DROPBOX_ALLOWED_SUPABASE_URL does not match SUPABASE_URL"
fi

if [[ -n "${SUPABASE_SECRETS_ENV:-}" ]]; then
  if [[ -f "${SUPABASE_SECRETS_ENV}" ]]; then
    ok "SUPABASE_SECRETS_ENV file exists (${SUPABASE_SECRETS_ENV})"
  else
    warn "SUPABASE_SECRETS_ENV file not found (${SUPABASE_SECRETS_ENV})"
  fi
fi

echo
echo "[JWT] Token claims (JWT-formatted keys)"
export OBS_AQI_PRIV_KEY
python3 - <<'PY'
import base64
import json
import os

def decode_payload(token: str):
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
    except Exception:
        return None

for key in ("SB_SECRET_KEY",):
    token = (os.environ.get(key, "") or "").strip()
    payload = decode_payload(token)
    if not payload:
      print(f"WARN  {key} is not a JWT payload (or invalid)")
      continue
    role = payload.get("role")
    ref = payload.get("ref")
    print(f"OK    {key} role={role} ref={ref}")

obs_aqi_token = (os.environ.get("OBS_AQI_PRIV_KEY", "") or "").strip()
obs_aqi_payload = decode_payload(obs_aqi_token)
if not obs_aqi_payload:
    print("WARN  OBS_AQIDB_SECRET_KEY is not a JWT payload (or invalid)")
else:
    role = obs_aqi_payload.get("role")
    ref = obs_aqi_payload.get("ref")
    print(f"OK    OBS_AQIDB_SECRET_KEY role={role} ref={ref}")
PY

if (( NO_NETWORK == 0 )); then
  echo
  echo "[Network] Live checks"

  code="$(http_code -H "Authorization: Bearer ${SUPABASE_ACCESS_TOKEN:-}" https://api.supabase.com/v1/projects)"
  if [[ "$code" == "200" ]]; then
    ok "SUPABASE_ACCESS_TOKEN can list projects (200)"
  else
    fail "SUPABASE_ACCESS_TOKEN projects check returned HTTP $code"
  fi

  code="$(http_code -H "apikey: ${SB_PUBLISHABLE_DEFAULT_KEY:-}" "${SUPABASE_URL:-}/rest/v1/")"
  if [[ "$code" == "200" ]]; then
    ok "SB_PUBLISHABLE_DEFAULT_KEY can access main /rest/v1/ (200)"
  else
    fail "SB_PUBLISHABLE_DEFAULT_KEY main /rest/v1/ check returned HTTP $code"
  fi

  main_priv_key="${SB_SECRET_KEY:-}"
  code="$(http_code -H "apikey: ${main_priv_key}" "${SUPABASE_URL:-}/rest/v1/")"
  if [[ "$code" == "200" ]]; then
    ok "Main privileged key (SB_SECRET_KEY) can access main /rest/v1/ (200)"
  else
    fail "Main privileged key (SB_SECRET_KEY) main /rest/v1/ check returned HTTP $code"
  fi

  code="$(http_code -H "apikey: ${OBS_AQI_PRIV_KEY:-}" "${OBS_AQIDB_SUPABASE_URL:-}/rest/v1/")"
  if [[ "$code" == "200" ]]; then
    ok "${OBS_AQI_PRIV_KEY_NAME} can access obs_aqidb /rest/v1/ (200)"
  else
    fail "${OBS_AQI_PRIV_KEY_NAME} obs_aqidb /rest/v1/ check returned HTTP $code"
  fi
else
  echo
  echo "[Network] Skipped (--no-network)"
fi

echo
if (( failures > 0 )); then
  echo "Result: FAIL (failures=$failures warnings=$warnings)"
  exit 1
fi

echo "Result: PASS (warnings=$warnings)"
