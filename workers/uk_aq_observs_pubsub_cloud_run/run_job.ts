import "../../supabase/functions/_shared/fetch_egress_patch.ts";

import {
  buildObservsSyncReceipts,
  observsUpsertObservations,
  prepareObservsRows,
  type ObservsObservationRow,
  type MainRpcCaller,
  upsertObservsSyncReceipts,
} from "../../supabase/functions/_shared/observs_client.ts";

type RpcError = { message: string };

type RpcResult<T> = {
  data: T | null;
  error: RpcError | null;
};

type PubsubPullMessage = {
  ackId?: unknown;
  message?: {
    data?: unknown;
  };
};

type PubsubPullResponse = {
  receivedMessages?: unknown;
};

type WriterSummary = {
  batches: number;
  max_batches: number;
  pulled_messages: number;
  decoded_rows: number;
  deduped_rows: number;
  delivered: number;
  receipts_upserted: number;
  acked_messages: number;
  malformed_messages: number;
  warnings: string[];
  errors: string[];
  stop_reason?: string;
  stopped_early?: boolean;
};

const SUPABASE_URL = requiredEnv("SUPABASE_URL");
const SUPABASE_PRIVILEGED_KEY = requiredEnvAny(["SB_SECRET_KEY"]);
requiredEnv("OBS_AQIDB_SUPABASE_URL");
requiredEnv("OBS_AQIDB_SECRET_KEY");

const MAIN_RPC_SCHEMA = (Deno.env.get("UK_AQ_PUBLIC_SCHEMA") || "uk_aq_public")
  .trim();

const PUBSUB_PROJECT_ID = (
  Deno.env.get("GCP_PROJECT_ID") ||
  Deno.env.get("GOOGLE_CLOUD_PROJECT") ||
  ""
).trim();

const PUBSUB_SUBSCRIPTION = (
  Deno.env.get("OBSERVS_PUBSUB_SUBSCRIPTION") ||
  "uk-aq-observs-observations-sub"
).trim();

const PUBSUB_PULL_MAX_MESSAGES = parsePositiveInt(
  Deno.env.get("OBSERVS_PUBSUB_PULL_MAX_MESSAGES"),
  1000,
);

const WRITER_MAX_BATCHES = parsePositiveInt(
  Deno.env.get("OBSERVS_PUBSUB_WRITER_MAX_BATCHES"),
  24,
);

const WRITER_BUDGET_SECONDS = parsePositiveInt(
  Deno.env.get("OBSERVS_PUBSUB_WRITER_BUDGET_SECONDS"),
  1200,
);

const WRITER_SHUTDOWN_BUFFER_SECONDS = parsePositiveInt(
  Deno.env.get("OBSERVS_PUBSUB_WRITER_SHUTDOWN_BUFFER_SECONDS"),
  20,
);

const WRITER_RPC_RETRIES = parsePositiveInt(
  Deno.env.get("OBSERVS_PUBSUB_WRITER_RPC_RETRIES"),
  3,
);

const WRITER_PUBSUB_RETRIES = parsePositiveInt(
  Deno.env.get("OBSERVS_PUBSUB_WRITER_PUBSUB_RETRIES"),
  3,
);

const MIN_BATCH_BUDGET_MS = 4000;
const REST_BASE_URL = `${SUPABASE_URL.replace(/\/$/, "")}/rest/v1`;

