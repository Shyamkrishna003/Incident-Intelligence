import { useState } from "react";
import { Link } from "react-router-dom";

import type { ProjectContext } from "../components/AppShell";
import { EmptyState, ErrorState, LoadingState, PageHeader } from "../components/ui";
import { useIncidents } from "../hooks/queries";
import { formatDateTime, formatDuration } from "../lib/format";
import type { Incident } from "../lib/types";

const WEEK_MINUTES = 7 * 24 * 60;

export function incidentDuration(incident: Incident): string {
  const end = incident.resolved_at ? Date.parse(incident.resolved_at) : Date.now();
  return formatDuration(incident.started_at, end);
}

/** Status in words; the dot only reinforces it. */
export function IncidentStatus({ incident }: { incident: Incident }) {
  if (incident.status === "open") {
    return (
      <span className="inline-flex items-center gap-1 font-semibold text-critical-ink">
        <span aria-hidden="true">●</span> Ongoing
      </span>
    );
  }
  return <span className="text-ink-2">{incident.status === "merged" ? "Merged" : "Resolved"}</span>;
}

export function IncidentsPage({ current }: { current: ProjectContext }) {
  const [onlyOpen, setOnlyOpen] = useState(false);
  const incidents = useIncidents(current.project.id, WEEK_MINUTES, onlyOpen);

  return (
    <>
      <PageHeader title="Incidents">
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
        Related anomalies from the last 7 days, grouped so that one problem is one incident.
      </p>

      {incidents.isPending ? (
        <LoadingState label="Loading incidents…" />
      ) : incidents.isError ? (
        <ErrorState
          title="Could not load incidents"
          error={incidents.error}
          onRetry={() => void incidents.refetch()}
        />
      ) : incidents.data.length === 0 ? (
        <EmptyState title={onlyOpen ? "No ongoing incidents" : "No incidents in the last 7 days"}>
          <p>
            An incident opens when a metric moves far outside its usual range. Nothing has been
            flagged.
          </p>
        </EmptyState>
      ) : (
        <div className={incidents.isPlaceholderData ? "opacity-60" : undefined}>
          <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Incidents, ongoing first, then newest first</caption>
              <thead className="text-ink-2">
                <tr>
                  {["Status", "Severity", "Incident", "Started", "Lasted", "Anomalies"].map(
                    (heading) => (
                      <th key={heading} scope="col" className="px-4 py-2 font-medium">
                        {heading}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline">
                {incidents.data.map((incident) => (
                  <tr key={incident.id} className="align-top">
                    <td className="whitespace-nowrap px-4 py-2">
                      <IncidentStatus incident={incident} />
                    </td>
                    <td className="px-4 py-2 capitalize text-ink">{incident.severity}</td>
                    <th scope="row" className="px-4 py-2 font-normal">
                      <Link
                        className="font-medium text-link underline"
                        to={`/p/${current.project.id}/incidents/${incident.id}`}
                      >
                        {incident.title}
                      </Link>
                      <div className="text-ink-2">{incident.services.join(", ")}</div>
                    </th>
                    <td className="whitespace-nowrap px-4 py-2 text-ink-2">
                      {formatDateTime(incident.started_at)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-ink-2">
                      {incidentDuration(incident)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-ink-2">
                      {incident.anomaly_count}
                      {incident.open_anomaly_count > 0 &&
                        ` (${incident.open_anomaly_count} ongoing)`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}
