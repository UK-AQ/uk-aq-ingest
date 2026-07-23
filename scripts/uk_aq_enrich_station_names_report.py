#!/usr/bin/env python3
"""
Write station name enrichment results to JSON files.

Outputs:
  - station_names_proposed.json (summary of all station_name null rows)
  - station_names_missing.json (detailed payloads where proposed name is null)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from uk_aq_enrich_station_names import build_station_summary, iter_station_payloads, parse_args

LOG = logging.getLogger("uk_aq_enrich_station_names_report")

_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
PROPOSED_OUTPUT_PATH = Path(f"station_names_proposed_{_TIMESTAMP}.json")
MISSING_OUTPUT_PATH = Path(f"station_names_missing_{_TIMESTAMP}.json")


def _closest_distance(matches: Iterable[Dict[str, Any]]) -> Optional[float]:
    for match in matches:
        return match.get("distance_m")
    return None


def _missing_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    if payload.get("group") == "ni_station":
        place_matches = payload.get("ni_place_matches") or []
        street_matches = payload.get("ni_street_matches") or []
        return {
            "place_match_count": len(place_matches),
            "street_match_count": len(street_matches),
            "closest_place_distance_m": _closest_distance(place_matches),
            "closest_street_distance_m": _closest_distance(street_matches),
        }
    gb_matches = payload.get("gb_matches") or []
    gb_place_matches = payload.get("gb_place_matches") or []
    gb_street_matches = payload.get("gb_street_matches") or []
    gb_other_matches = payload.get("gb_other_matches") or []
    return {
        "gb_match_count": len(gb_matches),
        "gb_place_match_count": len(gb_place_matches),
        "gb_street_match_count": len(gb_street_matches),
        "gb_other_match_count": len(gb_other_matches),
        "closest_gb_distance_m": _closest_distance(gb_matches),
        "closest_gb_place_distance_m": _closest_distance(gb_place_matches),
        "closest_gb_street_distance_m": _closest_distance(gb_street_matches),
    }


def _write_json_item(handle, item: Dict[str, Any], first: bool) -> bool:
    if not first:
        handle.write(",\n")
    handle.write(json.dumps(item, ensure_ascii=True, indent=2))
    return False


def _missing_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    detail = dict(payload)
    summary = build_station_summary(payload)
    detail["summary"] = summary
    detail["coordinates"] = summary.get("coordinates")
    detail["missing_summary"] = _missing_summary(payload)
    return detail


def main() -> int:
    args = parse_args()
    args.output_format = "summary"
    args.include_pollutants = True
    args.include_latest = True

    PROPOSED_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    MISSING_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    proposed_count = 0
    missing_count = 0
    with PROPOSED_OUTPUT_PATH.open("w", encoding="utf-8") as proposed_handle, (
        MISSING_OUTPUT_PATH.open("w", encoding="utf-8")
    ) as missing_handle:
        proposed_handle.write("[\n")
        missing_handle.write("[\n")
        first_proposed = True
        first_missing = True
        for payload in iter_station_payloads(args) or []:
            summary = build_station_summary(payload)
            first_proposed = _write_json_item(proposed_handle, summary, first_proposed)
            proposed_count += 1
            if payload.get("proposed_station_name") is None:
                detail = _missing_payload(payload)
                first_missing = _write_json_item(missing_handle, detail, first_missing)
                missing_count += 1
        proposed_handle.write("\n]\n")
        missing_handle.write("\n]\n")

    LOG.info("Wrote %s summaries to %s", proposed_count, PROPOSED_OUTPUT_PATH)
    LOG.info("Wrote %s missing proposals to %s", missing_count, MISSING_OUTPUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
