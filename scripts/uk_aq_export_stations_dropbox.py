#!/usr/bin/env python3
"""
Export all stations from Supabase and upload a timestamped JSON file to Dropbox.
"""

from __future__ import annotations

import argparse
import binascii
import json
import os
import re
import struct
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()

DROPBOX_TOKEN_URL = "https://api.dropbox.com/oauth2/token"
DROPBOX_UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload"

DEFAULT_PAGE_SIZE = 1000
DEFAULT_DROPBOX_DIR = "uk_aq_stations"
DEFAULT_SUMMARY_FILENAME = "daily_summary_{date}.json"

SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")

OPENAQ_PROVIDER_SHORTNAMES = {
    "London Air Quality Network": "LAQN",
}

DEFAULT_ERROR_LOG_DIR = "error_log"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export stations from Supabase and upload to Dropbox."
    )
    parser.add_argument(
        "--dropbox-dir",
        default=os.getenv("UK_AQ_STATIONS_DROPBOX_DIR", DEFAULT_DROPBOX_DIR),
        help="Dropbox folder for uploads (default: uk_aq_stations).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional local output path (default: ./uk_aq_stations_<timestamp>.json).",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help="Supabase page size (default: 1000).",
    )
    parser.add_argument(
        "--summary-output",
        default=DEFAULT_SUMMARY_FILENAME,
        help="Daily summary JSON output filename (default: daily_summary_{YYYY-MM-DD}.json).",
    )
    parser.add_argument(
        "--summary-openaq-json",
        default="openaq_stations.json",
        help="Path to OpenAQ stations JSON for provider counts (default: openaq_stations.json).",
    )
    parser.add_argument(
        "--skip-summary",
        action="store_true",
        help="Skip writing the daily summary JSON.",
    )
    return parser.parse_args()


def _dropbox_refresh_access_token() -> str:
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


def _dropbox_upload_file(access_token: str, local_path: Path, dropbox_path: str) -> None:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Dropbox-API-Arg": json.dumps(
            {
                "path": dropbox_path,
                "mode": "add",
                "autorename": True,
                "mute": False,
            }
        ),
        "Content-Type": "application/octet-stream",
    }
    with local_path.open("rb") as handle:
        resp = requests.post(DROPBOX_UPLOAD_URL, headers=headers, data=handle, timeout=120)
    if resp.status_code >= 400:
        raise RuntimeError(f"Dropbox upload failed ({resp.status_code}): {resp.text}")


def _normalize_dropbox_path(path: str) -> str:
    cleaned = (path or "").strip()
    if not cleaned:
        return ""
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    return cleaned.rstrip("/")


def _dropbox_root_folder() -> str:
    return _normalize_dropbox_path(os.getenv("UK_AQ_DROPBOX_ROOT", ""))


def _join_dropbox_paths(root: str, subdir: str) -> str:
    root_clean = _normalize_dropbox_path(root)
    sub_clean = _normalize_dropbox_path(subdir).lstrip("/")
    if not root_clean:
        return f"/{sub_clean}" if sub_clean else ""
    if not sub_clean:
        return root_clean
    return f"{root_clean}/{sub_clean}"


