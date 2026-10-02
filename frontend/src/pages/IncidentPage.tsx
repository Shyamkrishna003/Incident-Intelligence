import { Link, useParams } from "react-router-dom";

import type { ProjectContext } from "../components/AppShell";
import { InvestigationPanel } from "../components/InvestigationPanel";
import { Card, ErrorState, LoadingState } from "../components/ui";
import { useIncident } from "../hooks/queries";
import { formatDateTime, formatDuration, formatValue, seriesLabel } from "../lib/format";
import { describeEvent } from "../lib/timeline";
import type { IncidentDetail } from "../lib/types";
import { roleAtLeast } from "../lib/types";
import { IncidentStatus, incidentDuration } from "./IncidentsPage";

export function IncidentPage({ current }: { current: ProjectContext }) {
  const { incidentId = "" } = useParams();
  const incident = useIncident(current.project.id, incidentId);
  const base = `/p/${current.project.id}`;

  return (
    <>
      <p className="mb-2 text-sm">
        <Link className="text-link underline" to={`${base}/incidents`}>
          ← All incidents
        </Link>
      </p>
      {incident.isPending ? (
        <LoadingState label="Loading incident…" />
      ) : incident.isError ? (
        <ErrorState
          title="Could not load this incident"
          error={incident.error}
          onRetry={() => void incident.refetch()}
        />
      ) : (
        <IncidentView
          incident={incident.data}
          base={base}
          projectId={current.project.id}
          canInvestigate={roleAtLeast(current.organization.role, "member")}
        />
      )}
    </>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-sm text-ink-2">{label}</dt>
      <dd className="text-sm font-medium text-ink">{children}</dd>
    </div>
  );
}

function IncidentView({
  incident,
  base,
  projectId,
  canInvestigate,
}: {
  incident: IncidentDetail;
  base: string;
  projectId: string;
  canInvestigate: boolean;
}) {
  return (
    <>
      <h1 className="text-xl font-semibold text-ink">{incident.title}</h1>

      {incident.status === "merged" && incident.merged_into_id && (
        <div role="status" className="mt-3 rounded-md bg-wash px-3 py-2 text-sm text-ink">
          This incident was merged into another one.{" "}
          <Link className="text-link underline" to={`${base}/incidents/${incident.merged_into_id}`}>
            Open the combined incident
          </Link>
        </div>
      )}

      <dl className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Fact label="Status">
          <IncidentStatus incident={incident} />
        </Fact>
        <Fact label="Severity">
          <span className="capitalize">{incident.severity}</span>
        </Fact>
        <Fact label="Started">{formatDateTime(incident.started_at)}</Fact>
        <Fact label={incident.resolved_at ? "Lasted" : "Ongoing for"}>
          {incidentDuration(incident)}
        </Fact>
      </dl>

      <Card className="mt-5">
        <h2 className="text-base font-semibold text-ink">What was observed</h2>
        <p className="mt-1 text-sm text-ink-2">
          Metrics that moved far outside their usual range. Detected{" "}
          {formatDuration(incident.started_at, Date.parse(incident.detected_at))} after the
          first one started.
        </p>
        {incident.anomalies.length === 0 ? (
          <p className="mt-3 text-sm text-ink-2">This incident has no anomalies of its own.</p>
        ) : (
          <div className="mt-3 overflow-x-auto rounded-md border border-hairline">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Anomalies in this incident, in the order they started</caption>
              <thead className="text-ink-2">
                <tr>
                  {["Service and metric", "Started", "State", "Evidence"].map((heading) => (
                    <th key={heading} scope="col" className="px-3 py-2 font-medium">
                      {heading}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline">
                {incident.anomalies.map((anomaly) => {
                  const series = seriesLabel(anomaly.attributes);
                  return (
                    <tr key={anomaly.id} className="align-top">
                      <th scope="row" className="px-3 py-2 font-normal">
                        <Link
                          className="font-medium text-link underline"
                          to={`${base}/services/${encodeURIComponent(
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
                      <td className="whitespace-nowrap px-3 py-2 text-ink-2">
                        {formatDateTime(anomaly.started_at)}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-ink-2">
                        {anomaly.status === "open" ? "Ongoing" : "Ended"}
                      </td>
                      <td className="px-3 py-2 text-ink">
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
        )}
      </Card>

      <Card className="mt-5">
        <h2 className="text-base font-semibold text-ink">What changed</h2>
        <p className="mt-1 text-sm text-ink-2">
          Deployments of the affected services in the hour before the incident, or during it.
          These are candidates to check, not confirmed causes.
        </p>
        {incident.candidate_deployments.length === 0 ? (
          <p className="mt-3 text-sm text-ink">
            No deployments of these services were reported in that period.
          </p>
        ) : (
          <ul className="mt-3 divide-y divide-hairline rounded-md border border-hairline">
            {incident.candidate_deployments.map((deployment) => (
              <li key={deployment.id} className="px-3 py-2 text-sm">
                <span className="font-medium text-ink">
                  {deployment.service} {deployment.version}
                </span>{" "}
                <span className="text-ink-2">
                  deployed {formatDateTime(deployment.deployed_at)},{" "}
                  {deployment.timing === "during"
                    ? "during the incident"
                    : `${formatDuration(
                        deployment.deployed_at,
                        Date.parse(incident.started_at),
                      )} before the first anomaly`}
                  {deployment.commit_sha && ` · commit ${deployment.commit_sha.slice(0, 7)}`}
                  {deployment.deployed_by && ` · by ${deployment.deployed_by}`}
                </span>
                {deployment.description && (
                  <div className="text-ink-2">{deployment.description}</div>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>

      {incident.status !== "merged" && (
        <InvestigationPanel
          projectId={projectId}
          incidentId={incident.id}
          canInvestigate={canInvestigate}
        />
      )}

      <Card className="mt-5">
        <h2 className="text-base font-semibold text-ink">Not yet known</h2>
        <p className="mt-1 text-sm text-ink">
          The cause of this incident has not been confirmed. The grouping above is based on
          timing and declared service dependencies; it does not show what caused what. An AI
          investigation offers hypotheses to check, not a confirmed cause.
        </p>
      </Card>

      <Card className="mt-5">
        <h2 className="text-base font-semibold text-ink">Timeline</h2>
        <p className="mt-1 text-sm text-ink-2">
          What the system recorded, in order, including why each anomaly was grouped here.
        </p>
        {incident.dependencies.length > 0 && (
          <p className="mt-2 text-sm text-ink-2">
            Declared dependencies among these services:{" "}
            {incident.dependencies
              .map((edge) => `${edge.service} depends on ${edge.depends_on}`)
              .join("; ")}
            .
          </p>
        )}
        <ol className="mt-3 flex flex-col gap-2">
          {incident.timeline.map((event, index) => (
            <li
              key={`${event.ts}-${index}`}
              className="flex flex-col gap-x-3 text-sm sm:flex-row"
            >
              <time dateTime={event.ts} className="shrink-0 tabular-nums text-ink-2 sm:w-40">
                {formatDateTime(event.ts)}
              </time>
              {/* Metric names are long unbroken strings: let them wrap anywhere. */}
              <span className="min-w-0 text-ink [overflow-wrap:anywhere]">
                {describeEvent(event)}
              </span>
            </li>
          ))}
        </ol>
      </Card>
    </>
  );
}
