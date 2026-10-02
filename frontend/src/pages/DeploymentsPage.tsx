import { Link } from "react-router-dom";

import type { ProjectContext } from "../components/AppShell";
import { EmptyState, ErrorState, LoadingState, PageHeader } from "../components/ui";
import { useDeployments } from "../hooks/queries";
import { formatDateTime } from "../lib/format";

const WEEK_MINUTES = 7 * 24 * 60;

export function DeploymentsPage({ current }: { current: ProjectContext }) {
  const deployments = useDeployments(current.project.id, WEEK_MINUTES);

  return (
    <>
      <PageHeader title="Deployments" />
      <p className="mb-5 max-w-prose text-sm text-ink-2">
        Versions that went live in the last 7 days. When something breaks, a recent deployment
        is the first place to look.
      </p>
      {deployments.isPending ? (
        <LoadingState label="Loading deployments…" />
      ) : deployments.isError ? (
        <ErrorState
          title="Could not load deployments"
          error={deployments.error}
          onRetry={() => void deployments.refetch()}
        />
      ) : deployments.data.length === 0 ? (
        <EmptyState title="No deployments in the last 7 days">
          <p>
            Report each deployment from your CI/CD pipeline to{" "}
            <code>POST /v1/ingest/deployments</code> with the service, version and time.
          </p>
        </EmptyState>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Deployments, newest first</caption>
            <thead className="text-ink-2">
              <tr>
                {["When", "Service", "Version", "Commit", "Environment", "By", "Description"].map(
                  (heading) => (
                    <th key={heading} scope="col" className="px-4 py-2 font-medium">
                      {heading}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-hairline">
              {deployments.data.map((deployment) => (
                <tr key={deployment.id} className="align-top">
                  <td className="whitespace-nowrap px-4 py-2 text-ink-2">
                    {formatDateTime(deployment.deployed_at)}
                  </td>
                  <th scope="row" className="whitespace-nowrap px-4 py-2 font-medium">
                    <Link
                      className="text-link underline"
                      to={`/p/${current.project.id}/services/${encodeURIComponent(deployment.service)}`}
                    >
                      {deployment.service}
                    </Link>
                  </th>
                  <td className="whitespace-nowrap px-4 py-2 text-ink">{deployment.version}</td>
                  <td className="px-4 py-2 text-ink-2">
                    {deployment.commit_sha ? <code>{deployment.commit_sha.slice(0, 7)}</code> : "—"}
                  </td>
                  <td className="px-4 py-2 text-ink-2">{deployment.environment ?? "—"}</td>
                  <td className="px-4 py-2 text-ink-2">{deployment.deployed_by ?? "—"}</td>
                  <td className="px-4 py-2 text-ink-2">{deployment.description ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
