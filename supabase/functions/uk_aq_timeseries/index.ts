//trigger deploy 2026-02-12 17:17
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import "../_shared/fetch_egress_patch.ts";
import {
  CACHE_CONTROL_SUCCESS_SMAXAGE_300,
  cacheControlHeaders,
} from "../_shared/cache.ts";
import { createWeakEtag, ifNoneMatchMatches } from "../_shared/etag.ts";
import { logEndpointEgress } from "../_shared/egress_metrics.ts";
import { validateWorkerUpstreamAuth } from "../_shared/worker_auth.ts";

const DEFAULT_WINDOW = "24h";
const DEFAULT_FORMAT = "objects";
const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;

type NamedWindowLabel = "12h" | "24h" | "7d" | "31d" | "90d";
type IngestWindowLabel = "12h" | "24h" | "7d" | "30d";

const WINDOW_HOURS: Record<NamedWindowLabel, number> = {
  "12h": 12,
  "24h": 24,
  "7d": 24 * 7,
  "31d": 24 * 31,
  "90d": 24 * 90,
};
const MAX_WINDOW_DAYS = parsePositiveInteger(
  Deno.env.get("UK_AQ_TIMESERIES_MAX_WINDOW_DAYS"),
) ?? 366;
const DEFAULT_INGESTDB_RETENTION_DAYS = 5;
const MAX_INGESTDB_RETENTION_DAYS = 3650;
const INGESTDB_RETENTION_DAYS = Math.max(
  1,
  Math.min(
    MAX_INGESTDB_RETENTION_DAYS,
    parsePositiveInteger(Deno.env.get("INGESTDB_RETENTION_DAYS")) ??
      DEFAULT_INGESTDB_RETENTION_DAYS,
  ),
);
const INGEST_SOURCE_OF_TRUTH_HOURS = INGESTDB_RETENTION_DAYS * 24;

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ??
  Deno.env.get("SB_SUPABASE_URL") ??
  "";
const SB_SECRET_KEY = Deno.env.get("SB_SECRET_KEY") ?? "";
const SUPABASE_PRIVILEGED_KEY = SB_SECRET_KEY;
const EDGE_UPSTREAM_SECRET = Deno.env.get("UK_AQ_EDGE_UPSTREAM_SECRET") ?? "";
const OBSERVS_HISTORY_R2_API_URL = String(
  Deno.env.get("UK_AQ_OBSERVS_HISTORY_R2_API_URL") ?? "",
).trim();
const OBSERVS_HISTORY_R2_API_TIMEOUT_MS = Math.max(
  2000,
  Math.min(
    30000,
    parsePositiveInteger(
      Deno.env.get("UK_AQ_OBSERVS_HISTORY_R2_API_TIMEOUT_MS"),
    ) ??
      10000,
  ),
);
const OBSERVS_HISTORY_R2_CHUNK_DAYS = Math.max(
  1,
  Math.min(
    31,
    parsePositiveInteger(Deno.env.get("UK_AQ_OBSERVS_HISTORY_R2_CHUNK_DAYS")) ??
      7,
  ),
);
const OBSERVS_HISTORY_R2_CHUNK_MAX_RETRIES = Math.max(
  1,
  Math.min(
    4,
    parsePositiveInteger(
      Deno.env.get("UK_AQ_OBSERVS_HISTORY_R2_CHUNK_MAX_RETRIES"),
    ) ?? 4,
  ),
);
const OBSERVS_HISTORY_R2_REQUEST_MAX_ATTEMPTS = 3;
const OBSERVS_HISTORY_R2_REQUEST_RETRY_BASE_MS = 500;
const OBSERVS_HISTORY_R2_REQUEST_RETRY_CAP_MS = 3000;
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

type PostgrestQueryParams = Record<string, string | string[]>;

type PostgrestRequestConfig = {
  baseUrl?: string;
  apiKey?: string;
  caller?: string;
};

