#!/usr/bin/env python3
"""
Probe Breathe London Nodes /SensorData response shape.

Uses:
  BLONDON_NODES_API_KEY
  optional BLONDON_NODES_BASE_URL

Examples:
  python3 scripts/blondon_nodes/blondon_nodes_probe_sensor_data.py

  python3 scripts/blondon_nodes/blondon_nodes_probe_sensor_data.py \
    --site BL0001 --site BL0024 --hours 24

  python3 scripts/blondon_nodes/blondon_nodes_probe_sensor_data.py \
    --species PM25 --species NO2 --hours 48 --output blondon_nodes_sensor_data_probe.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

import requests


DEFAULT_BASE_URL = "https://breathe-london-7x54d7qf.ew.gateway.dev"
DEFAULT_SITES = ["BL0001", "BL0024"]
DEFAULT_SPECIES = ["PM25", "NO2", "PM25Index", "NO2Index"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def api_key() -> str:
    key = (os.getenv("BLONDON_NODES_API_KEY") or "").strip()
    if not key:
        raise SystemExit("Missing BLONDON_NODES_API_KEY")
    return key


def base_url() -> str:
    return (os.getenv("BLONDON_NODES_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"_non_json_text": resp.text[:2000]}


def flatten_payload(payload: Any) -> List[Dict[str, Any]]:
    """
    Try to find row-like dicts inside common API payload shapes.
    """
    if isinstance(payload, list):
        if payload and all(isinstance(x, dict) for x in payload):
            return list(payload)
        rows: List[Dict[str, Any]] = []
        for item in payload:
            rows.extend(flatten_payload(item))
        return rows

    if isinstance(payload, dict):
        for key in (
            "data",
            "rows",
            "results",
            "Result",
            "Results",
            "sensorData",
            "SensorData",
            "measurements",
            "Measurements",
            "values",
            "Values",
        ):
            value = payload.get(key)
            rows = flatten_payload(value)
            if rows:
                return rows

        # A single measurement row.
        if any(k in payload for k in ("SiteCode", "siteCode", "Species", "species", "ScaledValue", "DateTime")):
            return [payload]

    return []


def field_names(rows: Sequence[Dict[str, Any]]) -> List[str]:
    fields = set()
    for row in rows:
        fields.update(str(k) for k in row.keys())
    return sorted(fields)


def species_values(rows: Sequence[Dict[str, Any]]) -> Counter:
    values: Counter = Counter()
    for row in rows:
        for key in ("Species", "species", "Pollutant", "pollutant", "Parameter", "parameter"):
            if row.get(key) is not None:
                values[str(row.get(key))] += 1
                break
    return values


def null_scaled_value_count(rows: Sequence[Dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        for key in ("ScaledValue", "scaledValue", "Value", "value"):
            if key in row:
                if row.get(key) is None:
                    count += 1
                break
    return count


def candidate_requests(
    site: str,
    species: str,
    start: datetime,
    end: datetime,
) -> List[Tuple[str, str, Optional[Dict[str, str]]]]:
    """
    We do not yet know the exact /SensorData contract, so try a small set of
    plausible query and path formats and record which one works.
    """
    start_iso = iso_z(start)
    end_iso = iso_z(end)

    # Some older Breathe London endpoints used this readable GMT date style.
    start_gmt = start.astimezone(timezone.utc).strftime("%a %d %b %Y %H:%M:%S GMT")
    end_gmt = end.astimezone(timezone.utc).strftime("%a %d %b %Y %H:%M:%S GMT")

    return [
        (
            "/SensorData",
            "query_site_species_start_end_camel",
            {
                "siteCode": site,
                "species": species,
                "startTime": start_iso,
                "endTime": end_iso,
            },
        ),
        (
            "/SensorData",
            "query_site_species_start_end_documented",
            {
                "SiteCode": site,
                "Species": species,
                "startTime": start_iso,
                "endTime": end_iso,
            },
        ),
        (
            "/SensorData",
            "query_site_species_date_pascal",
            {
                "SiteCode": site,
                "Species": species,
                "StartDate": start_iso,
                "EndDate": end_iso,
            },
        ),
        (
            "/SensorData",
            "query_site_pollutant_date_pascal",
            {
                "SiteCode": site,
                "Pollutant": species,
                "StartDate": start_iso,
                "EndDate": end_iso,
            },
        ),
        (
            "/SensorData",
            "query_site_parameter_date_pascal",
            {
                "SiteCode": site,
                "Parameter": species,
                "StartDate": start_iso,
                "EndDate": end_iso,
            },
        ),
        (
            f"/SensorData/{quote(site)}/{quote(species)}/{quote(start_iso)}/{quote(end_iso)}",
            "path_site_species_iso",
            None,
        ),
        (
            f"/SensorData/{quote(site)}/{quote(species)}/{quote(start_gmt)}/{quote(end_gmt)}/Hourly",
            "path_site_species_gmt_hourly",
            None,
        ),
    ]


def request_once(
    session: requests.Session,
    url: str,
    params: Optional[Dict[str, str]],
    timeout: int,
) -> Tuple[int, Any, str]:
    resp = session.get(url, params=params, timeout=timeout)
    payload = safe_json(resp)
    return resp.status_code, payload, resp.url


def summarise_payload(payload: Any, max_rows: int) -> Dict[str, Any]:
    rows = flatten_payload(payload)
    return {
        "payload_type": type(payload).__name__,
        "row_count": len(rows),
        "field_names": field_names(rows),
        "species_values": dict(species_values(rows)),
        "null_scaled_value_count": null_scaled_value_count(rows),
        "sample_rows": rows[:max_rows],
        "top_level_keys": sorted(payload.keys()) if isinstance(payload, dict) else None,
    }


def probe_site_species(
    session: requests.Session,
    base: str,
    site: str,
    species: str,
    start: datetime,
    end: datetime,
    timeout: int,
    max_rows: int,
    include_failures: bool,
) -> Dict[str, Any]:
    attempts: List[Dict[str, Any]] = []

    for path, candidate_name, params in candidate_requests(site, species, start, end):
        url = f"{base}{path}"
        try:
            status, payload, final_url = request_once(session, url, params, timeout)
        except requests.RequestException as exc:
            attempts.append(
                {
                    "candidate": candidate_name,
                    "status": "request_exception",
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
            continue

        summary = summarise_payload(payload, max_rows=max_rows)
        attempt = {
            "candidate": candidate_name,
            "status": status,
            "url_without_key": final_url,
            **summary,
        }

        if status == 200 and summary["row_count"] > 0:
            attempt["selected"] = True
            attempts.append(attempt)
            return {
                "site": site,
                "species": species,
                "selected": attempt,
                "failures": attempts[:-1] if include_failures else [],
            }

        if include_failures or status == 200:
            attempts.append(attempt)

    return {
        "site": site,
        "species": species,
        "selected": None,
        "failures": attempts,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", action="append", dest="sites", default=None)
    parser.add_argument("--species", action="append", dest="species", default=None)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--max-sample-rows", type=int, default=3)
    parser.add_argument("--output", default="blondon_nodes_sensor_data_probe.json")
    parser.add_argument("--include-failures", action="store_true")
    args = parser.parse_args()

    sites = args.sites or DEFAULT_SITES
    species_list = args.species or DEFAULT_SPECIES

    end = utcnow()
    start = end - timedelta(hours=args.hours)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "uk-air-quality-networks/blondon-nodes-probe",
            "X-API-KEY": api_key(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )

    result: Dict[str, Any] = {
        "base_url": base_url(),
        "start": iso_z(start),
        "end": iso_z(end),
        "sites": sites,
        "species": species_list,
        "results": [],
    }

    print(f"Probing Breathe London Nodes SensorData")
    print(f"base_url={result['base_url']}")
    print(f"window={result['start']} to {result['end']}")
    print(f"sites={','.join(sites)}")
    print(f"species={','.join(species_list)}")

    for site in sites:
        for species in species_list:
            probe = probe_site_species(
                session=session,
                base=result["base_url"],
                site=site,
                species=species,
                start=start,
                end=end,
                timeout=args.timeout,
                max_rows=args.max_sample_rows,
                include_failures=args.include_failures,
            )
            result["results"].append(probe)

            selected = probe.get("selected")
            if selected:
                print(
                    "FOUND "
                    f"site={site} species={species} "
                    f"candidate={selected['candidate']} "
                    f"rows={selected['row_count']} "
                    f"fields={','.join(selected['field_names'])}"
                )
            else:
                print(f"NO_ROWS site={site} species={species}")

    output_path = Path(args.output)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(f"Wrote {output_path}")

    found_count = sum(1 for item in result["results"] if item.get("selected"))
    if found_count == 0:
        print("No working SensorData shape found. Re-run with --include-failures to inspect responses.")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())