function requiredEnv(name: string): string {
  const value = (Deno.env.get(name) || "").trim();
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

function requiredEnvAny(names: string[]): string {
  for (const name of names) {
    const value = (Deno.env.get(name) || "").trim();
    if (value) {
      return value;
    }
  }
  throw new Error(
    `Missing required environment variable: one of ${names.join(", ")}`,
  );
}

function parsePositiveInt(raw: string | undefined, fallback: number): number {
  const value = Number(raw || "");
  if (!Number.isFinite(value) || value <= 0) {
    return fallback;
  }
  return Math.trunc(value);
}

function isRetryableStatus(status: number): boolean {
  return status === 429 || status === 500 || status === 502 || status === 503 ||
    status === 504;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function subscriptionPath(): string {
  if (PUBSUB_SUBSCRIPTION.startsWith("projects/")) {
    return PUBSUB_SUBSCRIPTION;
  }
  if (!PUBSUB_PROJECT_ID) {
    throw new Error(
      "Missing GCP_PROJECT_ID (or GOOGLE_CLOUD_PROJECT)",
    );
  }
  return `projects/${PUBSUB_PROJECT_ID}/subscriptions/${PUBSUB_SUBSCRIPTION}`;
}

function shortError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.length > 400 ? `${message.slice(0, 397)}...` : message;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function buildEmptySummary(): WriterSummary {
  return {
    batches: 0,
    max_batches: WRITER_MAX_BATCHES,
    pulled_messages: 0,
    decoded_rows: 0,
    deduped_rows: 0,
    delivered: 0,
    receipts_upserted: 0,
    acked_messages: 0,
    malformed_messages: 0,
    warnings: [],
    errors: [],
  };
}

function getRemainingBudgetMs(startedAtMs: number): number {
  return Math.max(
    0,
    WRITER_BUDGET_SECONDS * 1000 -
      (Date.now() - startedAtMs) -
      WRITER_SHUTDOWN_BUFFER_SECONDS * 1000,
  );
}

async function fetchGoogleAccessToken(): Promise<string> {
  const response = await fetch(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
    {
      headers: { "Metadata-Flavor": "Google" },
    },
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(
      `Metadata token request failed (${response.status}): ${text}`,
    );
  }
  const payload = await response.json().catch(() => null);
  const token = typeof payload?.access_token === "string"
    ? payload.access_token.trim()
    : "";
  if (!token) {
    throw new Error("Metadata token response missing access_token");
  }
  return token;
}

async function mainRpc<T>(
  fn: string,
  args?: Record<string, unknown>,
): Promise<RpcResult<T>> {
  const url = `${REST_BASE_URL}/rpc/${fn}`;
  const headers: Record<string, string> = {
    apikey: SUPABASE_PRIVILEGED_KEY,
    "Content-Type": "application/json",
    Accept: "application/json",
    "Accept-Profile": MAIN_RPC_SCHEMA,
    "Content-Profile": MAIN_RPC_SCHEMA,
    "x-ukaq-egress-caller": "uk_aq_observs_pubsub_cloud_run",
  };

  for (let attempt = 1; attempt <= WRITER_RPC_RETRIES; attempt += 1) {
    try {
      const resp = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify(args ?? {}),
      });
      const contentType = (resp.headers.get("content-type") || "").toLowerCase();
      const payload = contentType.includes("application/json")
        ? await resp.json().catch(() => null)
        : await resp.text().catch(() => null);

      if (resp.ok) {
        return { data: payload as T, error: null };
      }

      const message = payload?.message || payload?.error_description ||
        payload?.error || resp.statusText || `HTTP ${resp.status}`;
      if (attempt < WRITER_RPC_RETRIES && isRetryableStatus(resp.status)) {
        await sleep(Math.min(5000, 1000 * attempt));
        continue;
      }
      return { data: null, error: { message: String(message) } };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (attempt < WRITER_RPC_RETRIES) {
        await sleep(Math.min(5000, 1000 * attempt));
        continue;
      }
      return { data: null, error: { message } };
    }
  }

  return { data: null, error: { message: "unknown_main_rpc_error" } };
}

async function pubsubPost(
  path: string,
  body: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const token = await fetchGoogleAccessToken();
  let lastError = "";

  for (let attempt = 1; attempt <= WRITER_PUBSUB_RETRIES; attempt += 1) {
    try {
      const response = await fetch(`https://pubsub.googleapis.com/v1/${path}`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      });

      const payload = await response.json().catch(() => ({}));
      if (response.ok) {
        return asRecord(payload) || {};
      }

      const message = asRecord(payload)?.error
        ? JSON.stringify(asRecord(payload)?.error)
        : `HTTP ${response.status}`;
      lastError = message;
      if (attempt < WRITER_PUBSUB_RETRIES && isRetryableStatus(response.status)) {
        await sleep(Math.min(5000, 1000 * attempt));
        continue;
      }
      throw new Error(message);
    } catch (error) {
      lastError = shortError(error);
      if (attempt < WRITER_PUBSUB_RETRIES) {
        await sleep(Math.min(5000, 1000 * attempt));
        continue;
      }
      throw new Error(`Pub/Sub request failed: ${lastError}`);
    }
  }

  throw new Error(`Pub/Sub request failed: ${lastError || "unknown"}`);
}

async function pullPubsubMessages(maxMessages: number): Promise<PubsubPullMessage[]> {
  const payload = await pubsubPost(`${subscriptionPath()}:pull`, {
    maxMessages,
    returnImmediately: true,
  }) as PubsubPullResponse;

  if (!Array.isArray(payload?.receivedMessages)) {
    return [];
  }
  return payload.receivedMessages as PubsubPullMessage[];
}

async function ackPubsubMessages(ackIds: string[]): Promise<void> {
  if (!ackIds.length) {
    return;
  }
  await pubsubPost(`${subscriptionPath()}:acknowledge`, {
    ackIds,
  });
}

function decodeMessageRow(message: PubsubPullMessage): {
  ackId: string | null;
  row: ObservsObservationRow | null;
} {
  const ackId = typeof message.ackId === "string" && message.ackId.trim()
    ? message.ackId
    : null;
  const data = message.message?.data;
  if (!ackId || typeof data !== "string" || !data.trim()) {
    return { ackId, row: null };
  }

  try {
    const decoded = atob(data);
    const parsed = JSON.parse(decoded);
    const record = asRecord(parsed);
    if (!record) {
      return { ackId, row: null };
    }
    const valueRaw = record.value;
    const statusRaw = record.status;

    const row: ObservsObservationRow = {
      connector_id: Number(record.connector_id),
      timeseries_id: Number(record.timeseries_id),
      observed_at: String(record.observed_at || ""),
      value: valueRaw === null || valueRaw === undefined
        ? null
        : Number(valueRaw),
      value_float8_hex: record.value_float8_hex === null ||
          record.value_float8_hex === undefined
        ? null
        : String(record.value_float8_hex),
      status: statusRaw === null || statusRaw === undefined
        ? null
        : String(statusRaw),
    };

    return { ackId, row };
  } catch {
    return { ackId, row: null };
  }
}