function postgrestHeaders(
  apiKey: string,
  schema = UK_AQ_CORE_SCHEMA,
  caller = "uk_aq_timeseries",
): Record<string, string> {
  const headers: Record<string, string> = {
    apikey: apiKey,
    Authorization: `Bearer ${apiKey}`,
    "Content-Type": "application/json",
    "x-ukaq-egress-caller": caller,
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
  params?: PostgrestQueryParams,
  schema?: string,
  body?: unknown,
  config?: PostgrestRequestConfig,
): Promise<{ data: T | null; error: { message: string } | null }> {
  const baseUrl = config?.baseUrl ?? REST_BASE_URL;
  const apiKey = config?.apiKey ?? SUPABASE_PRIVILEGED_KEY;
  const caller = config?.caller ?? "uk_aq_timeseries";
  if (!baseUrl || !apiKey) {
    return {
      data: null,
      error: { message: "Missing SUPABASE_URL or SB_SECRET_KEY." },
    };
  }
  const url = new URL(`${baseUrl}/${path}`);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null) {
      if (Array.isArray(value)) {
        for (const part of value) {
          if (part !== undefined && part !== null) {
            url.searchParams.append(key, String(part));
          }
        }
      } else {
        url.searchParams.set(key, String(value));
      }
    }
  }
  const resp = await fetch(url.toString(), {
    method,
    headers: postgrestHeaders(apiKey, schema, caller),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const contentType = resp.headers.get("content-type") ?? "";
  const payload: any = contentType.includes("application/json")
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
    logEndpointEgress(req, "uk_aq_timeseries", startedAtMs, response, fields);
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
  const timeseriesId = parseId(url.searchParams.get("timeseries_id"));
  if (!timeseriesId) {
    return await finish(
      json({ error: "Missing or invalid timeseries_id." }, 400),
      {
        error_type: "invalid_timeseries_id",
      },
    );
  }
  const rawWindow = url.searchParams.get("window");
  const rawDays = url.searchParams.get("days");
  const rawStart = firstNonEmptyParam(
    url.searchParams.get("start"),
    url.searchParams.get("start_utc"),
  );
  const rawEnd = firstNonEmptyParam(
    url.searchParams.get("end"),
    url.searchParams.get("end_utc"),
  );
  const now = new Date();
  const rangeResult = resolveRequestedRange({
    rawWindow,
    rawDays,
    rawStart,
    rawEnd,
    now,
  });
  if (!rangeResult.ok) {
    return await finish(json({ error: rangeResult.error }, 400), {
      error_type: "invalid_window_range",
    });
  }
  const range = rangeResult.range;
  const rawLimit = url.searchParams.get("limit");
  const limit = parseOptionalLimit(rawLimit);
  if (rawLimit !== null && limit === null) {
    return await finish(
      json({
        error: "Invalid limit. Provide a positive integer or omit limit.",
      }, 400),
      {
        error_type: "invalid_limit",
      },
    );
  }
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
  const rawFormat = url.searchParams.get("format");
  const responseFormat = normalizeFormat(rawFormat);
  if (rawFormat !== null && responseFormat === null) {
    return await finish(
      json({ error: "Invalid format. Use 'objects' or 'compact'." }, 400),
      {
        error_type: "invalid_format",
      },
    );
  }
  const format = responseFormat ?? DEFAULT_FORMAT;
  const ifNoneMatch = req.headers.get("if-none-match");
  const requestFields = {
    timeseries_id: timeseriesId,
    window: range.windowLabel,
    window_mode: range.mode,
    days: range.days ?? null,
    has_start_end: range.mode === "datetime",
    limit: limit ?? null,
    has_since: Boolean(since),
    format,
    has_if_none_match: Boolean(ifNoneMatch),
  };

  try {
    const stitched = await fetchTimeseriesRowsStitched({
      timeseriesId,
      limit,
      since,
      requestStart: range.start,
      requestEnd: range.end,
      now,
    });
    const rows = stitched.rows;
    const nextSince = maxObservedTimestamp(rows, since);
    const columns = timeseriesColumns();
    const payload = {
      timeseries_id: timeseriesId,
      window: range.windowLabel,
      window_mode: range.mode,
      start: range.start.toISOString(),
      end: range.end.toISOString(),
      since,
      next_since: nextSince,
      data_format: format,
      columns,
      count: rows.length,
      source: stitched.source,
      response_complete: stitched.meta.response_complete,
      source_split_boundary_utc: stitched.meta.source_split_boundary_utc,
      guideline: stitched.guideline,
      meta: {
        ...stitched.meta,
        row_count: rows.length,
        query_from_utc: range.start.toISOString(),
        query_to_utc: range.end.toISOString(),
        window: range.windowLabel,
        window_mode: range.mode,
      },
      data: shapeTimeseriesData(rows, format),
    };
    const etag = await createWeakEtag({
      endpoint: "uk_aq_timeseries",
      version: 2,
      payload: etagPayload(payload),
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
      source: stitched.source,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error("uk_aq_timeseries runtime failure", { message });
    return await finish(json({ error: "Internal server error." }, 500), {
      ...requestFields,
      error_type: "runtime",
    });
  }
});

function parseId(value: string | null): number | null {
  if (!value) {
    return null;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return null;
  }
  return Math.floor(parsed);
}

type TimeseriesRpcCallOptions = {
  timeseriesId: number;
  windowLabel: IngestWindowLabel;
  limit: number | null;
  since: string | null;
};

type StitchedFetchOptions = {
  timeseriesId: number;
  limit: number | null;
  since: string | null;
  requestStart: Date;
  requestEnd: Date;
  now: Date;
};

type StitchedFetchResult = {
  guideline: unknown;
  rows: TimeseriesRow[];
  source: "recent_only" | "history_only" | "recent_history_stitched";
  meta: {
    source_mode: "recent_only" | "history_only" | "recent_history_stitched";
    source_split_boundary_utc: string;
    overlap_start_utc: string;
    retention_start_utc: string;
    ingest_retention_days: number;
    source_of_truth_days: number;
    source_of_truth_hours: number;
    response_complete: boolean;
    has_gap: boolean;
    r2_coverage_start: string | null;
    r2_coverage_end: string | null;
    ingest_tail_start: string | null;
    row_count: number;
    r2_row_count: number;
    ingest_row_count: number;
    deduped_row_count: number;
    r2_errors: string[];
    ingest_errors: string[];
    coverage: Record<string, unknown>;
  };
};

type TimeseriesConnectorRow = {
  connector_id: unknown;
};

type ObservsHistoryWindowCallOptions = {
  timeseriesId: number;
  connectorId: number;
  startUtc: string;
  endUtc: string;
  since: string | null;
  limit: number | null;
};

type ObservsRecentWindowRow = {
  observed_at: unknown;
  value: unknown;
};

type ObservsHistoryApiPayload = {
  ok?: boolean;
  rows?: unknown[];
  row_count?: unknown;
  response_complete?: unknown;
  has_gap?: unknown;
  coverage_state?: unknown;
  partial_reasons?: unknown;
  coverage?: unknown;
  error?: string;
};

type ObservsHistoryWindowResult = {
  rows: TimeseriesRow[];
  responseComplete: boolean;
  hasGap: boolean;
  coverage: Record<string, unknown> | null;
  partialReasons: string[];
  rowCount: number | null;
};

type ChunkFetchResult = {
  rows: TimeseriesRow[];
  chunkCount: number;
  failedChunkCount: number;
  partialChunkCount: number;
  partialReasons: string[];
  coverage: Record<string, unknown>[];
  responseComplete: boolean;
  lastError: string | null;
};

async function callTimeseriesRpc(
  { timeseriesId, windowLabel, limit, since }: TimeseriesRpcCallOptions,
) {
  const withStatusArg = await postgrestRequest<any[]>(
    "POST",
    "rpc/uk_aq_timeseries_rpc",
    undefined,
    UK_AQ_PUBLIC_SCHEMA,
    {
      timeseries_id: timeseriesId,
      window_label: windowLabel,
      limit_rows: limit,
      since_ts: since,
      include_status: false,
    },
  );
  if (!withStatusArg.error) {
    return withStatusArg;
  }
  if (!looksLikeTimeseriesSignatureMismatch(withStatusArg.error.message)) {
    return withStatusArg;
  }
  return await postgrestRequest<any[]>(
    "POST",
    "rpc/uk_aq_timeseries_rpc",
    undefined,
    UK_AQ_PUBLIC_SCHEMA,
    {
      timeseries_id: timeseriesId,
      window_label: windowLabel,
      limit_rows: limit,
      since_ts: since,
    },
  );
}

function makeBoundedWindow(start: Date, end: Date): { start: Date; end: Date } | null {
  return end.getTime() > start.getTime() ? { start, end } : null;
}

function maxDate(left: Date, right: Date): Date {
  return left.getTime() >= right.getTime() ? left : right;
}

function minDate(left: Date, right: Date): Date {
  return left.getTime() <= right.getTime() ? left : right;
}

function shouldFetchWindow(
  window: { start: Date; end: Date } | null,
  sinceMs: number,
): boolean {
  if (!window) return false;
  return !Number.isFinite(sinceMs) || sinceMs < window.end.getTime();
}

function windowStartIso(window: { start: Date; end: Date } | null): string | null {
  return window ? window.start.toISOString() : null;
}

function windowEndIso(window: { start: Date; end: Date } | null): string | null {
  return window ? window.end.toISOString() : null;
}

function observedHourKey(observedAt: string): string | null {
  const observedMs = Date.parse(observedAt);
  if (!Number.isFinite(observedMs)) return null;
  return new Date(Math.floor(observedMs / HOUR_MS) * HOUR_MS).toISOString();
}

function filterRowsToMissingHours(
  candidateRows: TimeseriesRow[],
  preferredRows: TimeseriesRow[],
  start: Date,
  end: Date,
  since: string | null,
): TimeseriesRow[] {
  const preferredHours = new Set<string>();
  for (const row of filterRowsToWindow(preferredRows, start, end, since)) {
    const hourKey = observedHourKey(row.observed_at);
    if (hourKey) preferredHours.add(hourKey);
  }
  return filterRowsToWindow(candidateRows, start, end, since).filter((row) => {
    const hourKey = observedHourKey(row.observed_at);
    return hourKey !== null && !preferredHours.has(hourKey);
  });
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item ?? "").trim()).filter(Boolean);
}

