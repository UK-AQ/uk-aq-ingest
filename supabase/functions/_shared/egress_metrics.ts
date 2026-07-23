const DEFAULT_SAMPLE_RATE = 0.2;
const SAMPLE_RATE_ENV = "UK_AQ_EGRESS_LOG_SAMPLE_RATE";
const DB_ENABLED_ENV = "UK_AQ_EGRESS_METRICS_DB_ENABLED";
const CLEANUP_SAMPLE_RATE_ENV = "UK_AQ_EGRESS_METRICS_CLEANUP_SAMPLE_RATE";
const CLEANUP_MIN_INTERVAL_MS_ENV =
  "UK_AQ_EGRESS_METRICS_CLEANUP_MIN_INTERVAL_MS";
const AGG_RETENTION_DAYS_ENV = "UK_AQ_EGRESS_METRICS_AGG_RETENTION_DAYS";
const RAW_RETENTION_DAYS_ENV = "UK_AQ_EGRESS_METRICS_RAW_RETENTION_DAYS";
const DEFAULT_DB_ENABLED = true;
const DEFAULT_CLEANUP_SAMPLE_RATE = 0.01;
const DEFAULT_CLEANUP_MIN_INTERVAL_MS = 15 * 60 * 1000;
const DEFAULT_AGG_RETENTION_DAYS = 30;
const DEFAULT_RAW_RETENTION_DAYS = 7;
const MIN_DB_WARN_INTERVAL_MS = 60_000;
export const EGRESS_BYPASS_HEADER = "x-ukaq-egress-bypass";

export type MetricFields = Record<string, unknown>;
type MetricPayload = {
  endpoint: string;
  method: string;
  status: number;
  duration_ms: number;
  response_bytes: number | null;
  sample_rate: number;
  ts: string;
  request_meta: MetricFields;
};

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ??
  Deno.env.get("SB_SUPABASE_URL") ??
  "";
const SB_SECRET_KEY = Deno.env.get("SB_SECRET_KEY") ?? "";
const SUPABASE_PRIVILEGED_KEY = SB_SECRET_KEY;
const UK_AQ_PUBLIC_SCHEMA = Deno.env.get("UK_AQ_PUBLIC_SCHEMA") ??
  "uk_aq_public";
const REST_BASE_URL = SUPABASE_URL
  ? `${SUPABASE_URL.replace(/\/$/, "")}/rest/v1`
  : "";
const DB_METRICS_ENABLED = parseBoolean(
  Deno.env.get(DB_ENABLED_ENV),
  DEFAULT_DB_ENABLED,
);
const CLEANUP_SAMPLE_RATE = parseSampleRateWithFallback(
  Deno.env.get(CLEANUP_SAMPLE_RATE_ENV),
  DEFAULT_CLEANUP_SAMPLE_RATE,
);
const CLEANUP_MIN_INTERVAL_MS = parsePositiveInt(
  Deno.env.get(CLEANUP_MIN_INTERVAL_MS_ENV),
  DEFAULT_CLEANUP_MIN_INTERVAL_MS,
);
const AGG_RETENTION_DAYS = parsePositiveInt(
  Deno.env.get(AGG_RETENTION_DAYS_ENV),
  DEFAULT_AGG_RETENTION_DAYS,
);
const RAW_RETENTION_DAYS = parsePositiveInt(
  Deno.env.get(RAW_RETENTION_DAYS_ENV),
  DEFAULT_RAW_RETENTION_DAYS,
);

let dbWriteDisabledReason: string | null = null;
let lastDbWarnAtMs = 0;
let lastCleanupAttemptAtMs = 0;

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function parseSampleRateWithFallback(
  raw: string | undefined | null,
  fallback: number,
): number {
  if (!raw) {
    return fallback;
  }
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return clamp(parsed, 0, 1);
}

function parseSampleRate(raw: string | undefined | null): number {
  return parseSampleRateWithFallback(raw, DEFAULT_SAMPLE_RATE);
}

function parseBoolean(
  raw: string | undefined | null,
  fallback: boolean,
): boolean {
  if (!raw) {
    return fallback;
  }
  const normalized = raw.trim().toLowerCase();
  if (!normalized) {
    return fallback;
  }
  if (["1", "true", "yes", "on"].includes(normalized)) {
    return true;
  }
  if (["0", "false", "no", "off"].includes(normalized)) {
    return false;
  }
  return fallback;
}

function parsePositiveInt(
  raw: string | undefined | null,
  fallback: number,
): number {
  if (!raw) {
    return fallback;
  }
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed < 1) {
    return fallback;
  }
  return Math.floor(parsed);
}

function shouldLog(status: number, sampleRate: number): boolean {
  if (status >= 400 || status === 304) {
    return true;
  }
  if (status >= 200 && status < 300) {
    return Math.random() < sampleRate;
  }
  return false;
}

async function responseBytes(response: Response): Promise<number | null> {
  const contentLength = response.headers.get("content-length");
  if (contentLength) {
    const parsed = Number(contentLength);
    if (Number.isFinite(parsed) && parsed >= 0) {
      return Math.floor(parsed);
    }
  }
  if (!response.body) {
    return 0;
  }
  try {
    const bytes = await response.clone().arrayBuffer();
    return bytes.byteLength;
  } catch {
    return null;
  }
}

function cleanFields(fields: MetricFields): MetricFields {
  const output: MetricFields = {};
  for (const [key, value] of Object.entries(fields)) {
    if (value !== undefined) {
      output[key] = value;
    }
  }
  return output;
}

