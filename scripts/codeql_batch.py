#!/usr/bin/env python3
"""Create deterministic CodeQL alert batches from exported data."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SEVERITY_ORDER = {
    "critical": 0,
    "error": 1,
    "high": 2,
    "warning": 3,
    "medium": 4,
    "note": 5,
    "low": 6,
    "none": 7,
}


def parse_args() -> argparse.Namespace:
    today = dt.date.today().isoformat()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--alerts",
        default=f".codeql/exports/{today}/alerts.json",
        help="Path to alerts.json from codeql_alerts_export.py",
    )
    parser.add_argument(
        "--instances-dir",
        default=f".codeql/exports/{today}/instances",
        help="Directory containing per-alert instance exports",
    )
    parser.add_argument(
        "--outdir",
        default=f".codeql/batches/{today}",
        help="Output folder for batch-XX.json files",
    )
    parser.add_argument("--batch-size", type=int, default=10, help="Alerts per batch (default: 10)")
    parser.add_argument("--max-batches", type=int, default=None, help="Optional cap on number of batches")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def first_location_from_instance(instance: Dict[str, Any]) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    location = instance.get("location")
    if not isinstance(location, dict):
        return None, None, None
    path = location.get("path")
    start = location.get("start_line")
    end = location.get("end_line")
    return (path if isinstance(path, str) else None, start if isinstance(start, int) else None, end if isinstance(end, int) else None)


def extract_location(alert: Dict[str, Any], instances_dir: Path) -> Tuple[Optional[str], Optional[int], Optional[int], Optional[str]]:
    mri = alert.get("most_recent_instance")
    if isinstance(mri, dict):
        path, start, end = first_location_from_instance(mri)
        if path:
            ref = mri.get("ref")
            sha = mri.get("analysis_key")
            ref_or_sha = ref if isinstance(ref, str) else (sha if isinstance(sha, str) else None)
            return path, start, end, ref_or_sha

    alert_number = alert.get("number")
    if isinstance(alert_number, int):
        instance_path = instances_dir / f"{alert_number}.json"
        if instance_path.exists():
            instances = load_json(instance_path)
            if isinstance(instances, list) and instances:
                path, start, end = first_location_from_instance(instances[0])
                ref = instances[0].get("ref")
                ref_or_sha = ref if isinstance(ref, str) else None
                return path, start, end, ref_or_sha

    return None, None, None, None


def derive_why_this_matters(message: str, severity: str) -> str:
    text = " ".join(message.split())
    if len(text) > 140:
        text = text[:137].rstrip() + "..."
    sev = severity.lower() if severity else "unknown"
    return f"{sev.capitalize()} risk: {text}" if text else f"{sev.capitalize()} risk reported by CodeQL."


def normalize_alert(alert: Dict[str, Any], instances_dir: Path) -> Dict[str, Any]:
    rule = alert.get("rule") if isinstance(alert.get("rule"), dict) else {}
    rule_id = rule.get("id") if isinstance(rule.get("id"), str) else "unknown-rule"

    severity = (
        alert.get("rule_severity")
        or rule.get("severity")
        or alert.get("severity")
        or "none"
    )
    severity = str(severity).lower()

    message_block = alert.get("most_recent_instance")
    message = None
    if isinstance(message_block, dict):
        message_obj = message_block.get("message")
        if isinstance(message_obj, dict) and isinstance(message_obj.get("text"), str):
            message = message_obj["text"]

    if not message:
        message_obj = rule.get("description")
        message = message_obj if isinstance(message_obj, str) else "CodeQL alert"

    path, start_line, end_line, ref_or_sha = extract_location(alert, instances_dir)

    return {
        "alert_number": alert.get("number"),
        "rule_id": rule_id,
        "severity": severity,
        "description": message,
        "file_path": path,
        "start_line": start_line,
        "end_line": end_line,
        "ref_or_sha": ref_or_sha,
        "why_this_matters": derive_why_this_matters(message, severity),
    }


def sort_key(item: Dict[str, Any]) -> Tuple[int, str, int]:
    severity = str(item.get("severity", "none")).lower()
    sev_rank = SEVERITY_ORDER.get(severity, 99)
    rule_id = str(item.get("rule_id", ""))
    number = item.get("alert_number")
    alert_number = number if isinstance(number, int) else 0
    return sev_rank, rule_id, alert_number


def main() -> None:
    args = parse_args()
    alerts_path = Path(args.alerts)
    instances_dir = Path(args.instances_dir)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    alerts = load_json(alerts_path)
    if not isinstance(alerts, list):
        raise RuntimeError(f"Expected list in {alerts_path}")

    normalized = [normalize_alert(alert, instances_dir) for alert in alerts if isinstance(alert, dict)]
    normalized.sort(key=sort_key)

    if args.batch_size < 1:
        raise RuntimeError("--batch-size must be >= 1")

    batches: List[List[Dict[str, Any]]] = [
        normalized[i : i + args.batch_size] for i in range(0, len(normalized), args.batch_size)
    ]

    if args.max_batches is not None:
        batches = batches[: max(args.max_batches, 0)]

    for idx, batch in enumerate(batches, start=1):
        payload = {
            "batch_number": idx,
            "batch_size": len(batch),
            "total_alerts_considered": len(normalized),
            "alerts": batch,
        }
        save_json(outdir / f"batch-{idx:02d}.json", payload)

    print(f"Batching complete: {len(normalized)} alerts processed into {len(batches)} batch(es) under {outdir}.")


if __name__ == "__main__":
    main()