function addPartialReason(reasons: Set<string>, reason: string, condition: boolean): void {
  if (condition) reasons.add(reason);
}

function summarizeObservsHistoryPayloadCompleteness(
  payload: ObservsHistoryApiPayload | null,
): {
  responseComplete: boolean;
  hasGap: boolean;
  coverage: Record<string, unknown> | null;
  partialReasons: string[];
  rowCount: number | null;
} {
  const coverage = payload?.coverage && typeof payload.coverage === "object"
    ? payload.coverage as Record<string, unknown>
    : null;
  const index = coverage?.timeseries_index &&
      typeof coverage.timeseries_index === "object"
    ? coverage.timeseries_index as Record<string, unknown>
    : null;
  const reasons = new Set<string>([
    ...asStringArray(payload?.partial_reasons),
    ...asStringArray(coverage?.partial_reasons),
  ]);

  addPartialReason(
    reasons,
    "missing_day_manifest",
    Array.isArray(coverage?.missing_day_manifest_keys) &&
      coverage.missing_day_manifest_keys.length > 0,
  );
  addPartialReason(
    reasons,
    "missing_connector_manifest",
    Array.isArray(coverage?.missing_connector_manifest_keys) &&
      coverage.missing_connector_manifest_keys.length > 0,
  );
  addPartialReason(
    reasons,
    "missing_parquet",
    Array.isArray(coverage?.missing_parquet_keys) &&
      coverage.missing_parquet_keys.length > 0,
  );
  addPartialReason(
    reasons,
    "limited_by_limit",
    coverage?.limited_by_limit === true,
  );
  addPartialReason(
    reasons,
    "timeseries_index_skipped_day",
    Number(index?.skipped_days_by_file_range) > 0,
  );
  addPartialReason(
    reasons,
    "timeseries_index_warning",
    Array.isArray(index?.warnings) && index.warnings.length > 0,
  );

  const explicitComplete = typeof payload?.response_complete === "boolean"
    ? payload.response_complete
    : typeof coverage?.response_complete === "boolean"
    ? coverage.response_complete as boolean
    : null;
  const responseComplete = explicitComplete !== null
    ? explicitComplete && reasons.size === 0
    : Boolean(coverage) && reasons.size === 0;
  const explicitHasGap = typeof payload?.has_gap === "boolean"
    ? payload.has_gap
    : typeof coverage?.has_gap === "boolean"
    ? coverage.has_gap as boolean
    : null;
  const rowCount = Number(payload?.row_count);
  return {
    responseComplete,
    hasGap: explicitHasGap !== null ? explicitHasGap || !responseComplete : !responseComplete,
    coverage,
    partialReasons: Array.from(reasons),
    rowCount: Number.isFinite(rowCount) ? rowCount : null,
  };
}

