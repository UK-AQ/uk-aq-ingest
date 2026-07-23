export const CACHE_CONTROL_SUCCESS = "public, max-age=60, s-maxage=180, stale-while-revalidate=300, stale-if-error=86400";
export const CACHE_CONTROL_SUCCESS_SMAXAGE_300 = "public, max-age=60, s-maxage=300, stale-while-revalidate=300, stale-if-error=86400";
export const CACHE_CONTROL_ERROR = "no-store";

export function cacheControlHeaders(status: number, successValue = CACHE_CONTROL_SUCCESS): Record<string, string> {
  return {
    "Cache-Control": status >= 400 ? CACHE_CONTROL_ERROR : successValue,
  };
}
