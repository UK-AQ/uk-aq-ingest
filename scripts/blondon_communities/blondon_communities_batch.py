#!/usr/bin/env python3
"""
Batch Breathe London ingest by station refs via the Supabase Edge Function.

Example:
  python3 scripts/blondon_communities/blondon_communities_batch.py \
    --connector-code blondon_communities \
    --batch-size 10 \
    --active-only
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from supabase import Client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if PROJECT_ROOT.name == "scripts":
    PROJECT_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.uk_aq_supabase import SupabaseSchemas, create_supabase_client

TRUTHY = {"y", "yes", "true", "1"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch Breathe London ingest via Edge Function.")
    parser.add_argument(
        "--connector-code",
        default=os.getenv(
            "BLONDON_COMMUNITIES_CONNECTOR_CODE",
            "blondon_communities",
        ),
        help="Communities connector code (default: blondon_communities).",
    )
    parser.add_argument(
        "--service-ref",
        default=os.getenv("BLONDON_COMMUNITIES_SERVICE_REF", "breathelondon"),
        help="Service ref override (default: breathelondon).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Station refs per edge invocation (default: 10).",
    )
    parser.add_argument(
        "--active-only",
        action="store_true",
        help="Only include stations where stations.removed_at is null (default: false).",
    )
    parser.add_argument(
        "--skip-stations",
        action="store_true",
        help="Skip station discovery; use Supabase station refs (default: false).",
    )
    parser.add_argument("--limit", type=int, help="Limit station refs for testing.")
    parser.add_argument("--window-hours", type=int, help="Override window_hours.")
    parser.add_argument("--initial-days", type=int, help="Override initial_days.")
    parser.add_argument("--sleep-seconds", type=float, help="Override sleep_seconds.")
    parser.add_argument(
        "--edge-batch-size",
        type=int,
        help="Override observations batch_size in the edge function.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Run in dry-run mode.")
    parser.add_argument("--debug", action="store_true", help="Request debug output from the edge.")
    parser.add_argument("--timeout", type=int, default=180, help="Edge request timeout seconds.")
    parser.add_argument("--sleep-between", type=float, default=0.0, help="Sleep between batches.")
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue even if a batch fails.",
    )
    parser.add_argument(
        "--print-response",
        action="store_true",
        help="Print response bodies for each batch.",
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
    parser.add_argument(
        "--publishable-key",
        default=os.getenv("SB_PUBLISHABLE_DEFAULT_KEY"),
        help="Supabase publishable key (default: SB_PUBLISHABLE_DEFAULT_KEY).",
    )
    parser.add_argument(
        "--cron-secret",
        default=os.getenv("SB_UK_AQ_CRON_SECRET"),
        help="Cron secret header (default: SB_UK_AQ_CRON_SECRET).",
    )
    return parser.parse_args()


def _response_data(resp: Any) -> Any:
    return resp.data if hasattr(resp, "data") else resp.get("data")


def _coerce_metadata_attrs(row: Dict[str, Any]) -> Dict[str, Any]:
    meta = row.get("station_metadata")
    if isinstance(meta, list):
        meta = meta[0] if meta else {}
    if isinstance(meta, dict):
        return meta.get("attributes") or {}
    return {}


def _station_is_active(row: Dict[str, Any]) -> bool:
    return not row.get("removed_at")


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iter_station_rows(
    client,
    connector_id: int,
    service_ref: str,
    active_only: bool,
    page_size: int = 1000,
) -> Iterable[Dict[str, Any]]:
    offset = 0
    select = "id,station_ref,removed_at" if active_only else "id,station_ref"
    while True:
        resp = (
            client.table("stations")
            .select(select)
            .eq("connector_id", connector_id)
            .eq("service_ref", service_ref)
            .order("station_ref", desc=False)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        rows = _response_data(resp) or []
        if not rows:
            break
        for row in rows:
            yield row
        if len(rows) < page_size:
            break
        offset += page_size


def _chunk(values: List[Any], size: int) -> Iterable[List[Any]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def _load_oldest_fetch_map(
    client, station_ids: List[int]
) -> Dict[int, Tuple[Optional[datetime], Optional[datetime]]]:
    fetch_map: Dict[int, Tuple[Optional[datetime], Optional[datetime]]] = {
        station_id: (None, None) for station_id in station_ids
    }
    if not station_ids:
        return fetch_map
    for chunk in _chunk(station_ids, 500):
        resp = (
            client.table("blondon_communities_station_checkpoints")
            .select("station_id,next_due_at,last_polled_at")
            .in_("station_id", list(chunk))
            .execute()
        )
        rows = _response_data(resp) or []
        now = datetime.now(timezone.utc)
        for row in rows:
            station_id = row.get("station_id")
            if station_id is None:
                continue
            station_key = int(station_id)
            last_polled = _parse_timestamp(row.get("last_polled_at"))
            next_due = _parse_timestamp(row.get("next_due_at")) or now
            fetch_map[station_key] = (last_polled, next_due)
    return fetch_map


def main() -> int:
    args = parse_args()
    base_url = (args.base_url or "").rstrip("/")
    if not base_url:
        raise SystemExit("SUPABASE_URL (or --base-url) is required.")
    service_role_key = (args.service_role_key or "").strip()
    if not service_role_key:
        raise SystemExit("SB_SECRET_KEY (or --service-role-key) is required.")
    publishable_key = (args.publishable_key or "").strip()
    if not publishable_key:
        raise SystemExit("SB_PUBLISHABLE_DEFAULT_KEY (or --publishable-key) is required.")

    if args.connector_code != "blondon_communities":
        raise SystemExit(
            "Use connector_code=blondon_communities for Breathe London Communities. "
            "network_code/service_ref may remain breathelondon."
        )
    connector_code = args.connector_code
    service_ref = args.service_ref or "breathelondon"
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be greater than zero.")

    client: Client = create_supabase_client(base_url, service_role_key)
    schemas = SupabaseSchemas.from_client(client)
    core = schemas.core
    raw = schemas.raw
    connector_resp = (
        core.table("connectors")
        .select("id,connector_code")
        .eq("connector_code", connector_code)
        .limit(1)
        .execute()
    )
    connector_rows = _response_data(connector_resp) or []
    if not connector_rows:
        raise SystemExit(f"Connector not found: {connector_code}")
    connector_id = connector_rows[0]["id"]

    station_rows: List[Dict[str, Any]] = []
    for row in _iter_station_rows(core, connector_id, service_ref, args.active_only):
        station_ref = row.get("station_ref")
        if not station_ref:
            continue
        if args.active_only and not _station_is_active(row):
            continue
        station_rows.append(row)

    if not station_rows:
        print("No station refs found to ingest.")
        return 0

    station_ids = [int(row["id"]) for row in station_rows if row.get("id") is not None]
    fetch_map = _load_oldest_fetch_map(raw, station_ids)
    min_stamp = datetime.min.replace(tzinfo=timezone.utc)
    station_rows.sort(
        key=lambda row: (
            0 if fetch_map.get(int(row["id"])) is None else 1,
            (fetch_map.get(int(row["id"])) or (None, None))[0] or min_stamp,
            (fetch_map.get(int(row["id"])) or (None, None))[1] or min_stamp,
            str(row.get("station_ref") or ""),
        )
    )

    station_refs = [str(row["station_ref"]) for row in station_rows if row.get("station_ref")]
    if args.limit:
        station_refs = station_refs[: args.limit]

    total = len(station_refs)
    print(f"Loaded {total} station refs (connector={connector_code}, service_ref={service_ref}).")

    headers = {
        "Authorization": f"Bearer {publishable_key}",
        "apikey": publishable_key,
        "Content-Type": "application/json",
    }
    if args.cron_secret:
        headers["X-Cron-Secret"] = args.cron_secret

    url = f"{base_url}/functions/v1/ingest_breathelondon"
    failures = 0
    batches = list(_chunk(station_refs, args.batch_size))

    for idx, batch in enumerate(batches, start=1):
        payload: Dict[str, Any] = {
            "connector_code": connector_code,
            "station_refs": batch,
        }
        if args.skip_stations:
            payload["skip_stations"] = True
        if args.active_only:
            payload["active_only"] = True
        if args.window_hours is not None:
            payload["window_hours"] = args.window_hours
        if args.initial_days is not None:
            payload["initial_days"] = args.initial_days
        if args.sleep_seconds is not None:
            payload["sleep_seconds"] = args.sleep_seconds
        if args.edge_batch_size is not None:
            payload["batch_size"] = args.edge_batch_size
        if args.dry_run:
            payload["dry_run"] = True
        if args.debug:
            payload["debug"] = True

        print(f"Batch {idx}/{len(batches)} -> {len(batch)} stations")
        resp = requests.post(url, headers=headers, json=payload, timeout=args.timeout)
        print(f"  status: {resp.status_code}")
        if args.print_response or not resp.ok:
            print(resp.text)
        if not resp.ok:
            failures += 1
            if not args.continue_on_error:
                print("Stopping after failed batch.")
                return 1
        if args.sleep_between and idx < len(batches):
            time.sleep(args.sleep_between)

    if failures:
        print(f"Completed with {failures} failed batch(es).")
        return 1
    print("All batches completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