async function fetchTimeseriesRowsStitched(
  {
    timeseriesId,
    limit,
    since,
    requestStart,
    requestEnd,
    now,
  }: StitchedFetchOptions,
): Promise<StitchedFetchResult> {
  const retentionStart = new Date(
    now.getTime() - INGEST_SOURCE_OF_TRUTH_HOURS * HOUR_MS,
  );
  const overlapStart = new Date(retentionStart.getTime() - DAY_MS);
  const effectiveRequestEnd = minDate(requestEnd, now);
  const r2Window = makeBoundedWindow(
    requestStart,
    minDate(effectiveRequestEnd, retentionStart),
  );
  const historicalWindow = makeBoundedWindow(
    requestStart,
    minDate(effectiveRequestEnd, overlapStart),
  );
  const overlapWindow = makeBoundedWindow(
    maxDate(requestStart, overlapStart),
    minDate(effectiveRequestEnd, retentionStart),
  );
  const retentionWindow = makeBoundedWindow(
    maxDate(requestStart, retentionStart),
    effectiveRequestEnd,
  );
  const sinceMs = since ? Date.parse(since) : Number.NaN;
  const shouldFetchHistory = shouldFetchWindow(r2Window, sinceMs);
  const shouldFetchRetentionIngest = shouldFetchWindow(retentionWindow, sinceMs);
  const shouldFetchOverlapIngest = shouldFetchWindow(overlapWindow, sinceMs);
  const shouldFetchIngestRows = shouldFetchRetentionIngest ||
    shouldFetchOverlapIngest;
  const sourceSplitBoundaryUtc = retentionStart.toISOString();
  const historyErrors: string[] = [];
  const ingestErrors: string[] = [];
  let historyStatus = shouldFetchHistory ? "pending" : "not_requested";
  let ingestStatus = shouldFetchIngestRows ? "pending" : "not_requested";
  let historyChunkCount: number | null = null;
  let historyFailedChunkCount: number | null = null;
  let historyPartialChunkCount: number | null = null;
  let historyResponseComplete = !shouldFetchHistory;
  let historyCoverage: Record<string, unknown> | null = null;
  let historyChunkCoverages: Record<string, unknown>[] = [];
  let historyPartialReasons: string[] = [];

  const ingestQueryStart = shouldFetchIngestRows
    ? shouldFetchOverlapIngest && overlapWindow
      ? overlapWindow.start
      : retentionWindow?.start ?? effectiveRequestEnd
    : null;
  const ingestQueryEnd = shouldFetchIngestRows ? effectiveRequestEnd : null;

  const ingestWindowLabel = shouldFetchIngestRows
    ? selectIngestWindowLabel(ingestQueryStart ?? effectiveRequestEnd, now)
    : "12h";
  const ingestLimit = shouldFetchIngestRows
    ? null
    : limit;
  const ingestSince = shouldFetchIngestRows ? since : null;
  let guideline: unknown = null;
  let ingestRpcRows: TimeseriesRow[] = [];
  if (shouldFetchIngestRows) {
    const { data, error } = await callTimeseriesRpc({
      timeseriesId,
      windowLabel: ingestWindowLabel,
      limit: ingestLimit,
      since: ingestSince,
    });
    if (error) {
      ingestStatus = "error";
      ingestErrors.push(error.message);
      throw new Error(error.message);
    }
    ingestStatus = "ingestdb_complete";
    const ingestRow = Array.isArray(data) && data.length > 0 ? data[0] : null;
    guideline = ingestRow?.guideline ?? null;
    ingestRpcRows = normalizeTimeseriesRows(
      Array.isArray(ingestRow?.data) ? ingestRow.data : [],
    );
  } else {
    ingestStatus = "not_requested";
  }

  const connectorId = await resolveTimeseriesConnectorId(timeseriesId);

  let historyRows: TimeseriesRow[] = [];
  if (shouldFetchHistory && r2Window) {
    const historyStartUtc = r2Window.start.toISOString();
    const historyEndUtc = r2Window.end.toISOString();
    if (connectorId !== null) {
      try {
        const historyWindow = await callObservsHistoryWindow({
          timeseriesId,
          connectorId,
          startUtc: historyStartUtc,
          endUtc: historyEndUtc,
          since,
          limit,
        });
        historyRows = historyWindow.rows;
        historyResponseComplete = historyWindow.responseComplete;
        historyCoverage = historyWindow.coverage;
        historyPartialReasons = historyWindow.partialReasons;
        historyStatus = historyWindow.responseComplete
          ? "r2_complete"
          : "r2_partial";
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        historyErrors.push(message);
        if (
          shouldRetryHistoryChunked(message, historyStartUtc, historyEndUtc)
        ) {
          try {
            const historyWindow = await callObservsHistoryWindowChunked({
              timeseriesId,
              connectorId,
              startUtc: historyStartUtc,
              endUtc: historyEndUtc,
              since,
              limit,
            });
            historyRows = historyWindow.rows;
            historyResponseComplete = historyWindow.responseComplete;
            historyStatus = historyWindow.responseComplete
              ? "r2_chunked_complete"
              : "r2_chunked_partial";
            historyChunkCount = historyWindow.chunkCount;
            historyFailedChunkCount = historyWindow.failedChunkCount;
            historyPartialChunkCount = historyWindow.partialChunkCount;
            historyPartialReasons = historyWindow.partialReasons;
            historyChunkCoverages = historyWindow.coverage;
            console.info(
              "uk_aq_timeseries history fetch recovered via chunked retry",
              {
                timeseries_id: timeseriesId,
                connector_id: connectorId,
                chunk_days: OBSERVS_HISTORY_R2_CHUNK_DAYS,
                chunk_count: historyWindow.chunkCount,
                failed_chunk_count: historyWindow.failedChunkCount,
                partial_chunk_count: historyWindow.partialChunkCount,
                first_error: message,
              },
            );
          } catch (chunkedError) {
            const chunkedMessage = chunkedError instanceof Error
              ? chunkedError.message
              : String(chunkedError);
            historyErrors.push(chunkedMessage);
            historyStatus = "r2_error";
            console.warn("uk_aq_timeseries history fetch fallback", {
              timeseries_id: timeseriesId,
              connector_id: connectorId,
              message,
              chunked_retry_error: chunkedMessage,
              chunk_days: OBSERVS_HISTORY_R2_CHUNK_DAYS,
            });
          }
        } else {
          historyStatus = "r2_error";
          console.warn("uk_aq_timeseries history fetch fallback", {
            timeseries_id: timeseriesId,
            connector_id: connectorId,
            message,
          });
        }
      }

    } else {
      historyResponseComplete = false;
      historyStatus = "connector_unresolved";
      historyPartialReasons = ["connector_unresolved"];
      console.warn(
        "uk_aq_timeseries history fetch skipped: connector unresolved",
        {
          timeseries_id: timeseriesId,
        },
      );
    }
  }

  const retentionIngestRows = retentionWindow && shouldFetchRetentionIngest
    ? filterRowsToWindow(
      ingestRpcRows,
      retentionWindow.start,
      retentionWindow.end,
      since,
    )
    : [];
  const overlapR2Rows = overlapWindow
    ? filterRowsToWindow(
      historyRows,
      overlapWindow.start,
      overlapWindow.end,
      since,
    )
    : [];
  const overlapIngestCandidateRows = overlapWindow && shouldFetchOverlapIngest
    ? filterRowsToWindow(
      ingestRpcRows,
      overlapWindow.start,
      overlapWindow.end,
      since,
    )
    : [];
  const overlapIngestFillRows = overlapWindow && shouldFetchOverlapIngest
    ? filterRowsToMissingHours(
      overlapIngestCandidateRows,
      overlapR2Rows,
      overlapWindow.start,
      overlapWindow.end,
      since,
    )
    : [];
  const ingestRows = [...overlapIngestFillRows, ...retentionIngestRows];

  const source: StitchedFetchResult["source"] =
    shouldFetchHistory && shouldFetchIngestRows
      ? "recent_history_stitched"
    : shouldFetchHistory
      ? "history_only"
      : "recent_only";
  const mergedRows = finalizeStitchedRows(
    historyRows,
    ingestRows,
    since,
    limit,
    requestStart,
    requestEnd,
  );
  const overlapCanCoverR2Partial = !historicalWindow &&
    Boolean(overlapWindow) &&
    ingestStatus === "ingestdb_complete";
  const effectiveHistoryComplete = !shouldFetchHistory ||
    historyResponseComplete ||
    overlapCanCoverR2Partial;
  const responseComplete = effectiveHistoryComplete
    && (historyFailedChunkCount === null || historyFailedChunkCount === 0)
    && (!shouldFetchIngestRows || ingestStatus === "ingestdb_complete");

  return {
    guideline,
    rows: mergedRows,
    source,
    meta: {
      source_mode: source,
      source_split_boundary_utc: sourceSplitBoundaryUtc,
      overlap_start_utc: overlapStart.toISOString(),
      retention_start_utc: retentionStart.toISOString(),
      ingest_retention_days: INGESTDB_RETENTION_DAYS,
      source_of_truth_days: INGESTDB_RETENTION_DAYS,
      source_of_truth_hours: INGEST_SOURCE_OF_TRUTH_HOURS,
      response_complete: responseComplete,
      has_gap: !responseComplete,
      r2_coverage_start: shouldFetchHistory && r2Window ? r2Window.start.toISOString() : null,
      r2_coverage_end: shouldFetchHistory && r2Window ? r2Window.end.toISOString() : null,
      ingest_tail_start: shouldFetchIngestRows && ingestQueryStart ? ingestQueryStart.toISOString() : null,
      row_count: mergedRows.length,
      r2_row_count: historyRows.length,
      ingest_row_count: ingestRows.length,
      deduped_row_count: mergedRows.length,
      r2_errors: historyErrors,
      ingest_errors: ingestErrors,
      coverage: {
        ingest_retention_days: INGESTDB_RETENTION_DAYS,
        source_of_truth_hours: INGEST_SOURCE_OF_TRUTH_HOURS,
        source_split_boundary_utc: sourceSplitBoundaryUtc,
        overlap_start_utc: overlapStart.toISOString(),
        retention_start_utc: retentionStart.toISOString(),
        historical_window_from_utc: windowStartIso(historicalWindow),
        historical_window_to_utc: windowEndIso(historicalWindow),
        overlap_window_from_utc: windowStartIso(overlapWindow),
        overlap_window_to_utc: windowEndIso(overlapWindow),
        retention_window_from_utc: windowStartIso(retentionWindow),
        retention_window_to_utc: windowEndIso(retentionWindow),
        r2_window_from_utc: windowStartIso(r2Window),
        r2_window_to_utc: windowEndIso(r2Window),
        ingest_window_from_utc: shouldFetchIngestRows && ingestQueryStart ? ingestQueryStart.toISOString() : null,
        ingest_window_to_utc: shouldFetchIngestRows && ingestQueryEnd ? ingestQueryEnd.toISOString() : null,
        should_fetch_history: shouldFetchHistory,
        should_fetch_ingest: shouldFetchIngestRows,
        should_fetch_retention_ingest: shouldFetchRetentionIngest,
        should_fetch_overlap_ingest: shouldFetchOverlapIngest,
        history_status: historyStatus,
        ingest_status: ingestStatus,
        connector_id: connectorId,
        used_ingest_history_fallback: false,
        history_response_complete: historyResponseComplete,
        history_partial_reasons: historyPartialReasons,
        history_coverage: historyCoverage,
        history_chunk_coverages: historyChunkCoverages,
        history_chunk_count: historyChunkCount,
        history_failed_chunk_count: historyFailedChunkCount,
        history_partial_chunk_count: historyPartialChunkCount,
        overlap_r2_row_count: overlapR2Rows.length,
        overlap_ingest_candidate_row_count: overlapIngestCandidateRows.length,
        overlap_ingest_fill_row_count: overlapIngestFillRows.length,
        retention_ingest_row_count: retentionIngestRows.length,
      },
    },
  };
}

