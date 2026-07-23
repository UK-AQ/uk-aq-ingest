//trigger deploy 2026-02-09 13:34
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import "../_shared/fetch_egress_patch.ts";
import { cacheControlHeaders } from "../_shared/cache.ts";
import { createWeakEtag, ifNoneMatchMatches } from "../_shared/etag.ts";
import { logEndpointEgress } from "../_shared/egress_metrics.ts";
import { parsePublicNetworkFilter } from "../_shared/public_network_filter.ts";
import { validateWorkerUpstreamAuth } from "../_shared/worker_auth.ts";

const DEFAULT_LIMIT = 1000;
const MAX_LIMIT = 10000;
const DEFAULT_WINDOW = "all";

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
    "x-ukaq-egress-caller": "uk_aq_stations_chart",
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
    logEndpointEgress(
      req,
      "uk_aq_stations_chart",
      startedAtMs,
      response,
      fields,
    );
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
  const stationLike = normalizeText(
    url.searchParams.get("station_like") ?? url.searchParams.get("q"),
  );
  if (!stationLike) {
    return await finish(json({ error: "Missing station_like (or q)." }, 400), {
      error_type: "missing_station_like",
    });
  }
  const pollutant = normalizePollutant(url.searchParams.get("pollutant"));
  const windowLabel = normalizeWindow(url.searchParams.get("window"));
  const limit = parseLimit(url.searchParams.get("limit"), DEFAULT_LIMIT);
  const rawSince = url.searchParams.get("since");
  const since = rawSince === null ? null : normalizeTimestamp(rawSince);
  const rawSinceId = url.searchParams.get("since_id");
  const sinceId = rawSinceId === null ? null : normalizeCursorId(rawSinceId);
  if (rawSince !== null && since === null) {
    return await finish(
      json({
        error:
          "Invalid since timestamp. Provide ISO-8601 datetime (e.g. 2026-02-07T10:30:00Z).",
      }, 400),
      { error_type: "invalid_since" },
    );
  }
  if (rawSinceId !== null && sinceId === null) {
    return await finish(
      json({ error: "Invalid since_id. Provide a non-negative integer." }, 400),
      {
        error_type: "invalid_since_id",
      },
    );
  }
  if (!since && sinceId !== null) {
    return await finish(json({ error: "since_id requires since." }, 400), {
      error_type: "since_id_without_since",
    });
  }
  const ifNoneMatch = req.headers.get("if-none-match");
  const effectiveSinceId = since ? (sinceId ?? 0) : null;
  const requestFields = {
    station_like: stationLike,
    has_network_code: Boolean(networkCode),
    pollutant: pollutant ?? null,
    window: windowLabel,
    limit,
    has_since: Boolean(since),
    has_since_id: effectiveSinceId !== null,
    has_if_none_match: Boolean(ifNoneMatch),
  };

  try {
    if (since && ifNoneMatch) {
      const hasDelta = await hasLatestDelta({
        stationLike,
        networkCode,
        pollutant,
        windowLabel,
        limit,
        since,
        sinceId: effectiveSinceId,
      });
      if (!hasDelta) {
        const emptyPayload = {
          contract_version: 2,
          station_like: stationLike,
          network_code: networkCode,
          pollutant,
          window: windowLabel,
          since,
          since_id: effectiveSinceId,
          next_since: since,
          next_since_id: effectiveSinceId,
          count: 0,
          data: [],
        };
        const emptyEtag = await createWeakEtag({
          endpoint: "uk_aq_stations_chart",
          version: 2,
          payload: emptyPayload,
        });
        if (ifNoneMatchMatches(ifNoneMatch, emptyEtag)) {
          return await finish(notModified(emptyEtag), {
            ...requestFields,
            result: "empty_304",
          });
        }
        return await finish(json(emptyPayload, 200, { ETag: emptyEtag }), {
          ...requestFields,
          result: "empty_200",
          row_count: 0,
        });
      }
    }
    const result = await loadLatest({
      stationLike,
      networkCode,
      pollutant,
      windowLabel,
      limit,
      since,
      sinceId: effectiveSinceId,
    });
    const payload = {
      contract_version: 2,
      station_like: stationLike,
      network_code: networkCode,
      pollutant,
      window: windowLabel,
      since,
      since_id: effectiveSinceId,
      next_since: result.nextSince,
      next_since_id: result.nextSinceId,
      count: result.rows.length,
      data: result.rows,
    };
    const etag = await createWeakEtag({
      endpoint: "uk_aq_stations_chart",
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
      row_count: result.rows.length,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error("uk_aq_stations_chart runtime failure", { message });
    return await finish(json({ error: "Internal server error." }, 500), {
      ...requestFields,
      error_type: "runtime",
    });
  }
});

