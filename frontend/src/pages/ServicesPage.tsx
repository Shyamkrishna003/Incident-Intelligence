import { Link } from "react-router-dom";

import type { ProjectContext } from "../components/AppShell";
import { EmptyState, ErrorState, LoadingState, PageHeader } from "../components/ui";
import { useServices } from "../hooks/queries";
import { formatDateTime } from "../lib/format";
import { roleAtLeast } from "../lib/types";

export function ServicesPage({ current }: { current: ProjectContext }) {
  const services = useServices(current.project.id);
  const canManageKeys = roleAtLeast(current.organization.role, "admin");

  return (
    <>
      <PageHeader title="Services" />
      {services.isPending ? (
        <LoadingState label="Loading services…" />
      ) : services.isError ? (
        <ErrorState
          title="Could not load services"
          error={services.error}
          onRetry={() => void services.refetch()}
        />
      ) : services.data.length === 0 ? (
        <EmptyState title="No services yet">
          <p>
            A service appears here once it sends its first metrics to this project.{" "}
            {canManageKeys ? (
              <>
                <Link className="text-link underline" to={`/p/${current.project.id}/api-keys`}>
                  Create an API key
                </Link>{" "}
                and send a batch to <code>POST /v1/ingest/metrics</code>.
              </>
            ) : (
              "Ask an organization admin for an API key."
            )}
          </p>
        </EmptyState>
      ) : (
        <ul className="divide-y divide-hairline rounded-lg border border-hairline bg-surface">
          {services.data.map((service) => (
            <li key={service.id}>
              <Link
                to={`/p/${current.project.id}/services/${encodeURIComponent(service.name)}`}
                className="flex items-center justify-between gap-4 px-4 py-3 hover:bg-wash"
              >
                <span className="text-sm font-medium text-ink">{service.name}</span>
                <span className="text-sm text-ink-2">
                  First seen {formatDateTime(service.created_at)}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
