#!/usr/bin/env python3
"""
Load PCON boundaries into Aiven PostGIS.

Requires:
- PCON_AIVEN_PG_DSN
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv()


def polygon_to_wkt(coords: List[List[List[float]]]) -> str:
    rings = []
    for ring in coords:
        points = ", ".join(f"{point[0]} {point[1]}" for point in ring)
        rings.append(f"({points})")
    return f"({', '.join(rings)})"


def multipolygon_to_wkt(coords: List[List[List[List[float]]]]) -> str:
    polygons = [polygon_to_wkt(poly) for poly in coords]
    return f"MULTIPOLYGON({', '.join(polygons)})"


def geometry_to_wkt(geometry: Dict[str, Any]) -> Optional[str]:
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates")
    if geom_type == "Polygon":
        return f"MULTIPOLYGON({polygon_to_wkt(coords)})"
    if geom_type == "MultiPolygon":
        return multipolygon_to_wkt(coords)
    return None


def chunked(items: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def load_geojson(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load PCON boundaries into Aiven.")
    parser.add_argument("--geojson", required=True, help="Path to a GeoJSON boundary file.")
    parser.add_argument("--pcon-version", required=True, help="Boundary dataset version (e.g., 2024).")
    parser.add_argument("--code-field", default="PCON24CD", help="GeoJSON property for PCON code.")
    parser.add_argument("--name-field", default="PCON24NM", help="GeoJSON property for PCON name.")
    parser.add_argument("--batch-size", type=int, default=10, help="Rows per upsert batch.")
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.2,
        help="Sleep between batches to reduce DB load (seconds).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Max retries per batch on transient errors.",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=2.0,
        help="Base backoff seconds between retries.",
    )
    parser.add_argument(
        "--skip-if-exists",
        action="store_true",
        help="Skip boundary uploads if the target PCON version already exists.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    aiven_dsn = os.getenv("PCON_AIVEN_PG_DSN")
    if not aiven_dsn:
        print("Missing PCON_AIVEN_PG_DSN.", file=sys.stderr)
        return 1

    geojson_path = Path(args.geojson)
    if not geojson_path.exists():
        print(f"GeoJSON not found: {geojson_path}", file=sys.stderr)
        return 1

    payload = load_geojson(geojson_path)
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        print("GeoJSON does not contain a FeatureCollection.", file=sys.stderr)
        return 1

    conn = psycopg2.connect(aiven_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            if args.skip_if_exists:
                cursor.execute(
                    "select 1 from pcon_boundaries where pcon_version = %s limit 1",
                    (args.pcon_version,),
                )
                if cursor.fetchone():
                    print(f"Boundaries already exist for {args.pcon_version}; skipping upload.")
                    return 0

        rows: List[Dict[str, Any]] = []
        skipped = 0
        for feature in features:
            geometry = feature.get("geometry") if isinstance(feature, dict) else None
            props = feature.get("properties") if isinstance(feature, dict) else None
            if not geometry or not props:
                skipped += 1
                continue
            pcon_code = props.get(args.code_field)
            if not pcon_code:
                skipped += 1
                continue
            wkt = geometry_to_wkt(geometry)
            if not wkt:
                skipped += 1
                continue
            rows.append(
                {
                    "pcon_code": str(pcon_code),
                    "pcon_name": props.get(args.name_field),
                    "pcon_version": args.pcon_version,
                    "geometry": f"SRID=4326;{wkt}",
                }
            )

        if not rows:
            print("No boundaries parsed from the GeoJSON file.", file=sys.stderr)
            return 1

        values = [
            (
                row["pcon_code"],
                row.get("pcon_name"),
                row["pcon_version"],
                row["geometry"],
            )
            for row in rows
        ]
        query = (
            "insert into pcon_boundaries (pcon_code, pcon_name, pcon_version, geometry) "
            "values %s "
            "on conflict (pcon_code, pcon_version) do update "
            "set pcon_name = excluded.pcon_name, geometry = excluded.geometry"
        )
        template = "(%s, %s, %s, ST_GeomFromEWKT(%s))"

        print("Uploading boundaries", end="", flush=True)
        for batch in chunked(values, max(1, args.batch_size)):
            for attempt in range(1, max(1, args.max_retries) + 2):
                try:
                    with conn.cursor() as cursor:
                        execute_values(cursor, query, batch, template=template)
                    print(".", end="", flush=True)
                    break
                except Exception as exc:
                    if attempt >= max(1, args.max_retries) + 1:
                        print()
                        raise
                    print("!", end="", flush=True)
                    print(
                        f"\nRetrying batch (attempt {attempt}/{args.max_retries}) due to error: {exc}",
                        file=sys.stderr,
                    )
                    time.sleep(max(0.0, args.retry_backoff_seconds) * attempt)
            if args.sleep_seconds:
                time.sleep(max(0.0, args.sleep_seconds))
        print()

        print(f"Loaded {len(rows)} boundary rows (skipped {skipped}).")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
