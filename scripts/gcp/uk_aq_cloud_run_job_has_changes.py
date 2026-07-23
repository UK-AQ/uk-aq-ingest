#!/usr/bin/env python3
"""Return true/false when a Cloud Run job config differs from desired state."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Dict, List


def parse_kv_csv(raw: str) -> Dict[str, str]:
    if not raw:
        return {}
    row = next(csv.reader([raw]), [])
    out: Dict[str, str] = {}
    for item in row:
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"Invalid key/value item (missing '='): {item}")
        key, value = item.split("=", 1)
        out[key] = value
    return out


def parse_secret_csv(raw: str) -> Dict[str, str]:
    out = parse_kv_csv(raw)
    normalized: Dict[str, str] = {}
    for key, value in out.items():
        if ":" in value:
            secret_name, version = value.split(":", 1)
        else:
            secret_name, version = value, "latest"
        normalized[key] = f"{secret_name}:{version or 'latest'}"
    return normalized


def parse_env_file(path: str) -> Dict[str, str]:
    if not path:
        return {}
    out: Dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"Invalid env file line {idx}: {raw_line}")
        key, value = raw_line.split(":", 1)
        key = key.strip()
        value = value.strip()
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        out[key] = "" if parsed is None else str(parsed)
    return out


def parse_current_job(path: str) -> Dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    metadata = payload.get("metadata", {}) or {}
    spec = (
        payload.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("template", {})
        .get("spec", {})
    )
    container = {}
    containers = spec.get("containers") or []
    if containers:
        container = containers[0] or {}

    timeout = spec.get("timeoutSeconds")
    if timeout is None:
        timeout = spec.get("timeout")
    timeout_str = str(timeout or "")
    if timeout_str.endswith("s"):
        timeout_str = timeout_str[:-1]

    env_map: Dict[str, str] = {}
    secret_map: Dict[str, str] = {}
    for entry in container.get("env", []) or []:
        name = entry.get("name")
        if not name:
            continue
        if "value" in entry:
            env_map[name] = "" if entry["value"] is None else str(entry["value"])
            continue
        ref = (
            entry.get("valueFrom", {})
            .get("secretKeyRef", {})
        )
        secret_name = ref.get("name")
        if not secret_name:
            continue
        version = ref.get("key") or "latest"
        secret_map[name] = f"{secret_name}:{version}"

    labels: Dict[str, str] = {}
    for key, value in (metadata.get("labels", {}) or {}).items():
        labels[str(key)] = "" if value is None else str(value)

    return {
        "image": str(container.get("image", "")),
        "timeout_seconds": timeout_str,
        "max_retries": str(spec.get("maxRetries", "")),
        "service_account": str(spec.get("serviceAccountName", "")),
        "env": env_map,
        "secrets": secret_map,
        "labels": labels,
    }


def diff_dict(
    label: str, current: Dict[str, str], desired: Dict[str, str], diffs: List[str]
) -> None:
    if current == desired:
        return
    current_keys = set(current)
    desired_keys = set(desired)
    for key in sorted(desired_keys - current_keys):
        diffs.append(f"{label} missing current key: {key}")
    for key in sorted(current_keys - desired_keys):
        diffs.append(f"{label} extra current key: {key}")
    for key in sorted(current_keys & desired_keys):
        if current[key] != desired[key]:
            diffs.append(f"{label} value differs: {key}")


def diff_expected_labels(
    current: Dict[str, str], desired: Dict[str, str], diffs: List[str]
) -> None:
    for key, expected_value in sorted(desired.items()):
        if key not in current:
            diffs.append(f"label missing current key: {key}")
            continue
        if current[key] != expected_value:
            diffs.append(f"label value differs: {key}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-json", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--task-timeout-seconds", default="")
    parser.add_argument("--max-retries", default="")
    parser.add_argument("--service-account", default="")
    parser.add_argument("--env-csv", default="")
    parser.add_argument("--env-file", default="")
    parser.add_argument("--secret-csv", default="")
    parser.add_argument("--label-csv", default="")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    desired_env: Dict[str, str] = {}
    if args.env_csv:
        desired_env.update(parse_kv_csv(args.env_csv))
    if args.env_file:
        desired_env.update(parse_env_file(args.env_file))

    desired_secrets = parse_secret_csv(args.secret_csv)
    desired_labels = parse_kv_csv(args.label_csv)
    current = parse_current_job(args.job_json)

    diffs: List[str] = []
    if current["image"] != args.image:
        diffs.append("image differs")
    if args.task_timeout_seconds and str(current["timeout_seconds"]) != str(args.task_timeout_seconds):
        diffs.append("timeout differs")
    if args.max_retries and str(current["max_retries"]) != str(args.max_retries):
        diffs.append("max_retries differs")
    if args.service_account and str(current["service_account"]) != str(args.service_account):
        diffs.append("service_account differs")

    diff_dict("env", current["env"], desired_env, diffs)
    diff_dict("secret", current["secrets"], desired_secrets, diffs)
    diff_expected_labels(current["labels"], desired_labels, diffs)

    changed = bool(diffs)
    if args.verbose:
        if changed:
            for line in diffs:
                print(f"- {line}", file=sys.stderr)
        else:
            print("- no differences", file=sys.stderr)
    print("true" if changed else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