function summarizeForError(summary: WriterSummary): string {
  const payload = {
    batches: summary.batches,
    pulled_messages: summary.pulled_messages,
    decoded_rows: summary.decoded_rows,
    deduped_rows: summary.deduped_rows,
    delivered: summary.delivered,
    receipts_upserted: summary.receipts_upserted,
    acked_messages: summary.acked_messages,
    malformed_messages: summary.malformed_messages,
    stop_reason: summary.stop_reason || null,
    warnings: summary.warnings.slice(0, 3),
  };
  return JSON.stringify(payload);
}

async function flushPubsubInBudget(): Promise<WriterSummary> {
  const summary = buildEmptySummary();
  const startedAtMs = Date.now();

  for (let idx = 0; idx < WRITER_MAX_BATCHES; idx += 1) {
    if (getRemainingBudgetMs(startedAtMs) < MIN_BATCH_BUDGET_MS) {
      summary.stopped_early = true;
      summary.stop_reason = "runtime_budget";
      break;
    }

    const pulled = await pullPubsubMessages(PUBSUB_PULL_MAX_MESSAGES);
    if (!pulled.length) {
      summary.stop_reason = "subscription_empty";
      break;
    }

    summary.batches += 1;
    summary.pulled_messages += pulled.length;

    const malformedAckIds: string[] = [];
    const validAckIds: string[] = [];
    const rows: ObservsObservationRow[] = [];

    for (const message of pulled) {
      const decoded = decodeMessageRow(message);
      if (!decoded.ackId) {
        summary.malformed_messages += 1;
        continue;
      }
      if (!decoded.row) {
        malformedAckIds.push(decoded.ackId);
        summary.malformed_messages += 1;
        continue;
      }
      validAckIds.push(decoded.ackId);
      rows.push(decoded.row);
    }

    if (malformedAckIds.length) {
      try {
        await ackPubsubMessages(malformedAckIds);
        summary.acked_messages += malformedAckIds.length;
      } catch (error) {
        summary.warnings.push(
          `Malformed-message acknowledge failed: ${shortError(error)}`,
        );
      }
    }

    summary.decoded_rows += rows.length;
    const preparedRows = prepareObservsRows(rows);
    summary.deduped_rows += preparedRows.length;

    if (!preparedRows.length) {
      if (validAckIds.length) {
        try {
          await ackPubsubMessages(validAckIds);
          summary.acked_messages += validAckIds.length;
        } catch (error) {
          summary.errors.push(
            `Acknowledge failed for empty-dedup batch ${idx + 1}: ${shortError(error)}`,
          );
          summary.stopped_early = true;
          summary.stop_reason = "ack_failed";
          break;
        }
      }
      continue;
    }

    try {
      const delivered = await observsUpsertObservations(preparedRows);
      summary.delivered += delivered;
      const receipts = buildObservsSyncReceipts(preparedRows);
      summary.receipts_upserted += await upsertObservsSyncReceipts(
        mainRpc as MainRpcCaller,
        receipts,
      );
    } catch (error) {
      summary.errors.push(
        `Observs upsert failed for batch ${idx + 1}: ${shortError(error)}`,
      );
      summary.stopped_early = true;
      summary.stop_reason = "observs_upsert_failed";
      break;
    }

    try {
      await ackPubsubMessages(validAckIds);
      summary.acked_messages += validAckIds.length;
    } catch (error) {
      summary.errors.push(
        `Acknowledge failed for batch ${idx + 1}: ${shortError(error)}`,
      );
      summary.stopped_early = true;
      summary.stop_reason = "ack_failed";
      break;
    }
  }

  return summary;
}

async function main(): Promise<void> {
  const now = new Date().toISOString();
  console.log("observs_pubsub_cloud_run_start", {
    checked_at: now,
    max_batches: WRITER_MAX_BATCHES,
    pull_max_messages: PUBSUB_PULL_MAX_MESSAGES,
    budget_seconds: WRITER_BUDGET_SECONDS,
    shutdown_buffer_seconds: WRITER_SHUTDOWN_BUFFER_SECONDS,
    subscription: subscriptionPath(),
  });

  const summary = await flushPubsubInBudget();
  console.log("observs_pubsub_cloud_run_summary", {
    checked_at: now,
    observs_pubsub: summary,
  });

  if (summary.errors.length > 0) {
    throw new Error(`observs_pubsub_flush_error ${summarizeForError(summary)}`);
  }
}

if (import.meta.main) {
  await main();
}
