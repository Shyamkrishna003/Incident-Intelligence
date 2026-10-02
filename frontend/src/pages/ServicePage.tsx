import { Link, useParams, useSearchParams } from "react-router-dom";

import type { ProjectContext } from "../components/AppShell";
import { LogsPanel } from "../components/LogsPanel";
import { MetricChart } from "../components/MetricChart";
import { Card, EmptyState, ErrorState, LoadingState, PageHeader } from "../components/ui";
import { useDeployments, useMetricRange, useMetrics } from "../hooks/queries";

const RANGES = [
  { minutes: 15, label: "15 min" },
  { minutes: 60, label: "1 hour" },
  { minutes: 360, label: "6 hours" },
  { minutes: 1440, label: "24 hours" },
] as const;
const DEFAULT_RANGE = 60;

export function ServicePage({ current }: { current: ProjectContext }) {
  const { service = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const metrics = useMetrics(current.project.id, service);

  const requestedRange = Number(params.get("range"));
  const rangeMinutes = RANGES.some((range) => range.minutes === requestedRange)
    ? requestedRange
    : DEFAULT_RANGE;
  const requestedMetric = params.get("metric");
  const metric =
    metrics.data?.find((item) => item.name === requestedMetric)?.name ??
    metrics.data?.[0]?.name ??
    null;

  const range = useMetricRange(current.project.id, service, metric, rangeMinutes);
  // Marked on the chart. If this fails the chart still renders, just without markers.
  const deployments = useDeployments(current.project.id, rangeMinutes, service);

  function update(next: { metric?: string; range?: number }) {
    const updated = new URLSearchParams(params);
    if (next.metric !== undefined) updated.set("metric", next.metric);
    if (next.range !== undefined) updated.set("range", String(next.range));
    setParams(updated, { replace: true });
  }

  return (
    <>
      <p className="mb-2 text-sm">
        <Link className="text-link underline" to={`/p/${current.project.id}/services`}>
          ← All services
        </Link>
      </p>
      <PageHeader title={service} />

      {metrics.isPending ? (
        <LoadingState label="Loading metrics…" />
      ) : metrics.isError ? (
        <ErrorState
          title="Could not load this service"
          error={metrics.error}
          onRetry={() => void metrics.refetch()}
        />
      ) : (
        <>
          {/* Filters sit in one row above everything they scope. */}
          <div className="mb-4 flex flex-wrap items-end gap-4">
            {metric !== null && (
              <label className="flex flex-col gap-1 text-sm font-medium text-ink">
                Metric
                <select
                  className="rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm font-normal text-ink"
                  value={metric}
                  onChange={(event) => { update({ metric: event.target.value }); }}
                >
                  {metrics.data.map((item) => (
                    <option key={item.name} value={item.name}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </label>
            )}

            <fieldset>
              <legend className="mb-1 text-sm font-medium text-ink">Time range</legend>
              <div className="flex overflow-hidden rounded-md border border-hairline">
                {RANGES.map((option) => (
                  <label
                    key={option.minutes}
                    className="cursor-pointer border-r border-hairline px-3 py-1.5 text-sm text-ink-2 last:border-r-0 has-checked:bg-wash has-checked:font-medium has-checked:text-ink has-focus-visible:outline-2"
                  >
                    <input
                      type="radio"
                      name="range"
                      className="sr-only"
                      checked={rangeMinutes === option.minutes}
                      onChange={() => { update({ range: option.minutes }); }}
                    />
                    {option.label}
                  </label>
                ))}
              </div>
            </fieldset>
          </div>

          {metric === null ? (
            <EmptyState title="No metrics yet">
              <p>This service has not reported any metrics.</p>
            </EmptyState>
          ) : (
            <Card>
              <h2 className="mb-3 text-base font-semibold text-ink">{metric}</h2>
              {range.isPending ? (
                <LoadingState label="Loading data…" />
              ) : range.isError ? (
                <ErrorState
                  title="Could not load this metric"
                  error={range.error}
                  onRetry={() => void range.refetch()}
                />
              ) : (
                <MetricChart
                  data={range.data}
                  stale={range.isPlaceholderData}
                  deployments={deployments.data ?? []}
                />
              )}
            </Card>
          )}

          <LogsPanel projectId={current.project.id} service={service} rangeMinutes={rangeMinutes} />
        </>
      )}
    </>
  );
}
