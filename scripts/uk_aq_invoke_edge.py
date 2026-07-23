#!/usr/bin/env python3
"""
Invoke Supabase Edge Functions for UK AQ.

Examples:
  python3 scripts/uk_aq_invoke_edge.py --function ingest_breathelondon --connector-code blondon_communities
  python3 scripts/uk_aq_invoke_edge.py --function ingest_sensorcommunity --connector-code sensorcommunity --payload '{"dry_run":true}'
  python3 scripts/uk_aq_invoke_edge.py --function uk_aq_latest --connector-code blondon_communities --method GET --params '{"limit":5}'
"""

import argparse
import json
import os
from typing import Any, Dict, Optional

import requests


def parse_json(value: Optional[str], label: str) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON for {label}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit(f"{label} must be a JSON object.")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Invoke Supabase Edge Functions.")
    parser.add_argument("--function", required=True, help="Edge function name.")
    parser.add_argument(
        "--connector-code",
        required=True,
        help="Connector code (required for ingest functions; included in requests).",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("SUPABASE_URL"),
        help="Supabase URL (defaults to SUPABASE_URL).",
    )
    parser.add_argument(
        "--publishable-key",
        default=os.getenv("SB_PUBLISHABLE_DEFAULT_KEY"),
        help="Supabase publishable key (defaults to SB_PUBLISHABLE_DEFAULT_KEY).",
    )
    parser.add_argument(
        "--cron-secret",
        default=os.getenv("SB_UK_AQ_CRON_SECRET"),
        help="Cron secret header (defaults to SB_UK_AQ_CRON_SECRET).",
    )
    parser.add_argument(
        "--method",
        choices=("GET", "POST"),
        help="HTTP method (default: POST for ingest_*, GET otherwise).",
    )
    parser.add_argument("--params", help="JSON object for query params.")
    parser.add_argument("--payload", help="JSON object for request body.")
    parser.add_argument("--payload-file", help="JSON file for request body.")
    parser.add_argument("--timeout", type=int, default=60, help="Request timeout seconds.")
    parser.add_argument(
        "--print-response",
        action="store_true",
        help="Print the response body.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = (args.base_url or "").rstrip("/")
    if not base_url:
        raise SystemExit("SUPABASE_URL (or --base-url) is required.")
    publishable_key = args.publishable_key or ""
    if not publishable_key:
        raise SystemExit("SB_PUBLISHABLE_DEFAULT_KEY (or --publishable-key) is required.")

    method = args.method or ("POST" if args.function.startswith("ingest_") else "GET")
    params = parse_json(args.params, "params")
    payload = parse_json(args.payload, "payload")
    if args.payload_file:
        payload_path = args.payload_file
        try:
            with open(payload_path, "r", encoding="utf-8") as handle:
                payload.update(json.load(handle))
        except OSError as exc:
            raise SystemExit(f"Failed to read payload file: {exc}") from exc

    if method == "GET":
        params.setdefault("connector_code", args.connector_code)
    else:
        payload.setdefault("connector_code", args.connector_code)

    headers = {
        "Authorization": f"Bearer {publishable_key}",
        "apikey": publishable_key,
        "Content-Type": "application/json",
    }
    if args.cron_secret:
        headers["X-Cron-Secret"] = args.cron_secret

    url = f"{base_url}/functions/v1/{args.function}"
    resp = requests.request(
        method,
        url,
        headers=headers,
        params=params if params else None,
        json=payload if method == "POST" else None,
        timeout=args.timeout,
    )
    print(f"{method} {url} -> {resp.status_code}")
    if args.print_response:
        print(resp.text)
    return 0 if resp.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
