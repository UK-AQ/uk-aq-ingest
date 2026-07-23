#!/usr/bin/env python3
"""
Fix swapped station geometry coordinates (lat/lon reversed) in Supabase.

Requires:
- SUPABASE_URL
- SB_SECRET_KEY
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if PROJECT_ROOT.name == "scripts":
    PROJECT_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.uk_aq_supabase import SupabaseSchemas, create_supabase_client

load_dotenv()


def main() -> int:
    supabase_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SB_SECRET_KEY")
    if not supabase_url or not service_role_key:
        print("Missing SUPABASE_URL or SB_SECRET_KEY.", file=sys.stderr)
        return 1

    client: Client = create_supabase_client(supabase_url, service_role_key)
    schemas = SupabaseSchemas.from_client(client)
    response = schemas.core.rpc("uk_aq_fix_station_geometry_swapped").execute()
    updated = response.data if hasattr(response, "data") else None
    print(f"Updated station geometries: {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
