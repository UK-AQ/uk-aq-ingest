import { parsePublicNetworkFilter } from "./public_network_filter.ts";

function assert(condition: boolean, message: string): void {
  if (!condition) {
    throw new Error(message);
  }
}

Deno.test("normalizes a public network code", () => {
  const result = parsePublicNetworkFilter(
    new URL("https://example.test/?network_code=BreatheLondon"),
  );
  assert(result.ok, "expected a valid network filter");
  if (result.ok) {
    assert(
      result.networkCode === "breathelondon",
      "expected a lowercase network code",
    );
  }
});

for (const legacyName of ["connector", "connector_id", "connector_code"]) {
  Deno.test(`rejects legacy ${legacyName} filter`, () => {
    const result = parsePublicNetworkFilter(
      new URL(`https://example.test/?${legacyName}=legacy`),
    );
    assert(!result.ok, `expected ${legacyName} to be rejected`);
  });
}

Deno.test("rejects an empty network code", () => {
  const result = parsePublicNetworkFilter(
    new URL("https://example.test/?network_code=%20"),
  );
  assert(!result.ok, "expected an empty network code to be rejected");
});
