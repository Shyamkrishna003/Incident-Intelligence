import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { MAX_SERIES, pivotSeries, seriesKey, type ChartRow } from "../lib/chart";
import {
  formatNumber,
  formatTime,
  formatTimeWithSeconds,
  formatValue,
  seriesLabel,
} from "../lib/format";
import type { MetricRange } from "../lib/types";
import { EmptyState } from "./ui";

const TABLE_ROW_LIMIT = 200;
const seriesColor = (index: number): string => `var(--series-${index + 1})`;

/** A short stroke in the series color: the key that ties a label to its line. */
function LineKey({ index }: { index: number }) {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-0.5 w-4 shrink-0 rounded-full"
      style={{ backgroundColor: seriesColor(index) }}
    />
  );
}

interface TooltipProps {
  active?: boolean;
  label?: unknown;
  payload?: readonly { dataKey?: unknown; value?: unknown }[];
}

export function MetricChart({ data, stale }: { data: MetricRange; stale: boolean }) {
  const shown = data.series.slice(0, MAX_SERIES);
  const hidden = data.series.length - shown.length;
  const labels = shown.map((series) => seriesLabel(series.attributes));
  const unit = shown[0]?.unit ?? null;
  const rows = pivotSeries(shown);
  const startMs = Date.parse(data.start);
  const endMs = Date.parse(data.end);
  // Evenly spaced across the whole requested range, not only where data exists.
  const ticks = [0, 1, 2, 3, 4].map((step) => startMs + ((endMs - startMs) * step) / 4);

  if (rows.length === 0) {
    return (
      <EmptyState title="No data in this time range">
        <p>Choose a longer range, or check that the service is still sending this metric.</p>
      </EmptyState>
    );
  }

  function ChartTooltip({ active, label, payload }: TooltipProps) {
    if (active !== true || typeof label !== "number" || !payload) return null;
    return (
      <div className="rounded-md border border-hairline bg-surface px-3 py-2 text-sm shadow-md">
        <div className="mb-1 text-xs text-ink-2">{formatTimeWithSeconds(label)}</div>
        <ul className="flex flex-col gap-1">
          {payload.map((entry) => {
            const index = Number(String(entry.dataKey).slice(1));
            if (typeof entry.value !== "number") return null;
            return (
              <li key={index} className="flex items-center gap-2">
                <LineKey index={index} />
                <span className="font-semibold text-ink">{formatValue(entry.value, unit)}</span>
                {shown.length > 1 && <span className="text-ink-2">{labels[index]}</span>}
              </li>
            );
          })}
        </ul>
      </div>
    );
  }

  return (
    <figure className={stale ? "opacity-60 transition-opacity" : "transition-opacity"}>
      {shown.length > 1 && (
        <ul aria-label="Series" className="mb-3 flex flex-wrap gap-x-4 gap-y-1">
          {labels.map((label, index) => (
            <li key={label} className="flex items-center gap-2 text-sm text-ink-2">
              <LineKey index={index} />
              {label}
            </li>
          ))}
        </ul>
      )}

      <div
        role="img"
        aria-label={`Line chart of ${data.metric} for ${data.service}. The same values are in the table below.`}
      >
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke="var(--grid)" />
            <XAxis
              dataKey="t"
              type="number"
              scale="time"
              domain={[startMs, endMs]}
              ticks={ticks}
              tickFormatter={formatTime}
              stroke="var(--axis)"
              tickLine={false}
              tick={{ fill: "var(--muted)", fontSize: 12 }}
              minTickGap={48}
            />
            <YAxis
              width={64}
              tickFormatter={formatNumber}
              axisLine={false}
              tickLine={false}
              tick={{ fill: "var(--muted)", fontSize: 12 }}
            />
            <Tooltip
              content={ChartTooltip}
              cursor={{ stroke: "var(--axis)", strokeWidth: 1 }}
              isAnimationActive={false}
            />
            {shown.map((series, index) => (
              <Line
                key={labels[index]}
                dataKey={seriesKey(index)}
                type="linear"
                stroke={seriesColor(index)}
                strokeWidth={2}
                strokeLinecap="round"
                strokeLinejoin="round"
                // A lone point has no line to draw, so show it as a dot.
                dot={series.points.length === 1 ? { r: 4, fill: seriesColor(index) } : false}
                activeDot={{ r: 4, stroke: "var(--surface)", strokeWidth: 2 }}
                connectNulls
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>

      <figcaption className="mt-2 flex flex-col gap-1 text-sm text-ink-2">
        {unit && <span>Values in {unit}.</span>}
        {hidden > 0 && (
          <span>
            Showing the first {MAX_SERIES} of {data.series.length} series.
          </span>
        )}
        {data.truncated && (
          <span>
            This range has more points than can be shown. Choose a shorter range to see all of
            them.
          </span>
        )}
      </figcaption>

      <details className="mt-3">
        <summary className="cursor-pointer text-sm text-link">View as table</summary>
        <DataTable rows={rows} labels={labels} unit={unit} />
      </details>
    </figure>
  );
}

function DataTable({
  rows,
  labels,
  unit,
}: {
  rows: ChartRow[];
  labels: string[];
  unit: string | null;
}) {
  const latest = rows.slice(-TABLE_ROW_LIMIT).reverse();
  return (
    <div className="mt-2 max-h-80 overflow-auto rounded-md border border-hairline">
      <table className="w-full text-left text-sm tabular-nums">
        <caption className="sr-only">Metric values by time, newest first</caption>
        <thead className="sticky top-0 bg-surface text-ink-2">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">
              Time
            </th>
            {labels.map((label) => (
              <th key={label} scope="col" className="px-3 py-2 font-medium">
                {labels.length > 1 ? label : `Value${unit ? ` (${unit})` : ""}`}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-hairline">
          {latest.map((row) => (
            <tr key={row.t}>
              <th scope="row" className="px-3 py-1.5 font-normal text-ink-2">
                {formatTimeWithSeconds(row.t)}
              </th>
              {labels.map((label, index) => {
                const value = row[seriesKey(index)];
                return (
                  <td key={label} className="px-3 py-1.5 text-ink">
                    {value === undefined ? "—" : formatNumber(value)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > TABLE_ROW_LIMIT && (
        <p className="px-3 py-2 text-sm text-ink-2">
          Showing the newest {TABLE_ROW_LIMIT} of {rows.length} rows.
        </p>
      )}
    </div>
  );
}
