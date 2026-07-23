const LEGACY_CONNECTOR_FILTERS = [
  "connector",
  "connector_id",
  "connector_code",
] as const;

export type PublicNetworkFilterResult =
  | { ok: true; networkCode: string | null }
  | { ok: false; error: string };

export function parsePublicNetworkFilter(url: URL): PublicNetworkFilterResult {
  const legacyFilter = LEGACY_CONNECTOR_FILTERS.find((name) =>
    url.searchParams.has(name)
  );
  if (legacyFilter) {
    return {
      ok: false,
      error:
        `Unsupported public filter '${legacyFilter}'. Use network_code instead.`,
    };
  }

  const rawNetworkCode = url.searchParams.get("network_code");
  if (rawNetworkCode === null) {
    return { ok: true, networkCode: null };
  }

  const networkCode = rawNetworkCode.trim().toLowerCase();
  if (!networkCode) {
    return { ok: false, error: "network_code must not be empty." };
  }

  return { ok: true, networkCode };
}
