#!/usr/bin/env python3
"""Build pollutant-aware duplicate station candidates in long CSV format.

Default output is one CSV where each duplicate-group member gets a row and
shares the same ``dup_id``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

def _default_json_root() -> Path:
    dropbox_local_root = Path(os.path.expanduser(os.getenv("UK_AQ_DROPBOX_LOCAL_ROOT", "~/Dropbox")))
    dropbox_root = (os.getenv("UK_AQ_DROPBOX_ROOT") or "LIVE").strip().strip("/")
    if not dropbox_root:
        dropbox_root = "LIVE"
    return (
        dropbox_local_root
        / "Apps"
        / "github-uk-air-quality-networks"
        / dropbox_root
        / "uk_aq_stations"
    )


DEFAULT_JSON_ROOT = _default_json_root()
DEFAULT_AURN_DIR = Path("network_info/AURN")
DEFAULT_OUTPUT_CSV = Path("plans/uk_aq_station_duplicate_candidates_long.csv")

UK_AIR_ID_RE = re.compile(r"\bUKA\d{5}\b")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
COORD_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
WKT_POINT_RE = re.compile(
    r"POINT\s*\(\s*([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)\s*\)",
    re.IGNORECASE,
)
PM_COUNT_RE = re.compile(
    r"\bpm\s*([0-9]+(?:[.,][0-9]+)?)\s*count\b",
    re.IGNORECASE,
)
POLLUTANT_UNICODE_TRANSLATION = str.maketrans({
    "₀": "0",
    "₁": "1",
    "₂": "2",
    "₃": "3",
    "₄": "4",
    "₅": "5",
    "₆": "6",
    "₇": "7",
    "₈": "8",
    "₉": "9",
    "⁰": "0",
    "¹": "1",
    "²": "2",
    "³": "3",
    "⁴": "4",
    "⁵": "5",
    "⁶": "6",
    "⁷": "7",
    "⁸": "8",
    "⁹": "9",
})

POLLUTANT_PATTERNS: Sequence[Tuple[re.Pattern[str], str]] = (
    (re.compile(r"\bpm\s*2[.,]?\s*5\b", re.IGNORECASE), "pm25"),
    (re.compile(r"\bpm\s*10\b", re.IGNORECASE), "pm10"),
    (re.compile(r"\bpm\s*1\b", re.IGNORECASE), "pm1"),
    (re.compile(r"\bno2\b|\bnitrogen dioxide\b", re.IGNORECASE), "no2"),
    (re.compile(r"\bnox\b|\bnitrogen oxides\b", re.IGNORECASE), "nox"),
    (re.compile(r"\bno\b|\bnitrogen monoxide\b", re.IGNORECASE), "no"),
    (re.compile(r"\bo3\b|\bozone\b", re.IGNORECASE), "o3"),
    (re.compile(r"\bso2\b|\bsulphur dioxide\b|\bsulfur dioxide\b", re.IGNORECASE), "so2"),
    (re.compile(r"\bco\b|\bcarbon monoxide\b", re.IGNORECASE), "co"),
    (re.compile(r"\bnh3\b|\bammonia\b", re.IGNORECASE), "nh3"),
    (re.compile(r"\btemperature\b|\btemp\b", re.IGNORECASE), "temperature"),
    (re.compile(r"\brh\b|\brelative humidity\b|\bhumidity\b", re.IGNORECASE), "rh"),
    (re.compile(r"\bpressure\b|\bbarometric pressure\b", re.IGNORECASE), "pressure"),
)

CSV_FIELDS: Sequence[str] = (
    "dup_id",
    "source",
    "connector_code",
    "station_id",
    "station_ref",
    "station_name",
    "timeseries_id",
    "timeseries_ref",
    "timeseries_label",
    "pollutant",
    "last_value",
    "last_value_at",
    "distance_m",
    "lat_lon",
    "aurn_uk_air_id",
    "aurn_networks",
)


@dataclass(frozen=True)
class JsonTimeseries:
    node_id: str
    connector_code: str
    station_id: str
    station_ref: str
    station_name: str
    timeseries_id: str
    timeseries_ref: str
    timeseries_label: str
    pollutant_key: str
    lat: Optional[float]
    lon: Optional[float]
    detected_uk_air_id: str
    last_value: str
    last_value_at: str


@dataclass(frozen=True)
class AurnSite:
    base_key: str
    uk_air_id: str
    station_name: str
    lat: Optional[float]
    lon: Optional[float]
    networks: str


@dataclass(frozen=True)
class VirtualAurnNode:
    node_id: str
    uk_air_id: str
    pollutant_key: str
    station_name: str
    lat: Optional[float]
    lon: Optional[float]
    networks: str


@dataclass(frozen=True)
class GroupRow:
    dup_id: str
    source: str
    connector_code: str
    station_id: str
    station_ref: str
    station_name: str
    timeseries_id: str
    timeseries_ref: str
    timeseries_label: str
    pollutant: str
    last_value: str
    last_value_at: str
    distance_m: str
    lat_lon: str
    aurn_uk_air_id: str
    aurn_networks: str


class UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}
        self.rank: Dict[str, int] = {}

    def add(self, item: str) -> None:
        if item in self.parent:
            return
        self.parent[item] = item
        self.rank[item] = 0

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            parent = self.parent[item]
            self.parent[item] = root
            item = parent
        return root

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        left_rank = self.rank[left_root]
        right_rank = self.rank[right_root]
        if left_rank < right_rank:
            self.parent[left_root] = right_root
        elif left_rank > right_rank:
            self.parent[right_root] = left_root
        else:
            self.parent[right_root] = left_root
            self.rank[left_root] += 1

    def items(self) -> Iterable[str]:
        return self.parent.keys()


def _normalize_header(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    normalized = NON_ALNUM_RE.sub("_", normalized)
    return normalized.strip("_")


def _float_or_none(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text_or_blank(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _format_lat_lon(lat: Optional[float], lon: Optional[float]) -> str:
    if lat is None or lon is None:
        return ""
    return f"{lat:.6f} {lon:.6f}"


def _parse_coordinates(value: str) -> Tuple[Optional[float], Optional[float]]:
    matches = COORD_NUMBER_RE.findall(value)
    if len(matches) < 2:
        return None, None
    lat = _float_or_none(matches[0])
    lon = _float_or_none(matches[1])
    return lat, lon


def _parse_wkt_point(value: str) -> Tuple[Optional[float], Optional[float]]:
    match = WKT_POINT_RE.search(value)
    if not match:
        return None, None
    lon = _float_or_none(match.group(1))
    lat = _float_or_none(match.group(2))
    return lat, lon


def _extract_lat_lon(record: Dict[str, object]) -> Tuple[Optional[float], Optional[float]]:
    coordinates = str(record.get("coordinates") or "").strip()
    if coordinates:
        lat, lon = _parse_coordinates(coordinates)
        if lat is not None and lon is not None:
            return lat, lon

    geometry = str(record.get("geometry") or "").strip()
    if geometry:
        lat, lon = _parse_wkt_point(geometry)
        if lat is not None and lon is not None:
            return lat, lon

    return None, None


def _extract_json_pollutant(timeseries_label: str, station_name: str) -> str:
    clean_label = timeseries_label.strip()
    if not clean_label:
        return ""

    station = station_name.strip()
    if station:
        marker = f"{station}-"
        if clean_label.lower().startswith(marker.lower()):
            return clean_label[len(marker) :].strip()

    if "-" in clean_label:
        _, rhs = clean_label.split("-", 1)
        return rhs.strip()

    return clean_label


def _pollutant_key_from_text(value: str) -> str:
    raw = value.strip().translate(POLLUTANT_UNICODE_TRANSLATION)
    if not raw:
        return "unknown"

    pm_count_match = PM_COUNT_RE.search(raw)
    if pm_count_match:
        size_raw = pm_count_match.group(1).replace(",", ".")
        try:
            size_value = float(size_raw)
        except ValueError:
            size_value = None
        if size_value is not None:
            # Keep PM count channels distinct from mass channels like pm25/pm10.
            size_key = f"{size_value:.3f}".rstrip("0").rstrip(".").replace(".", "")
            if size_key:
                return f"pm{size_key}_count"

    for pattern, key in POLLUTANT_PATTERNS:
        if pattern.search(raw):
            return key

    compact = NON_ALNUM_RE.sub("", raw.lower())
    if compact in {
        "pm25",
        "pm10",
        "pm1",
        "no2",
        "nox",
        "no",
        "o3",
        "so2",
        "co",
        "nh3",
        "temperature",
        "temp",
        "rh",
        "pressure",
    }:
        if compact == "temp":
            return "temperature"
        return compact
    if compact in {"relativehumidity", "humidity"}:
        return "rh"
    if compact in {"temperaturec", "degc", "celsius"}:
        return "temperature"

    return "unknown"


def _detect_uk_air_id(timeseries_label: str, station_name: str, station_ref: str) -> str:
    texts = [timeseries_label or "", station_name or "", station_ref or ""]
    found: List[str] = []
    for text in texts:
        found.extend(UK_AIR_ID_RE.findall(text))

    if not found:
        return ""

    if len(found) > 1 and len(set(found)) > 1:
        print(
            f"warning: multiple UK-AIR IDs detected in JSON text: {found}",
            file=sys.stderr,
        )

    return found[0]


def _discover_latest_json_file(json_root: Path) -> Path:
    candidates = [
        path
        for path in json_root.rglob("uk_aq_stations*.json")
        if path.is_file() and path.name.startswith("uk_aq_stations")
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No uk_aq_stations*.json files found under {json_root}"
        )
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def _discover_latest_aurn_csv(aurn_dir: Path) -> Path:
    candidates = [
        path for path in aurn_dir.glob("uk-air-search-results-*.csv") if path.is_file()
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No uk-air-search-results-*.csv files found under {aurn_dir}"
        )
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def _read_env_value(env_path: Path, key: str) -> str:
    if not env_path.exists():
        return ""
    prefix = f"{key}="
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        return line[len(prefix):].strip().strip("\"").strip("'")
    return ""


def _resolve_supabase_db_url(explicit_db_url: str) -> str:
    direct = explicit_db_url.strip()
    if direct:
        return direct
    env_value = (os.getenv("SUPABASE_DB_URL") or "").strip()
    if env_value:
        return env_value
    return _read_env_value(Path(".env"), "SUPABASE_DB_URL")


def _load_station_timeseries_rows(
    supabase_db_url: str,
    station_ids: Sequence[str],
) -> Dict[str, List[Tuple[str, str, str, str, str, str]]]:
    """Map station_id -> list of (timeseries_id, timeseries_ref, pollutant_key, last_value, last_value_at, timeseries_label)."""
    unique_ids = sorted({int(item) for item in station_ids if str(item).isdigit()})
    if not unique_ids:
        return {}

    try:
        import psycopg2  # type: ignore
    except Exception as error:  # pragma: no cover - dependency/runtime guard
        raise RuntimeError(
            "psycopg2 is required to resolve station_id from DB IDs."
        ) from error

    sql = (
        "select t.id::text as timeseries_id, "
        "t.station_id::text as station_id, "
        "coalesce(t.timeseries_ref::text, '') as timeseries_ref, "
        "coalesce(t.label::text, '') as timeseries_label, "
        "coalesce(t.last_value::text, '') as last_value, "
        "coalesce(t.last_value_at::text, '') as last_value_at, "
        "coalesce(p.notation::text, '') as notation, "
        "coalesce(p.label::text, '') as phenomenon_label "
        "from uk_aq_core.timeseries t "
        "left join uk_aq_core.phenomena p on p.id = t.phenomenon_id "
        "where t.station_id = any(%s)"
    )

    output: Dict[str, List[Tuple[str, str, str, str, str, str]]] = {}
    with psycopg2.connect(supabase_db_url) as conn:  # type: ignore[arg-type]
        with conn.cursor() as cur:
            cur.execute(sql, (unique_ids,))
            for (
                timeseries_id,
                station_id,
                timeseries_ref,
                timeseries_label,
                last_value,
                last_value_at,
                notation,
                phenomenon_label,
            ) in cur.fetchall():
                pollutant = _pollutant_key_from_text(str(notation or "")) or "unknown"
                if pollutant == "unknown":
                    pollutant = _pollutant_key_from_text(str(phenomenon_label or ""))
                output.setdefault(str(station_id), []).append(
                    (
                        _text_or_blank(timeseries_id),
                        _text_or_blank(timeseries_ref),
                        pollutant or "unknown",
                        _text_or_blank(last_value),
                        _text_or_blank(last_value_at),
                        _text_or_blank(timeseries_label),
                    )
                )
    for station_id in output:
        output[station_id].sort(key=lambda item: int(item[0]))
    return output


def _apply_db_ids_to_json_rows(
    json_rows: Sequence[JsonTimeseries],
    station_timeseries_rows: Dict[str, List[Tuple[str, str, str, str, str, str]]],
) -> List[JsonTimeseries]:
    output: List[JsonTimeseries] = []
    missing_station_timeseries = 0
    for row in json_rows:
        station_candidates = station_timeseries_rows.get(row.station_id, [])
        if not station_candidates:
            missing_station_timeseries += 1
            output.append(
                JsonTimeseries(
                    node_id=f"json_station:{row.station_id}:{row.pollutant_key}",
                    connector_code=row.connector_code,
                    station_id=row.station_id,
                    station_ref=row.station_ref,
                    station_name=row.station_name,
                    timeseries_id="",
                    timeseries_ref="",
                    timeseries_label=row.timeseries_label,
                    pollutant_key=row.pollutant_key,
                    lat=row.lat,
                    lon=row.lon,
                    detected_uk_air_id=row.detected_uk_air_id,
                    last_value="",
                    last_value_at="",
                )
            )
            continue

        for (
            mapped_timeseries_id,
            mapped_timeseries_ref,
            mapped_pollutant_key,
            mapped_last_value,
            mapped_last_value_at,
            mapped_timeseries_label,
        ) in station_candidates:
            output.append(
                JsonTimeseries(
                    node_id=f"json:{mapped_timeseries_id}",
                    connector_code=row.connector_code,
                    station_id=row.station_id,
                    station_ref=row.station_ref,
                    station_name=row.station_name,
                    timeseries_id=mapped_timeseries_id,
                    timeseries_ref=mapped_timeseries_ref or row.timeseries_ref,
                    timeseries_label=mapped_timeseries_label or row.timeseries_label,
                    pollutant_key=mapped_pollutant_key or row.pollutant_key,
                    lat=row.lat,
                    lon=row.lon,
                    detected_uk_air_id=row.detected_uk_air_id,
                    last_value=mapped_last_value,
                    last_value_at=mapped_last_value_at,
                )
            )
    if missing_station_timeseries:
        print(
            "warning: no timeseries row found for "
            f"{missing_station_timeseries} station rows; leaving timeseries_id blank.",
            file=sys.stderr,
        )
    return output


def _load_json_timeseries(path: Path) -> List[JsonTimeseries]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("stations")
        if not isinstance(rows, list):
            raise ValueError(f"JSON payload at {path} is dict but missing list key 'stations'")
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError(f"Unsupported JSON structure at {path}: {type(payload).__name__}")

    output: List[JsonTimeseries] = []
    for idx, record_obj in enumerate(rows):
        if not isinstance(record_obj, dict):
            continue
        record = record_obj
        connector_code = str(record.get("connector_code") or "").strip() or "unknown"
        station_name = str(record.get("station_name") or "").strip()
        station_ref = str(record.get("station_ref") or "").strip()
        timeseries_label = str(record.get("label") or "").strip()

        station_id_raw = record.get("id")
        station_id = str(station_id_raw).strip() if station_id_raw is not None else ""
        if not station_id or not station_id.isdigit():
            continue

        # Some feeds expose only station_ref in this payload, so reuse station_ref when
        # a distinct timeseries reference is not available.
        timeseries_ref = str(record.get("timeseries_ref") or "").strip() or station_ref

        lat, lon = _extract_lat_lon(record)
        pollutant_text = _extract_json_pollutant(timeseries_label, station_name)
        pollutant_key = _pollutant_key_from_text(pollutant_text)
        detected_uk_air_id = _detect_uk_air_id(timeseries_label, station_name, station_ref)

        output.append(
            JsonTimeseries(
                node_id=f"json_station:{station_id}:{idx}",
                connector_code=connector_code,
                station_id=station_id,
                station_ref=station_ref,
                station_name=station_name,
                timeseries_id="",
                timeseries_ref=timeseries_ref,
                timeseries_label=timeseries_label,
                pollutant_key=pollutant_key,
                lat=lat,
                lon=lon,
                detected_uk_air_id=detected_uk_air_id,
                last_value="",
                last_value_at="",
            )
        )

    return output


def _load_aurn_sites(path: Path) -> List[AurnSite]:
    output: List[AurnSite] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return output
        original_headers = list(reader.fieldnames)
        normalized_headers = [_normalize_header(h) for h in original_headers]

        for idx, row in enumerate(reader):
            normalized: Dict[str, str] = {}
            for original, normalized_key in zip(original_headers, normalized_headers):
                normalized[normalized_key] = str(row.get(original) or "").strip()

            uk_air_id = normalized.get("uk_air_id", "")
            lat = _float_or_none(normalized.get("latitude", ""))
            lon = _float_or_none(normalized.get("longitude", ""))
            station_name = normalized.get("site_name", "")
            networks = normalized.get("networks", "")
            if uk_air_id:
                base_key = f"aurn_base:{uk_air_id}"
            else:
                name_key = NON_ALNUM_RE.sub("", station_name.lower()) or "unknown_site"
                lat_key = f"{lat:.6f}" if lat is not None else "na"
                lon_key = f"{lon:.6f}" if lon is not None else "na"
                base_key = f"aurn_base_missing:{name_key}:{lat_key}:{lon_key}:{idx}"

            output.append(
                AurnSite(
                    base_key=base_key,
                    uk_air_id=uk_air_id,
                    station_name=station_name,
                    lat=lat,
                    lon=lon,
                    networks=networks,
                )
            )

    return output


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return radius_m * c


def _spatial_pairs_within_distance(
    json_rows: Sequence[JsonTimeseries],
    aurn_rows: Sequence[AurnSite],
    distance_m: float,
    include_aurn_aurn: bool,
) -> List[Tuple[str, int, str, int, float]]:
    """Return nearby pairs as tuples:

    (left_kind, left_index, right_kind, right_index, distance_m)
    """

    points: List[Tuple[str, int, float, float]] = []
    for idx, row in enumerate(json_rows):
        if row.lat is None or row.lon is None:
            continue
        points.append(("json", idx, row.lat, row.lon))
    for idx, row in enumerate(aurn_rows):
        if row.lat is None or row.lon is None:
            continue
        points.append(("aurn", idx, row.lat, row.lon))

    if not points:
        return []

    # Fast default without external dependencies: grid-bucket prefilter + haversine check.
    cell_deg = max(distance_m / 111320.0, 1e-6)
    buckets: Dict[Tuple[int, int], List[int]] = {}
    for idx, (_, _, lat, lon) in enumerate(points):
        cell = (int(math.floor(lat / cell_deg)), int(math.floor(lon / cell_deg)))
        buckets.setdefault(cell, []).append(idx)

    result: List[Tuple[str, int, str, int, float]] = []
    seen: Set[Tuple[int, int]] = set()
    for idx, (left_kind, left_index, left_lat, left_lon) in enumerate(points):
        cell = (int(math.floor(left_lat / cell_deg)), int(math.floor(left_lon / cell_deg)))
        for dlat in (-1, 0, 1):
            for dlon in (-1, 0, 1):
                neighbor_cell = (cell[0] + dlat, cell[1] + dlon)
                for jdx in buckets.get(neighbor_cell, []):
                    if jdx <= idx:
                        continue
                    pair_key = (idx, jdx)
                    if pair_key in seen:
                        continue
                    seen.add(pair_key)

                    right_kind, right_index, right_lat, right_lon = points[jdx]

                    allowed = (
                        (left_kind == "json" and right_kind == "json")
                        or (left_kind == "json" and right_kind == "aurn")
                        or (left_kind == "aurn" and right_kind == "json")
                        or (
                            include_aurn_aurn
                            and left_kind == "aurn"
                            and right_kind == "aurn"
                        )
                    )
                    if not allowed:
                        continue

                    dist = _haversine_m(left_lat, left_lon, right_lat, right_lon)
                    if dist <= distance_m:
                        result.append((left_kind, left_index, right_kind, right_index, dist))

    return result


def _build_groups(
    json_rows: Sequence[JsonTimeseries],
    aurn_rows: Sequence[AurnSite],
    distance_m: float,
    min_group_size: int,
    include_aurn_aurn: bool,
) -> Tuple[List[Tuple[str, List[str], str]], Dict[str, VirtualAurnNode]]:
    """Return sorted groups and virtual AURN node map.

    Group tuple: (dup_id_placeholder_signature, member_node_ids, group_pollutant)
    """

    json_by_node = {row.node_id: row for row in json_rows}
    virtual_aurn_nodes: Dict[str, VirtualAurnNode] = {}

    uf = UnionFind()
    pairs = _spatial_pairs_within_distance(
        json_rows=json_rows,
        aurn_rows=aurn_rows,
        distance_m=distance_m,
        include_aurn_aurn=include_aurn_aurn,
    )

    for left_kind, left_index, right_kind, right_index, _ in pairs:
        if left_kind == "json" and right_kind == "json":
            left_json = json_rows[left_index]
            right_json = json_rows[right_index]
            if left_json.pollutant_key != right_json.pollutant_key:
                continue
            uf.union(left_json.node_id, right_json.node_id)
            continue

        if left_kind == "json" and right_kind == "aurn":
            json_row = json_rows[left_index]
            aurn_row = aurn_rows[right_index]
            pollutant = json_row.pollutant_key or "unknown"
            aurn_node_id = f"aurn:{aurn_row.base_key}:{pollutant}"
            if aurn_node_id not in virtual_aurn_nodes:
                virtual_aurn_nodes[aurn_node_id] = VirtualAurnNode(
                    node_id=aurn_node_id,
                    uk_air_id=aurn_row.uk_air_id,
                    pollutant_key=pollutant,
                    station_name=aurn_row.station_name,
                    lat=aurn_row.lat,
                    lon=aurn_row.lon,
                    networks=aurn_row.networks,
                )
            uf.union(json_row.node_id, aurn_node_id)
            continue

        if left_kind == "aurn" and right_kind == "json":
            aurn_row = aurn_rows[left_index]
            json_row = json_rows[right_index]
            pollutant = json_row.pollutant_key or "unknown"
            aurn_node_id = f"aurn:{aurn_row.base_key}:{pollutant}"
            if aurn_node_id not in virtual_aurn_nodes:
                virtual_aurn_nodes[aurn_node_id] = VirtualAurnNode(
                    node_id=aurn_node_id,
                    uk_air_id=aurn_row.uk_air_id,
                    pollutant_key=pollutant,
                    station_name=aurn_row.station_name,
                    lat=aurn_row.lat,
                    lon=aurn_row.lon,
                    networks=aurn_row.networks,
                )
            uf.union(json_row.node_id, aurn_node_id)
            continue

        if include_aurn_aurn and left_kind == "aurn" and right_kind == "aurn":
            left_site = aurn_rows[left_index]
            right_site = aurn_rows[right_index]
            left_node_id = f"aurn:{left_site.base_key}:unknown"
            right_node_id = f"aurn:{right_site.base_key}:unknown"
            if left_node_id not in virtual_aurn_nodes:
                virtual_aurn_nodes[left_node_id] = VirtualAurnNode(
                    node_id=left_node_id,
                    uk_air_id=left_site.uk_air_id,
                    pollutant_key="unknown",
                    station_name=left_site.station_name,
                    lat=left_site.lat,
                    lon=left_site.lon,
                    networks=left_site.networks,
                )
            if right_node_id not in virtual_aurn_nodes:
                virtual_aurn_nodes[right_node_id] = VirtualAurnNode(
                    node_id=right_node_id,
                    uk_air_id=right_site.uk_air_id,
                    pollutant_key="unknown",
                    station_name=right_site.station_name,
                    lat=right_site.lat,
                    lon=right_site.lon,
                    networks=right_site.networks,
                )
            uf.union(left_node_id, right_node_id)

    components: Dict[str, List[str]] = {}
    for node_id in uf.items():
        root = uf.find(node_id)
        components.setdefault(root, []).append(node_id)

    filtered_components: List[List[str]] = [
        sorted(nodes) for nodes in components.values() if len(nodes) >= min_group_size
    ]

    signatures: List[Tuple[str, List[str], str]] = []
    for nodes in filtered_components:
        non_unknown_json_pollutants: Set[str] = set()
        for node_id in nodes:
            json_row = json_by_node.get(node_id)
            if json_row and json_row.pollutant_key != "unknown":
                non_unknown_json_pollutants.add(json_row.pollutant_key)

        if len(non_unknown_json_pollutants) == 1:
            group_pollutant = next(iter(non_unknown_json_pollutants))
        else:
            group_pollutant = "unknown"

        signature = f"{group_pollutant}|{'|'.join(nodes)}"
        signatures.append((signature, nodes, group_pollutant))

    signatures.sort(key=lambda item: item[0])
    return signatures, virtual_aurn_nodes


def _member_sort_key_for_anchor(row: GroupRow) -> Tuple[int, str, str, str]:
    source_rank = 0 if row.source == "aurn" else 1
    return (source_rank, row.connector_code, row.station_name, row.timeseries_id)


def _build_output_rows(
    groups: Sequence[Tuple[str, List[str], str]],
    json_rows: Sequence[JsonTimeseries],
    virtual_aurn_nodes: Dict[str, VirtualAurnNode],
) -> List[GroupRow]:
    json_by_node = {row.node_id: row for row in json_rows}

    all_rows: List[GroupRow] = []
    dup_counter = 0
    for _, node_ids, group_pollutant in groups:

        provisional_rows: List[GroupRow] = []
        member_positions: List[Tuple[Optional[float], Optional[float]]] = []

        for node_id in node_ids:
            json_row = json_by_node.get(node_id)
            if json_row:
                provisional_rows.append(
                    GroupRow(
                        dup_id="",
                        source="json",
                        connector_code=json_row.connector_code,
                        station_id=json_row.station_id,
                        station_ref=json_row.station_ref,
                        station_name=json_row.station_name,
                        timeseries_id=json_row.timeseries_id,
                        timeseries_ref=json_row.timeseries_ref,
                        timeseries_label=json_row.timeseries_label,
                        pollutant=json_row.pollutant_key or group_pollutant,
                        last_value=json_row.last_value,
                        last_value_at=json_row.last_value_at,
                        distance_m="",
                        lat_lon=_format_lat_lon(json_row.lat, json_row.lon),
                        aurn_uk_air_id="",
                        aurn_networks="",
                    )
                )
                member_positions.append((json_row.lat, json_row.lon))
                continue

            aurn_node = virtual_aurn_nodes.get(node_id)
            if not aurn_node:
                continue
            provisional_rows.append(
                GroupRow(
                    dup_id="",
                    source="aurn",
                    connector_code="aurn_register",
                    station_id="",
                    station_ref=aurn_node.uk_air_id,
                    station_name=aurn_node.station_name,
                    timeseries_id="",
                    timeseries_ref="",
                    timeseries_label="",
                    pollutant=group_pollutant,
                    last_value="",
                    last_value_at="",
                    distance_m="",
                    lat_lon=_format_lat_lon(aurn_node.lat, aurn_node.lon),
                    aurn_uk_air_id=aurn_node.uk_air_id,
                    aurn_networks=aurn_node.networks,
                )
            )
            member_positions.append((aurn_node.lat, aurn_node.lon))

        if not provisional_rows:
            continue
        if not any(row.timeseries_id for row in provisional_rows):
            continue
        if not any(row.last_value.strip() != "" for row in provisional_rows):
            continue
        connector_codes = {row.connector_code for row in provisional_rows if row.connector_code}
        if len(connector_codes) < 2:
            continue

        dup_counter += 1
        dup_id = f"DUP{dup_counter:06d}"
        provisional_rows = [
            GroupRow(
                dup_id=dup_id,
                source=row.source,
                connector_code=row.connector_code,
                station_id=row.station_id,
                station_ref=row.station_ref,
                station_name=row.station_name,
                timeseries_id=row.timeseries_id,
                timeseries_ref=row.timeseries_ref,
                timeseries_label=row.timeseries_label,
                pollutant=row.pollutant,
                last_value=row.last_value,
                last_value_at=row.last_value_at,
                distance_m=row.distance_m,
                lat_lon=row.lat_lon,
                aurn_uk_air_id=row.aurn_uk_air_id,
                aurn_networks=row.aurn_networks,
            )
            for row in provisional_rows
        ]

        anchor_idx = min(
            range(len(provisional_rows)),
            key=lambda i: _member_sort_key_for_anchor(provisional_rows[i]),
        )
        anchor_lat, anchor_lon = member_positions[anchor_idx]

        for i, row in enumerate(provisional_rows):
            lat, lon = member_positions[i]
            if (
                anchor_lat is None
                or anchor_lon is None
                or lat is None
                or lon is None
            ):
                distance_m = ""
            elif i == anchor_idx:
                distance_m = "0"
            else:
                distance_m = f"{_haversine_m(anchor_lat, anchor_lon, lat, lon):.3f}"

            all_rows.append(
                GroupRow(
                    dup_id=row.dup_id,
                    source=row.source,
                    connector_code=row.connector_code,
                    station_id=row.station_id,
                    station_ref=row.station_ref,
                    station_name=row.station_name,
                    timeseries_id=row.timeseries_id,
                    timeseries_ref=row.timeseries_ref,
                    timeseries_label=row.timeseries_label,
                    pollutant=row.pollutant,
                    last_value=row.last_value,
                    last_value_at=row.last_value_at,
                    distance_m=distance_m,
                    lat_lon=row.lat_lon,
                    aurn_uk_air_id=row.aurn_uk_air_id,
                    aurn_networks=row.aurn_networks,
                )
            )

    all_rows.sort(
        key=lambda row: (
            row.dup_id,
            0 if row.source == "aurn" else 1,
            row.connector_code,
            row.station_name,
            row.timeseries_id,
        )
    )
    return all_rows


def _write_long_csv(path: Path, rows: Sequence[GroupRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "dup_id": row.dup_id,
                    "source": row.source,
                    "connector_code": row.connector_code,
                    "station_id": row.station_id,
                    "station_ref": row.station_ref,
                    "station_name": row.station_name,
                    "timeseries_id": row.timeseries_id,
                    "timeseries_ref": row.timeseries_ref,
                    "timeseries_label": row.timeseries_label,
                    "pollutant": row.pollutant,
                    "last_value": row.last_value,
                    "last_value_at": row.last_value_at,
                    "distance_m": row.distance_m,
                    "lat_lon": row.lat_lon,
                    "aurn_uk_air_id": row.aurn_uk_air_id,
                    "aurn_networks": row.aurn_networks,
                }
            )


def _summarize_groups(rows: Sequence[GroupRow]) -> Dict[str, int]:
    grouped: Dict[str, List[GroupRow]] = {}
    for row in rows:
        grouped.setdefault(row.dup_id, []).append(row)

    json_only_groups = 0
    includes_aurn_groups = 0
    for group_rows in grouped.values():
        has_aurn = any(row.source == "aurn" for row in group_rows)
        if has_aurn:
            includes_aurn_groups += 1
        else:
            json_only_groups += 1

    return {
        "groups": len(grouped),
        "rows": len(rows),
        "json_only_groups": json_only_groups,
        "includes_aurn_groups": includes_aurn_groups,
    }


def _validate_numeric_internal_ids(rows: Sequence[GroupRow]) -> None:
    bad_station_rows = [
        row
        for row in rows
        if row.source == "json" and (not row.station_id or not row.station_id.isdigit())
    ]
    aurn_station_rows_with_id = [
        row for row in rows if row.source == "aurn" and row.station_id
    ]
    bad_timeseries_rows = [
        row
        for row in rows
        if row.source == "json"
        and row.timeseries_id
        and not row.timeseries_id.isdigit()
    ]
    aurn_timeseries_rows_with_id = [
        row for row in rows if row.source == "aurn" and row.timeseries_id
    ]
    if (
        not bad_station_rows
        and not bad_timeseries_rows
        and not aurn_station_rows_with_id
        and not aurn_timeseries_rows_with_id
    ):
        return

    parts: List[str] = []
    if bad_station_rows:
        sample = ", ".join(f"{row.dup_id}:{row.source}:{row.station_id}" for row in bad_station_rows[:5])
        parts.append(
            f"station_id non-numeric rows={len(bad_station_rows)} sample=[{sample}]"
        )
    if bad_timeseries_rows:
        sample = ", ".join(
            f"{row.dup_id}:{row.source}:{row.timeseries_id}" for row in bad_timeseries_rows[:5]
        )
        parts.append(
            f"timeseries_id non-numeric rows={len(bad_timeseries_rows)} sample=[{sample}]"
        )
    if aurn_station_rows_with_id:
        sample = ", ".join(f"{row.dup_id}:{row.station_id}" for row in aurn_station_rows_with_id[:5])
        parts.append(
            f"aurn rows must not carry station_id rows={len(aurn_station_rows_with_id)} sample=[{sample}]"
        )
    if aurn_timeseries_rows_with_id:
        sample = ", ".join(f"{row.dup_id}:{row.timeseries_id}" for row in aurn_timeseries_rows_with_id[:5])
        parts.append(
            f"aurn rows must not carry timeseries_id rows={len(aurn_timeseries_rows_with_id)} sample=[{sample}]"
        )
    raise RuntimeError("; ".join(parts))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create pollutant-aware duplicate candidates in long CSV format."
    )
    parser.add_argument(
        "--json-root",
        default=str(DEFAULT_JSON_ROOT),
        help=f"Root directory for uk_aq_stations JSON snapshots (default: {DEFAULT_JSON_ROOT})",
    )
    parser.add_argument(
        "--json-file",
        default="",
        help="Optional explicit JSON file path. If omitted, latest file is auto-discovered.",
    )
    parser.add_argument(
        "--aurn-dir",
        default=str(DEFAULT_AURN_DIR),
        help=f"Directory containing uk-air-search-results-*.csv (default: {DEFAULT_AURN_DIR})",
    )
    parser.add_argument(
        "--aurn-csv",
        default="",
        help="Optional explicit AURN CSV path. If omitted, latest file is auto-discovered.",
    )
    parser.add_argument(
        "--supabase-db-url",
        default="",
        help=(
            "Optional DB URL for resolving internal IDs from uk_aq_core.timeseries. "
            "If omitted, uses SUPABASE_DB_URL from env or .env."
        ),
    )
    parser.add_argument(
        "--distance-m",
        type=float,
        default=30.0,
        help="Duplicate distance threshold in meters (default: 30).",
    )
    parser.add_argument(
        "--min-group-size",
        type=int,
        default=2,
        help="Minimum rows per group to output (default: 2).",
    )
    parser.add_argument(
        "--include-aurn-aurn",
        action="store_true",
        help="Include AURN↔AURN proximity edges (default: off).",
    )
    parser.add_argument(
        "--output-csv",
        default=str(DEFAULT_OUTPUT_CSV),
        help=f"Long-format output CSV (default: {DEFAULT_OUTPUT_CSV})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    json_file = Path(args.json_file) if args.json_file else _discover_latest_json_file(Path(args.json_root))
    aurn_csv = Path(args.aurn_csv) if args.aurn_csv else _discover_latest_aurn_csv(Path(args.aurn_dir))
    output_csv = Path(args.output_csv)

    if not json_file.exists():
        raise FileNotFoundError(f"JSON file not found: {json_file}")
    if not aurn_csv.exists():
        raise FileNotFoundError(f"AURN CSV not found: {aurn_csv}")

    supabase_db_url = _resolve_supabase_db_url(args.supabase_db_url)
    if not supabase_db_url:
        raise RuntimeError(
            "SUPABASE_DB_URL is required to resolve numeric station_id/timeseries_id."
        )

    json_rows = _load_json_timeseries(json_file)
    station_timeseries_rows = _load_station_timeseries_rows(
        supabase_db_url=supabase_db_url,
        station_ids=[row.station_id for row in json_rows],
    )
    json_rows = _apply_db_ids_to_json_rows(
        json_rows=json_rows,
        station_timeseries_rows=station_timeseries_rows,
    )
    aurn_rows = _load_aurn_sites(aurn_csv)

    groups, virtual_aurn_nodes = _build_groups(
        json_rows=json_rows,
        aurn_rows=aurn_rows,
        distance_m=float(args.distance_m),
        min_group_size=max(2, int(args.min_group_size)),
        include_aurn_aurn=bool(args.include_aurn_aurn),
    )
    output_rows = _build_output_rows(
        groups=groups,
        json_rows=json_rows,
        virtual_aurn_nodes=virtual_aurn_nodes,
    )
    _validate_numeric_internal_ids(output_rows)

    _write_long_csv(output_csv, output_rows)

    summary = _summarize_groups(output_rows)

    print(f"JSON input: {json_file}")
    print(f"AURN input: {aurn_csv}")
    print(f"Wrote long CSV: {output_csv} ({summary['rows']} rows)")
    print(
        "Group summary: "
        f"groups={summary['groups']}, "
        f"json_only_groups={summary['json_only_groups']}, "
        f"includes_aurn_groups={summary['includes_aurn_groups']}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