def _timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _error_log_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _stations_month_folder() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _summary_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _error_log_dir() -> Path:
    log_dir = Path(DEFAULT_ERROR_LOG_DIR) / _error_log_date()
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _error_log_payload(
    exc: BaseException,
    args: Optional[argparse.Namespace],
) -> Dict[str, Any]:
    return {
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "script": Path(__file__).name,
        "error_type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
        "args": vars(args) if args else {},
        "env": {
            "SUPABASE_URL_set": bool(os.getenv("SUPABASE_URL")),
            "SB_SECRET_KEY_set": bool(os.getenv("SB_SECRET_KEY")),
            "SUPABASE_DB_URL_set": bool(os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")),
            "DROPBOX_APP_KEY_set": bool(os.getenv("DROPBOX_APP_KEY")),
            "DROPBOX_APP_SECRET_set": bool(os.getenv("DROPBOX_APP_SECRET")),
            "DROPBOX_REFRESH_TOKEN_set": bool(os.getenv("DROPBOX_REFRESH_TOKEN")),
            "UK_AQ_DROPBOX_ROOT_set": bool(os.getenv("UK_AQ_DROPBOX_ROOT")),
            "UK_AQ_STATIONS_DROPBOX_DIR_set": bool(os.getenv("UK_AQ_STATIONS_DROPBOX_DIR")),
        },
    }


def _write_error_log(payload: Dict[str, Any]) -> Path:
    log_dir = _error_log_dir()
    filename = f"uk_aq_error_{_timestamp_utc()}_{uuid.uuid4()}.json"
    log_path = log_dir / filename
    log_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return log_path


def _upload_error_log(local_path: Path) -> None:
    root = _dropbox_root_folder()
    if not root:
        print("Skipping Dropbox error log upload: UK_AQ_DROPBOX_ROOT not set.")
        return
    dropbox_dir = _join_dropbox_paths(root, DEFAULT_ERROR_LOG_DIR)
    dropbox_dir = _join_dropbox_paths(dropbox_dir, _error_log_date())
    dropbox_path = f"{dropbox_dir}/{local_path.name}"
    access_token = _dropbox_refresh_access_token()
    _dropbox_upload_file(access_token, local_path, dropbox_path)
    print(f"Uploaded error log to Dropbox: {dropbox_path}")


def _normalize_geometry(value: Any) -> Optional[Any]:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return value
    return None


def _coords_from_geometry(value: Any) -> Tuple[Optional[float], Optional[float]]:
    if value is None:
        return None, None
    if isinstance(value, dict):
        coords = value.get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            lon, lat = coords[0], coords[1]
            if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
                return float(lat), float(lon)
        return None, None
    if isinstance(value, str):
        try:
            raw = binascii.unhexlify(value)
        except (binascii.Error, ValueError):
            return None, None
        if len(raw) < 21:
            return None, None
        endian_flag = raw[0]
        if endian_flag == 0:
            endian = ">"
        elif endian_flag == 1:
            endian = "<"
        else:
            return None, None
        offset = 1
        try:
            geom_type = struct.unpack(f"{endian}I", raw[offset:offset + 4])[0]
        except struct.error:
            return None, None
        offset += 4
        has_srid = bool(geom_type & 0x20000000)
        base_type = geom_type & 0xFF
        if base_type != 1:
            return None, None
        if has_srid:
            if len(raw) < offset + 4:
                return None, None
            offset += 4
        if len(raw) < offset + 16:
            return None, None
        try:
            x, y = struct.unpack(f"{endian}dd", raw[offset:offset + 16])
        except struct.error:
            return None, None
        return float(y), float(x)
    return None, None


def _db_connect():
    if not SUPABASE_DB_URL:
        raise RuntimeError("Missing SUPABASE_DB_URL (or DATABASE_URL).")
    try:
        import psycopg2  # type: ignore
    except ImportError as exc:
        raise RuntimeError("psycopg2 is required to export stations.") from exc
    return psycopg2.connect(SUPABASE_DB_URL)


def _iter_stations(page_size: int) -> Iterable[Dict[str, Any]]:
    conn = _db_connect()
    query = """
        select
          stn.id,
          stn.station_ref,
          stn.label,
          stn.station_name,
          stn.station_type,
          stn.station_exposure,
          stn.region,
          stn.la_code,
          stn.la_version,
          stn.pcon_code,
          stn.pcon_version,
          stn.service_ref,
          stn.connector_id,
          stn.network_id,
          n.network_code,
          n.display_name as network_label,
          stn.removed_at,
          stn.last_seen_at,
          stn.created_at,
          st_x(stn.geometry::geometry) as longitude,
          st_y(stn.geometry::geometry) as latitude,
          st_asewkt(stn.geometry::geometry) as geometry,
          c.connector_code
        from uk_aq_core.stations stn
        join uk_aq_core.connectors c on c.id = stn.connector_id
        join uk_aq_core.networks n on n.id = stn.network_id
        order by stn.id
    """
    try:
        with conn, conn.cursor() as cursor:
            cursor.itersize = page_size
            cursor.execute(query)
            cols = [desc[0] for desc in cursor.description]
            while True:
                rows = cursor.fetchmany(page_size)
                if not rows:
                    break
                for row in rows:
                    yield dict(zip(cols, row))
    finally:
        conn.close()


def _fetch_connector_counts(conn) -> List[Dict[str, Any]]:
    query = """
        select
          c.connector_code,
          count(*) as station_count,
          count(*) filter (where s.removed_at is null) as active_count
        from uk_aq_core.stations s
        join uk_aq_core.connectors c on c.id = s.connector_id
        group by c.connector_code
        order by c.connector_code
    """
    with conn.cursor() as cursor:
        cursor.execute(query)
        rows = cursor.fetchall()
    return [
        {
            "connector_code": row[0],
            "station_count": int(row[1]),
            "active_count": int(row[2]),
        }
        for row in rows
    ]


def _fetch_network_counts(conn) -> List[Dict[str, Any]]:
    query = """
        select
          n.network_code,
          n.display_name as network_label,
          count(s.id) as station_count,
          count(s.id) filter (where s.removed_at is null) as active_count
        from uk_aq_core.networks n
        left join uk_aq_core.stations s on s.network_id = n.id
        group by n.id, n.network_code, n.display_name
        order by n.network_code
    """
    with conn.cursor() as cursor:
        cursor.execute(query)
        rows = cursor.fetchall()
    return [
        {
            "network_code": row[0],
            "network_label": row[1],
            "station_count": int(row[2]),
            "active_count": int(row[3]),
        }
        for row in rows
    ]


def _fetch_station_totals(conn) -> Dict[str, int]:
    query = """
        select
          count(*) as station_count,
          count(*) filter (where removed_at is null) as active_count
        from uk_aq_core.stations
    """
    with conn.cursor() as cursor:
        cursor.execute(query)
        row = cursor.fetchone()
    if not row:
        return {"station_count": 0, "active_count": 0}
    return {"station_count": int(row[0]), "active_count": int(row[1])}


def _openaq_provider_counts(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "source": str(path),
            "missing": True,
            "providers": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    stations = payload.get("stations") or []
    counts: Dict[str, int] = {}
    for station in stations:
        provider = station.get("provider")
        if provider is None:
            continue
        provider = str(provider).strip()
        if not provider:
            continue
        provider = OPENAQ_PROVIDER_SHORTNAMES.get(provider, provider)
        counts[provider] = counts.get(provider, 0) + 1
    providers = [
        {"provider": name, "station_count": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    ]
    return {
        "source": str(path),
        "missing": False,
        "providers": providers,
    }


def _build_summary(summary_openaq_path: Path) -> Dict[str, Any]:
    conn = _db_connect()
    try:
        totals = _fetch_station_totals(conn)
        connectors = _fetch_connector_counts(conn)
        networks = _fetch_network_counts(conn)
    finally:
        conn.close()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_definition": "stations.removed_at is null",
        "station_totals": totals,
        "connectors": connectors,
        "networks": networks,
        "openaq_providers": _openaq_provider_counts(summary_openaq_path),
    }


def main() -> int:
    args: Optional[argparse.Namespace] = None
    try:
        args = parse_args()
        timestamp = _timestamp_utc()
        output_path = Path(args.output) if args.output else Path(f"uk_aq_stations_{timestamp}.json")
        root = _dropbox_root_folder()
        if not root:
            raise RuntimeError("UK_AQ_DROPBOX_ROOT must be set for stations export.")
        dropbox_dir = _join_dropbox_paths(root, args.dropbox_dir or "uk_aq_stations")
        dropbox_dir = _join_dropbox_paths(dropbox_dir, _stations_month_folder())
        dropbox_path = f"{dropbox_dir}/{output_path.name}"

        stations: List[Dict[str, Any]] = []
        for row in _iter_stations(args.page_size):
            geometry = _normalize_geometry(row.get("geometry"))
            lat = row.get("latitude")
            lon = row.get("longitude")
            if lat is None or lon is None:
                lat, lon = _coords_from_geometry(geometry)
            coordinates = None
            if lat is not None and lon is not None:
                coordinates = f"{lat:.6f} {lon:.6f}"
            stations.append(
                {
                    "id": row.get("id"),
                    "station_ref": row.get("station_ref"),
                    "label": row.get("label"),
                    "station_name": row.get("station_name"),
                    "station_type": row.get("station_type"),
                    "station_exposure": row.get("station_exposure"),
                    "coordinates": coordinates,
                    "region": row.get("region"),
                    "la_code": row.get("la_code"),
                    "la_version": row.get("la_version"),
                    "pcon_code": row.get("pcon_code"),
                    "pcon_version": row.get("pcon_version"),
                    "geometry": geometry,
                    "service_ref": row.get("service_ref"),
                    "connector_id": row.get("connector_id"),
                    "connector_code": row.get("connector_code"),
                    "network_id": row.get("network_id"),
                    "network_code": row.get("network_code"),
                    "network_label": row.get("network_label"),
                }
            )

        payload = {
            "source": "supabase",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "count": len(stations),
            "stations": stations,
        }

        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        access_token = _dropbox_refresh_access_token()
        _dropbox_upload_file(access_token, output_path, dropbox_path)
        print(f"Dropbox root: {root}")
        print(f"Uploaded {output_path.name} to Dropbox: {dropbox_path}")

        if not args.skip_summary:
            date_tag = _summary_date()
            summary_name = (args.summary_output or DEFAULT_SUMMARY_FILENAME)
            summary_name = summary_name.replace("{YYYY-MM-DD}", date_tag).replace("{date}", date_tag)
            summary_path = Path(summary_name)
            summary_payload = _build_summary(Path(args.summary_openaq_json))
            summary_path.write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")
            summary_dropbox_path = f"{dropbox_dir}/{summary_path.name}"
            _dropbox_upload_file(access_token, summary_path, summary_dropbox_path)
            print(f"Uploaded {summary_path.name} to Dropbox: {summary_dropbox_path}")
        return 0
    except Exception as exc:
        payload = _error_log_payload(exc, args)
        log_path = _write_error_log(payload)
        print(f"Wrote error log to {log_path}")
        try:
            _upload_error_log(log_path)
        except Exception as upload_exc:
            print(f"Failed to upload error log to Dropbox: {upload_exc}")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