function shouldDisableDbWrites(message: string): boolean {
  const normalized = String(message || "").toLowerCase();
  return normalized.includes("could not find the function") ||
    normalized.includes("relation") && normalized.includes("does not exist") ||
    normalized.includes("missing supabase_url") ||
    normalized.includes("missing supabase_service_role_key");
}

function warnDb(message: string, context: Record<string, unknown> = {}): void {
  const now = Date.now();
  if (now - lastDbWarnAtMs < MIN_DB_WARN_INTERVAL_MS) {
    return;
  }
  lastDbWarnAtMs = now;
  console.warn(JSON.stringify({
    metric: "uk_aq_endpoint_egress_db_warning",
    message,
    ts: new Date(now).toISOString(),
    ...context,
  }));
}

function postgrestHeaders(
  schema = UK_AQ_PUBLIC_SCHEMA,
): Record<string, string> {
  const headers: Record<string, string> = {
    apikey: SUPABASE_PRIVILEGED_KEY,
    "Content-Type": "application/json",
    [EGRESS_BYPASS_HEADER]: "1",
  };
  if (schema && schema !== "public") {
    headers["Accept-Profile"] = schema;
    headers["Content-Profile"] = schema;
  }
  return headers;
}

async function postgrestRpc(fn: string, args: Record<string, unknown>): Promise<
  { ok: boolean; message?: string }
> {
  if (!REST_BASE_URL || !SUPABASE_PRIVILEGED_KEY) {
    return {
      ok: false,
      message: "Missing SUPABASE_URL or SB_SECRET_KEY.",
    };
  }
  const url = `${REST_BASE_URL}/rpc/${fn}`;
  const resp = await fetch(url, {
    method: "POST",
    headers: postgrestHeaders(),
    body: JSON.stringify(args),
  });
  if (resp.ok) {
    return { ok: true };
  }
  const contentType = resp.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await resp.json().catch(() => null)
    : await resp.text().catch(() => null);
  const message = payload?.message || payload?.error_description ||
    payload?.error ||
    (typeof payload === "string" ? payload : resp.statusText);
  return { ok: false, message: String(message || "PostgREST RPC failed.") };
}

async function maybeCleanupMetrics(): Promise<void> {
  if (CLEANUP_SAMPLE_RATE <= 0) {
    return;
  }
  const now = Date.now();
  if (now - lastCleanupAttemptAtMs < CLEANUP_MIN_INTERVAL_MS) {
    return;
  }
  if (Math.random() >= CLEANUP_SAMPLE_RATE) {
    return;
  }
  lastCleanupAttemptAtMs = now;
  const cleanup = await postgrestRpc("uk_aq_cleanup_endpoint_metrics", {
    p_aggregate_retention_days: AGG_RETENTION_DAYS,
    p_event_retention_days: RAW_RETENTION_DAYS,
  });
  if (!cleanup.ok) {
    warnDb("metrics cleanup RPC failed", {
      reason: cleanup.message ?? null,
    });
  }
}

async function persistMetricToDb(payload: MetricPayload): Promise<void> {
  if (!DB_METRICS_ENABLED || dbWriteDisabledReason) {
    return;
  }
  const writeResult = await postgrestRpc("uk_aq_record_endpoint_metric", {
    p_endpoint: payload.endpoint,
    p_method: payload.method,
    p_status: payload.status,
    p_duration_ms: payload.duration_ms,
    p_response_bytes: payload.response_bytes,
    p_sample_rate: payload.sample_rate,
    p_occurred_at: payload.ts,
    p_request_meta: payload.request_meta,
  });
  if (!writeResult.ok) {
    const reason = writeResult.message ?? "unknown_error";
    if (shouldDisableDbWrites(reason)) {
      dbWriteDisabledReason = reason;
      warnDb("metrics DB writes disabled", { reason });
      return;
    }
    warnDb("metrics DB write failed", { reason });
    return;
  }
  await maybeCleanupMetrics();
}

export async function recordEgressMetric(input: {
  endpoint: string;
  method: string;
  status: number;
  durationMs: number;
  responseBytes: number | null;
  fields?: MetricFields;
  sampleRate?: number;
  force?: boolean;
}): Promise<void> {
  const sampleRate = clamp(
    Number.isFinite(input.sampleRate)
      ? Number(input.sampleRate)
      : parseSampleRate(Deno.env.get(SAMPLE_RATE_ENV)),
    0,
    1,
  );
  const status = Number.isFinite(input.status) ? Math.floor(input.status) : 0;
  if (!input.force && !shouldLog(status, sampleRate)) {
    return;
  }
  const payload: MetricPayload = {
    endpoint: String(input.endpoint || "").trim() || "unknown",
    method: String(input.method || "GET").toUpperCase(),
    status,
    duration_ms: Math.max(0, Math.floor(input.durationMs || 0)),
    response_bytes:
      input.responseBytes === null || input.responseBytes === undefined
        ? null
        : Math.max(0, Math.floor(input.responseBytes)),
    sample_rate: sampleRate,
    ts: new Date().toISOString(),
    request_meta: cleanFields(input.fields ?? {}),
  };
  console.log(JSON.stringify({
    metric: "uk_aq_endpoint_egress",
    ...payload,
    ...payload.request_meta,
  }));
  await persistMetricToDb(payload);
}

export async function logEndpointEgress(
  req: Request,
  endpoint: string,
  startedAtMs: number,
  response: Response,
  fields: MetricFields = {},
): Promise<Response> {
  await recordEgressMetric({
    endpoint,
    method: req.method,
    status: response.status,
    durationMs: Date.now() - startedAtMs,
    responseBytes: await responseBytes(response),
    fields,
  });
  return response;
}
