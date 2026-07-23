#!/usr/bin/env python3
"""
Fetch recent error_logs rows from uk_aq_raw for debugging (service_role required).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import requests


def _postgrest_headers(service_role_key: str, schema: str) -> Dict[str, str]:
    return {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Accept-Profile": schema,
    }


def _fetch_json(url: str, headers: Dict[str, str], params: Dict[str, str]) -> List[Dict[str, object]]:
    resp = requests.get(url, headers=headers, params=params, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"PostgREST error {resp.status_code}: {resp.text}")
    payload = resp.json()
    return payload if isinstance(payload, list) else []


def _format_ts(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch recent uk_aq_raw.error_logs rows.")
    parser.add_argument(
        "--supabase-url",
        default=os.getenv("SUPABASE_URL"),
        help="Supabase URL (default: SUPABASE_URL).",
    )
    parser.add_argument(
        "--service-role-key",
        default=os.getenv("SB_SECRET_KEY"),
        help="Supabase service role key (default: SB_SECRET_KEY).",
    )
    parser.add_argument(
        "--raw-schema",
        default=os.getenv("UK_AQ_RAW_SCHEMA", "uk_aq_raw"),
        help="Raw schema name (default: uk_aq_raw).",
    )
    parser.add_argument(
        "--source",
        default="erg_laqn",
        help="Filter source (ILIKE pattern, default: erg_laqn).",
    )
    parser.add_argument(
        "--since-hours",
        type=float,
        default=24,
        help="Lookback window in hours (default: 24).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max rows to return (default: 50).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    supabase_url = (args.supabase_url or "").strip().rstrip("/")
    service_role_key = (args.service_role_key or "").strip()
    raw_schema = (args.raw_schema or "uk_aq_raw").strip() or "uk_aq_raw"

    if not supabase_url or not service_role_key:
        raise SystemExit("SUPABASE_URL and SB_SECRET_KEY are required.")

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=max(0, args.since_hours))

    headers = _postgrest_headers(service_role_key, raw_schema)
    base_url = f"{supabase_url}/rest/v1"

    params: Dict[str, str] = {
        "select": "created_at,source,severity,message,connector_id,station_id,timeseries_id,dropbox_path,context,stack",
        "order": "created_at.desc.nullslast",
        "limit": str(max(1, args.limit)),
        "created_at": f"gte.{_format_ts(since)}",
    }
    if args.source:
        params["source"] = f"ilike.*{args.source}*"

    rows = _fetch_json(f"{base_url}/error_logs", headers, params)
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
