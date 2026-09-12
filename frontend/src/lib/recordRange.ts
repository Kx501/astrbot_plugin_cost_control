import type { RecordsFilter } from "./types";

/** Convert the dates shown by the browser to explicit instants for the API. */
export function rangeParams(
  filter: Pick<RecordsFilter, "preset" | "start" | "end">,
  now = new Date(),
): { start: string; end: string } {
  const localDate = (raw: string): Date => {
    const [year, month, day] = raw.split("-").map(Number);
    return new Date(year, month - 1, day);
  };
  const endOfDay = (date: Date): string => {
    date.setDate(date.getDate() + 1);
    // The API accepts inclusive upper bounds, so exclude the next midnight.
    date.setMilliseconds(date.getMilliseconds() - 1);
    return date.toISOString().replace(".999Z", ".999999Z");
  };
  if (filter.preset === "custom") {
    return {
      start: filter.start ? localDate(filter.start).toISOString() : "",
      end: filter.end ? endOfDay(localDate(filter.end)) : "",
    };
  }
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const end = endOfDay(new Date(start));
  const days = filter.preset === "30d" ? 30 : filter.preset === "7d" ? 7 : 1;
  start.setDate(start.getDate() - days + 1);
  return { start: start.toISOString(), end };
}
