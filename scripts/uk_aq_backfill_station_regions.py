#!/usr/bin/env python3
"""
Backfill stations.region using OS Open Names GB GPKG lookups.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv
from supabase import Client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.uk_aq_supabase import SupabaseSchemas, create_supabase_client
from shapely.geometry import Point
from shapely.ops import transform as shapely_transform
from shapely import wkb as shapely_wkb
from pyproj import Transformer

from uk_aq_enrich_station_names import (
    DEFAULT_GB_GPKG_PATH,
    NI_BBOX,
    OpenNamesLookup,
    _coerce_float,
    _ensure_gb_gpkg,
    _in_bbox,
    _parse_geometry_coords,
)

load_dotenv()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill stations.region using OS Open Names GB lookups."
    )
    parser.add_argument("--limit", type=int, default=0, help="Max stations to process (0 = no limit).")
    parser.add_argument("--page-size", type=int, default=1000, help="Supabase page size (default: 1000).")
    parser.add_argument(
        "--gb-search-radius-m",
        type=float,
        default=5000.0,
        help="Search radius in meters for OS Open Names lookups (default: 5000).",
    )
    parser.add_argument(
        "--max-distance-m",
        type=float,
        default=None,
        help="Optional max distance in meters for region matches.",
    )
    parser.add_argument(
        "--gb-gpkg-path",
        default=None,
        help=(
            "Path to the OS Open Names GB GPKG (default: "
            "UK_AQ_OS_OPEN_NAMES_GB_LOCAL_PATH or"
            f" {DEFAULT_GB_GPKG_PATH})."
        ),
    )
    parser.add_argument(
        "--lad-gpkg-path",
        default=os.getenv("UK_AQ_LAD_GPKG_PATH"),
        help="Optional path to LAD GPKG (LAD_MAY_2025_UK_BGC_V2...). If provided, regions/la_code come from LAD polygons.",
    )
    parser.add_argument(
        "--gb-gpkg-dropbox-path",
        default=None,
        help="Dropbox path for the GB GPKG (optional).",
    )
    parser.add_argument(
        "--download-gb-gpkg",
        action="store_true",
        help="Download the GB GPKG from Dropbox if missing.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write region updates back to Supabase.",
    )
    parser.add_argument(
        "--apply-batch-size",
        type=int,
        default=200,
        help="Batch size for region updates (default: 200).",
    )
    return parser.parse_args()


def _default_gb_gpkg_path() -> str:
    """Prefer the locally cached GPKG path from .env when available."""
    return os.getenv("UK_AQ_OS_OPEN_NAMES_GB_LOCAL_PATH") or DEFAULT_GB_GPKG_PATH


def _extract_wkb_from_gpkg(blob: bytes) -> Optional[bytes]:
    """Strip GeoPackage header and return WKB payload."""
    if not blob or len(blob) < 8:
        return None
    if blob[0:2] != b"GP":
        return None
    flags = blob[3]
    envelope_indicator = (flags >> 1) & 0x07
    envelope_sizes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    envelope_bytes = envelope_sizes.get(envelope_indicator)
    if envelope_bytes is None:
        return None
    wkb_offset = 8 + envelope_bytes
    if len(blob) < wkb_offset + 1:
        return None
    return blob[wkb_offset:]


class LADLookup:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.table = self._resolve_table()
        self.geom_col, self.srs_id = self._resolve_geometry()
        self.code_col = self._resolve_column(("LAD25CD",))
        self.name_col = self._resolve_column(("LAD25NM",))
        if not (self.geom_col and self.code_col and self.name_col):
            raise RuntimeError("LAD GPKG missing required columns (geom/code/name).")
        target_srid = self.srs_id or 4326
        self.to_wgs84 = Transformer.from_crs(target_srid, 4326, always_xy=True)
        self.polygons: List[Tuple[str, str, Any, Tuple[float, float, float, float]]] = []
        self._load_polygons()

    def close(self) -> None:
        self.conn.close()

    def _resolve_table(self) -> str:
        row = self.conn.execute("select table_name from gpkg_contents where data_type='features'").fetchone()
        if not row:
            raise RuntimeError("No feature table found in LAD GPKG.")
        return row[0]

    def _resolve_geometry(self) -> Tuple[Optional[str], Optional[int]]:
        row = self.conn.execute(
            "select column_name, srs_id from gpkg_geometry_columns where table_name = ?",
            (self.table,),
        ).fetchone()
        if not row:
            return None, None
        return row[0], int(row[1]) if row[1] is not None else None

    def _resolve_column(self, candidates: Sequence[str]) -> Optional[str]:
        rows = self.conn.execute(f"pragma table_info({self.table})").fetchall()
        lower_map = {row[1].lower(): row[1] for row in rows}
        for cand in candidates:
            if cand.lower() in lower_map:
                return lower_map[cand.lower()]
        return None

    def _load_polygons(self) -> None:
        cursor = self.conn.execute(
            f"select {self.code_col}, {self.name_col}, {self.geom_col} from {self.table}"
        )
        for row in cursor.fetchall():
            geom_blob = row[self.geom_col]
            if not geom_blob:
                continue
            wkb = _extract_wkb_from_gpkg(geom_blob)
            if not wkb:
                continue
            try:
                geom = shapely_wkb.loads(wkb)
            except Exception:
                continue
            try:
                geom_wgs = shapely_transform(self.to_wgs84.transform, geom)
            except Exception:
                continue
            bounds = geom_wgs.bounds
            code = str(row[self.code_col])
            name = str(row[self.name_col])
            self.polygons.append((code, name, geom_wgs, bounds))

    def lookup(self, lon: float, lat: float) -> Optional[Tuple[str, str]]:
        point = Point(lon, lat)
        for code, name, geom, bounds in self.polygons:
            minx, miny, maxx, maxy = bounds
            if not (minx <= lon <= maxx and miny <= lat <= maxy):
                continue
            if geom.contains(point) or geom.intersects(point):
                return name, code
        return None


def _fetch_stations(page_size: int) -> List[Dict[str, Any]]:
    supabase_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SB_SECRET_KEY")
    if not supabase_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SB_SECRET_KEY.")
    client: Client = create_supabase_client(supabase_url, service_role_key)
    schemas = SupabaseSchemas.from_client(client)
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        response = (
            schemas.core.table("stations")
            .select("id,station_ref,label,region,geometry")
            .is_("region", "null")
            .order("id")
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = response.data if hasattr(response, "data") else response.get("data")
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def _resolve_region(matches: Sequence[Dict[str, Any]], max_distance_m: Optional[float]) -> Optional[str]:
    for match in matches:
        region = match.get("region")
        if not region:
            continue
        distance_m = match.get("distance_m")
        if max_distance_m is not None and distance_m is not None:
            distance_value = _coerce_float(distance_m)
            if distance_value is not None and distance_value > max_distance_m:
                continue
        return str(region)
    return None


def _apply_updates(updates: List[Dict[str, Any]], batch_size: int) -> int:
    if not updates:
        return 0
    supabase_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SB_SECRET_KEY")
    if not supabase_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SB_SECRET_KEY.")
    client: Client = create_supabase_client(supabase_url, service_role_key)
    schemas = SupabaseSchemas.from_client(client)
    applied = 0
    for idx, update in enumerate(updates, start=1):
        station_id = update.get("id")
        region = update.get("region")
        if station_id is None or not region:
            continue
        la_code = update.get("la_code")
        payload = {"region": region}
        if la_code:
            payload["la_code"] = la_code
        response = (
            schemas.core.table("stations")
            .update(payload)
            .eq("id", station_id)
            .execute()
        )
        error = getattr(response, "error", None)
        if error:
            raise RuntimeError(f"Region update failed for id={station_id}: {error}")
        data = getattr(response, "data", None) or []
        if not data:
            continue
        applied += 1
        if idx % max(1, batch_size) == 0:
            print(".", end="", flush=True)
    if applied >= max(1, batch_size):
        print()
    return applied


def main() -> int:
    args = parse_args()
    if not args.gb_gpkg_path:
        args.gb_gpkg_path = _default_gb_gpkg_path()
    print(f"Using GB GPKG: {args.gb_gpkg_path}")
    gb_path = _ensure_gb_gpkg(args)
    gb_lookup = OpenNamesLookup(gb_path)

    lad_lookup: Optional[LADLookup] = None
    if args.lad_gpkg_path:
        lad_path = Path(args.lad_gpkg_path).expanduser()
        print(f"Using LAD GPKG: {lad_path}")
        lad_lookup = LADLookup(lad_path)

    stations = _fetch_stations(args.page_size)
    processed = 0
    updates: List[Dict[str, Any]] = []
    dots_printed = 0
    for station in stations:
        if args.limit and processed >= args.limit:
            break
        coords = _parse_geometry_coords(station.get("geometry"))
        if coords is None:
            continue
        lon, lat = coords
        lad_region = None
        lad_code = None
        if lad_lookup is not None:
            lad_result = lad_lookup.lookup(lon, lat)
            if lad_result:
                lad_region, lad_code = lad_result
        if lad_region is not None:
            updates.append({"id": station.get("id"), "region": lad_region, "la_code": lad_code})
        else:
            if _in_bbox(lon, lat, NI_BBOX):
                continue
            matches = gb_lookup.nearest_matches(
                lon,
                lat,
                limit=5,
                search_radius_m=args.gb_search_radius_m,
                max_candidates=None,
            )
            region = _resolve_region(matches, args.max_distance_m)
            if region:
                updates.append({"id": station.get("id"), "region": region})
        processed += 1
        if processed % 50 == 0:
            print(".", end="", flush=True)
            dots_printed += 1

    gb_lookup.close()
    if lad_lookup is not None:
        lad_lookup.close()

    if dots_printed:
        print()

    print(f"Stations scanned={processed}, region updates proposed={len(updates)}")
    if not args.apply:
        for entry in updates:
            station_id = entry.get("id")
            region = entry.get("region")
            print(f"id={station_id}, proposed_region={region}")
    if args.apply:
        applied = _apply_updates(updates, args.apply_batch_size)
        print(f"Region updates applied={applied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
