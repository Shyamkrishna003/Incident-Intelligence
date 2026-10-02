import { Link } from "react-router-dom";

import type { ProjectContext } from "../components/AppShell";
import { EmptyState, ErrorState, LoadingState, PageHeader } from "../components/ui";
import { useLearningRecords } from "../hooks/queries";
import { formatDateTime } from "../lib/format";
import { VERDICTS, VERDICT_LABEL } from "../lib/verdict";

export function LearningPage({ current }: { current: ProjectContext }) {
  const learning = useLearningRecords(current.project.id);

  return (
    <>
      <PageHeader title="Learning" />
      <p className="mb-5 max-w-prose text-sm text-ink-2">
        AI investigations that someone has reviewed. Each is kept with its evidence, the
        report and the reviewer's answer, so changes to the model or prompt can be measured
        against them.
      </p>
      {learning.isPending ? (
        <LoadingState label="Loading…" />
      ) : learning.isError ? (
        <ErrorState
          title="Could not load learning records"
          error={learning.error}
          onRetry={() => void learning.refetch()}
        />
      ) : learning.data.records.length === 0 ? (
        <EmptyState title="No reviewed investigations yet">
          <p>
            Open an incident, run an AI investigation, and answer “Was this analysis right?”.
          </p>
        </EmptyState>
      ) : (
        <>
          <dl className="mb-2 grid grid-cols-3 gap-4 sm:max-w-md">
            {VERDICTS.map((verdict) => (
              <div key={verdict}>
                <dt className="text-sm text-ink-2">{VERDICT_LABEL[verdict]}</dt>
                <dd className="text-2xl font-semibold text-ink">
                  {learning.data.counts[verdict]}
                </dd>
              </div>
            ))}
          </dl>
          <p className="mb-4 max-w-prose text-sm text-ink-2">
            These count only investigations that someone chose to review, so they are not a
            measured accuracy rate.
          </p>
          <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Reviewed investigations, newest first</caption>
              <thead className="text-ink-2">
                <tr>
                  {["Reviewed", "Incident", "Verdict", "Actual cause", "Model"].map((heading) => (
                    <th key={heading} scope="col" className="px-4 py-2 font-medium">
                      {heading}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline">
                {learning.data.records.map((record) => (
                  <tr key={record.id} className="align-top">
                    <td className="whitespace-nowrap px-4 py-2 text-ink-2">
                      {formatDateTime(record.updated_at)}
                    </td>
                    <th scope="row" className="px-4 py-2 font-normal">
                      <Link
                        className="font-medium text-link underline"
                        to={`/p/${current.project.id}/incidents/${record.incident_id}`}
                      >
                        {record.incident_title ?? "Incident"}
                      </Link>
                    </th>
                    <td className="whitespace-nowrap px-4 py-2 text-ink">
                      {VERDICT_LABEL[record.verdict]}
                    </td>
                    <td className="px-4 py-2 text-ink-2">{record.confirmed_cause ?? "—"}</td>
                    <td className="whitespace-nowrap px-4 py-2 text-ink-2">
                      {record.model ?? "—"}
                      {record.prompt_version && ` · prompt v${record.prompt_version}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}
