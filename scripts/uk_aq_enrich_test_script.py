#!/usr/bin/env python3
"""
Debug Supabase REST counts for stations missing station_name.

Examples:
  python3 scripts/uk_aq_enrich_test_script.py
  python3 scripts/uk_aq_enrich_test_script.py --samples 10 --verbose
"""

import argparse
import json
import os
import re
import sys
from typing import List, Optional, Tuple
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

DEFAULT_TIMEOUT = 30


def _redact(value: Optional[str], keep: int = 4) -> str:
    if not value:
        return "<missing>"
    if len(value) <= keep * 2:
        return value
    return f"{value[:keep]}...{value[-keep:]}"


def _project_ref(url: str) -> Optional[str]:
    match = re.search(r"https?://([^./]+)\.supabase\.(co|in)", url)
    if match:
        return match.group(1)
    return None


def _rest_base(url: str) -> str:
    return url.rstrip("/") + "/rest/v1"


def _parse_count(content_range: str) -> Optional[int]:
    match = re.search(r"/(\d+)$", content_range)
    if not match:
        return None
    return int(match.group(1))


def _request(
    base_url: str,
    headers: dict,
    params: List[Tuple[str, str]],
    timeout: int,
    verbose: bool,
) -> requests.Response:
    url = f"{base_url}/stations"
    if verbose:
        print(f"Request URL: {url}?{urlencode(params)}")
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    if verbose:
        print(f"Status: {resp.status_code}")
    return resp


def _count_query(
    base_url: str,
    headers: dict,
    params: List[Tuple[str, str]],
    label: str,
    timeout: int,
    verbose: bool,
) -> Optional[int]:
    resp = _request(base_url, headers, params, timeout, verbose)
    content_range = resp.headers.get("content-range", "")
    count = _parse_count(content_range)
    print(f"{label}: status={resp.status_code} content-range='{content_range}' count={count}")
    if resp.status_code >= 400:
        print("Response body:")
        print(resp.text[:2000])
        return None
    if count is None:
        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            count = len(data)
            print(f"{label}: fallback list length={count}")
    return count


def _sample_rows(
    base_url: str,
    headers: dict,
    params: List[Tuple[str, str]],
    label: str,
    timeout: int,
    verbose: bool,
) -> None:
    resp = _request(base_url, headers, params, timeout, verbose)
    print(f"{label}: status={resp.status_code}")
    if resp.status_code >= 400:
        print("Response body:")
        print(resp.text[:2000])
        return
    try:
        data = resp.json()
    except json.JSONDecodeError:
        print("Response body is not JSON.")
        return
    if not data:
        print(f"{label}: no rows returned.")
        return
    print(f"{label}: showing {len(data)} rows")
    print(json.dumps(data, indent=2, ensure_ascii=True)[:4000])


def _build_headers(service_role_key: str, profile: Optional[str]) -> dict:
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Prefer": "count=exact",
    }
    if profile:
        headers["Accept-Profile"] = profile
        headers["Content-Profile"] = profile
    return headers


def _print_env_summary(supabase_url: str, service_role_key: str, profile: Optional[str]) -> None:
    print("Supabase URL:", supabase_url)
    print("Supabase project ref:", _project_ref(supabase_url) or "<unknown>")
    print("Service role key:", _redact(service_role_key))
    print("REST base:", _rest_base(supabase_url))
    if profile:
        print("REST profile:", profile)
    else:
        print("REST profile: <default>")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Debug REST counts for station_name enrichment.",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=5,
        help="Number of sample rows to fetch for null station_name.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional PostgREST profile/schema (Accept-Profile).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed request info.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    supabase_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SB_SECRET_KEY")
    default_profile = os.getenv("UK_AQ_CORE_SCHEMA", "uk_aq_core")
    if not supabase_url or not service_role_key:
        raise SystemExit("SUPABASE_URL and SB_SECRET_KEY are required.")

    base_url = _rest_base(supabase_url)
    profile = args.profile or default_profile
    headers = _build_headers(service_role_key, profile)

    _print_env_summary(supabase_url, service_role_key, profile)

    print("")
    print("Counts (REST):")
    total_params = [("select", "id")]
    _count_query(base_url, headers, total_params, "stations_total", args.timeout, args.verbose)

    null_params = [("select", "id"), ("station_name", "is.null")]
    _count_query(base_url, headers, null_params, "station_name_null", args.timeout, args.verbose)

    null_geom_params = [
        ("select", "id"),
        ("station_name", "is.null"),
        ("geometry", "not.is.null"),
    ]
    _count_query(
        base_url,
        headers,
        null_geom_params,
        "station_name_null_with_geometry",
        args.timeout,
        args.verbose,
    )

    null_geom_missing_params = [
        ("select", "id"),
        ("station_name", "is.null"),
        ("geometry", "is.null"),
    ]
    _count_query(
        base_url,
        headers,
        null_geom_missing_params,
        "station_name_null_without_geometry",
        args.timeout,
        args.verbose,
    )

    blank_params = [("select", "id"), ("station_name", "eq.")]
    _count_query(
        base_url,
        headers,
        blank_params,
        "station_name_empty_string",
        args.timeout,
        args.verbose,
    )

    if args.samples > 0:
        print("")
        print("Sample rows (REST):")
        sample_params = [
            ("select", "id,station_ref,label,station_name,geometry,connector_id,service_ref"),
            ("station_name", "is.null"),
            ("limit", str(args.samples)),
        ]
        _sample_rows(
            base_url,
            headers,
            sample_params,
            "station_name_null_samples",
            args.timeout,
            args.verbose,
        )

        sample_geom_params = [
            ("select", "id,station_ref,label,station_name,geometry,connector_id,service_ref"),
            ("station_name", "is.null"),
            ("geometry", "not.is.null"),
            ("limit", str(args.samples)),
        ]
        _sample_rows(
            base_url,
            headers,
            sample_geom_params,
            "station_name_null_with_geometry_samples",
            args.timeout,
            args.verbose,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
