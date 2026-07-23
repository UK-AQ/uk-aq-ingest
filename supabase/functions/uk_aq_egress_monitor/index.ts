import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import "../_shared/fetch_egress_patch.ts";

type EgressMinuteRow = {
  endpoint: string | null;
  response_bytes_sum: number | null;
  observed_requests: number | null;
  estimated_requests: number | null;
  bucket_minute: string | null;
};

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ??
  Deno.env.get("SB_SUPABASE_URL") ??
  "";
const SB_SECRET_KEY = Deno.env.get("SB_SECRET_KEY") ??
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ??
  "";
const SUPABASE_PRIVILEGED_KEY = SB_SECRET_KEY;
const SB_UK_AQ_CRON_SECRET = Deno.env.get("SB_UK_AQ_CRON_SECRET") ?? "";
const UK_AQ_PUBLIC_SCHEMA = Deno.env.get("UK_AQ_PUBLIC_SCHEMA") ??
  "uk_aq_public";
const UK_AQ_RAW_SCHEMA = Deno.env.get("UK_AQ_RAW_SCHEMA") ?? "uk_aq_raw";
const REST_BASE_URL = SUPABASE_URL
  ? `${SUPABASE_URL.replace(/\/$/, "")}/rest/v1`
  : "";

const DEFAULT_LOOKBACK_MINUTES = 60;
const DEFAULT_TOP_N = 20;
const DEFAULT_ALERT_MB = 250;
const DEFAULT_WRITE_ERROR_LOG = true;
const DEFAULT_PAGE_SIZE = 1000;
const DEFAULT_MAX_ROWS = 100_000;
const DEFAULT_RUNTIME_BUDGET_MS = 120_000;
const DEFAULT_REQUEST_TIMEOUT_MS = 20_000;
const MIN_REQUEST_TIMEOUT_MS = 1_000;
const RUNTIME_BUDGET_BUFFER_MS = 1_000;
const EGRESS_BYPASS_HEADER = "x-ukaq-egress-bypass";

function parsePositiveInt(
  raw: string | undefined | null,
  fallback: number,
  min = 1,
  max = 100_000,
): number {
  const parsed = Number(raw ?? "");
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(min, Math.min(max, Math.floor(parsed)));
}

function parseBoolean(
  raw: string | undefined | null,
  fallback: boolean,
): boolean {
  if (!raw) {
    return fallback;
  }
  const normalized = raw.trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(normalized)) {
    return true;
  }
  if (["0", "false", "no", "off"].includes(normalized)) {
    return false;
  }
  return fallback;
}

function parsePositiveNumber(
  raw: string | undefined | null,
  fallback: number,
): number {
  const parsed = Number(raw ?? "");
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return fallback;
  }
  return parsed;
}

function requireCronSecret(req: Request): Response | null {
  if (!SB_UK_AQ_CRON_SECRET) {
    return null;
  }
  const header = req.headers.get("x-cron-secret");
  if (!header || header !== SB_UK_AQ_CRON_SECRET) {
    return new Response("Unauthorized", { status: 401 });
  }
  return null;
}

function postgrestHeaders(
  schema = UK_AQ_PUBLIC_SCHEMA,
): Record<string, string> {
  const headers: Record<string, string> = {
    apikey: SUPABASE_PRIVILEGED_KEY,
    "Content-Type": "application/json",
    [EGRESS_BYPASS_HEADER]: "1",
    "x-ukaq-egress-caller": "uk_aq_egress_monitor",
  };
  // Keep legacy JWT bearer support during migration; non-JWT secret keys use apikey only.
  if (schema && schema !== "public") {
    headers["Accept-Profile"] = schema;
    headers["Content-Profile"] = schema;
  }
  return headers;
}

