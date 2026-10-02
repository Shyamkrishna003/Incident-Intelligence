import type { MetricSeries } from "./types";

/** The categorical palette has eight validated slots; more lines than that are unreadable. */
export const MAX_SERIES = 8;

export const seriesKey = (index: number): string => `s${index}`;

/** One row per timestamp, with a column per series: the shape a line chart needs. */
export type ChartRow = { t: number } & Record<string, number>;

export function pivotSeries(series: MetricSeries[]): ChartRow[] {
  const rows = new Map<number, ChartRow>();
  series.forEach((item, index) => {
    for (const point of item.points) {
      const t = Date.parse(point.timestamp);
      const row = rows.get(t) ?? ({ t });
      row[seriesKey(index)] = point.value;
      rows.set(t, row);
    }
  });
  return [...rows.values()].sort((a, b) => a.t - b.t);
}
