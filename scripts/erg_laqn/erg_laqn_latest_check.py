#!/usr/bin/env python3
"""
Check latest ERG LAQN observations for a sample of sites/species.

Example:
  python3 scripts/erg_laqn/erg_laqn_latest_check.py --days 2 --species NO2,PM10
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


DEFAULT_BASE_URL = (os.getenv("LAQN_BASE_URL") or "https://api.erg.ic.ac.uk/AirQuality").rstrip(
    "/"
)
DEFAULT_STATIONS_JSON = os.getenv("LAQN_STATIONS_JSON") or "erg_laqn_stations.json"


def _extract_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        nested = payload.get("RawAQData") or payload.get("rawAQData") or payload
        if isinstance(nested, dict):
            for key in ("RawData", "rawData", "Data", "data", "Measurements", "measurements"):
                value = nested.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
    return []


def _parse_datetime(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        return None


def _latest_observed(rows: List[Dict[str, Any]]) -> Optional[dt.datetime]:
    latest: Optional[dt.datetime] = None
    for row in rows:
        observed = _parse_datetime(
            row.get("@MeasurementDateGMT")
            or row.get("@MeasurementDate")
            or row.get("@DateTimeGMT")
            or row.get("@DateTime")
            or row.get("DateTimeGMT")
            or row.get("DateTime")
            or row.get("Date")
        )
        if observed is None:
            continue
        if latest is None or observed > latest:
            latest = observed
    return latest


def _load_sites(path: str, max_sites: int) -> List[str]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    stations = payload.get("stations") if isinstance(payload, dict) else None
    if not isinstance(stations, list):
        raise ValueError(f"Invalid stations JSON: {path}")
    sites = [
        station.get("station_ref")
        for station in stations
        if isinstance(station, dict) and station.get("station_ref") and not station.get("removed_at")
    ]
    return sites[:max_sites]


def _fetch_json(url: str, timeout: int) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check latest ERG LAQN observations.")
    parser.add_argument("--days", type=int, default=2, help="Lookback window in days (default: 2).")
    parser.add_argument(
        "--species",
        default="NO2",
        help="Comma-separated species list (default: NO2).",
    )
    parser.add_argument(
        "--max-sites",
        type=int,
        default=5,
        help="Max number of active sites to test (default: 5).",
    )
    parser.add_argument(
        "--stations-json",
        default=DEFAULT_STATIONS_JSON,
        help=f"Stations JSON path (default: {DEFAULT_STATIONS_JSON}).",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"ERG base URL (default: {DEFAULT_BASE_URL}).",
    )
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds.")
    args = parser.parse_args()

    species_list = [s.strip() for s in args.species.split(",") if s.strip()]
    if not species_list:
        print("No species provided.", file=sys.stderr)
        return 2

    try:
        sites = _load_sites(args.stations_json, args.max_sites)
    except (OSError, ValueError) as exc:
        print(f"Failed to load stations JSON: {exc}", file=sys.stderr)
        return 2

    end_date = dt.datetime.utcnow().date()
    start_date = end_date - dt.timedelta(days=max(args.days, 0))
    start_str = start_date.isoformat()
    end_str = end_date.isoformat()

    for site in sites:
        for species in species_list:
            url = (
                f"{args.base_url}/Data/SiteSpecies/SiteCode={site}/SpeciesCode={species}"
                f"/StartDate={start_str}/EndDate={end_str}/Json"
            )
            try:
                payload = _fetch_json(url, timeout=args.timeout)
                latest = _latest_observed(_extract_rows(payload))
                if latest:
                    print(f"{site} {species}: {latest.isoformat(sep=' ')}")
                else:
                    print(f"{site} {species}: no data")
            except urllib.error.HTTPError as exc:
                print(f"{site} {species}: HTTP {exc.code}")
            except urllib.error.URLError as exc:
                print(f"{site} {species}: error {exc.reason}")
            except json.JSONDecodeError:
                print(f"{site} {species}: non-JSON response")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
