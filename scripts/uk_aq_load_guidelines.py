#!/usr/bin/env python3
"""
Load WHO GAQG 2021 guideline limits into uk_aq_guidelines.

Requires:
- SUPABASE_URL
- SB_SECRET_KEY

CSV columns expected:
pollutant, averaging_time, unit, AQG_2021, IT1, IT2, IT3, IT4, notes, source
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv
from supabase import Client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.uk_aq_supabase import SupabaseSchemas, create_supabase_client

load_dotenv()

LEVEL_COLUMNS = ("AQG_2021", "IT1", "IT2", "IT3", "IT4")


def normalize_pollutant(value: str) -> str:
    return value.strip().upper()


def parse_averaging_time(value: str) -> Tuple[str, Optional[str]]:
    if not value:
        return ("", None)
    normalized = value.strip().lower()
    if "annual" in normalized:
        return ("annual", None)
    match = re.match(r"(\\d+)\\s*-?\\s*hour", normalized)
    if match:
        hours = int(match.group(1))
        return (f"{hours}-hour", f"{hours} hours")
    return (normalized.replace(" ", ""), None)


def chunked(items: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load WHO guideline limits into Supabase.")
    parser.add_argument(
        "--csv",
        default="data/WHO-guidelines/WHO_GAQG_2021_pollutant_limits.csv",
        help="Path to the WHO guideline CSV file.",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Override source label for all rows (default uses CSV source column).",
    )
    parser.add_argument("--batch-size", type=int, default=200, help="Rows per upsert batch.")
    return parser.parse_args()


def load_csv(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [row for row in reader]


def build_rows(records: List[Dict[str, Any]], source_override: Optional[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for record in records:
        pollutant_raw = record.get("pollutant", "")
        averaging_time_raw = record.get("averaging_time", "")
        if not pollutant_raw or not averaging_time_raw:
            continue
        pollutant = normalize_pollutant(pollutant_raw)
        averaging_label, averaging_interval = parse_averaging_time(averaging_time_raw)
        if not averaging_label:
            continue
        uom = record.get("unit") or ""
        notes = record.get("notes")
        source = source_override or record.get("source")
        for level in LEVEL_COLUMNS:
            value_raw = (record.get(level) or "").strip()
            if not value_raw:
                continue
            try:
                limit_value = float(value_raw)
            except ValueError:
                continue
            rows.append(
                {
                    "pollutant": pollutant,
                    "averaging_period_label": averaging_label,
                    "averaging_period_interval": averaging_interval,
                    "level_label": level,
                    "limit_value": limit_value,
                    "uom": uom,
                    "source": source,
                    "notes": notes,
                }
            )
    return rows


def main() -> int:
    args = parse_args()
    supabase_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SB_SECRET_KEY")
    if not supabase_url or not service_role_key:
        print("Missing SUPABASE_URL or SB_SECRET_KEY.", file=sys.stderr)
        return 1

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 1

    records = load_csv(csv_path)
    rows = build_rows(records, args.source)
    if not rows:
        print("No guideline rows parsed from the CSV.", file=sys.stderr)
        return 1

    client: Client = create_supabase_client(supabase_url, service_role_key)
    schemas = SupabaseSchemas.from_client(client)
    total = 0
    for batch in chunked(rows, max(1, args.batch_size)):
        schemas.core.table("uk_aq_guidelines").upsert(
            batch,
            on_conflict="pollutant,averaging_period_label,level_label,source",
        ).execute()
        total += len(batch)

    print(f"Loaded {total} guideline rows into uk_aq_guidelines.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