function selectIngestWindowLabel(start: Date, now: Date): IngestWindowLabel {
  const spanHours = (now.getTime() - start.getTime()) / HOUR_MS;
  if (spanHours <= 12) {
    return "12h";
  }
  if (spanHours <= 24) {
    return "24h";
  }
  if (spanHours <= 24 * 7) {
    return "7d";
  }
  return "30d";
}

function sleepMs(delayMs: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, delayMs)));
}

function getObservsHistoryRetryDelayMs(attempt: number): number {
  const delayMs = OBSERVS_HISTORY_R2_REQUEST_RETRY_BASE_MS *
    (2 ** Math.max(0, attempt - 1));
  return Math.min(delayMs, OBSERVS_HISTORY_R2_REQUEST_RETRY_CAP_MS);
}

function shouldRetryObservsHistoryRequest(message: string): boolean {
  const normalized = String(message || "").toLowerCase();
  return normalized.includes("response failed (5") ||
    normalized.includes("response failed (429") ||
    normalized.includes("error_code\":1102") ||
    normalized.includes("worker exceeded resource limits") ||
    normalized.includes("timed out") ||
    normalized.includes("request failed");
}

async function resolveTimeseriesConnectorId(
  timeseriesId: number,
): Promise<number | null> {
  const response = await postgrestRequest<TimeseriesConnectorRow[]>(
    "GET",
    "timeseries",
    {
      select: "connector_id",
      id: `eq.${timeseriesId}`,
      limit: "1",
    },
    UK_AQ_CORE_SCHEMA,
    undefined,
    {
      caller: "uk_aq_timeseries_connector_lookup",
    },
  );
  if (response.error) {
    console.warn("uk_aq_timeseries connector lookup failed", {
      timeseries_id: timeseriesId,
      message: response.error.message,
    });
    return null;
  }
  const row = Array.isArray(response.data) && response.data.length > 0
    ? response.data[0]
    : null;
  const connectorId = Number(row?.connector_id);
  if (!Number.isFinite(connectorId) || connectorId <= 0) {
    return null;
  }
  return Math.trunc(connectorId);
}

