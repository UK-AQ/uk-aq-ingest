#!/usr/bin/env python3
"""
Probe OSNI Gazetteer place names for stations missing station_name.

Example:
  python3 scripts/uk_aq_enrich_station_names.py --limit 10 --matches 5
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sqlite3
import struct
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from dotenv import load_dotenv
from supabase import Client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.uk_aq_supabase import SupabaseSchemas, create_supabase_client

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
if _ENV_PATH.exists():
    load_dotenv(dotenv_path=_ENV_PATH)
else:
    load_dotenv()

LOG = logging.getLogger("uk_aq_osni_probe")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("postgrest").setLevel(logging.WARNING)
logging.getLogger("supabase").setLevel(logging.WARNING)

DEFAULT_PLACENAMES_GEOJSON_PATH = (
    "data/geojson/OSNI/osni_open_data_-_gazetteer_-_place_names.geojson"
)
DEFAULT_STREETNAMES_GEOJSON_PATH = (
    "data/geojson/OSNI/osni_open_data_-_gazetteer_-_streetnames.geojson"
)
DEFAULT_GB_GPKG_PATH = "data/gpkg/OS/os_open_names_gpkg/Data/opname_gb.gpkg"

DROPBOX_TOKEN_URL = "https://api.dropbox.com/oauth2/token"
DROPBOX_DOWNLOAD_URL = "https://content.dropboxapi.com/2/files/download"

try:
    from pyproj import Transformer
except ImportError:  # pragma: no cover - optional dependency for projected GPKG.
    Transformer = None

NI_BBOX = {
    "west": -8.4,
    "south": 53.9,
    "east": -5.3,
    "north": 55.5,
}

GB_PLACE_TOKENS = (
    "city",
    "town",
    "village",
    "hamlet",
    "suburban",
    "suburb",
    "locality",
    "settlement",
    "district",
    "neighbourhood",
    "neighborhood",
)

GB_STREET_TOKENS = (
    "road",
    "street",
    "lane",
    "avenue",
    "drive",
    "way",
    "close",
    "place",
    "court",
    "crescent",
    "terrace",
    "row",
    "grove",
    "square",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview OSNI Gazetteer place names for stations without station_name."
    )
    parser.add_argument(
        "--geojson",
        default=DEFAULT_PLACENAMES_GEOJSON_PATH,
        help=(
            "OSNI Gazetteer placenames GeoJSON "
            f"(default: {DEFAULT_PLACENAMES_GEOJSON_PATH})."
        ),
    )
    parser.add_argument(
        "--streetnames-geojson",
        default=DEFAULT_STREETNAMES_GEOJSON_PATH,
        help=(
            "OSNI Gazetteer streetnames GeoJSON "
            f"(default: {DEFAULT_STREETNAMES_GEOJSON_PATH})."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Number of stations to inspect (0 means no limit).",
    )
    parser.add_argument(
        "--matches",
        type=int,
        default=5,
        help="Number of nearby names to list per station (default: 5).",
    )
    parser.add_argument(
        "--max-distance-m",
        type=float,
        default=None,
        help="Optional max distance in meters for matches.",
    )
    parser.add_argument(
        "--no-ni-filter",
        action="store_true",
        help="Also attempt OSNI matching for non-NI stations (debugging only).",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=1000,
        help="Batch size for Supabase pagination (default: 1000).",
    )
    parser.add_argument(
        "--gb-gpkg-path",
        default=DEFAULT_GB_GPKG_PATH,
        help=f"Path to the OS Open Names GB GPKG (default: {DEFAULT_GB_GPKG_PATH}).",
    )
    parser.add_argument(
        "--gb-gpkg-dropbox-path",
        help="Dropbox path for the GB GPKG (default: UK_AQ_OS_OPEN_NAMES_GB_DROPBOX_PATH or --gb-gpkg-path).",
    )
    parser.add_argument(
        "--download-gb-gpkg",
        action="store_true",
        help="Download the GB GPKG from Dropbox if missing.",
    )
    parser.add_argument(
        "--include-gb",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include GB stations using OS Open Names lookups (default: true).",
    )
    parser.add_argument(
        "--gb-search-radius-m",
        type=float,
        default=5000.0,
        help="Search radius in meters for OS Open Names lookups (default: 5000).",
    )
    parser.add_argument(
        "--include-pollutants",
        action="store_true",
        help="Include pollutant list for each station (uses timeseries/phenomena).",
    )
    parser.add_argument(
        "--include-latest",
        action="store_true",
        help="Include latest observation per station (timeseries/observations lookup).",
    )
    parser.add_argument(
        "--output-format",
        choices=("summary", "json"),
        default="summary",
        help="Output format (summary or json).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Update station_name in Supabase for stations with proposed names.",
    )
    parser.add_argument(
        "--apply-batch-size",
        type=int,
        default=200,
        help="Batch size for station_name updates (default: 200).",
    )
    return parser.parse_args()


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _supabase_project_ref(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    match = re.search(r"https?://([^./]+)\.supabase\.(co|in)", url)
    if match:
        return match.group(1)
    return None


def _chunked(items: Sequence[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    if size <= 0:
        raise ValueError("chunk size must be > 0")
    for idx in range(0, len(items), size):
        yield list(items[idx : idx + size])


def _parse_wkb_point(hex_value: str) -> Optional[Tuple[float, float]]:
    try:
        data = bytes.fromhex(hex_value)
    except ValueError:
        return None
    if len(data) < 1 + 4 + 16:
        return None
    byte_order = data[0]
    if byte_order not in (0, 1):
        return None
    endian = "<" if byte_order == 1 else ">"
    type_val = struct.unpack(endian + "I", data[1:5])[0]
    has_srid = bool(type_val & 0x20000000)
    geom_type = type_val & 0xFF
    if geom_type != 1:
        return None
    offset = 5 + (4 if has_srid else 0)
    if len(data) < offset + 16:
        return None
    try:
        lon, lat = struct.unpack(endian + "dd", data[offset : offset + 16])
    except struct.error:
        return None
    lon_value = _coerce_float(lon)
    lat_value = _coerce_float(lat)
    if lon_value is None or lat_value is None:
        return None
    return lon_value, lat_value


def _parse_wkb_point_bytes(data: bytes) -> Optional[Tuple[float, float]]:
    if len(data) < 21:
        return None
    byte_order = data[0]
    if byte_order not in (0, 1):
        return None
    endian = "<" if byte_order == 1 else ">"
    geom_type = struct.unpack(endian + "I", data[1:5])[0] & 0xFF
    if geom_type != 1:
        return None
    try:
        x, y = struct.unpack(endian + "dd", data[5:21])
    except struct.error:
        return None
    lon_value = _coerce_float(x)
    lat_value = _coerce_float(y)
    if lon_value is None or lat_value is None:
        return None
    return lon_value, lat_value


def _parse_gpkg_point(blob: Optional[bytes]) -> Optional[Tuple[float, float]]:
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
    if len(blob) < wkb_offset + 21:
        return None
    return _parse_wkb_point_bytes(blob[wkb_offset:])


def _parse_geometry_coords(value: Any) -> Optional[Tuple[float, float]]:
    if value is None:
        return None
    if isinstance(value, dict):
        coords = value.get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            lon = _coerce_float(coords[0])
            lat = _coerce_float(coords[1])
            if lon is not None and lat is not None:
                return lon, lat
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        lon = _coerce_float(value[0])
        lat = _coerce_float(value[1])
        if lon is not None and lat is not None:
            return lon, lat
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        match = re.search(
            r"POINT\\s*\\(\\s*(-?\\d+(?:\\.\\d+)?)\\s+(-?\\d+(?:\\.\\d+)?)\\s*\\)",
            text,
        )
        if match:
            lon = _coerce_float(match.group(1))
            lat = _coerce_float(match.group(2))
            if lon is not None and lat is not None:
                return lon, lat
        if re.fullmatch(r"[0-9A-Fa-f]+", text):
            return _parse_wkb_point(text)
    return None


def _in_bbox(lon: float, lat: float, bbox: Dict[str, float]) -> bool:
    return bbox["west"] <= lon <= bbox["east"] and bbox["south"] <= lat <= bbox["north"]


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _load_osni_places(path: str) -> List[Tuple[str, float, float]]:
    geojson_path = Path(path)
    if not geojson_path.exists():
        LOG.warning("GeoJSON not found (skipping): %s", path)
        return []
    with geojson_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    features = payload.get("features", []) if isinstance(payload, dict) else []
    places = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        name = props.get("PLACENAME") or props.get("name")
        coords = _parse_geometry_coords(geometry)
        if not name or coords is None:
            continue
        lon, lat = coords
        places.append((str(name), lon, lat))
    return places


def _load_osni_streetnames(path: str) -> List[Tuple[str, Optional[str], float, float]]:
    geojson_path = Path(path)
    if not geojson_path.exists():
        LOG.warning("GeoJSON not found (skipping): %s", path)
        return []
    with geojson_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    features = payload.get("features", []) if isinstance(payload, dict) else []
    streets = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        name = props.get("STREETNAME") or props.get("name")
        coords = _parse_geometry_coords(geometry)
        if not name or coords is None:
            continue
        lon, lat = coords
        ref = props.get("USRN")
        streets.append((str(name), str(ref) if ref is not None else None, lon, lat))
    return streets


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
            .select(
                "id,station_ref,label,station_name,station_type,region,geometry,connector_id,service_ref,"
                "first_seen_at,last_seen_at,removed_at"
            )
            .is_("station_name", "null")
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


def _fetch_station_pollutants(station_ids: Sequence[int]) -> Dict[int, List[str]]:
    if not station_ids:
        return {}
    supabase_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SB_SECRET_KEY")
    if not supabase_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SB_SECRET_KEY.")
    client: Client = create_supabase_client(supabase_url, service_role_key)
    schemas = SupabaseSchemas.from_client(client)
    response = (
        schemas.core.table("timeseries")
        .select("station_id,phenomenon_id")
        .in_("station_id", list(station_ids))
        .execute()
    )
    rows = response.data if hasattr(response, "data") else response.get("data")
    if not rows:
        return {}
    station_to_phenomenon: Dict[int, List[int]] = {}
    phenomenon_ids = set()
    for row in rows:
        try:
            station_id = int(row.get("station_id"))
            phenomenon_id = int(row.get("phenomenon_id"))
        except (TypeError, ValueError):
            continue
        station_to_phenomenon.setdefault(station_id, []).append(phenomenon_id)
        phenomenon_ids.add(phenomenon_id)
    if not phenomenon_ids:
        return {}
    response = (
        schemas.core.table("phenomena")
        .select("id,label,notation,source_label")
        .in_("id", list(phenomenon_ids))
        .execute()
    )
    rows = response.data if hasattr(response, "data") else response.get("data")
    label_by_id: Dict[int, str] = {}
    for row in rows or []:
        try:
            phen_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        label = row.get("label") or row.get("notation") or row.get("source_label")
        if label:
            label_by_id[phen_id] = str(label)
    result: Dict[int, List[str]] = {}
    for station_id, phen_ids in station_to_phenomenon.items():
        labels = [label_by_id.get(pid) for pid in phen_ids]
        unique = sorted({label for label in labels if label})
        if unique:
            result[station_id] = unique
    return result


def _fetch_station_latest_observations(
    station_ids: Sequence[int], max_rows: int = 20000
) -> Dict[int, Dict[str, Dict[str, Any]]]:
    if not station_ids:
        return {}
    supabase_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SB_SECRET_KEY")
    if not supabase_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SB_SECRET_KEY.")
    client: Client = create_supabase_client(supabase_url, service_role_key)
    schemas = SupabaseSchemas.from_client(client)
    response = (
        schemas.core.table("timeseries")
        .select("id,station_id,phenomenon_id")
        .in_("station_id", list(station_ids))
        .execute()
    )
    rows = response.data if hasattr(response, "data") else response.get("data")
    if not rows:
        return {}
    timeseries_to_station: Dict[int, int] = {}
    timeseries_to_phenomenon: Dict[int, int] = {}
    timeseries_ids = []
    phenomenon_ids = set()
    for row in rows:
        try:
            ts_id = int(row.get("id"))
            station_id = int(row.get("station_id"))
            phenomenon_id = int(row.get("phenomenon_id"))
        except (TypeError, ValueError):
            continue
        timeseries_to_station[ts_id] = station_id
        timeseries_to_phenomenon[ts_id] = phenomenon_id
        timeseries_ids.append(ts_id)
        phenomenon_ids.add(phenomenon_id)
    if not timeseries_ids:
        return {}
    label_by_id: Dict[int, str] = {}
    if phenomenon_ids:
        response = (
            schemas.core.table("phenomena")
            .select("id,label,notation,source_label")
            .in_("id", list(phenomenon_ids))
            .execute()
        )
        rows = response.data if hasattr(response, "data") else response.get("data")
        for row in rows or []:
            try:
                phen_id = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            label = row.get("label") or row.get("notation") or row.get("source_label")
            if label:
                label_by_id[phen_id] = str(label)
    response = (
        schemas.core.table("observations")
        .select("timeseries_id,observed_at,value")
        .in_("timeseries_id", timeseries_ids)
        .order("observed_at", desc=True)
        .limit(max_rows)
        .execute()
    )
    rows = response.data if hasattr(response, "data") else response.get("data")
    latest_by_station: Dict[int, Dict[str, Dict[str, Any]]] = {}
    for row in rows or []:
        try:
            ts_id = int(row.get("timeseries_id"))
        except (TypeError, ValueError):
            continue
        station_id = timeseries_to_station.get(ts_id)
        if station_id is None:
            continue
        phenomenon_id = timeseries_to_phenomenon.get(ts_id)
        if phenomenon_id is None:
            continue
        phen_label = label_by_id.get(phenomenon_id)
        if not phen_label:
            continue
        station_latest = latest_by_station.setdefault(station_id, {})
        if phen_label in station_latest:
            continue
        station_latest[phen_label] = {
            "observed_at": row.get("observed_at"),
            "value": row.get("value"),
            "timeseries_id": ts_id,
            "phenomenon_id": phenomenon_id,
        }
    return latest_by_station


def _build_matches(
    station_lon: float,
    station_lat: float,
    places: Sequence[Tuple[str, float, float]],
    max_distance_m: Optional[float],
    limit: int,
    name_key: str = "place_name",
) -> List[Dict[str, Any]]:
    matches = []
    for name, lon, lat in places:
        distance_m = _haversine_m(station_lon, station_lat, lon, lat)
        if max_distance_m is not None and distance_m > max_distance_m:
            continue
        entry = {
            name_key: name,
            "distance_m": round(distance_m, 1),
            "lon": lon,
            "lat": lat,
        }
        matches.append(entry)
    matches.sort(key=lambda item: item["distance_m"])
    return matches[:limit]


def _build_street_matches(
    station_lon: float,
    station_lat: float,
    streets: Sequence[Tuple[str, Optional[str], float, float]],
    max_distance_m: Optional[float],
    limit: int,
) -> List[Dict[str, Any]]:
    matches = []
    for name, ref, lon, lat in streets:
        distance_m = _haversine_m(station_lon, station_lat, lon, lat)
        if max_distance_m is not None and distance_m > max_distance_m:
            continue
        entry = {
            "street_name": name,
            "distance_m": round(distance_m, 1),
            "lon": lon,
            "lat": lat,
        }
        if ref is not None:
            entry["usrn"] = ref
        matches.append(entry)
    matches.sort(key=lambda item: item["distance_m"])
    return matches[:limit]


def _title_case(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        return None
    words = []
    for word in cleaned.split(" "):
        if not word:
            continue
        segments = []
        for segment in word.split("-"):
            if not segment:
                segments.append(segment)
                continue
            if "'" in segment:
                parts = segment.split("'")
                first = parts[0]
                if first:
                    first = first[0].upper() + first[1:].lower()
                else:
                    first = ""
                rest_parts = []
                for part in parts[1:]:
                    if not part:
                        rest_parts.append(part)
                        continue
                    if len(parts[0]) == 1:
                        rest_parts.append(part[0].upper() + part[1:].lower())
                    else:
                        rest_parts.append(part.lower())
                segments.append("'".join([first] + rest_parts))
            else:
                segments.append(segment[0].upper() + segment[1:].lower())
        words.append("-".join(segments))
    return " ".join(words)


def _proposed_station_name(
    place_matches: Sequence[Dict[str, Any]],
    street_matches: Sequence[Dict[str, Any]],
) -> Optional[str]:
    place_name = place_matches[0].get("place_name") if place_matches else None
    street_name = street_matches[0].get("street_name") if street_matches else None
    place_title = _title_case(place_name)
    street_title = _title_case(street_name)
    if place_title and street_title:
        return f"{place_title} - {street_title}"
    return None


def _proposed_gb_station_name(
    place_matches: Sequence[Dict[str, Any]],
    street_matches: Sequence[Dict[str, Any]],
) -> Optional[str]:
    place_name = place_matches[0].get("name") if place_matches else None
    street_entry = street_matches[0] if street_matches else {}
    street_name = street_entry.get("name")
    place_title = _title_case(place_name)
    street_title = _title_case(street_name)
    local_type = str(street_entry.get("local_type") or "").strip().lower()
    if "postcode" in local_type and street_name:
        street_title = str(street_name)
    if place_title and street_title:
        return f"{place_title} - {street_title}"
    return None


def _format_latest_summary(latest: Dict[str, Dict[str, Any]]) -> str:
    if not latest:
        return ""
    parts = []
    for pollutant in sorted(latest.keys()):
        entry = latest.get(pollutant) or {}
        value = entry.get("value")
        observed_at = entry.get("observed_at")
        parts.append(f"{pollutant}:{value}@{observed_at}")
    return "; ".join(parts)


def _resolve_region_from_gb_matches(
    matches: Sequence[Dict[str, Any]], max_distance_m: Optional[float] = None
) -> Optional[str]:
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


def build_station_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    station = payload.get("station") or {}
    lat = station.get("station_lat")
    lon = station.get("station_lon")
    return {
        "label": station.get("label"),
        "coordinates": f"{lat} {lon}",
        "proposed_station_name": payload.get("proposed_station_name"),
    }


def _classify_gb_match(match: Dict[str, Any]) -> str:
    local_type = str(match.get("local_type") or "").strip().lower()
    for token in GB_STREET_TOKENS:
        if token in local_type:
            return "street"
    for token in GB_PLACE_TOKENS:
        if token in local_type:
            return "place"
    return "other"


def _split_gb_matches(
    matches: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    place_matches: List[Dict[str, Any]] = []
    street_matches: List[Dict[str, Any]] = []
    other_matches: List[Dict[str, Any]] = []
    for match in matches:
        category = _classify_gb_match(match)
        if category == "street":
            street_matches.append(match)
        elif category == "place":
            place_matches.append(match)
        else:
            other_matches.append(match)
    return place_matches, street_matches, other_matches


def _short_place_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if "#" in text:
        return text.rsplit("#", 1)[-1]
    if "/" in text:
        return text.rsplit("/", 1)[-1]
    return text


def _extract_gb_place_matches(
    matches: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    places: Dict[str, Dict[str, Any]] = {}
    for match in matches:
        candidates: List[Tuple[str, str, Optional[str]]] = []
        populated = match.get("populated_place")
        if populated:
            candidates.append(("populated_place", str(populated), match.get("populated_place_type")))
        if not candidates and _classify_gb_match(match) == "place":
            name = match.get("name")
            if name:
                candidates.append(("match_name", str(name), match.get("local_type")))
        if not candidates:
            fallback = match.get("district_borough") or match.get("county_unitary")
            if fallback:
                candidates.append(("district_borough", str(fallback), None))
        for source, name, place_type in candidates:
            if name not in places or match["distance_m"] < places[name]["distance_m"]:
                entry = {
                    "name": name,
                    "distance_m": match["distance_m"],
                    "source": source,
                }
                short_type = _short_place_type(place_type)
                if short_type:
                    entry["place_type"] = short_type
                if match.get("region"):
                    entry["region"] = match.get("region")
                if match.get("country"):
                    entry["country"] = match.get("country")
                places[name] = entry
    return sorted(places.values(), key=lambda item: item["distance_m"])


def iter_station_payloads(args: argparse.Namespace):
    if args.download_gb_gpkg:
        _ensure_gb_gpkg(args)
    places = _load_osni_places(args.geojson)
    streets = _load_osni_streetnames(args.streetnames_geojson)
    LOG.info("Loaded %s OSNI place names.", len(places))
    LOG.info("Loaded %s OSNI street names.", len(streets))
    gb_lookup: Optional[OpenNamesLookup] = None
    try:
        if args.include_gb:
            gb_path = _ensure_gb_gpkg(args)
            gb_lookup = OpenNamesLookup(gb_path)
            LOG.info(
                "Loaded OS Open Names GPKG table %s (srs_id=%s).",
                gb_lookup.table,
                gb_lookup.srs_id,
            )
            LOG.info(
                "OS Open Names columns: geom=%s lat=%s lon=%s rtree=%s name=%s",
                gb_lookup.geom_col,
                gb_lookup.lat_col,
                gb_lookup.lon_col,
                gb_lookup.rtree_table,
                gb_lookup.name_col,
            )
        else:
            LOG.info("Skipping GB lookups (--include-gb=false).")

        rows = _fetch_stations(args.page_size)
        if not rows:
            LOG.warning("No stations returned from Supabase.")
            return

        stations: List[Dict[str, Any]] = []
        missing_geometry = 0
        for row in rows:
            coords = _parse_geometry_coords(row.get("geometry"))
            if coords is None:
                missing_geometry += 1
                continue
            lon, lat = coords
            row["station_lon"] = lon
            row["station_lat"] = lat
            stations.append(row)

        LOG.info("Stations fetched=%s, with geometry=%s, missing geometry=%s.", len(rows), len(stations), missing_geometry)
        if not stations:
            LOG.warning("No stations with station_name null matched the filters.")
            return

        station_ids = [
            int(station["id"]) for station in stations if station.get("id") is not None
        ]
        summary_context_enabled = args.output_format == "summary" and not args.apply
        include_pollutants = args.include_pollutants or summary_context_enabled
        pollutant_map: Dict[int, List[str]] = {}
        if include_pollutants:
            pollutant_map = _fetch_station_pollutants(station_ids)
        include_latest = args.include_latest or summary_context_enabled
        latest_map: Dict[int, Dict[str, Any]] = {}
        if include_latest:
            latest_map = _fetch_station_latest_observations(station_ids)

        LOG.info("Inspecting %s stations with station_name null.", len(stations))
        output_count = 0
        for station in stations:
            if args.limit and output_count >= args.limit:
                break
            lon = station.get("station_lon")
            lat = station.get("station_lat")
            if lon is None or lat is None:
                continue
            ni_place_matches: List[Dict[str, Any]] = []
            ni_street_matches: List[Dict[str, Any]] = []
            gb_matches: List[Dict[str, Any]] = []
            gb_place_matches: List[Dict[str, Any]] = []
            gb_street_matches: List[Dict[str, Any]] = []
            gb_other_matches: List[Dict[str, Any]] = []
            proposed_region = None
            is_ni = _in_bbox(lon, lat, NI_BBOX)
            if is_ni:
                if places:
                    ni_place_matches = _build_matches(
                        lon,
                        lat,
                        places,
                        max_distance_m=args.max_distance_m,
                        limit=args.matches,
                        name_key="place_name",
                    )
                if streets:
                    ni_street_matches = _build_street_matches(
                        lon,
                        lat,
                        streets,
                        max_distance_m=args.max_distance_m,
                        limit=args.matches,
                    )
            if args.include_gb and not is_ni and gb_lookup is not None:
                gb_matches = gb_lookup.nearest_matches(
                    lon,
                    lat,
                    limit=args.matches,
                    search_radius_m=args.gb_search_radius_m,
                    max_candidates=None,
                )
                gb_place_matches = _extract_gb_place_matches(gb_matches)
                _, gb_street_matches, gb_other_matches = _split_gb_matches(gb_matches)
                proposed_region = _resolve_region_from_gb_matches(
                    gb_matches,
                    max_distance_m=args.max_distance_m,
                )
            proposed_name = None
            if is_ni:
                proposed_name = _proposed_station_name(ni_place_matches, ni_street_matches)
            elif args.include_gb:
                gb_street_fallback = gb_street_matches or gb_other_matches
                proposed_name = _proposed_gb_station_name(
                    gb_place_matches, gb_street_fallback
                )
            station_id = station.get("id")
            pollutants = pollutant_map.get(int(station_id), []) if station_id else []
            latest_obs = latest_map.get(int(station_id), {}) if station_id else {}
            payload = {
                "group": "ni_station" if is_ni else "gb_station",
                "station": station,
                "ni_place_matches": ni_place_matches,
                "ni_street_matches": ni_street_matches,
                "gb_matches": gb_matches,
                "gb_place_matches": gb_place_matches,
                "gb_street_matches": gb_street_matches,
                "gb_other_matches": gb_other_matches,
                "proposed_station_name": proposed_name,
                "proposed_region": proposed_region,
                "pollutants": pollutants if include_pollutants else [],
                "latest_observation": latest_obs if include_latest else {},
            }
            yield payload
            output_count += 1
    finally:
        if gb_lookup is not None:
            gb_lookup.close()


def _apply_station_name_updates(updates: Sequence[Dict[str, Any]], batch_size: int) -> int:
    if not updates:
        return 0
    supabase_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SB_SECRET_KEY")
    if not supabase_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SB_SECRET_KEY.")
    client: Client = create_supabase_client(supabase_url, service_role_key)
    schemas = SupabaseSchemas.from_client(client)
    applied = 0
    for chunk in _chunked(list(updates), batch_size):
        for update in chunk:
            station_id = update.get("id")
            if station_id is None:
                continue
            update_fields = {k: v for k, v in update.items() if k != "id"}
            if not update_fields:
                continue
            response = (
                schemas.core.table("stations")
                .update(update_fields)
                .eq("id", station_id)
                .execute()
            )
            error = getattr(response, "error", None)
            if error:
                raise RuntimeError(f"Station_name update failed for id={station_id}: {error}")
            data = getattr(response, "data", None) or []
            if not data:
                LOG.warning("Station_name update returned no rows for id=%s", station_id)
                continue
            applied += 1
    return applied


def _normalize_dropbox_path(path: str) -> str:
    cleaned = (path or "").strip()
    if not cleaned:
        return ""
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    if cleaned.startswith("/Apps/"):
        parts = cleaned.split("/", 3)
        if len(parts) >= 4:
            cleaned = f"/{parts[3]}"
    return cleaned


def _refresh_dropbox_access_token() -> str:
    app_key = os.getenv("DROPBOX_APP_KEY", "").strip()
    app_secret = os.getenv("DROPBOX_APP_SECRET", "").strip()
    refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN", "").strip()
    if not (app_key and app_secret and refresh_token):
        raise RuntimeError("Dropbox credentials are required.")
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": app_key,
        "client_secret": app_secret,
    }
    resp = requests.post(DROPBOX_TOKEN_URL, data=payload, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Dropbox token request failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Dropbox token response missing access_token.")
    return token


def _download_dropbox_file(dropbox_path: str, target_path: Path) -> None:
    access_token = _refresh_dropbox_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Dropbox-API-Arg": json.dumps({"path": dropbox_path}),
    }
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_suffix(target_path.suffix + ".partial")
    with requests.post(DROPBOX_DOWNLOAD_URL, headers=headers, stream=True, timeout=300) as resp:
        if resp.status_code >= 400:
            raise RuntimeError(f"Dropbox download failed ({resp.status_code}): {resp.text}")
        with temp_path.open("wb") as handle:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
    temp_path.replace(target_path)


def _ensure_gb_gpkg(args: argparse.Namespace) -> Path:
    path = Path(args.gb_gpkg_path)
    if path.exists():
        return path
    dropbox_path = args.gb_gpkg_dropbox_path or os.getenv(
        "UK_AQ_OS_OPEN_NAMES_GB_DROPBOX_PATH", ""
    )
    if dropbox_path:
        local_candidate = Path(dropbox_path).expanduser()
        if local_candidate.exists():
            return local_candidate
    if not (args.download_gb_gpkg or dropbox_path):
        raise FileNotFoundError(
            f"GB GPKG not found at {path}. Set --download-gb-gpkg or "
            "UK_AQ_OS_OPEN_NAMES_GB_DROPBOX_PATH to download from Dropbox."
        )
    if not dropbox_path:
        dropbox_path = str(path)
    if not dropbox_path.endswith(".gpkg"):
        dropbox_path = dropbox_path.rstrip("/") + "/opname_gb.gpkg"
    dropbox_path = _normalize_dropbox_path(dropbox_path)
    if not dropbox_path:
        raise RuntimeError("Dropbox path for GB GPKG is required.")
    LOG.info("Downloading GB GPKG from Dropbox: %s", dropbox_path)
    _download_dropbox_file(dropbox_path, path)
    LOG.info("Saved GB GPKG to %s", path)
    return path


class OpenNamesLookup:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.table = self._resolve_table()
        self.pk_col = self._resolve_primary_key()
        self.geom_col, self.srs_id = self._resolve_geometry()
        self.columns = self._list_columns()
        self.name_col = self._resolve_column(
            ("name1", "name", "name_1", "placename", "place_name")
        )
        self.local_type_col = self._resolve_column(
            ("local_type", "localtype", "type", "feature_type", "class")
        )
        self.populated_place_col = self._resolve_column(
            ("populated_place", "pop_place", "settlement", "town", "city")
        )
        self.populated_place_type_col = self._resolve_column(
            ("populated_place_type", "settlement_type", "town_type", "city_type")
        )
        self.district_borough_col = self._resolve_column(
            ("district_borough", "district", "borough")
        )
        self.county_unitary_col = self._resolve_column(
            ("county_unitary", "county", "unitary")
        )
        self.region_col = self._resolve_column(("region",))
        self.country_col = self._resolve_column(("country",))
        self.lat_col = self._resolve_column(("lat", "latitude"))
        self.lon_col = self._resolve_column(("lon", "long", "longitude"))
        self.rtree_table = self._resolve_rtree_table()
        self.to_dataset = None
        self.to_wgs84 = None
        if self.srs_id and self.srs_id != 4326 and not (self.lat_col and self.lon_col):
            if Transformer is None:
                raise RuntimeError(
                    "pyproj is required to use OS Open Names GPKG with non-4326 CRS."
                )
            self.to_dataset = Transformer.from_crs(4326, self.srs_id, always_xy=True)
            self.to_wgs84 = Transformer.from_crs(self.srs_id, 4326, always_xy=True)

    def close(self) -> None:
        self.conn.close()

    def _resolve_table(self) -> str:
        rows = self.conn.execute(
            "select table_name from gpkg_contents where data_type = 'features'"
        ).fetchall()
        if not rows:
            raise RuntimeError("No feature tables found in GPKG.")
        names = [row[0] for row in rows]
        for candidate in ("opname_gb", "opname", "open_names"):
            for name in names:
                if name.lower() == candidate:
                    return name
        for name in names:
            if "opname" in name.lower():
                return name
        return names[0]

    def _resolve_primary_key(self) -> str:
        rows = self.conn.execute(f"pragma table_info({self.table})").fetchall()
        for row in rows:
            if row[5] == 1:
                return row[1]
        return "rowid"

    def _resolve_geometry(self) -> Tuple[Optional[str], Optional[int]]:
        row = self.conn.execute(
            "select column_name, srs_id from gpkg_geometry_columns where table_name = ?",
            (self.table,),
        ).fetchone()
        if not row:
            return None, None
        return row[0], int(row[1]) if row[1] is not None else None

    def _list_columns(self) -> List[str]:
        rows = self.conn.execute(f"pragma table_info({self.table})").fetchall()
        return [row[1] for row in rows]

    def _resolve_column(self, candidates: Sequence[str]) -> Optional[str]:
        lower_map = {col.lower(): col for col in self.columns}
        for candidate in candidates:
            if candidate.lower() in lower_map:
                return lower_map[candidate.lower()]
        return None

    def _resolve_rtree_table(self) -> Optional[str]:
        if not self.geom_col:
            return None
        name = f"rtree_{self.table}_{self.geom_col}"
        row = self.conn.execute(
            "select name from sqlite_master where type = 'table' and name = ?",
            (name,),
        ).fetchone()
        return row[0] if row else None

    def _station_to_dataset(self, lon: float, lat: float) -> Tuple[float, float]:
        if self.srs_id and self.srs_id != 4326 and self.to_dataset is not None:
            x, y = self.to_dataset.transform(lon, lat)
            return float(x), float(y)
        return lon, lat

    def _dataset_to_wgs84(self, x: float, y: float) -> Tuple[float, float]:
        if self.srs_id and self.srs_id != 4326 and self.to_wgs84 is not None:
            lon, lat = self.to_wgs84.transform(x, y)
            return float(lon), float(lat)
        return x, y

    def nearest_matches(
        self,
        lon: float,
        lat: float,
        limit: int,
        search_radius_m: float,
        max_candidates: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if not self.name_col:
            LOG.warning("No name column found in OS Open Names table.")
            return []
        if self.lat_col and self.lon_col:
            return self._nearest_by_latlon(lon, lat, limit, search_radius_m)
        if not self.geom_col or not self.rtree_table:
            LOG.warning("OS Open Names lookup requires geometry + rtree.")
            return []
        x, y = self._station_to_dataset(lon, lat)
        minx, maxx, miny, maxy = self._bbox_for_search(x, y, lat, search_radius_m)
        ids = self._query_rtree(minx, maxx, miny, maxy, max_candidates)
        if not ids:
            return []
        rows = self._fetch_rows(ids)
        matches = []
        for row in rows:
            geom = row[self.geom_col] if self.geom_col else None
            coords = _parse_gpkg_point(geom)
            if coords is None:
                continue
            gx, gy = coords
            if self.srs_id and self.srs_id != 4326:
                distance_m = math.hypot(gx - x, gy - y)
                lon_out, lat_out = self._dataset_to_wgs84(gx, gy)
            else:
                distance_m = _haversine_m(lon, lat, gx, gy)
                lon_out, lat_out = gx, gy
            name = row[self.name_col]
            if not name:
                continue
            entry = {
                "name": str(name),
                "distance_m": round(distance_m, 1),
                "lon": lon_out,
                "lat": lat_out,
            }
            if self.local_type_col and row[self.local_type_col]:
                entry["local_type"] = row[self.local_type_col]
            if self.populated_place_col and row[self.populated_place_col]:
                entry["populated_place"] = row[self.populated_place_col]
            if self.populated_place_type_col and row[self.populated_place_type_col]:
                entry["populated_place_type"] = row[self.populated_place_type_col]
            if self.district_borough_col and row[self.district_borough_col]:
                entry["district_borough"] = row[self.district_borough_col]
            if self.county_unitary_col and row[self.county_unitary_col]:
                entry["county_unitary"] = row[self.county_unitary_col]
            if self.region_col and row[self.region_col]:
                entry["region"] = row[self.region_col]
            if self.country_col and row[self.country_col]:
                entry["country"] = row[self.country_col]
            matches.append(entry)
        matches.sort(key=lambda item: item["distance_m"])
        return matches[:limit]

    def _bbox_for_search(
        self, x: float, y: float, lat: float, search_radius_m: float
    ) -> Tuple[float, float, float, float]:
        if self.srs_id and self.srs_id != 4326:
            delta = search_radius_m
            return x - delta, x + delta, y - delta, y + delta
        lat_rad = math.radians(lat)
        meters_per_deg_lat = 111320.0
        meters_per_deg_lon = max(1.0, meters_per_deg_lat * math.cos(lat_rad))
        delta_lat = search_radius_m / meters_per_deg_lat
        delta_lon = search_radius_m / meters_per_deg_lon
        return x - delta_lon, x + delta_lon, y - delta_lat, y + delta_lat

    def _query_rtree(
        self, minx: float, maxx: float, miny: float, maxy: float, max_candidates: Optional[int]
    ) -> List[int]:
        if not self.rtree_table:
            return []
        if max_candidates is None:
            rows = self.conn.execute(
                f"select id from {self.rtree_table} where minx <= ? and maxx >= ? and miny <= ? and maxy >= ?",
                (maxx, minx, maxy, miny),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"select id from {self.rtree_table} where minx <= ? and maxx >= ? and miny <= ? and maxy >= ? limit ?",
                (maxx, minx, maxy, miny, max_candidates),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def _fetch_rows(self, ids: Sequence[int]) -> List[sqlite3.Row]:
        if not ids:
            return []
        columns = [self.pk_col, self.name_col]
        if self.local_type_col and self.local_type_col not in columns:
            columns.append(self.local_type_col)
        for col in (
            self.populated_place_col,
            self.populated_place_type_col,
            self.district_borough_col,
            self.county_unitary_col,
            self.region_col,
            self.country_col,
        ):
            if col and col not in columns:
                columns.append(col)
        if self.geom_col and self.geom_col not in columns:
            columns.append(self.geom_col)
        query_prefix = f"select {', '.join(columns)} from {self.table} where {self.pk_col} in "
        rows: List[sqlite3.Row] = []
        chunk_size = 900
        for idx in range(0, len(ids), chunk_size):
            chunk = ids[idx : idx + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            query = query_prefix + f"({placeholders})"
            rows.extend(self.conn.execute(query, tuple(chunk)).fetchall())
        return rows

    def _nearest_by_latlon(
        self, lon: float, lat: float, limit: int, search_radius_m: float
    ) -> List[Dict[str, Any]]:
        lat_rad = math.radians(lat)
        meters_per_deg_lat = 111320.0
        meters_per_deg_lon = max(1.0, meters_per_deg_lat * math.cos(lat_rad))
        delta_lat = search_radius_m / meters_per_deg_lat
        delta_lon = search_radius_m / meters_per_deg_lon
        min_lat = lat - delta_lat
        max_lat = lat + delta_lat
        min_lon = lon - delta_lon
        max_lon = lon + delta_lon
        columns = [self.name_col, self.lat_col, self.lon_col]
        if self.local_type_col:
            columns.append(self.local_type_col)
        for col in (
            self.populated_place_col,
            self.populated_place_type_col,
            self.district_borough_col,
            self.county_unitary_col,
            self.region_col,
            self.country_col,
        ):
            if col:
                columns.append(col)
        query = (
            f"select {', '.join(columns)} from {self.table} "
            f"where {self.lat_col} between ? and ? and {self.lon_col} between ? and ?"
        )
        rows = self.conn.execute(query, (min_lat, max_lat, min_lon, max_lon)).fetchall()
        matches = []
        for row in rows:
            row_lat = _coerce_float(row[self.lat_col]) if self.lat_col else None
            row_lon = _coerce_float(row[self.lon_col]) if self.lon_col else None
            if row_lat is None or row_lon is None:
                continue
            name = row[self.name_col]
            if not name:
                continue
            distance_m = _haversine_m(lon, lat, row_lon, row_lat)
            entry = {
                "name": str(name),
                "distance_m": round(distance_m, 1),
                "lon": row_lon,
                "lat": row_lat,
            }
            if self.local_type_col and row[self.local_type_col]:
                entry["local_type"] = row[self.local_type_col]
            if self.populated_place_col and row[self.populated_place_col]:
                entry["populated_place"] = row[self.populated_place_col]
            if self.populated_place_type_col and row[self.populated_place_type_col]:
                entry["populated_place_type"] = row[self.populated_place_type_col]
            if self.district_borough_col and row[self.district_borough_col]:
                entry["district_borough"] = row[self.district_borough_col]
            if self.county_unitary_col and row[self.county_unitary_col]:
                entry["county_unitary"] = row[self.county_unitary_col]
            if self.region_col and row[self.region_col]:
                entry["region"] = row[self.region_col]
            if self.country_col and row[self.country_col]:
                entry["country"] = row[self.country_col]
            matches.append(entry)
        matches.sort(key=lambda item: item["distance_m"])
        return matches[:limit]


def main() -> int:
    args = parse_args()
    supabase_url = os.getenv("SUPABASE_URL")
    project_ref = _supabase_project_ref(supabase_url)
    if project_ref:
        LOG.info("Supabase project ref: %s", project_ref)
    else:
        LOG.info("Supabase project ref: <unknown>")
    output_count = 0
    updates: List[Dict[str, Any]] = []
    proposed_count = 0
    proposed_region_count = 0
    for payload in iter_station_payloads(args) or []:
        if args.apply:
            proposed_name = payload.get("proposed_station_name")
            proposed_region = payload.get("proposed_region")
            if proposed_name is not None:
                proposed_text = str(proposed_name).strip()
            else:
                proposed_text = ""
            station = payload.get("station") or {}
            station_id = station.get("id")
            update_fields: Dict[str, Any] = {}
            if proposed_text:
                proposed_count += 1
                update_fields["station_name"] = proposed_text
            if proposed_region and not station.get("region"):
                proposed_region_count += 1
                update_fields["region"] = proposed_region
            if station_id is not None and update_fields:
                updates.append({"id": station_id, **update_fields})
        if args.output_format == "json":
            print(json.dumps(payload, indent=2))
        else:
            summary = build_station_summary(payload)
            print(json.dumps(summary, ensure_ascii=True, indent=2))
        output_count += 1
    if args.output_format == "summary":
        LOG.info("Summary stations output=%s", output_count)
    LOG.info("Proposed station_name count=%s (out of %s).", proposed_count, output_count)
    LOG.info("Proposed region count=%s (out of %s).", proposed_region_count, output_count)
    if args.apply:
        applied = _apply_station_name_updates(updates, args.apply_batch_size)
        LOG.info("Applied station_name updates=%s (proposed=%s).", applied, len(updates))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
