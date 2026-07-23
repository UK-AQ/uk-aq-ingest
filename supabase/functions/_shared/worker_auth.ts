const UK_AQ_EDGE_UPSTREAM_SECRET = Deno.env.get("UK_AQ_EDGE_UPSTREAM_SECRET") ?? "";
const UPSTREAM_AUTH_HEADER = "x-uk-aq-upstream-auth";

type UpstreamAuthValidation =
  | { ok: true }
  | { ok: false; status: 401 | 500; error: string };

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) {
    return false;
  }
  let mismatch = 0;
  for (let i = 0; i < a.length; i += 1) {
    mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return mismatch === 0;
}

export function validateWorkerUpstreamAuth(req: Request): UpstreamAuthValidation {
  if (!UK_AQ_EDGE_UPSTREAM_SECRET) {
    return { ok: false, status: 500, error: "Missing UK_AQ_EDGE_UPSTREAM_SECRET." };
  }
  const supplied = req.headers.get(UPSTREAM_AUTH_HEADER);
  if (!supplied || !timingSafeEqual(supplied, UK_AQ_EDGE_UPSTREAM_SECRET)) {
    return { ok: false, status: 401, error: "Unauthorized." };
  }
  return { ok: true };
}
