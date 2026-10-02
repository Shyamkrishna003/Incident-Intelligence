import { useState } from "react";
import { Link } from "react-router-dom";

import type { ProjectContext } from "../components/AppShell";
import { EmptyState, ErrorState, LoadingState, PageHeader } from "../components/ui";
import { useAnomalies } from "../hooks/queries";
import { formatDateTime, formatValue, seriesLabel } from "../lib/format";
import type { Anomaly } from "../lib/types";

const DAY_MINUTES = 24 * 60;

function duration(anomaly: Anomaly): string {
  const end = anomaly.status === "open" ? Date.now() : Date.parse(anomaly.last_anomalous_at);
  const minutes = Math.max(0, Math.round((end - Date.parse(anomaly.started_at)) / 60_000));
  if (minutes < 1) return "under a minute";
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

/** Status in words; the dot only reinforces it. */
function StatusLabel({ anomaly }: { anomaly: Anomaly }) {
  if (anomaly.status === "open") {
    return (
      <span className="inline-flex items-center gap-1 font-semibold text-critical-ink">
        <span aria-hidden="true">●</span> Ongoing
      </span>
    );
  }
  return (
    <span className="text-ink-2">
      {anomaly.closed_reason === "persisted" ? "Became the new normal" : "Recovered"}
    </span>
  );
}

export function AnomaliesPage({ current }: { current: ProjectContext }) {
  const [onlyOpen, setOnlyOpen] = useState(false);
  const anomalies = useAnomalies(current.project.id, DAY_MINUTES, onlyOpen ? { status: "open" } : {});

  return (
    <>
      <PageHeader title="Anomalies">
        <label className="flex items-center gap-2 text-sm text-ink">
          <input
            type="checkbox"
            checked={onlyOpen}
            onChange={(event) => { setOnlyOpen(event.target.checked); }}
          />
          Ongoing only
        </label>
      </PageHeader>
      <p className="mb-5 max-w-prose text-sm text-ink-2">
        Metrics that moved far outside their recent normal range in the last 24 hours. Each row
        shows the evidence: the most extreme value and what was usual just before. These are
        statistical flags to investigate, not confirmed problems.
      </p>

      {anomalies.isPending ? (
        <LoadingState label="Loading anomalies…" />
      ) : anomalies.isError ? (
        <ErrorState
          title="Could not load anomalies"
          error={anomalies.error}
          onRetry={() => void anomalies.refetch()}
        />
      ) : anomalies.data.length === 0 ? (
        <EmptyState title={onlyOpen ? "No ongoing anomalies" : "No anomalies in the last 24 hours"}>
          <p>
            A metric is checked once it has about 30 recent points of history. Nothing is
            flagged while it stays within its usual range.
          </p>
        </EmptyState>
      ) : (
        <div className={anomalies.isPlaceholderData ? "opacity-60" : undefined}>
          <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Anomalies, ongoing first, then newest first</caption>
              <thead className="text-ink-2">
                <tr>
                  {["Status", "Severity", "Service and metric", "Started", "Lasted", "Evidence"].map(
                    (heading) => (
                      <th key={heading} scope="col" className="px-4 py-2 font-medium">
                        {heading}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline">
                {anomalies.data.map((anomaly) => {
                  const series = seriesLabel(anomaly.attributes);
                  return (
                    <tr key={anomaly.id} className="align-top">
                      <td className="whitespace-nowrap px-4 py-2">
                        <StatusLabel anomaly={anomaly} />
                      </td>
                      <td className="px-4 py-2 capitalize text-ink">{anomaly.severity}</td>
                      <th scope="row" className="px-4 py-2 font-normal">
                        <Link
                          className="font-medium text-link underline"
                          to={`/p/${current.project.id}/services/${encodeURIComponent(
                            anomaly.service,
                          )}?metric=${encodeURIComponent(anomaly.metric)}&range=1440`}
                        >
                          {anomaly.service}
                        </Link>
                        <div className="text-ink-2">
                          {anomaly.metric}
                          {series !== "all" && ` (${series})`}
                        </div>
                      </th>
                      <td className="whitespace-nowrap px-4 py-2 text-ink-2">
                        {formatDateTime(anomaly.started_at)}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-ink-2">
                        {duration(anomaly)}
                      </td>
                      <td className="px-4 py-2 text-ink">
                        {anomaly.direction === "above" ? "Rose to" : "Fell to"}{" "}
                        <strong>{formatValue(anomaly.peak_value, anomaly.unit)}</strong>
                        <span className="text-ink-2">
                          {" "}
                          (usually about {formatValue(anomaly.baseline_center, anomaly.unit)})
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-sm text-ink-2">
            Severity is a fixed rule on how far the value moved relative to its usual
            variation. It is not a probability that something is wrong.
          </p>
        </div>
      )}
    </>
  );
}
