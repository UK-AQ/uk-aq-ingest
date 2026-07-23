#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Check whether Cloud Billing export to BigQuery is enabled and populated.

Usage:
  scripts/gcp_billing_export_check.sh [options]

Options:
  --project <id>          Billing export BigQuery project.
                          Defaults to BILLING_EXPORT_PROJECT, then gcloud default project.
  --dataset <id>          Billing export BigQuery dataset.
                          Defaults to BILLING_EXPORT_DATASET.
  --billing-account <id>  Optional billing account id (for table name hinting only).
                          Defaults to BILLING_ACCOUNT_ID.
  -h, --help              Show help.

Examples:
  BILLING_EXPORT_PROJECT=my-billing-proj BILLING_EXPORT_DATASET=billing_export \
    ./scripts/gcp_billing_export_check.sh

  ./scripts/gcp_billing_export_check.sh --project my-billing-proj --dataset billing_export
EOF
}

PROJECT_ID="${BILLING_EXPORT_PROJECT:-}"
DATASET_ID="${BILLING_EXPORT_DATASET:-}"
BILLING_ACCOUNT_ID="${BILLING_ACCOUNT_ID:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      PROJECT_ID="${2:-}"
      shift 2
      ;;
    --dataset)
      DATASET_ID="${2:-}"
      shift 2
      ;;
    --billing-account)
      BILLING_ACCOUNT_ID="${2:-}"
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

if ! command -v bq >/dev/null 2>&1; then
  echo "FAIL: bq CLI is required but not found in PATH."
  exit 1
fi

if [[ -z "${PROJECT_ID}" ]] && command -v gcloud >/dev/null 2>&1; then
  PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
  PROJECT_ID="${PROJECT_ID//$'\r'/}"
  PROJECT_ID="${PROJECT_ID//$'\n'/}"
fi

if [[ -z "${PROJECT_ID}" ]]; then
  echo "FAIL: Billing export project is not set."
  echo "Set BILLING_EXPORT_PROJECT or pass --project."
  exit 1
fi

echo "Checking BigQuery billing export in project: ${PROJECT_ID}"
if [[ -n "${DATASET_ID}" ]]; then
  echo "Requested dataset: ${DATASET_ID}"
else
  echo "Requested dataset: <not provided; scanning all datasets>"
fi

DATASETS_JSON="$(bq --project_id="${PROJECT_ID}" ls --format=prettyjson 2>/dev/null || true)"
if [[ -z "${DATASETS_JSON}" ]]; then
  echo "FAIL: Could not list datasets in project ${PROJECT_ID}."
  echo "Confirm BigQuery access and project id."
  exit 1
fi

DATASET_LIST="$(python3 -c 'import json,sys; rows=json.load(sys.stdin); print("\n".join(sorted((r.get("datasetReference") or {}).get("datasetId","") for r in rows if (r.get("datasetReference") or {}).get("datasetId"))))' <<<"${DATASETS_JSON}")"

if [[ -z "${DATASET_LIST}" ]]; then
  echo "FAIL: No datasets found in project ${PROJECT_ID}."
  echo "Console path to enable export: Billing -> Billing export -> BigQuery export."
  exit 1
fi

scan_dataset_for_export_tables() {
  local project="$1"
  local dataset="$2"
  local tables_json

  tables_json="$(bq --project_id="${project}" ls --format=prettyjson "${project}:${dataset}" 2>/dev/null || true)"
  if [[ -z "${tables_json}" ]]; then
    return 1
  fi

  python3 - "${BILLING_ACCOUNT_ID}" <<'PY' <<<"${tables_json}"
import json
import re
import sys

tables = json.load(sys.stdin)
billing_account = (sys.argv[1] or "").strip().lower().replace("-", "_")
prefixes = (
    "gcp_billing_export_v1_",
    "gcp_billing_export_resource_v1_",
)

matches = []
for row in tables:
    table_id = ((row.get("tableReference") or {}).get("tableId") or "").strip()
    if not table_id:
        continue
    if table_id.startswith(prefixes):
        if billing_account and billing_account not in table_id.lower():
            continue
        matches.append(table_id)

print("\n".join(matches))
PY
}

verify_labels_column() {
  local project="$1"
  local dataset="$2"
  local table="$3"
  local schema_json

  schema_json="$(bq --project_id="${project}" show --schema --format=prettyjson "${project}:${dataset}.${table}" 2>/dev/null || true)"
  if [[ -z "${schema_json}" ]]; then
    echo "WARN: Could not read schema for ${project}:${dataset}.${table}."
    return 0
  fi

  if python3 -c 'import json,sys
def has_labels(fields):
    for field in fields or []:
        if (field.get("name") or "") == "labels":
            return True
        if has_labels(field.get("fields") or []):
            return True
    return False
print("true" if has_labels(json.load(sys.stdin)) else "false")' <<<"${schema_json}" | grep -qx "true"; then
    echo "INFO: labels field found in schema (${project}:${dataset}.${table})."
  else
    echo "WARN: labels field not found in sampled schema (${project}:${dataset}.${table})."
  fi
}

if [[ -n "${DATASET_ID}" ]]; then
  if ! grep -qx "${DATASET_ID}" <<<"${DATASET_LIST}"; then
    echo "FAIL: Dataset ${DATASET_ID} not found in project ${PROJECT_ID}."
    echo "Console path: Billing -> Billing export -> BigQuery export."
    echo "Enable Standard usage cost export (and Detailed usage cost export for richer label analysis)."
    exit 1
  fi

  TABLE_MATCHES="$(scan_dataset_for_export_tables "${PROJECT_ID}" "${DATASET_ID}" || true)"
  if [[ -z "${TABLE_MATCHES}" ]]; then
    echo "FAIL: Dataset ${PROJECT_ID}:${DATASET_ID} has no billing export tables yet."
    echo "Expected names like gcp_billing_export_v1_* (and optionally gcp_billing_export_resource_v1_*)."
    echo "If export was just enabled, first data can take several hours to arrive."
    exit 1
  fi

  first_table="$(head -n 1 <<<"${TABLE_MATCHES}")"
  echo "PASS: Billing export tables found in ${PROJECT_ID}:${DATASET_ID}"
  printf '%s\n' "${TABLE_MATCHES}" | sed 's/^/- /'
  verify_labels_column "${PROJECT_ID}" "${DATASET_ID}" "${first_table}"
  exit 0
fi

found=0
while IFS= read -r dataset; do
  [[ -z "${dataset}" ]] && continue
  TABLE_MATCHES="$(scan_dataset_for_export_tables "${PROJECT_ID}" "${dataset}" || true)"
  if [[ -z "${TABLE_MATCHES}" ]]; then
    continue
  fi
  found=1
  first_table="$(head -n 1 <<<"${TABLE_MATCHES}")"
  echo "PASS: Billing export tables found in ${PROJECT_ID}:${dataset}"
  printf '%s\n' "${TABLE_MATCHES}" | sed 's/^/- /'
  verify_labels_column "${PROJECT_ID}" "${dataset}" "${first_table}"
done <<<"${DATASET_LIST}"

if [[ "${found}" -eq 0 ]]; then
  echo "FAIL: No billing export tables found in project ${PROJECT_ID}."
  echo "Checked for table prefixes: gcp_billing_export_v1_* and gcp_billing_export_resource_v1_*."
  echo "Console path: Billing -> Billing export -> BigQuery export."
  echo "Enable Standard usage cost export (and Detailed usage cost export for richer label analysis)."
  exit 1
fi

