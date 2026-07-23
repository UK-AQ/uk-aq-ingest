#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  uk_aq_secret_manager_prune_versions.sh --project <id> [options]

Destroy old Secret Manager versions while keeping the newest N per secret.

Options:
  --project <id>        GCP project id (required)
  --keep <n>            Number of newest versions to keep per secret (default: 1)
  --secret <name>       Only prune this secret (repeatable)
  --dry-run             Print actions without making changes
  -h, --help            Show help
USAGE
}

PROJECT_ID=""
KEEP_COUNT="1"
DRY_RUN="0"
declare -a ONLY_SECRETS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      PROJECT_ID="${2:-}"
      shift 2
      ;;
    --keep)
      KEEP_COUNT="${2:-}"
      shift 2
      ;;
    --secret)
      ONLY_SECRETS+=("${2:-}")
      shift 2
      ;;
    --dry-run)
      DRY_RUN="1"
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

if [[ -z "${PROJECT_ID}" ]]; then
  echo "--project is required." >&2
  exit 2
fi

if ! [[ "${KEEP_COUNT}" =~ ^[0-9]+$ ]]; then
  echo "--keep must be a non-negative integer." >&2
  exit 2
fi

declare -a secrets=()
if [[ ${#ONLY_SECRETS[@]} -gt 0 ]]; then
  secrets=("${ONLY_SECRETS[@]}")
else
  while IFS= read -r name; do
    [[ -z "${name}" ]] && continue
    secrets+=("${name}")
  done < <(gcloud secrets list --project "${PROJECT_ID}" --format="value(name)")
fi

if [[ ${#secrets[@]} -eq 0 ]]; then
  echo "No secrets found in project ${PROJECT_ID}."
  exit 0
fi

total_destroy=0
total_keep=0

for secret in "${secrets[@]}"; do
  echo "Processing secret: ${secret}"
  versions=()
  while IFS= read -r version_name; do
    [[ -z "${version_name}" ]] && continue
    versions+=("${version_name}")
  done < <(
    gcloud secrets versions list "${secret}" \
      --project "${PROJECT_ID}" \
      --filter="state!=DESTROYED" \
      --sort-by="~createTime" \
      --format="value(name)"
  )

  if [[ ${#versions[@]} -eq 0 ]]; then
    echo "  no active versions"
    continue
  fi

  idx=0
  for full_name in "${versions[@]}"; do
    idx=$((idx + 1))
    version="${full_name##*/}"
    if (( idx <= KEEP_COUNT )); then
      echo "  keep version ${version}"
      total_keep=$((total_keep + 1))
      continue
    fi
    if [[ "${DRY_RUN}" == "1" ]]; then
      echo "  dry-run destroy version ${version}"
    else
      gcloud secrets versions destroy "${version}" \
        --project "${PROJECT_ID}" \
        --secret "${secret}" \
        --quiet >/dev/null
      echo "  destroyed version ${version}"
    fi
    total_destroy=$((total_destroy + 1))
  done
done

echo
if [[ "${DRY_RUN}" == "1" ]]; then
  echo "Dry run complete. planned_destroys=${total_destroy} kept=${total_keep}"
else
  echo "Prune complete. destroyed=${total_destroy} kept=${total_keep}"
fi