async function postgrestRequest<T>(
  method: string,
  table: string,
  params?: Record<string, string>,
  body?: unknown,
  schema = UK_AQ_PUBLIC_SCHEMA,
  signal?: AbortSignal,
): Promise<{ data: T | null; error: { message: string } | null }> {
  if (!REST_BASE_URL || !SUPABASE_PRIVILEGED_KEY) {
    return {
      data: null,
      error: { message: "Missing SUPABASE_URL or SB_SECRET_KEY." },
    };
  }
  const url = new URL(`${REST_BASE_URL}/${table}`);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null) {
      url.searchParams.set(key, String(value));
    }
  }
  const resp = await fetch(url.toString(), {
    method,
    headers: postgrestHeaders(schema),
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  const contentType = resp.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await resp.json().catch(() => null)
    : await resp.text().catch(() => null);
  if (!resp.ok) {
    const message = payload?.message || payload?.error_description ||
      payload?.error || resp.statusText;
    return {
      data: null,
      error: { message: String(message || "PostgREST request failed.") },
    };
  }
  return { data: payload as T, error: null };
}

function toMiB(bytes: number): number {
  return bytes / (1024 * 1024);
}

function estimateBytes(row: EgressMinuteRow, observedBytes: number): number {
  const observedRequests = Math.max(0, Number(row.observed_requests ?? 0));
  const estimatedRequests = Math.max(
    0,
    Number(row.estimated_requests ?? observedRequests),
  );
  if (observedRequests <= 0) {
    return observedBytes;
  }
  const ratio = estimatedRequests / observedRequests;
  if (!Number.isFinite(ratio) || ratio <= 0) {
    return observedBytes;
  }
  return observedBytes * ratio;
}

function splitEndpointCaller(rawEndpoint: string): {
  endpoint: string;
  caller: string | null;
} {
  const marker = "|caller=";
  const markerIndex = rawEndpoint.indexOf(marker);
  if (markerIndex < 0) {
    return { endpoint: rawEndpoint, caller: null };
  }
  const endpoint = rawEndpoint.slice(0, markerIndex) || "unknown";
  const callerRaw = rawEndpoint.slice(markerIndex + marker.length).trim();
  const caller = callerRaw || null;
  return { endpoint, caller };
}

async function fetchEgressRowsSince(
  sinceIso: string,
  pageSize: number,
  maxRows: number,
  options: {
    startedAtMs: number;
    runtimeBudgetMs: number;
    requestTimeoutMs: number;
  },
): Promise<{
  rows: EgressMinuteRow[];
  pages: number;
  truncated: boolean;
  budget_exhausted: boolean;
  request_timed_out: boolean;
}> {
  const rows: EgressMinuteRow[] = [];
  let pages = 0;
  let offset = 0;
  let budgetExhausted = false;
  let requestTimedOut = false;
  const metricsSources: Array<{ table: string; schema: string }> = [
    { table: "uk_aq_endpoint_egress_metrics_minute", schema: UK_AQ_PUBLIC_SCHEMA },
    { table: "endpoint_egress_metrics_minute", schema: UK_AQ_RAW_SCHEMA },
  ];
  while (rows.length < maxRows) {
    const elapsedMs = Date.now() - options.startedAtMs;
    const remainingBudgetMs = options.runtimeBudgetMs - elapsedMs;
    if (remainingBudgetMs <= RUNTIME_BUDGET_BUFFER_MS) {
      budgetExhausted = true;
      break;
    }
    const perRequestTimeoutMs = Math.min(
      options.requestTimeoutMs,
      remainingBudgetMs - RUNTIME_BUDGET_BUFFER_MS,
    );
    if (perRequestTimeoutMs < MIN_REQUEST_TIMEOUT_MS) {
      budgetExhausted = true;
      break;
    }
    const limit = Math.min(pageSize, maxRows - rows.length);
    const requestController = new AbortController();
    const requestTimer = setTimeout(
      () => requestController.abort(),
      perRequestTimeoutMs,
    );
    let data: EgressMinuteRow[] | null = null;
    let error: { message: string } | null = null;
    const sourceErrors: string[] = [];
    try {
      for (const source of metricsSources) {
        const response = await postgrestRequest<EgressMinuteRow[]>(
          "GET",
          source.table,
          {
            select:
              "endpoint,response_bytes_sum,observed_requests,estimated_requests,bucket_minute",
            bucket_minute: `gte.${sinceIso}`,
            order: "bucket_minute.asc",
            limit: String(limit),
            offset: String(offset),
          },
          undefined,
          source.schema,
          requestController.signal,
        );
        data = response.data;
        error = response.error;
        if (!error) {
          break;
        }
        sourceErrors.push(`${source.schema}.${source.table}: ${error.message}`);
      }
    } catch (requestError) {
      if (
        requestError instanceof DOMException &&
        requestError.name === "AbortError"
      ) {
        requestTimedOut = true;
        break;
      }
      throw requestError;
    } finally {
      clearTimeout(requestTimer);
    }
    if (error) {
      const errorDetails = sourceErrors.length > 0
        ? sourceErrors.join(" | ")
        : error.message;
      throw new Error(`Failed to load egress metrics: ${errorDetails}`);
    }
    const batch = data ?? [];
    rows.push(...batch);
    pages += 1;
    if (batch.length < limit) {
      return {
        rows,
        pages,
        truncated: false,
        budget_exhausted: false,
        request_timed_out: false,
      };
    }
    offset += limit;
  }
  return {
    rows,
    pages,
    truncated: rows.length >= maxRows || budgetExhausted || requestTimedOut,
    budget_exhausted: budgetExhausted,
    request_timed_out: requestTimedOut,
  };
}

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204 });
  }
  if (req.method !== "GET" && req.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }
  const authResponse = requireCronSecret(req);
  if (authResponse) {
    return authResponse;
  }
  try {
    const startedAtMs = Date.now();
    const url = new URL(req.url);
    const lookbackMinutes = parsePositiveInt(
      url.searchParams.get("lookback_minutes") ??
        Deno.env.get("UK_AQ_EGRESS_MONITOR_LOOKBACK_MINUTES"),
      DEFAULT_LOOKBACK_MINUTES,
      1,
      1440,
    );
    const topN = parsePositiveInt(
      url.searchParams.get("top_n") ??
        Deno.env.get("UK_AQ_EGRESS_MONITOR_TOP_N"),
      DEFAULT_TOP_N,
      1,
      100,
    );
    const alertMb = parsePositiveNumber(
      url.searchParams.get("alert_mb") ??
        Deno.env.get("UK_AQ_EGRESS_MONITOR_ALERT_MB"),
      DEFAULT_ALERT_MB,
    );
    const writeErrorLog = parseBoolean(
      url.searchParams.get("write_error_log") ??
        Deno.env.get("UK_AQ_EGRESS_MONITOR_WRITE_ERROR_LOG"),
      DEFAULT_WRITE_ERROR_LOG,
    );
    const pageSize = parsePositiveInt(
      url.searchParams.get("page_size") ??
        Deno.env.get("UK_AQ_EGRESS_MONITOR_PAGE_SIZE"),
      DEFAULT_PAGE_SIZE,
      100,
      10_000,
    );
    const maxRows = parsePositiveInt(
      url.searchParams.get("max_rows") ??
        Deno.env.get("UK_AQ_EGRESS_MONITOR_MAX_ROWS"),
      DEFAULT_MAX_ROWS,
      1_000,
      1_000_000,
    );
    const runtimeBudgetMs = parsePositiveInt(
      url.searchParams.get("runtime_budget_ms") ??
        Deno.env.get("UK_AQ_EGRESS_MONITOR_RUNTIME_BUDGET_MS"),
      DEFAULT_RUNTIME_BUDGET_MS,
      10_000,
      300_000,
    );
    const requestTimeoutMs = parsePositiveInt(
      url.searchParams.get("request_timeout_ms") ??
        Deno.env.get("UK_AQ_EGRESS_MONITOR_REQUEST_TIMEOUT_MS"),
      DEFAULT_REQUEST_TIMEOUT_MS,
      MIN_REQUEST_TIMEOUT_MS,
      120_000,
    );

    const sinceIso = new Date(Date.now() - (lookbackMinutes * 60 * 1000))
      .toISOString();
    const fetchResult = await fetchEgressRowsSince(
      sinceIso,
      pageSize,
      maxRows,
      {
        startedAtMs,
        runtimeBudgetMs,
        requestTimeoutMs,
      },
    );
    const data = fetchResult.rows;

    const totalsByEndpoint = new Map<
      string,
      {
        observedBytes: number;
        estimatedBytes: number;
        observedRequests: number;
        estimatedRequests: number;
      }
    >();
    const totalsByEndpointCaller = new Map<
      string,
      {
        endpoint: string;
        caller: string | null;
        observedBytes: number;
        estimatedBytes: number;
        observedRequests: number;
        estimatedRequests: number;
      }
    >();
    let totalObservedBytes = 0;
    let totalEstimatedBytes = 0;
    let totalObservedRequests = 0;
    let totalEstimatedRequests = 0;
    for (const row of data ?? []) {
      const rawEndpoint = String(row.endpoint ?? "").trim() || "unknown";
      const split = splitEndpointCaller(rawEndpoint);
      const endpoint = split.endpoint;
      const observedBytes = Math.max(0, Number(row.response_bytes_sum ?? 0));
      const estimatedBytes = Math.max(0, estimateBytes(row, observedBytes));
      const observedRequests = Math.max(0, Number(row.observed_requests ?? 0));
      const estimatedRequests = Math.max(
        0,
        Number(row.estimated_requests ?? observedRequests),
      );
      totalObservedBytes += observedBytes;
      totalEstimatedBytes += estimatedBytes;
      totalObservedRequests += observedRequests;
      totalEstimatedRequests += estimatedRequests;
      const existing = totalsByEndpoint.get(endpoint) ?? {
        observedBytes: 0,
        estimatedBytes: 0,
        observedRequests: 0,
        estimatedRequests: 0,
      };
      existing.observedBytes += observedBytes;
      existing.estimatedBytes += estimatedBytes;
      existing.observedRequests += observedRequests;
      existing.estimatedRequests += estimatedRequests;
      totalsByEndpoint.set(endpoint, existing);

      const endpointCallerKey = `${endpoint}|caller=${
        split.caller ?? "unknown"
      }`;
      const existingEndpointCaller =
        totalsByEndpointCaller.get(endpointCallerKey) ?? {
          endpoint,
          caller: split.caller,
          observedBytes: 0,
          estimatedBytes: 0,
          observedRequests: 0,
          estimatedRequests: 0,
        };
      existingEndpointCaller.observedBytes += observedBytes;
      existingEndpointCaller.estimatedBytes += estimatedBytes;
      existingEndpointCaller.observedRequests += observedRequests;
      existingEndpointCaller.estimatedRequests += estimatedRequests;
      totalsByEndpointCaller.set(endpointCallerKey, existingEndpointCaller);
    }

    const topEndpointsObserved = Array.from(totalsByEndpoint.entries())
      .map(([endpoint, value]) => ({
        endpoint,
        mb: Number(toMiB(value.observedBytes).toFixed(3)),
        estimated_mb: Number(toMiB(value.estimatedBytes).toFixed(3)),
        requests: Math.round(value.observedRequests),
        estimated_requests: Math.round(value.estimatedRequests),
      }))
      .sort((a, b) => b.mb - a.mb)
      .slice(0, topN);

    const topEndpointsEstimated = Array.from(totalsByEndpoint.entries())
      .map(([endpoint, value]) => ({
        endpoint,
        mb: Number(toMiB(value.estimatedBytes).toFixed(3)),
        observed_mb: Number(toMiB(value.observedBytes).toFixed(3)),
        requests: Math.round(value.estimatedRequests),
        observed_requests: Math.round(value.observedRequests),
      }))
      .sort((a, b) => b.mb - a.mb)
      .slice(0, topN);
    const topEndpointCallersEstimated = Array.from(
      totalsByEndpointCaller.values(),
    )
      .map((value) => ({
        endpoint: value.endpoint,
        caller: value.caller,
        mb: Number(toMiB(value.estimatedBytes).toFixed(3)),
        observed_mb: Number(toMiB(value.observedBytes).toFixed(3)),
        requests: Math.round(value.estimatedRequests),
        observed_requests: Math.round(value.observedRequests),
      }))
      .sort((a, b) => b.mb - a.mb)
      .slice(0, topN);

    const totalObservedMb = Number(toMiB(totalObservedBytes).toFixed(3));
    const totalEstimatedMb = Number(toMiB(totalEstimatedBytes).toFixed(3));
    const thresholdExceeded = totalEstimatedMb >= alertMb;
    const rowsTruncatedReason = fetchResult.request_timed_out
      ? "request_timeout"
      : fetchResult.budget_exhausted
      ? "runtime_budget"
      : fetchResult.truncated
      ? "max_rows"
      : null;

    if (thresholdExceeded) {
      console.warn("uk_aq_egress_monitor_threshold_exceeded", {
        total_observed_mb: totalObservedMb,
        total_estimated_mb: totalEstimatedMb,
        alert_mb: alertMb,
        lookback_minutes: lookbackMinutes,
        top_endpoint: topEndpointsEstimated[0]?.endpoint ?? null,
      });
      if (writeErrorLog) {
        const { error: logError } = await postgrestRequest(
          "POST",
          "error_logs",
          undefined,
          {
            source: "edge",
            severity: "warn",
            message: "uk_aq_egress_monitor threshold exceeded",
            stack: null,
            context: {
              total_observed_mb: totalObservedMb,
              total_estimated_mb: totalEstimatedMb,
              alert_mb: alertMb,
              lookback_minutes: lookbackMinutes,
              top_endpoints_estimated: topEndpointsEstimated.slice(0, 5),
            },
            connector_id: null,
            station_id: null,
            timeseries_id: null,
          },
          UK_AQ_RAW_SCHEMA,
        );
        if (logError) {
          console.warn("uk_aq_egress_monitor_error_log_failed", {
            error: logError.message,
          });
        }
      }
    }

    return new Response(
      JSON.stringify({
        checked_at: new Date().toISOString(),
        lookback_minutes: lookbackMinutes,
        rows_scanned: data.length,
        pages_scanned: fetchResult.pages,
        rows_truncated: fetchResult.truncated,
        page_size: pageSize,
        max_rows: maxRows,
        runtime_budget_ms: runtimeBudgetMs,
        request_timeout_ms: requestTimeoutMs,
        runtime_elapsed_ms: Date.now() - startedAtMs,
        total_observed_mb: totalObservedMb,
        total_estimated_mb: totalEstimatedMb,
        total_observed_requests: Math.round(totalObservedRequests),
        total_estimated_requests: Math.round(totalEstimatedRequests),
        threshold_basis: "estimated_mb",
        total_mb: totalObservedMb,
        total_requests: Math.round(totalObservedRequests),
        alert_threshold_mb: alertMb,
        threshold_exceeded: thresholdExceeded,
        rows_truncated_reason: rowsTruncatedReason,
        runtime_budget_exhausted: fetchResult.budget_exhausted,
        request_timed_out: fetchResult.request_timed_out,
        top_endpoints: topEndpointsObserved,
        top_endpoints_estimated: topEndpointsEstimated,
        top_endpoint_callers_estimated: topEndpointCallersEstimated,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error("uk_aq_egress_monitor failed", { message });
    return new Response(JSON.stringify({
      error: "Internal server error.",
      message,
    }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }
});
