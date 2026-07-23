const textEncoder = new TextEncoder();

export async function createWeakEtag(value: unknown): Promise<string> {
  const canonicalJson = JSON.stringify(canonicalize(value)) ?? "null";
  const digest = await crypto.subtle.digest("SHA-256", textEncoder.encode(canonicalJson));
  const hash = Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  return `W/"${hash}"`;
}

export function ifNoneMatchMatches(ifNoneMatch: string | null, etag: string): boolean {
  if (!ifNoneMatch) {
    return false;
  }
  const trimmed = ifNoneMatch.trim();
  if (!trimmed) {
    return false;
  }
  if (trimmed === "*") {
    return true;
  }
  const target = normalizeEtagToken(etag);
  if (!target) {
    return false;
  }
  return trimmed
    .split(",")
    .map((token) => normalizeEtagToken(token))
    .some((token) => token !== null && token === target);
}

function normalizeEtagToken(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  const withoutWeak = /^W\//i.test(trimmed) ? trimmed.slice(2).trim() : trimmed;
  if (withoutWeak.length >= 2 && withoutWeak.startsWith("\"") && withoutWeak.endsWith("\"")) {
    return withoutWeak.slice(1, -1);
  }
  return withoutWeak;
}

function canonicalize(value: unknown): unknown {
  if (value === null) {
    return null;
  }
  if (Array.isArray(value)) {
    return value.map((entry) => canonicalize(entry));
  }
  if (value instanceof Date) {
    return value.toISOString();
  }
  const valueType = typeof value;
  if (valueType === "number") {
    return Number.isFinite(value as number) ? value : null;
  }
  if (valueType === "bigint") {
    return (value as bigint).toString();
  }
  if (valueType === "object") {
    const objectValue = value as Record<string, unknown>;
    const normalized: Record<string, unknown> = {};
    Object.keys(objectValue).sort().forEach((key) => {
      const normalizedValue = canonicalize(objectValue[key]);
      if (normalizedValue !== undefined) {
        normalized[key] = normalizedValue;
      }
    });
    return normalized;
  }
  if (valueType === "undefined") {
    return undefined;
  }
  return value;
}
