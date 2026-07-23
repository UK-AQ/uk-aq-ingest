type DropboxConfig = {
  appKey: string;
  appSecret: string;
  refreshToken: string;
};

type UploadDropboxErrorLogParams = {
  appKey: string;
  appSecret: string;
  refreshToken: string;
  allowedSupabaseUrl: string;
  supabaseUrl: string;
  dropboxRoot: string;
  errorFolder: string;
  errorId: string;
  createdAtIso: string;
  connectorCode: string;
  payload: unknown;
};

const DROPBOX_TOKEN_URL = "https://api.dropbox.com/oauth2/token";
const DROPBOX_UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload";

export function normalizeDropboxPath(raw: string): string {
  const trimmed = String(raw || "").trim().replace(/\\/g, "/");
  if (!trimmed) {
    return "";
  }
  const withLeadingSlash = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  const collapsed = withLeadingSlash.replace(/\/{2,}/g, "/");
  return collapsed.length > 1 ? collapsed.replace(/\/+$/, "") : collapsed;
}

function dropboxWithRoot(dropboxRoot: string, path: string): string {
  const root = normalizeDropboxPath(dropboxRoot);
  const cleaned = normalizeDropboxPath(path);
  if (!root) {
    return cleaned;
  }
  if (!cleaned || cleaned === "/") {
    return root;
  }
  if (cleaned === root || cleaned.startsWith(`${root}/`)) {
    return cleaned;
  }
  return `${root}${cleaned}`;
}

function formatCompactTimestamp(timestamp: Date): string {
  return timestamp.toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
}

function normalizeConnectorPrefix(connectorCode: string): string {
  const normalized = String(connectorCode || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return normalized || "connector";
}

function loadDropboxConfig(params: UploadDropboxErrorLogParams): DropboxConfig | null {
  if (!params.appKey || !params.appSecret || !params.refreshToken) {
    return null;
  }
  const allowedSupabaseUrl = params.allowedSupabaseUrl.trim();
  const supabaseUrl = params.supabaseUrl.trim();
  if (!allowedSupabaseUrl || allowedSupabaseUrl !== supabaseUrl) {
    return null;
  }
  return {
    appKey: params.appKey,
    appSecret: params.appSecret,
    refreshToken: params.refreshToken,
  };
}

async function dropboxRefreshAccessToken(config: DropboxConfig): Promise<string> {
  const credentials = btoa(`${config.appKey}:${config.appSecret}`);
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: config.refreshToken,
  });
  const response = await fetch(DROPBOX_TOKEN_URL, {
    method: "POST",
    headers: {
      Authorization: `Basic ${credentials}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body,
  });
  if (!response.ok) {
    throw new Error(`Dropbox token request failed (${response.status})`);
  }
  const payload = await response.json();
  const accessToken = String(payload?.access_token || "").trim();
  if (!accessToken) {
    throw new Error("Dropbox token response missing access_token.");
  }
  return accessToken;
}

async function dropboxUploadFile(
  accessToken: string,
  path: string,
  contents: string,
): Promise<void> {
  const response = await fetch(DROPBOX_UPLOAD_URL, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/octet-stream",
      "Dropbox-API-Arg": JSON.stringify({
        path,
        mode: "overwrite",
        mute: true,
      }),
    },
    body: contents,
  });
  if (!response.ok) {
    const text = await response.text();
    const error = new Error(`Dropbox upload failed (${response.status}): ${text}`);
    (error as Error & { status?: number }).status = response.status;
    throw error;
  }
}

async function dropboxUploadFileWithRetry(
  accessToken: string,
  path: string,
  contents: string,
  refreshToken: () => Promise<string>,
): Promise<void> {
  try {
    await dropboxUploadFile(accessToken, path, contents);
  } catch (error) {
    const status = Number((error as { status?: unknown })?.status);
    if (status === 401) {
      const refreshedToken = await refreshToken();
      await dropboxUploadFile(refreshedToken, path, contents);
      return;
    }
    throw error;
  }
}

function buildDropboxErrorPath(params: UploadDropboxErrorLogParams): string {
  const createdAt = new Date(params.createdAtIso);
  const timestamp = Number.isNaN(createdAt.getTime()) ? new Date() : createdAt;
  const stamp = formatCompactTimestamp(timestamp);
  const dateFolder = timestamp.toISOString().slice(0, 10);
  const prefix = normalizeConnectorPrefix(params.connectorCode);
  const errorFolder = dropboxWithRoot(
    params.dropboxRoot,
    params.errorFolder || "/error_log",
  );
  return `${errorFolder}/${dateFolder}/uk_aq_error_cloud_run_${prefix}_${stamp}_${params.errorId}.json`;
}

export async function uploadErrorLogJsonToDropbox(
  params: UploadDropboxErrorLogParams,
): Promise<string | null> {
  const config = loadDropboxConfig(params);
  if (!config) {
    return null;
  }
  const dropboxPath = buildDropboxErrorPath(params);
  let accessToken = await dropboxRefreshAccessToken(config);
  const refresh = () => dropboxRefreshAccessToken(config);
  const payloadText = `${JSON.stringify(params.payload, null, 2)}\n`;
  await dropboxUploadFileWithRetry(
    accessToken,
    dropboxPath,
    payloadText,
    async () => {
      accessToken = await refresh();
      return accessToken;
    },
  );
  return dropboxPath;
}
