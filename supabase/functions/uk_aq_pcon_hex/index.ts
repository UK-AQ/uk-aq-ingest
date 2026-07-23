//trigger deploy 2026-02-09 13:34
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import "../_shared/fetch_egress_patch.ts";
import {
  CACHE_CONTROL_SUCCESS_SMAXAGE_300,
  cacheControlHeaders,
} from "../_shared/cache.ts";
import { createWeakEtag, ifNoneMatchMatches } from "../_shared/etag.ts";
import { logEndpointEgress } from "../_shared/egress_metrics.ts";
import { parsePublicNetworkFilter } from "../_shared/public_network_filter.ts";
import { validateWorkerUpstreamAuth } from "../_shared/worker_auth.ts";

const DEFAULT_LIMIT = 10000;
const MAX_LIMIT = 20000;

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ??
  Deno.env.get("SB_SUPABASE_URL") ??
  "";
const SB_SECRET_KEY = Deno.env.get("SB_SECRET_KEY") ?? "";
const SUPABASE_PRIVILEGED_KEY = SB_SECRET_KEY;
const UK_AQ_CORE_SCHEMA = Deno.env.get("UK_AQ_CORE_SCHEMA") ??
  "uk_aq_core";
const UK_AQ_PUBLIC_SCHEMA = Deno.env.get("UK_AQ_PUBLIC_SCHEMA") ??
  "uk_aq_public";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, if-none-match",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Expose-Headers": "ETag",
};

const REST_BASE_URL = SUPABASE_URL
  ? `${SUPABASE_URL.replace(/\/$/, "")}/rest/v1`
  : "";

function postgrestHeaders(schema = UK_AQ_CORE_SCHEMA): Record<string, string> {
  const headers: Record<string, string> = {
    apikey: SUPABASE_PRIVILEGED_KEY,
    "Content-Type": "application/json",
    "x-ukaq-egress-caller": "uk_aq_pcon_hex",
  };
  if (schema && schema !== "public") {
    headers["Accept-Profile"] = schema;
    headers["Content-Profile"] = schema;
  }
  return headers;
}

async function postgrestRequest<T>(
  method: string,
  path: string,
  params?: Record<string, string>,
  schema?: string,
  body?: unknown,
): Promise<{ data: T | null; error: { message: string } | null }> {
  if (!REST_BASE_URL || !SUPABASE_PRIVILEGED_KEY) {
    return {
      data: null,
      error: { message: "Missing SUPABASE_URL or SB_SECRET_KEY." },
    };
  }
  const url = new URL(`${REST_BASE_URL}/${path}`);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null) {
      url.searchParams.set(key, String(value));
    }
  }
  const resp = await fetch(url.toString(), {
    method,
    headers: postgrestHeaders(schema),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const contentType = resp.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await resp.json()
    : await resp.text();
  if (!resp.ok) {
    const message = payload?.message || payload?.error_description ||
      payload?.error || resp.statusText;
    return { data: null, error: { message: String(message) } };
  }
  return { data: payload as T, error: null };
}

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        ...CORS_HEADERS,
        "Access-Control-Max-Age": "86400",
        ...cacheControlHeaders(204, CACHE_CONTROL_SUCCESS_SMAXAGE_300),
      },
    });
  }
  if (req.method !== "GET") {
    return new Response("Method not allowed", {
      status: 405,
      headers: {
        ...CORS_HEADERS,
        ...cacheControlHeaders(405, CACHE_CONTROL_SUCCESS_SMAXAGE_300),
      },
    });
  }
  const startedAtMs = Date.now();
  const finish = (response: Response, fields: Record<string, unknown> = {}) =>
    logEndpointEgress(req, "uk_aq_pcon_hex", startedAtMs, response, fields);
  const auth = validateWorkerUpstreamAuth(req);
  if (!auth.ok) {
    return await finish(json({ error: auth.error }, auth.status), {
      error_type: "upstream_auth",
      auth_status: auth.status,
    });
  }
  if (!SUPABASE_URL || !SUPABASE_PRIVILEGED_KEY) {
    return await finish(
      json({ error: "Missing SUPABASE_URL or SB_SECRET_KEY." }, 500),
      {
        error_type: "missing_env",
      },
    );
  }

  const url = new URL(req.url);
  const networkFilter = parsePublicNetworkFilter(url);
  if (!networkFilter.ok) {
    return await finish(json({ error: networkFilter.error }, 400), {
      error_type: "invalid_public_filter",
    });
  }
  const networkCode = networkFilter.networkCode;
  const pconVersion = normalizeText(url.searchParams.get("pcon_version"));
  const limit = parseLimit(url.searchParams.get("limit"), DEFAULT_LIMIT);
  const rawSince = url.searchParams.get("since");
  const since = rawSince === null ? null : normalizeTimestamp(rawSince);
  if (rawSince !== null && since === null) {
    return await finish(
      json({
        error:
          "Invalid since timestamp. Provide ISO-8601 datetime (e.g. 2026-02-07T10:30:00Z).",
      }, 400),
      { error_type: "invalid_since" },
    );
  }
  const ifNoneMatch = req.headers.get("if-none-match");
  const requestFields = {
    has_network_code: Boolean(networkCode),
    has_pcon_version: Boolean(pconVersion),
    limit,
    has_since: Boolean(since),
    has_if_none_match: Boolean(ifNoneMatch),
  };

  try {
    const rows = await loadLatest({ pconVersion, networkCode, limit, since });
    const versions = Array.from(
      new Set(rows.map((row) => row.pcon_version).filter(Boolean)),
    ).sort();
    const lastUpdated = maxTimestamp(rows.map((row) => row.latest_value_at));
    const nextSince = lastUpdated ?? since;
    const payload = {
      contract_version: 2,
      network_code: networkCode,
      metric_default: "median",
      since,
      next_since: nextSince,
      count: rows.length,
      pcon_versions: versions,
      last_updated: lastUpdated,
      data: rows,
    };
    const etag = await createWeakEtag({
      endpoint: "uk_aq_pcon_hex",
      version: 2,
      payload,
    });
    if (ifNoneMatchMatches(ifNoneMatch, etag)) {
      return await finish(notModified(etag), {
        ...requestFields,
        result: "not_modified",
      });
    }
    return await finish(json(payload, 200, { ETag: etag }), {
      ...requestFields,
      result: "ok",
      row_count: rows.length,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error("uk_aq_pcon_hex runtime failure", { message });
    return await finish(json({ error: "Internal server error." }, 500), {
      ...requestFields,
      error_type: "runtime",
    });
  }
});

