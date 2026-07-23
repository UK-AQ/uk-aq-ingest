import { buildErgDateRange } from "./uk_aq_erg_laqn_date_range.ts";

Deno.test("buildErgDateRange uses tomorrow UTC for default end date", () => {
  const now = new Date("2026-01-26T08:30:00Z");
  const result = buildErgDateRange({ now, days: 1 });
  if (result.endDateStr !== "2026-01-27") {
    throw new Error(`Expected end date 2026-01-27, got ${result.endDateStr}`);
  }
  if (result.startDateStr !== "2026-01-26") {
    throw new Error(`Expected start date 2026-01-26, got ${result.startDateStr}`);
  }
});

Deno.test("buildErgDateRange uses UTC day boundaries", () => {
  const now = new Date("2026-01-26T00:30:00-08:00");
  const result = buildErgDateRange({ now, days: 1 });
  if (result.endDateStr !== "2026-01-27") {
    throw new Error(`Expected end date 2026-01-27, got ${result.endDateStr}`);
  }
  if (result.utcTodayStart.toISOString() !== "2026-01-26T00:00:00.000Z") {
    throw new Error(
      `Expected UTC day start 2026-01-26T00:00:00.000Z, got ${result.utcTodayStart.toISOString()}`,
    );
  }
});
