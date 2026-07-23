//trigger deploy 2026-02-09 13:34
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import "../_shared/fetch_egress_patch.ts";
import { cacheControlHeaders } from "../_shared/cache.ts";
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

const REGION_LA_CODES: Record<string, string[]> = {
  "East Midlands": [
    "E06000015",
    "E06000016",
    "E06000017",
    "E06000018",
    "E06000061",
    "E06000062",
    "E07000032",
    "E07000033",
    "E07000034",
    "E07000035",
    "E07000036",
    "E07000037",
    "E07000038",
    "E07000039",
    "E07000129",
    "E07000130",
    "E07000131",
    "E07000132",
    "E07000133",
    "E07000134",
    "E07000135",
    "E07000136",
    "E07000137",
    "E07000138",
    "E07000139",
    "E07000140",
    "E07000141",
    "E07000142",
    "E07000170",
    "E07000171",
    "E07000172",
    "E07000173",
    "E07000174",
    "E07000175",
    "E07000176",
  ],
  "East of England": [
    "E06000031",
    "E06000032",
    "E06000033",
    "E06000034",
    "E06000055",
    "E06000056",
    "E07000008",
    "E07000009",
    "E07000010",
    "E07000011",
    "E07000012",
    "E07000066",
    "E07000067",
    "E07000068",
    "E07000069",
    "E07000070",
    "E07000071",
    "E07000072",
    "E07000073",
    "E07000074",
    "E07000075",
    "E07000076",
    "E07000077",
    "E07000095",
    "E07000096",
    "E07000098",
    "E07000099",
    "E07000102",
    "E07000103",
    "E07000143",
    "E07000144",
    "E07000145",
    "E07000146",
    "E07000147",
    "E07000148",
    "E07000149",
    "E07000200",
    "E07000202",
    "E07000203",
    "E07000240",
    "E07000241",
    "E07000242",
    "E07000243",
    "E07000244",
    "E07000245",
  ],
  "London": [
    "E09000001",
    "E09000002",
    "E09000003",
    "E09000004",
    "E09000005",
    "E09000006",
    "E09000007",
    "E09000008",
    "E09000009",
    "E09000010",
    "E09000011",
    "E09000012",
    "E09000013",
    "E09000014",
    "E09000015",
    "E09000016",
    "E09000017",
    "E09000018",
    "E09000019",
    "E09000020",
    "E09000021",
    "E09000022",
    "E09000023",
    "E09000024",
    "E09000025",
    "E09000026",
    "E09000027",
    "E09000028",
    "E09000029",
    "E09000030",
    "E09000031",
    "E09000032",
    "E09000033",
  ],
  "North East": [
    "E06000001",
    "E06000002",
    "E06000003",
    "E06000004",
    "E06000005",
    "E06000047",
    "E06000057",
    "E08000021",
    "E08000022",
    "E08000023",
    "E08000024",
    "E08000037",
  ],
  "North West": [
    "E06000006",
    "E06000007",
    "E06000008",
    "E06000009",
    "E06000049",
    "E06000050",
    "E06000063",
    "E06000064",
    "E07000117",
    "E07000118",
    "E07000119",
    "E07000120",
    "E07000121",
    "E07000122",
    "E07000123",
    "E07000124",
    "E07000125",
    "E07000126",
    "E07000127",
    "E07000128",
    "E08000001",
    "E08000002",
    "E08000003",
    "E08000004",
    "E08000005",
    "E08000006",
    "E08000007",
    "E08000008",
    "E08000009",
    "E08000010",
    "E08000011",
    "E08000012",
    "E08000013",
    "E08000014",
    "E08000015",
  ],
  "Northern Ireland": [
    "N09000001",
    "N09000002",
    "N09000003",
    "N09000004",
    "N09000005",
    "N09000006",
    "N09000007",
    "N09000008",
    "N09000009",
    "N09000010",
    "N09000011",
  ],
  "Scotland": [
    "S12000005",
    "S12000006",
    "S12000008",
    "S12000010",
    "S12000011",
    "S12000013",
    "S12000014",
    "S12000017",
    "S12000018",
    "S12000019",
    "S12000020",
    "S12000021",
    "S12000023",
    "S12000026",
    "S12000027",
    "S12000028",
    "S12000029",
    "S12000030",
    "S12000033",
    "S12000034",
    "S12000035",
    "S12000036",
    "S12000038",
    "S12000039",
    "S12000040",
    "S12000041",
    "S12000042",
    "S12000045",
    "S12000047",
    "S12000048",
    "S12000049",
    "S12000050",
  ],
  "South East": [
    "E06000035",
    "E06000036",
    "E06000037",
    "E06000038",
    "E06000039",
    "E06000040",
    "E06000041",
    "E06000042",
    "E06000043",
    "E06000044",
    "E06000045",
    "E06000046",
    "E06000060",
    "E07000061",
    "E07000062",
    "E07000063",
    "E07000064",
    "E07000065",
    "E07000084",
    "E07000085",
    "E07000086",
    "E07000087",
    "E07000088",
    "E07000089",
    "E07000090",
    "E07000091",
    "E07000092",
    "E07000093",
    "E07000094",
    "E07000105",
    "E07000106",
    "E07000107",
    "E07000108",
    "E07000109",
    "E07000110",
    "E07000111",
    "E07000112",
    "E07000113",
    "E07000114",
    "E07000115",
    "E07000116",
    "E07000177",
    "E07000178",
    "E07000179",
    "E07000180",
    "E07000181",
    "E07000207",
    "E07000208",
    "E07000209",
    "E07000210",
    "E07000211",
    "E07000212",
    "E07000213",
    "E07000214",
    "E07000215",
    "E07000216",
    "E07000217",
    "E07000223",
    "E07000224",
    "E07000225",
    "E07000226",
    "E07000227",
    "E07000228",
    "E07000229",
  ],
  "South West": [
    "E06000010",
    "E06000022",
    "E06000023",
    "E06000024",
    "E06000025",
    "E06000026",
    "E06000027",
    "E06000030",
    "E06000052",
    "E06000053",
    "E06000054",
    "E06000058",
    "E06000059",
    "E06000066",
    "E07000040",
    "E07000041",
    "E07000042",
    "E07000043",
    "E07000044",
    "E07000045",
    "E07000046",
    "E07000047",
    "E07000078",
    "E07000079",
    "E07000080",
    "E07000081",
    "E07000082",
    "E07000083",
  ],
  "Wales": [
    "W06000001",
    "W06000002",
    "W06000003",
    "W06000004",
    "W06000005",
    "W06000006",
    "W06000008",
    "W06000009",
    "W06000010",
    "W06000011",
    "W06000012",
    "W06000013",
    "W06000014",
    "W06000015",
    "W06000016",
    "W06000018",
    "W06000019",
    "W06000020",
    "W06000021",
    "W06000022",
    "W06000023",
    "W06000024",
  ],
  "West Midlands": [
    "E06000019",
    "E06000020",
    "E06000021",
    "E06000051",
    "E07000192",
    "E07000193",
    "E07000194",
    "E07000195",
    "E07000196",
    "E07000197",
    "E07000198",
    "E07000199",
    "E07000218",
    "E07000219",
    "E07000220",
    "E07000221",
    "E07000222",
    "E07000234",
    "E07000235",
    "E07000236",
    "E07000237",
    "E07000238",
    "E07000239",
    "E08000025",
    "E08000026",
    "E08000027",
    "E08000028",
    "E08000029",
    "E08000030",
    "E08000031",
  ],
  "Yorkshire and The Humber": [
    "E06000011",
    "E06000012",
    "E06000013",
    "E06000065",
    "E08000016",
    "E08000017",
    "E08000018",
    "E08000019",
    "E08000032",
    "E08000033",
    "E08000034",
    "E08000035",
    "E08000036",
  ],
};
const REGION_LA_CODES_LOOKUP = new Map(
  Object.entries(REGION_LA_CODES).map((
    [name, codes],
  ) => [name.toLowerCase(), codes]),
);

