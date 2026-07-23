const DAY_MS = 24 * 60 * 60 * 1000;

type ErgDateRangeInput = {
  now: Date;
  startDateOverride?: Date | null;
  endDateOverride?: Date | null;
  days: number;
};

export type ErgDateRange = {
  startDate: Date;
  endDate: Date;
  startDateStr: string;
  endDateStr: string;
  utcTodayStart: Date;
};

export function formatUtcDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}

export function utcDayStart(date: Date): Date {
  return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
}

export function addUtcDays(date: Date, days: number): Date {
  return new Date(date.getTime() + days * DAY_MS);
}

export function buildErgDateRange({
  now,
  startDateOverride,
  endDateOverride,
  days,
}: ErgDateRangeInput): ErgDateRange {
  const todayUtcStart = utcDayStart(now);
  const defaultEndDate = addUtcDays(todayUtcStart, 1);
  let endDate = endDateOverride ? new Date(endDateOverride.getTime()) : defaultEndDate;
  let startDate = startDateOverride
    ? new Date(startDateOverride.getTime())
    : new Date(endDate.getTime() - Math.max(days, 1) * DAY_MS);
  if (startDate > endDate) {
    const tmp = startDate;
    startDate = endDate;
    endDate = tmp;
  }
  return {
    startDate,
    endDate,
    startDateStr: formatUtcDate(startDate),
    endDateStr: formatUtcDate(endDate),
    utcTodayStart: todayUtcStart,
  };
}
