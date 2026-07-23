#!/usr/bin/env python3
"""
List available ERG LAQN groups.

Examples:
  python3 scripts/erg_laqn/erg_laqn_list_groups.py
  python3 scripts/erg_laqn/erg_laqn_list_groups.py --format json
  python3 scripts/erg_laqn/erg_laqn_list_groups.py --output laqn_groups.json --format json
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.request import Request, urlopen

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if PROJECT_ROOT.name == "scripts":
    PROJECT_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()

LAQN_BASE_URL = (os.getenv("LAQN_BASE_URL") or "https://api.erg.ic.ac.uk/AirQuality").rstrip(
    "/"
)
LAQN_USER_AGENT = os.getenv("LAQN_USER_AGENT", "uk-air-quality-networks")


def _extract_groups(payload: Any) -> List[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("Groups", "groups"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                for subkey in ("Group", "group", "groups"):
                    nested = value.get(subkey)
                    if isinstance(nested, list):
                        return nested
                    if isinstance(nested, dict):
                        return [nested]
    return []


def _extract_group_names(groups: List[Any]) -> List[str]:
    names: List[str] = []
    for entry in groups:
        if isinstance(entry, dict):
            for key in ("GroupName", "Group", "Name", "@GroupName"):
                value = entry.get(key)
                if value:
                    names.append(str(value).strip())
                    break
            else:
                names.append(json.dumps(entry, ensure_ascii=True))
        else:
            names.append(str(entry).strip())
    return [name for name in names if name]


def fetch_groups() -> Dict[str, Any]:
    url = f"{LAQN_BASE_URL}/Information/Groups/Json"
    request = Request(url, headers={"User-Agent": LAQN_USER_AGENT})
    with urlopen(request, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        return {"payload": data}
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List ERG LAQN group names.")
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (text or json).",
    )
    parser.add_argument(
        "--output",
        help="Optional output file path (defaults to stdout).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = fetch_groups()
    groups = _extract_groups(payload)
    names = _extract_group_names(groups)

    if args.format == "json":
        output = json.dumps(payload, indent=2)
    else:
        output = "\n".join(names)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