function postgrestHeaders(schema = UK_AQ_CORE_SCHEMA): Record<string, string> {
  const headers: Record<string, string> = {
    apikey: SUPABASE_PRIVILEGED_KEY,
    "Content-Type": "application/json",
    "x-ukaq-egress-caller": "uk_aq_la_hex",
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
      headers: { ...CORS_HEADERS, ...cacheControlHeaders(204) },
    });
  }
  if (req.method !== "GET") {
    return new Response("Method not allowed", {
      status: 405,
      headers: { ...CORS_HEADERS, ...cacheControlHeaders(405) },
    });
  }
  const startedAtMs = Date.now();
  const finish = (response: Response, fields: Record<string, unknown> = {}) =>
    logEndpointEgress(req, "uk_aq_la_hex", startedAtMs, response, fields);
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
  const laVersion = normalizeText(url.searchParams.get("la_version"));
  const region = normalizeText(url.searchParams.get("region"));
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
    has_la_version: Boolean(laVersion),
    has_region: Boolean(region),
    limit,
    has_since: Boolean(since),
    has_if_none_match: Boolean(ifNoneMatch),
  };

  try {
    const rows = await loadLatest({
      laVersion,
      region,
      networkCode,
      limit,
      since,
    });
    const versions = Array.from(
      new Set(rows.map((row) => row.la_version).filter(Boolean)),
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
      la_versions: versions,
      last_updated: lastUpdated,
      data: rows,
    };
    const etag = await createWeakEtag({
      endpoint: "uk_aq_la_hex",
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
    console.error("uk_aq_la_hex runtime failure", { message });
    return await finish(json({ error: "Internal server error." }, 500), {
      ...requestFields,
      error_type: "runtime",
    });
  }
});