type LoadOptions = {
  stationLike: string | null;
  networkCode: string | null;
  pollutant: string | null;
  windowLabel: string;
  limit: number;
  since: string | null;
  sinceId: number | null;
};

async function loadLatest(
  { stationLike, networkCode, pollutant, windowLabel, limit, since, sinceId }:
    LoadOptions,
) {
  const pollutantKey = normalizePollutant(pollutant);
  const { data, error } = await callLatestRpc({
    stationLike,
    networkCode,
    pollutant: pollutantKey,
    windowLabel,
    limit,
    since,
    sinceId,
    useLimitOne: false,
  });
  if (error) {
    throw new Error(error.message);
  }
  const rows = data ?? [];
  const nextCursor = deriveNextCursor(rows, since, sinceId);

  const filtered = rows.filter(passesOutlierThreshold);

  return {
    nextSince: nextCursor.since,
    nextSinceId: nextCursor.sinceId,
    rows: filtered.map((row) => {
      const station = row.station ?? null;
      const stationLabel = resolveStationLabel(
        station?.label,
        station?.station_ref,
        row.label,
      );
      const pollutantLabel = resolvePhenomenonLabel(
        row.phenomenon?.observed_property_display_name,
        row.phenomenon?.pollutant_label,
        row.phenomenon?.label,
        row.phenomenon?.notation,
        row.phenomenon?.source_label ?? row.phenomenon?.eionet_uri,
      );
      const observedPropertyCode = normalizePollutant(
        row.phenomenon?.observed_property_code ??
          row.phenomenon?.notation ??
          row.phenomenon?.pollutant_label ??
          row.phenomenon?.label ??
          null,
      );
      const connector = row.connector ?? null;

      return {
        id: row.id ?? null,
        last_value: row.last_value ?? null,
        last_value_at: row.last_value_at ?? null,
        network_id: row.network_id ?? null,
        network_code: row.network_code ?? null,
        network_label: row.network_label ?? null,
        connector_id: row.connector_id ?? connector?.id ?? null,
        connector_code: row.connector_code ?? connector?.connector_code ?? null,
        connector_label: row.connector_label ?? connector?.display_name ??
          connector?.label ?? null,
        station_id: station?.id ?? null,
        station_ref: station?.station_ref ?? null,
        station_name: station?.station_name ?? null,
        station_label: stationLabel,
        display_name: formatDisplayName(
          connector?.station_display_name_template,
          station?.station_name,
          stationLabel,
          station?.station_ref,
          station?.id,
        ),
        pcon_code: station?.pcon_code ?? null,
        la_code: station?.la_code ?? null,
        phenomenon_label: pollutantLabel,
        pollutant_label: pollutantLabel,
        observed_property_code: observedPropertyCode,
        uom_display: formatUnit(row.uom),
      };
    }).sort((a, b) => {
      const aPollutant = a.phenomenon_label ?? a.pollutant_label ?? "";
      const bPollutant = b.phenomenon_label ?? b.pollutant_label ?? "";
      const pollutantCompare = aPollutant.localeCompare(bPollutant);
      if (pollutantCompare !== 0) {
        return pollutantCompare;
      }
      const aStation = a.station_label ?? "";
      const bStation = b.station_label ?? "";
      return aStation.localeCompare(bStation);
    }),
  };
}

type LatestRpcCallOptions = {
  stationLike: string | null;
  networkCode: string | null;
  pollutant: string | null;
  windowLabel: string;
  limit: number;
  since: string | null;
  sinceId: number | null;
  useLimitOne: boolean;
};

async function callLatestRpc(options: LatestRpcCallOptions) {
  const {
    stationLike,
    networkCode,
    pollutant,
    windowLabel,
    limit,
    since,
    sinceId,
    useLimitOne,
  } = options;
  const limitRows = useLimitOne ? 1 : limit;
  const cursorBody = {
    region: null,
    pcon_code: null,
    station_like: stationLike,
    network_code: networkCode,
    pollutant,
    window_label: windowLabel,
    limit_rows: limitRows,
    since_updated_at: since,
    since_updated_id: since ? (sinceId ?? 0) : null,
  };
  return await postgrestRequest<any[]>(
    "POST",
    "rpc/uk_aq_latest_rpc",
    undefined,
    UK_AQ_PUBLIC_SCHEMA,
    cursorBody,
  );
}