type LoadOptions = {
  pconVersion: string | null;
  networkCode: string | null;
  limit: number;
  since: string | null;
};

type PconRow = {
  pcon_code: string;
  pcon_name: string | null;
  pcon_version: string | null;
  network_id: number;
  network_code: string;
  network_label: string;
  station_count: number | null;
  single_site: boolean | null;
  median_value: number | null;
  mean_value: number | null;
  latest_value_at: string | null;
};

async function loadLatest(
  { pconVersion, networkCode, limit, since }: LoadOptions,
): Promise<PconRow[]> {
  const { data, error } = await callPconHexRpc({
    pconVersion,
    networkCode,
    limit,
    since,
  });
  if (error) {
    throw new Error(error.message);
  }
  const rows = (data ?? []).filter((row) =>
    !since || isTimestampAfter(row?.latest_value_at, since)
  );
  return rows.filter((row) =>
    typeof row?.pcon_code === "string" && row.pcon_code.trim().length > 0
  );
}

type PconRpcCallOptions = {
  pconVersion: string | null;
  networkCode: string | null;
  limit: number;
  since: string | null;
};

async function callPconHexRpc(options: PconRpcCallOptions) {
  const { pconVersion, networkCode, limit, since } = options;
  return await postgrestRequest<PconRow[]>(
    "POST",
    "rpc/uk_aq_pcon_hex_rpc",
    undefined,
    UK_AQ_PUBLIC_SCHEMA,
    {
      pcon_version: pconVersion,
      network_code: networkCode,
      limit_rows: limit,
      since_ts: since,
    },
  );
}

function normalizeText(value: string | null): string | null {
  if (!value) {
    return null;
  }
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function normalizeTimestamp(value: string): string | null {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }
  return parsed.toISOString();
}

function parseLimit(value: string | null, fallback: number): number {
  if (!value) {
    return fallback;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(1, Math.min(MAX_LIMIT, Math.floor(parsed)));
}

function isTimestampAfter(
  candidate: string | null | undefined,
  since: string,
): boolean {
  if (!candidate) {
    return false;
  }
  const candidateDate = new Date(candidate);
  const sinceDate = new Date(since);
  if (
    Number.isNaN(candidateDate.getTime()) || Number.isNaN(sinceDate.getTime())
  ) {
    return false;
  }
  return candidateDate.getTime() > sinceDate.getTime();
}

function maxTimestamp(values: Array<string | null | undefined>): string | null {
  let maxValue: string | null = null;
  for (const value of values) {
    if (!value) {
      continue;
    }
    if (!maxValue || value > maxValue) {
      maxValue = value;
    }
  }
  return maxValue;
}

function json(
  payload: unknown,
  status = 200,
  extraHeaders: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...CORS_HEADERS,
      ...cacheControlHeaders(status, CACHE_CONTROL_SUCCESS_SMAXAGE_300),
      ...extraHeaders,
    },
  });
}

function notModified(etag: string): Response {
  return new Response(null, {
    status: 304,
    headers: {
      ...CORS_HEADERS,
      ...cacheControlHeaders(200, CACHE_CONTROL_SUCCESS_SMAXAGE_300),
      ETag: etag,
    },
  });
}