type LoadOptions = {
  laVersion: string | null;
  region: string | null;
  networkCode: string | null;
  limit: number;
  since: string | null;
};

type LaRow = {
  la_code: string;
  la_codes?: string[] | string | null;
  la_name: string | null;
  la_version: string | null;
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
  { laVersion, region, networkCode, limit, since }: LoadOptions,
): Promise<LaRow[]> {
  const regionCodes = resolveRegionCodes(region);
  if (region && !regionCodes) {
    return [];
  }
  const { data, error } = await callLaHexRpc({
    regionCodes,
    laVersion,
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
  return normalizeLaRows(rows, regionCodes);
}

type LaRpcCallOptions = {
  regionCodes: string[] | null;
  laVersion: string | null;
  networkCode: string | null;
  limit: number;
  since: string | null;
};

async function callLaHexRpc(options: LaRpcCallOptions) {
  const { regionCodes, laVersion, networkCode, limit, since } = options;
  return await postgrestRequest<LaRow[]>(
    "POST",
    "rpc/uk_aq_la_hex_rpc",
    undefined,
    UK_AQ_PUBLIC_SCHEMA,
    {
      region: regionCodes,
      la_version: laVersion,
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

function resolveRegionCodes(region: string | null): string[] | null {
  if (!region) {
    return null;
  }
  const normalized = region.trim().toLowerCase();
  if (!normalized || normalized == "uk") {
    return null;
  }
  return REGION_LA_CODES_LOOKUP.get(normalized) ?? null;
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

function normalizeLaRows(rows: LaRow[], regionCodes: string[] | null): LaRow[] {
  if (!rows.length) {
    return [];
  }
  const regionSet = regionCodes ? new Set(regionCodes) : null;
  const normalized: LaRow[] = [];
  rows.forEach((row) => {
    const codes = collectLaCodes(row);
    if (!codes.length) {
      return;
    }
    codes.forEach((code) => {
      if (regionSet && !regionSet.has(code)) {
        return;
      }
      normalized.push({ ...row, la_code: code });
    });
  });
  return normalized;
}

function collectLaCodes(row: LaRow): string[] {
  const codes = new Set<string>();
  addLaCode(codes, row?.la_code);
  parseLaCodes(row?.la_codes).forEach((code) => addLaCode(codes, code));
  return Array.from(codes);
}

function addLaCode(set: Set<string>, value: string | null | undefined): void {
  if (typeof value !== "string") {
    return;
  }
  const trimmed = value.trim();
  if (trimmed) {
    set.add(trimmed);
  }
}

function parseLaCodes(value: string[] | string | null | undefined): string[] {
  if (!value) {
    return [];
  }
  if (Array.isArray(value)) {
    return value.filter((code) => typeof code === "string");
  }
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) {
      return [];
    }
    if (trimmed.startsWith("[") && trimmed.endsWith("]")) {
      try {
        const parsed = JSON.parse(trimmed);
        if (Array.isArray(parsed)) {
          return parsed.filter((code) => typeof code === "string");
        }
      } catch {
        // Fall back to basic parsing below.
      }
    }
    const cleaned = trimmed.replace(/^\{|\}$/g, "");
    return cleaned.split(",").map((code) => code.trim()).filter(Boolean);
  }
  return [];
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
      ...cacheControlHeaders(status),
      ...extraHeaders,
    },
  });
}

function notModified(etag: string): Response {
  return new Response(null, {
    status: 304,
    headers: {
      ...CORS_HEADERS,
      ...cacheControlHeaders(200),
      ETag: etag,
    },
  });
}