async function callObservsHistoryWindow(
  {
    timeseriesId,
    connectorId,
    startUtc,
    endUtc,
    since,
    limit,
  }: ObservsHistoryWindowCallOptions,
): Promise<ObservsHistoryWindowResult> {
  if (!OBSERVS_HISTORY_R2_API_URL) {
    throw new Error("Missing UK_AQ_OBSERVS_HISTORY_R2_API_URL.");
  }
  if (!EDGE_UPSTREAM_SECRET) {
    throw new Error("Missing UK_AQ_EDGE_UPSTREAM_SECRET.");
  }

  let lastMessage = "observs history R2 request failed";
  for (
    let attempt = 1;
    attempt <= OBSERVS_HISTORY_R2_REQUEST_MAX_ATTEMPTS;
    attempt += 1
  ) {
    const endpoint = new URL(OBSERVS_HISTORY_R2_API_URL);
    if (!endpoint.pathname || endpoint.pathname === "/") {
      endpoint.pathname = "/v1/observations";
    }
    endpoint.searchParams.set("timeseries_id", String(timeseriesId));
    endpoint.searchParams.set("connector_id", String(connectorId));
    endpoint.searchParams.set("start_utc", startUtc);
    endpoint.searchParams.set("end_utc", endUtc);
    if (since) {
      endpoint.searchParams.set("since_utc", since);
    }
    if (limit !== null) {
      endpoint.searchParams.set("limit", String(Math.max(1, limit)));
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(
      () => controller.abort(),
      OBSERVS_HISTORY_R2_API_TIMEOUT_MS,
    );
    let response: Response;
    try {
      response = await fetch(endpoint.toString(), {
        method: "GET",
        headers: {
          Accept: "application/json",
          "x-uk-aq-upstream-auth": EDGE_UPSTREAM_SECRET,
          "x-ukaq-egress-caller": "uk_aq_timeseries_history_r2",
        },
        signal: controller.signal,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        lastMessage = "observs history R2 request timed out";
      } else {
        lastMessage = `observs history R2 request failed: ${String(error)}`;
      }
      clearTimeout(timeoutId);
      if (
        attempt >= OBSERVS_HISTORY_R2_REQUEST_MAX_ATTEMPTS ||
        !shouldRetryObservsHistoryRequest(lastMessage)
      ) {
        throw new Error(lastMessage);
      }
      await sleepMs(getObservsHistoryRetryDelayMs(attempt));
      continue;
    } finally {
      clearTimeout(timeoutId);
    }

    const payloadText = await response.text();
    let payload: ObservsHistoryApiPayload | null = null;
    try {
      payload = payloadText ? JSON.parse(payloadText) : null;
    } catch (_error) {
      payload = null;
    }
    if (!response.ok) {
      const message = payload?.error || payloadText || `HTTP ${response.status}`;
      lastMessage = `observs history R2 response failed (${response.status}): ${
        String(message)
      }`;
      if (
        attempt >= OBSERVS_HISTORY_R2_REQUEST_MAX_ATTEMPTS ||
        !shouldRetryObservsHistoryRequest(lastMessage)
      ) {
        throw new Error(lastMessage);
      }
      await sleepMs(getObservsHistoryRetryDelayMs(attempt));
      continue;
    }
    if (payload && payload.ok === false) {
      lastMessage = `observs history R2 returned error: ${
        String(payload.error || "unknown")
      }`;
      if (
        attempt >= OBSERVS_HISTORY_R2_REQUEST_MAX_ATTEMPTS ||
        !shouldRetryObservsHistoryRequest(lastMessage)
      ) {
        throw new Error(lastMessage);
      }
      await sleepMs(getObservsHistoryRetryDelayMs(attempt));
      continue;
    }
    const completeness = summarizeObservsHistoryPayloadCompleteness(payload);
    return {
      rows: normalizeTimeseriesRows(
        Array.isArray(payload?.rows) ? payload.rows : [],
      ),
      responseComplete: completeness.responseComplete,
      hasGap: completeness.hasGap,
      coverage: completeness.coverage,
      partialReasons: completeness.partialReasons,
      rowCount: completeness.rowCount,
    };
  }
  throw new Error(lastMessage);
}

async function callObservsHistoryWindowChunked(
  {
    timeseriesId,
    connectorId,
    startUtc,
    endUtc,
    since,
    limit,
  }: ObservsHistoryWindowCallOptions,
): Promise<
  {
    rows: TimeseriesRow[];
    chunkCount: number;
    failedChunkCount: number;
    partialChunkCount: number;
    partialReasons: string[];
    coverage: Record<string, unknown>[];
    responseComplete: boolean;
  }
> {
  const startMs = Date.parse(startUtc);
  const endMs = Date.parse(endUtc);
  if (
    !Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs
  ) {
    return {
      rows: [],
      chunkCount: 0,
      failedChunkCount: 0,
      partialChunkCount: 0,
      partialReasons: [],
      coverage: [],
      responseComplete: true,
    };
  }

  const chunkMs = OBSERVS_HISTORY_R2_CHUNK_DAYS * DAY_MS;
  const mergedRows: TimeseriesRow[] = [];
  const partialReasons = new Set<string>();
  const coverage: Record<string, unknown>[] = [];
  let cursorMs = startMs;
  let chunkCount = 0;
  let failedChunkCount = 0;
  let partialChunkCount = 0;
  while (cursorMs < endMs) {
    const chunkEndMs = Math.min(endMs, cursorMs + chunkMs);
    const chunkStartUtc = new Date(cursorMs).toISOString();
    const chunkEndUtc = new Date(chunkEndMs).toISOString();
    const chunkResult = await fetchHistoryChunkWithBisectRetry({
      timeseriesId,
      connectorId,
      startUtc: chunkStartUtc,
      endUtc: chunkEndUtc,
      since,
      limit: null,
    });
    chunkCount += chunkResult.chunkCount;
    failedChunkCount += chunkResult.failedChunkCount;
    partialChunkCount += chunkResult.partialChunkCount;
    chunkResult.partialReasons.forEach((reason) => partialReasons.add(reason));
    coverage.push(...chunkResult.coverage);
    if (chunkResult.rows.length > 0) {
      mergedRows.push(...chunkResult.rows);
    }
    if (chunkResult.failedChunkCount > 0) {
      console.warn("uk_aq_timeseries history chunk partially skipped", {
        timeseries_id: timeseriesId,
        connector_id: connectorId,
        chunk_start_utc: chunkStartUtc,
        chunk_end_utc: chunkEndUtc,
        failed_chunk_count: chunkResult.failedChunkCount,
        partial_chunk_count: chunkResult.partialChunkCount,
        chunk_error: chunkResult.lastError,
        chunk_retry_attempts: OBSERVS_HISTORY_R2_CHUNK_MAX_RETRIES,
      });
    }
    if (limit !== null && mergedRows.length >= limit) {
      return {
        rows: mergedRows.slice(0, limit),
        chunkCount,
        failedChunkCount,
        partialChunkCount,
        partialReasons: Array.from(partialReasons),
        coverage,
        responseComplete: failedChunkCount === 0 && partialChunkCount === 0,
      };
    }
    cursorMs = chunkEndMs;
  }
  return {
    rows: mergedRows,
    chunkCount,
    failedChunkCount,
    partialChunkCount,
    partialReasons: Array.from(partialReasons),
    coverage,
    responseComplete: failedChunkCount === 0 && partialChunkCount === 0,
  };
}

async function fetchHistoryChunkWithBisectRetry(
  {
    timeseriesId,
    connectorId,
    startUtc,
    endUtc,
    since,
  }: ObservsHistoryWindowCallOptions,
): Promise<ChunkFetchResult> {
  let lastError = "";
  for (
    let attempt = 1;
    attempt <= OBSERVS_HISTORY_R2_CHUNK_MAX_RETRIES;
    attempt += 1
  ) {
    try {
      const historyWindow = await callObservsHistoryWindow({
        timeseriesId,
        connectorId,
        startUtc,
        endUtc,
        since,
        limit: null,
      });
      return {
        rows: historyWindow.rows,
        chunkCount: 1,
        failedChunkCount: 0,
        partialChunkCount: historyWindow.responseComplete ? 0 : 1,
        partialReasons: historyWindow.partialReasons,
        coverage: historyWindow.coverage ? [historyWindow.coverage] : [],
        responseComplete: historyWindow.responseComplete,
        lastError: null,
      };
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
  }

  const startMs = Date.parse(startUtc);
  const endMs = Date.parse(endUtc);
  const spanMs = endMs - startMs;
  if (!Number.isFinite(spanMs) || spanMs <= DAY_MS) {
    return {
      rows: [],
      chunkCount: 1,
      failedChunkCount: 1,
      partialChunkCount: 0,
      partialReasons: [],
      coverage: [],
      responseComplete: false,
      lastError: lastError || "history chunk failed",
    };
  }

  const middleMs = startMs + Math.floor(spanMs / 2);
  if (middleMs <= startMs || middleMs >= endMs) {
    return {
      rows: [],
      chunkCount: 1,
      failedChunkCount: 1,
      partialChunkCount: 0,
      partialReasons: [],
      coverage: [],
      responseComplete: false,
      lastError: lastError || "history chunk failed",
    };
  }
  const left = await fetchHistoryChunkWithBisectRetry({
    timeseriesId,
    connectorId,
    startUtc: new Date(startMs).toISOString(),
    endUtc: new Date(middleMs).toISOString(),
    since,
    limit: null,
  });
  const right = await fetchHistoryChunkWithBisectRetry({
    timeseriesId,
    connectorId,
    startUtc: new Date(middleMs).toISOString(),
    endUtc: new Date(endMs).toISOString(),
    since,
    limit: null,
  });
  return {
    rows: [...left.rows, ...right.rows],
    chunkCount: left.chunkCount + right.chunkCount,
    failedChunkCount: left.failedChunkCount + right.failedChunkCount,
    partialChunkCount: left.partialChunkCount + right.partialChunkCount,
    partialReasons: Array.from(
      new Set([...left.partialReasons, ...right.partialReasons]),
    ),
    coverage: [...left.coverage, ...right.coverage],
    responseComplete: left.responseComplete && right.responseComplete,
    lastError: right.lastError || left.lastError ||
      (lastError || "history chunk failed"),
  };
}

function shouldRetryHistoryChunked(
  message: string,
  startUtc: string,
  endUtc: string,
): boolean {
  const spanMs = Date.parse(endUtc) - Date.parse(startUtc);
  if (
    !Number.isFinite(spanMs) ||
    spanMs <= DAY_MS
  ) {
    return false;
  }
  const normalized = String(message || "").toLowerCase();
  return normalized.includes("response failed (5") ||
    normalized.includes("response failed (429") ||
    normalized.includes("error_code\":1102") ||
    normalized.includes("worker exceeded resource limits") ||
    normalized.includes("timed out") ||
    normalized.includes("request failed");
}

function finalizeStitchedRows(
  historyRows: TimeseriesRow[],
  ingestRows: TimeseriesRow[],
  since: string | null,
  limit: number | null,
  requestStart: Date,
  requestEnd: Date,
): TimeseriesRow[] {
  return filterRowsToWindow(
    mergeRowsPreferNewestSource(historyRows, ingestRows),
    requestStart,
    requestEnd,
    since,
    limit,
  );
}

function mergeRowsPreferNewestSource(
  historyRows: TimeseriesRow[],
  ingestRows: TimeseriesRow[],
): TimeseriesRow[] {
  const byObservedAt = new Map<string, TimeseriesRow>();
  // Insert ingest first so R2 history wins duplicate observed_at values.
  for (const row of ingestRows) {
    byObservedAt.set(row.observed_at, row);
  }
  for (const row of historyRows) {
    byObservedAt.set(row.observed_at, row);
  }
  return Array.from(byObservedAt.values()).sort((a, b) =>
    Date.parse(a.observed_at) - Date.parse(b.observed_at)
  );
}

function filterRowsToWindow(
  rows: TimeseriesRow[],
  start: Date,
  end: Date,
  since: string | null,
  limit: number | null = null,
): TimeseriesRow[] {
  const startMs = start.getTime();
  const endMs = end.getTime();
  const sinceMs = since ? Date.parse(since) : Number.NaN;
  let filteredRows = rows.filter((row) => {
    const observedMs = Date.parse(row.observed_at);
    if (!Number.isFinite(observedMs)) {
      return false;
    }
    if (observedMs < startMs || observedMs > endMs) {
      return false;
    }
    return !Number.isFinite(sinceMs) || observedMs > sinceMs;
  });

  if (limit !== null && filteredRows.length > limit) {
    filteredRows = filteredRows.slice(0, limit);
  }

  return filteredRows;
}

function looksLikeTimeseriesSignatureMismatch(message: string): boolean {
  const normalized = String(message || "").toLowerCase();
  return normalized.includes("could not find the function") &&
    normalized.includes("uk_aq_timeseries_rpc");
}

type WindowMode = "window" | "days" | "datetime";

type ResolvedWindowRange = {
  mode: WindowMode;
  windowLabel: string;
  start: Date;
  end: Date;
  days: number | null;
};

type ResolveRequestedRangeInput = {
  rawWindow: string | null;
  rawDays: string | null;
  rawStart: string | null;
  rawEnd: string | null;
  now: Date;
};

type ResolveRequestedRangeResult =
  | { ok: true; range: ResolvedWindowRange }
  | { ok: false; error: string };

function resolveRequestedRange(
  { rawWindow, rawDays, rawStart, rawEnd, now }: ResolveRequestedRangeInput,
): ResolveRequestedRangeResult {
  const windowToken = normalizeOptionalParam(rawWindow);
  const daysToken = normalizeOptionalParam(rawDays);
  const startToken = normalizeOptionalParam(rawStart);
  const endToken = normalizeOptionalParam(rawEnd);

  const hasWindow = windowToken !== null;
  const hasDays = daysToken !== null;
  const hasDateTime = startToken !== null || endToken !== null;

  if (hasDateTime && (startToken === null || endToken === null)) {
    return {
      ok: false,
      error: "Provide both start and end (or start_utc and end_utc).",
    };
  }

  const modeCount = Number(hasWindow) + Number(hasDays) + Number(hasDateTime);
  if (modeCount > 1) {
    return {
      ok: false,
      error: "Use only one range selector: window, days, or start/end.",
    };
  }

  if (hasWindow) {
    const parsedWindow = parseNamedWindowLabel(windowToken);
    if (!parsedWindow) {
      return {
        ok: false,
        error: "Invalid window. Use 12h, 24h, 7d, 31d, or 90d.",
      };
    }
    const hours = WINDOW_HOURS[parsedWindow];
    return {
      ok: true,
      range: {
        mode: "window",
        windowLabel: parsedWindow,
        start: new Date(now.getTime() - hours * HOUR_MS),
        end: new Date(now.getTime()),
        days: null,
      },
    };
  }

  if (hasDays) {
    const parsedDays = parsePositiveInteger(daysToken);
    if (parsedDays === null) {
      return { ok: false, error: "Invalid days. Provide a positive integer." };
    }
    if (parsedDays > MAX_WINDOW_DAYS) {
      return {
        ok: false,
        error: `days exceeds maximum supported range (${MAX_WINDOW_DAYS}).`,
      };
    }
    return {
      ok: true,
      range: {
        mode: "days",
        windowLabel: `${parsedDays}d`,
        start: new Date(now.getTime() - parsedDays * DAY_MS),
        end: new Date(now.getTime()),
        days: parsedDays,
      },
    };
  }

  if (hasDateTime) {
    const startIso = normalizeTimestamp(startToken as string);
    const endIso = normalizeTimestamp(endToken as string);
    if (!startIso || !endIso) {
      return {
        ok: false,
        error: "Invalid start/end. Provide ISO-8601 datetimes.",
      };
    }
    const start = new Date(startIso);
    const requestedEnd = new Date(endIso);
    if (requestedEnd.getTime() <= start.getTime()) {
      return { ok: false, error: "end must be greater than start." };
    }
    const end = requestedEnd.getTime() > now.getTime()
      ? new Date(now.getTime())
      : requestedEnd;
    if (end.getTime() <= start.getTime()) {
      return {
        ok: false,
        error: "start must be before the effective end time.",
      };
    }
    const spanDays = (end.getTime() - start.getTime()) / DAY_MS;
    if (spanDays > MAX_WINDOW_DAYS) {
      return {
        ok: false,
        error:
          `Requested span exceeds maximum supported range (${MAX_WINDOW_DAYS} days).`,
      };
    }
    return {
      ok: true,
      range: {
        mode: "datetime",
        windowLabel: "custom",
        start,
        end,
        days: null,
      },
    };
  }

  const defaultWindow = DEFAULT_WINDOW as NamedWindowLabel;
  return {
    ok: true,
    range: {
      mode: "window",
      windowLabel: defaultWindow,
      start: new Date(now.getTime() - WINDOW_HOURS[defaultWindow] * HOUR_MS),
      end: new Date(now.getTime()),
      days: null,
    },
  };
}

function parseNamedWindowLabel(value: string): NamedWindowLabel | null {
  const normalized = value.trim().toLowerCase();
  if (!normalized) {
    return null;
  }
  return Object.prototype.hasOwnProperty.call(WINDOW_HOURS, normalized)
    ? normalized as NamedWindowLabel
    : null;
}

function normalizeOptionalParam(value: string | null): string | null {
  if (value === null) {
    return null;
  }
  const trimmed = value.trim();
  return trimmed || null;
}

function firstNonEmptyParam(...values: Array<string | null>): string | null {
  for (const value of values) {
    if (value !== null && value.trim()) {
      return value;
    }
  }
  return null;
}

function normalizeFormat(value: string | null): "objects" | "compact" | null {
  if (!value) {
    return DEFAULT_FORMAT;
  }
  const normalized = value.trim().toLowerCase();
  if (normalized === "objects" || normalized === "object") {
    return "objects";
  }
  if (
    normalized === "compact" || normalized === "array" ||
    normalized === "arrays"
  ) {
    return "compact";
  }
  return null;
}

function parseOptionalLimit(value: string | null): number | null {
  return parsePositiveInteger(value);
}

function parsePositiveInteger(value: string | null | undefined): number | null {
  if (value === null || value === undefined) {
    return null;
  }
  const trimmed = String(value).trim();
  if (!trimmed || !/^\d+$/.test(trimmed)) {
    return null;
  }
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed) || parsed < 1) {
    return null;
  }
  return Math.floor(parsed);
}

function normalizeTimestamp(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  const parsed = new Date(trimmed);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }
  return parsed.toISOString();
}

