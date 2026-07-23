import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import "../_shared/fetch_egress_patch.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.49.8";
// deploy trigger: 2026-02-14

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")
  ?? Deno.env.get("SB_SUPABASE_URL")
  ?? "";
const SUPABASE_PUBLISHABLE_KEY = Deno.env.get("SB_PUBLISHABLE_DEFAULT_KEY")
  ?? "";
const UK_AQ_PUBLIC_SCHEMA = Deno.env.get("UK_AQ_PUBLIC_SCHEMA")
  ?? "uk_aq_public";

const CACHE_CONTROL_SUCCESS = "public, max-age=30";
const CACHE_CONTROL_ERROR = "no-store";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
};

type SnapshotWindow = "6h" | "24h" | "7d" | "21d" | "31d" | "90d";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": status >= 400 ? CACHE_CONTROL_ERROR : CACHE_CONTROL_SUCCESS,
      ...CORS_HEADERS,
    },
  });
}

function parseBigIntParam(value: string | null): bigint | null {
  if (!value) {
    return null;
  }
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  try {
    return BigInt(trimmed);
  } catch (_err) {
    return null;
  }
}

function parseInt4Param(value: string | null): number | null {
  if (!value) {
    return null;
  }
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  const parsed = Number(trimmed);
  if (!Number.isInteger(parsed)) {
    return null;
  }
  if (parsed < -2147483648 || parsed > 2147483647) {
    return null;
  }
  return parsed;
}

function parseWindow(value: string | null): SnapshotWindow | null {
  if (!value || !value.trim()) {
    return "6h";
  }
  const normalized = value.trim().toLowerCase();
  if (
    normalized === "6h" || normalized === "24h" || normalized === "7d"
    || normalized === "21d" || normalized === "31d" || normalized === "90d"
  ) {
    return normalized;
  }
  return null;
}

function parseObsLimit(value: string | null): 100 | 1000 | null {
  if (!value || !value.trim()) {
    return 100;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return null;
  }
  if (parsed === 100) {
    return 100;
  }
  if (parsed === 1000) {
    return 1000;
  }
  return null;
}

function requireAuthHeader(req: Request): string | null {
  const authHeader = req.headers.get("Authorization") ?? req.headers.get("authorization");
  if (!authHeader) {
    return null;
  }
  if (!authHeader.startsWith("Bearer ")) {
    return null;
  }
  return authHeader;
}

function extractBearerToken(authHeader: string): string {
  return authHeader.slice("Bearer ".length).trim();
}

async function validateAccessToken(accessToken: string): Promise<boolean> {
  const resp = await fetch(`${SUPABASE_URL.replace(/\/$/, "")}/auth/v1/user`, {
    method: "GET",
    headers: {
      apikey: SUPABASE_PUBLISHABLE_KEY,
      Authorization: `Bearer ${accessToken}`,
    },
  });
  return resp.ok;
}

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        ...CORS_HEADERS,
        "Access-Control-Max-Age": "86400",
        "Cache-Control": CACHE_CONTROL_SUCCESS,
      },
    });
  }

  if (req.method !== "GET") {
    return jsonResponse({ error: "Method not allowed" }, 405);
  }

  if (!SUPABASE_URL || !SUPABASE_PUBLISHABLE_KEY) {
    return jsonResponse(
      { error: "Missing SUPABASE_URL or publishable key for authenticated requests." },
      500,
    );
  }

  const authHeader = requireAuthHeader(req);
  if (!authHeader) {
    return jsonResponse({ error: "Authorization Bearer token required." }, 401);
  }
  const accessToken = extractBearerToken(authHeader);
  if (!accessToken) {
    return jsonResponse({ error: "Authorization Bearer token required." }, 401);
  }

  const url = new URL(req.url);
  const stationId = parseBigIntParam(url.searchParams.get("station_id"));
  const stationRef = (url.searchParams.get("station_ref") ?? "").trim() || null;
  const timeseriesId = parseInt4Param(url.searchParams.get("timeseries_id"));
  const windowValue = parseWindow(url.searchParams.get("window"));
  const obsLimit = parseObsLimit(url.searchParams.get("obs_limit"));

  if (stationId === null && stationRef === null) {
    return jsonResponse({ error: "station_id or station_ref is required." }, 400);
  }
  if (windowValue === null) {
    return jsonResponse({ error: "window must be one of: 6h, 24h, 7d, 21d, 31d, 90d." }, 400);
  }
  if (obsLimit === null) {
    return jsonResponse({ error: "obs_limit must be 100 or 1000." }, 400);
  }

  const supabase = createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, {
    auth: {
      persistSession: false,
      autoRefreshToken: false,
    },
    global: {
      headers: {
        Authorization: authHeader,
      },
    },
  });

  const tokenValid = await validateAccessToken(accessToken);
  if (!tokenValid) {
    return jsonResponse({ error: "Unauthorized." }, 401);
  }

  const { data, error } = await supabase
    .schema(UK_AQ_PUBLIC_SCHEMA)
    .rpc("uk_aq_station_snapshot", {
      p_station_id: stationId === null ? null : stationId.toString(),
      p_station_ref: stationRef,
      p_timeseries_id: timeseriesId,
      p_window: windowValue,
      p_obs_limit: obsLimit,
    });

  if (error) {
    return jsonResponse({ error: error.message }, 500);
  }

  const stationPayload = data && typeof data === "object" && "station" in data
    ? (data as Record<string, unknown>).station
    : null;
  if (!data || stationPayload === null || stationPayload === undefined) {
    return jsonResponse({ error: "Station not found." }, 404);
  }

  return jsonResponse(data, 200);
});
