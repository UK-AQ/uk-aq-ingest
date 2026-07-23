#!/usr/bin/env python3
"""
Export a snapshot of connector polling settings and basic counts to CSV.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _fetch_json(base_url: str, headers: Dict[str, str], table: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
    query = urlencode(params, safe=",.*()")
    req = Request(f"{base_url}/{table}?{query}", headers=headers, method="GET")
    with urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, list) else []


def _fetch_all(
    base_url: str,
    headers: Dict[str, str],
    table: str,
    params: Dict[str, str],
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch_params = dict(params)
        batch_params["limit"] = str(limit)
        batch_params["offset"] = str(offset)
        batch = _fetch_json(base_url, headers, table, batch_params)
        rows.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return rows


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _format_ts(value: Optional[datetime]) -> str:
    if not value:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _hours_since(value: Optional[datetime], now: datetime) -> str:
    if not value:
        return ""
    hours = (now - value).total_seconds() / 3600.0
    return f"{hours:.2f}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export connector polling settings and counts to CSV.",
    )
    parser.add_argument(
        "--output",
        help=(
            "Output CSV path (default: "
            "network_info/uk_aq/uk_aq_connectors_snapshot_<timestamp>.csv)."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("SUPABASE_URL"),
        help="Supabase URL (default: SUPABASE_URL).",
    )
    parser.add_argument(
        "--service-role-key",
        default=os.getenv("SB_SECRET_KEY"),
        help="Supabase service role key (default: SB_SECRET_KEY).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    _load_env(Path(".env"))

    supabase_url = (args.base_url or "").strip().rstrip("/")
    service_role_key = (args.service_role_key or "").strip()
    if not supabase_url or not service_role_key:
        raise SystemExit("SUPABASE_URL and SB_SECRET_KEY are required.")

    base_url = f"{supabase_url}/rest/v1"
    core_schema = os.getenv("UK_AQ_CORE_SCHEMA", "uk_aq_core")
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Accept-Profile": core_schema,
    }

    connectors = _fetch_all(
        base_url,
        headers,
        "connectors",
        {
            "select": ",".join(
                [
                    "id",
                    "connector_code",
                    "label",
                    "display_name",
                    "service_url",
                    "station_display_name_template",
                    "overwrite_station_name",
                    "poll_enabled",
                    "poll_interval_minutes",
                    "poll_window_hours",
                    "poll_timeseries_batch_size",
                    "stations_bbox_supported",
                    "timeseries_station_filter_supported",
                    "last_polled_at",
                    "last_run_start",
                    "last_run_end",
                    "last_run_status",
                    "last_run_message",
                    "created_at",
                ]
            ),
            "order": "connector_code.asc",
        },
    )

    stations = _fetch_all(
        base_url,
        headers,
        "stations",
        {"select": "id,connector_id,removed_at"},
    )

    station_counts: Dict[int, int] = {}
    active_station_counts: Dict[int, int] = {}
    for row in stations:
        connector_id = row.get("connector_id")
        if connector_id is None:
            continue
        station_counts[connector_id] = station_counts.get(connector_id, 0) + 1
        if row.get("removed_at") is None:
            active_station_counts[connector_id] = active_station_counts.get(connector_id, 0) + 1

    timeseries = _fetch_all(
        base_url,
        headers,
        "timeseries",
        {"select": "id,connector_id,last_value_at"},
    )

    timeseries_stats: Dict[int, Dict[str, Any]] = {}
    for row in timeseries:
        connector_id = row.get("connector_id")
        if connector_id is None:
            continue
        stats = timeseries_stats.setdefault(
            connector_id,
            {"count": 0, "with_value": 0, "min": None, "max": None},
        )
        stats["count"] += 1
        last_value_at = _parse_ts(row.get("last_value_at"))
        if last_value_at:
            stats["with_value"] += 1
            if stats["min"] is None or last_value_at < stats["min"]:
                stats["min"] = last_value_at
            if stats["max"] is None or last_value_at > stats["max"]:
                stats["max"] = last_value_at

    now = datetime.now(timezone.utc)
    fieldnames = [
        "connector_id",
        "connector_code",
        "label",
        "display_name",
        "service_url",
        "station_display_name_template",
        "overwrite_station_name",
        "poll_enabled",
        "poll_interval_minutes",
        "poll_window_hours",
        "poll_timeseries_batch_size",
        "stations_bbox_supported",
        "timeseries_station_filter_supported",
        "station_count",
        "active_station_count",
        "removed_station_count",
        "timeseries_count",
        "timeseries_with_value_count",
        "latest_value_at",
        "oldest_value_at",
        "hours_since_latest_value",
        "last_polled_at",
        "hours_since_last_polled",
        "last_run_start",
        "last_run_end",
        "hours_since_last_run_end",
        "last_run_status",
        "last_run_message",
        "created_at",
    ]

    rows: List[Dict[str, Any]] = []
    for connector in connectors:
        connector_id = connector.get("id")
        stats = timeseries_stats.get(connector_id, {})
        last_polled_at = _parse_ts(connector.get("last_polled_at"))
        last_run_start = _parse_ts(connector.get("last_run_start"))
        last_run_end = _parse_ts(connector.get("last_run_end"))
        latest_value_at = stats.get("max")
        oldest_value_at = stats.get("min")
        station_total = station_counts.get(connector_id, 0)
        station_active = active_station_counts.get(connector_id, 0)

        row = {
            "connector_id": connector_id or "",
            "connector_code": connector.get("connector_code") or "",
            "label": connector.get("label") or "",
            "display_name": connector.get("display_name") or "",
            "service_url": connector.get("service_url") or "",
            "station_display_name_template": connector.get("station_display_name_template") or "",
            "overwrite_station_name": connector.get("overwrite_station_name"),
            "poll_enabled": connector.get("poll_enabled"),
            "poll_interval_minutes": connector.get("poll_interval_minutes"),
            "poll_window_hours": connector.get("poll_window_hours"),
            "poll_timeseries_batch_size": connector.get("poll_timeseries_batch_size"),
            "stations_bbox_supported": connector.get("stations_bbox_supported"),
            "timeseries_station_filter_supported": connector.get("timeseries_station_filter_supported"),
            "station_count": station_total,
            "active_station_count": station_active,
            "removed_station_count": station_total - station_active,
            "timeseries_count": stats.get("count", 0),
            "timeseries_with_value_count": stats.get("with_value", 0),
            "latest_value_at": _format_ts(latest_value_at),
            "oldest_value_at": _format_ts(oldest_value_at),
            "hours_since_latest_value": _hours_since(latest_value_at, now),
            "last_polled_at": _format_ts(last_polled_at),
            "hours_since_last_polled": _hours_since(last_polled_at, now),
            "last_run_start": _format_ts(last_run_start),
            "last_run_end": _format_ts(last_run_end),
            "hours_since_last_run_end": _hours_since(last_run_end, now),
            "last_run_status": connector.get("last_run_status") or "",
            "last_run_message": connector.get("last_run_message") or "",
            "created_at": connector.get("created_at") or "",
        }

        for key, value in row.items():
            if value is None:
                row[key] = ""

        rows.append(row)

    output_path = Path(args.output) if args.output else None
    if output_path is None:
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        output_path = Path("network_info") / "uk_aq" / f"uk_aq_connectors_snapshot_{timestamp}.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} connectors to {output_path}")


if __name__ == "__main__":
    main()