async function hasLatestDelta(
  { stationLike, networkCode, pollutant, windowLabel, since, sinceId }:
    LoadOptions,
): Promise<boolean> {
  if (!since) {
    return true;
  }
  const pollutantKey = normalizePollutant(pollutant);
  const { data, error } = await callLatestRpc({
    stationLike,
    networkCode,
    pollutant: pollutantKey,
    windowLabel,
    limit: 1,
    since,
    sinceId,
    useLimitOne: true,
  });
  if (error) {
    throw new Error(error.message);
  }
  return Array.isArray(data) && data.length > 0;
}

function normalizeText(value: string | null): string | null {
  if (!value) {
    return null;
  }
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function normalizePollutant(value: string | null): string | null {
  const normalized = normalizeText(value);
  if (!normalized) {
    return null;
  }
  const compact = normalized.toLowerCase().replace(/[\s_]/g, "");
  if (compact === "pm25" || compact === "pm2.5") {
    return "pm2.5";
  }
  if (compact === "pm10") {
    return "pm10";
  }
  if (compact === "no2") {
    return "no2";
  }
  if (compact === "o3") {
    return "o3";
  }
  return normalized.toLowerCase();
}

function normalizeWindow(value: string | null): string {
  const normalized = normalizeText(value)?.toLowerCase();
  if (!normalized) {
    return DEFAULT_WINDOW;
  }
  return ["3h", "6h", "1d", "7d", "all"].includes(normalized)
    ? normalized
    : DEFAULT_WINDOW;
}

function formatDisplayName(
  template: string | null | undefined,
  stationName: string | null | undefined,
  stationLabel: string | null | undefined,
  stationRef: string | number,
  stationId: string | number | null | undefined,
): string | null {
  const refText = normalizeNonEmptyText(
    stationRef !== null && stationRef !== undefined ? String(stationRef) : null,
  );
  const fallback = formatFallbackDisplayName(
    stationName,
    stationLabel,
    refText,
    stationId,
  );
  const effectiveTemplate = template?.trim();
  if (!effectiveTemplate) {
    return fallback;
  }
  const rendered = renderDisplayTemplate(effectiveTemplate, {
    station_name: stationName ?? "",
    station_label: stationLabel ?? "",
    station_ref: refText ?? "",
  });
  if (rendered) {
    return rendered;
  }
  return fallback;
}

function formatFallbackDisplayName(
  stationName: string | null | undefined,
  stationLabel: string | null | undefined,
  stationRef: string | null,
  stationId: string | number | null | undefined,
): string | null {
  const normalizedName = normalizeNonEmptyText(stationName);
  const normalizedLabel = normalizeNonEmptyText(stationLabel);
  const normalizedRef = normalizeNonEmptyText(stationRef);
  const normalizedId = normalizeNonEmptyText(
    stationId !== null && stationId !== undefined ? String(stationId) : null,
  );
  const base = normalizedName ?? normalizedLabel ?? null;
  if (!base) {
    if (normalizedRef) {
      return normalizedRef;
    }
    return normalizedId ? `Station ${normalizedId}` : null;
  }
  if (!normalizedName) {
    return base;
  }
  const normalizedBase = base.toLowerCase();
  if (normalizedRef && normalizedBase.includes(normalizedRef.toLowerCase())) {
    return base;
  }
  return normalizedRef ? `${base} - ${normalizedRef}` : base;
}

function renderDisplayTemplate(
  template: string,
  tokens: Record<string, string>,
): string | null {
  const rendered = template.replace(
    /\{(station_name|station_label|station_ref)\}/g,
    (_, key) => {
      return tokens[key] ?? "";
    },
  );
  const cleaned = rendered.replace(/\s+-\s+/g, " - ").replace(/\s+/g, " ")
    .trim();
  return cleaned ? cleaned : null;
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

function normalizeCursorId(value: unknown): number | null {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return null;
  }
  return Math.trunc(parsed);
}

function deriveNextCursor(
  rows: any[],
  fallbackSince: string | null,
  fallbackSinceId: number | null,
): { since: string | null; sinceId: number | null } {
  let bestSince = fallbackSince ? normalizeTimestamp(fallbackSince) : null;
  let bestId = bestSince ? (fallbackSinceId ?? 0) : null;
  let bestMs = bestSince ? Date.parse(bestSince) : Number.NEGATIVE_INFINITY;
  for (const row of rows) {
    const rowSince = normalizeTimestamp(
      row?.updated_at ?? row?.last_value_at ?? "",
    );
    if (!rowSince) {
      continue;
    }
    const rowMs = Date.parse(rowSince);
    const rowId = normalizeCursorId(row?.id) ?? 0;
    if (rowMs > bestMs) {
      bestMs = rowMs;
      bestSince = rowSince;
      bestId = rowId;
      continue;
    }
    if (rowMs === bestMs) {
      const currentId = bestId ?? 0;
      if (rowId > currentId) {
        bestId = rowId;
      }
    }
  }
  if (!bestSince) {
    return { since: null, sinceId: null };
  }
  return { since: bestSince, sinceId: bestId ?? 0 };
}

function _maxTimestamp(
  values: Array<string | null | undefined>,
  fallback: string | null,
): string | null {
  let best = fallback ? normalizeTimestamp(fallback) : null;
  let bestMs = best ? Date.parse(best) : Number.NEGATIVE_INFINITY;
  values.forEach((value) => {
    if (!value) {
      return;
    }
    const normalized = normalizeTimestamp(value);
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

function deriveStationLabel(label: string | null): string | null {
  if (!label) {
    return null;
  }
  const separator = label.includes(" - ") ? " - " : "-";
  const parts = label.split(separator).map((part) => part.trim()).filter(
    Boolean,
  );
  if (!parts.length) {
    return label;
  }
  if (
    parts.length > 1 &&
    (looksLikePollutantUri(parts[0]) || looksLikeUrl(parts[0]))
  ) {
    return parts[parts.length - 1];
  }
  if (parts.length === 1 && looksLikeUrl(parts[0])) {
    return null;
  }
  return parts[0];
}

function resolveStationLabel(
  stationLabel: string | null | undefined,
  stationRef: string | null | undefined,
  seriesLabel: string | null,
): string | null {
  const normalizedStationLabel = normalizeNonEmptyText(stationLabel);
  if (normalizedStationLabel) {
    return normalizedStationLabel;
  }
  const derived = normalizeNonEmptyText(deriveStationLabel(seriesLabel));
  if (derived) {
    return derived;
  }
  return normalizeNonEmptyText(stationRef);
}

function resolvePhenomenonLabel(
  observedPropertyDisplayName: string | null | undefined,
  pollutantLabel: string | null | undefined,
  label: string | null | undefined,
  notation: string | null | undefined,
  sourceLabel: string | null | undefined,
): string | null {
  if (observedPropertyDisplayName) {
    return observedPropertyDisplayName;
  }
  if (notation) {
    return notation;
  }
  if (pollutantLabel) {
    return pollutantLabel;
  }
  if (label) {
    return label;
  }
  if (sourceLabel) {
    return sourceLabel.split(/[:/]/).filter(Boolean).pop() ?? null;
  }
  return null;
}

function looksLikeUrl(value: string): boolean {
  return /^https?:\/\//i.test(value.trim());
}

function looksLikePollutantUri(value: string): boolean {
  return /dd\.eionet\.europa\.eu\/vocabulary\/aq\/pollutant\//i.test(value);
}

function formatUnit(unit: string | null): string | null {
  if (!unit) {
    return null;
  }
  const trimmed = unit.trim();
  if (!trimmed) {
    return null;
  }
  const normalized = trimmed.toLowerCase().replace(/µ/g, "u");
  if (normalized.includes("ug") && /m\s*[-^]?\s*3/.test(normalized)) {
    return "µg/m³";
  }
  return trimmed;
}

function normalizeNonEmptyText(
  value: string | null | undefined,
): string | null {
  if (value === null || value === undefined) {
    return null;
  }
  const trimmed = String(value).trim();
  return trimmed ? trimmed : null;
}

function passesOutlierThreshold(row: any): boolean {
  const rawValue = row?.last_value;
  const value = Number(rawValue);
  if (!Number.isFinite(value)) {
    return false;
  }
  const pollutant = normalizePollutant(
    row?.phenomenon?.observed_property_code ??
      row?.phenomenon?.notation ??
      row?.phenomenon?.pollutant_label ??
      row?.phenomenon?.label ??
      row?.phenomenon_label ??
      null,
  );
  if (!pollutant) {
    return true;
  }
  const thresholds: Record<string, { min: number; max: number }> = {
    "pm2.5": { min: 0, max: 500 },
    "pm25": { min: 0, max: 500 },
    "pm10": { min: 0, max: 600 },
  };
  const bounds = thresholds[pollutant];
  if (!bounds) {
    return true;
  }
  return value >= bounds.min && value <= bounds.max;
}