function maxObservedTimestamp(
  rows: any[],
  fallback: string | null,
): string | null {
  let best = fallback ? normalizeTimestamp(fallback) : null;
  let bestMs = best ? Date.parse(best) : Number.NEGATIVE_INFINITY;
  rows.forEach((row) => {
    const observedAt = row?.observed_at;
    if (!observedAt) {
      return;
    }
    const normalized = normalizeTimestamp(String(observedAt));
    if (!normalized) {
      return;
    }
    const ms = Date.parse(normalized);
    if (ms > bestMs) {
      bestMs = ms;
      best = normalized;
    }
  });
  return best;
}

type TimeseriesRow = {
  observed_at: string;
  value: number | null;
};

function normalizeTimeseriesRows(
  rows: unknown[],
): TimeseriesRow[] {
  const normalizedRows: TimeseriesRow[] = [];
  for (const row of rows) {
    const observedAt = normalizeTimestamp(
      String((row as any)?.observed_at ?? ""),
    );
    if (!observedAt) {
      continue;
    }
    const rawValue = (row as any)?.value;
    const parsedValue = rawValue === null || rawValue === undefined
      ? null
      : Number(rawValue);
    const baseRow: TimeseriesRow = {
      observed_at: observedAt,
      value: parsedValue === null || Number.isFinite(parsedValue)
        ? parsedValue
        : null,
    };
    normalizedRows.push(baseRow);
  }
  return normalizedRows;
}

function timeseriesColumns(): string[] {
  return ["observed_at", "value"];
}

function shapeTimeseriesData(
  rows: TimeseriesRow[],
  format: "objects" | "compact",
): unknown[] {
  if (format === "compact") {
    return rows.map((row) => [row.observed_at, row.value]);
  }
  return rows.map((row) => ({
    observed_at: row.observed_at,
    value: row.value,
  }));
}

function etagPayload(payload: {
  timeseries_id: number;
  window: string;
  since: string | null;
  next_since: string | null;
  data_format: string;
  columns: string[];
  count: number;
  guideline: unknown;
  data: unknown;
}) {
  return {
    timeseries_id: payload.timeseries_id,
    window: payload.window,
    since: payload.since,
    next_since: payload.next_since,
    data_format: payload.data_format,
    columns: payload.columns,
    count: payload.count,
    guideline: payload.guideline,
    data: payload.data,
  };
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
