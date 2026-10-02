import { useState, type SubmitEvent } from "react";

import { useLogs } from "../hooks/queries";
import { formatTimeWithSeconds } from "../lib/format";
import type { Severity } from "../lib/types";
import { Button, Card, EmptyState, ErrorState, LoadingState } from "./ui";

const SEVERITY_FILTERS: { value: Severity | ""; label: string }[] = [
  { value: "", label: "All severities" },
  { value: "warn", label: "Warnings and errors" },
  { value: "error", label: "Errors only" },
];

/** Severity is always spelled out; the icon and colour only reinforce it. */
function SeverityLabel({ severity }: { severity: Severity }) {
  const serious = severity === "error" || severity === "fatal";
  const notable = serious || severity === "warn";
  return (
    <span
      className={`inline-flex items-center gap-1 text-xs font-semibold uppercase ${
        serious ? "text-critical-ink" : notable ? "text-ink" : "text-ink-2"
      }`}
    >
      {notable && <span aria-hidden="true">{serious ? "●" : "▲"}</span>}
      {severity}
    </span>
  );
}

export function LogsPanel({
  projectId,
  service,
  rangeMinutes,
}: {
  projectId: string;
  service: string;
  rangeMinutes: number;
}) {
  const [severity, setSeverity] = useState<Severity | "">("");
  const [draft, setDraft] = useState("");
  const [search, setSearch] = useState("");
  const logs = useLogs(projectId, service, rangeMinutes, severity === "" ? null : severity, search);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setSearch(draft.trim());
  }

  return (
    <Card className="mt-5">
      <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
        <h2 className="text-base font-semibold text-ink">Logs</h2>
        <form onSubmit={submit} className="flex flex-wrap items-end gap-2" role="search">
          <label className="flex flex-col gap-1 text-sm font-medium text-ink">
            Severity
            <select
              className="rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm font-normal text-ink"
              value={severity}
              onChange={(event) => { setSeverity(event.target.value as Severity | ""); }}
            >
              {SEVERITY_FILTERS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm font-medium text-ink">
            Message contains
            <input
              type="search"
              maxLength={200}
              className="w-48 rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm font-normal text-ink"
              value={draft}
              onChange={(event) => { setDraft(event.target.value); }}
            />
          </label>
          <Button type="submit">Search</Button>
        </form>
      </div>

      {logs.isPending ? (
        <LoadingState label="Loading logs…" />
      ) : logs.isError ? (
        <ErrorState
          title="Could not load logs"
          error={logs.error}
          onRetry={() => void logs.refetch()}
        />
      ) : logs.data.records.length === 0 ? (
        <EmptyState title="No logs match">
          <p>
            {search || severity
              ? "Nothing matches these filters in this time range."
              : "This service sent no logs in this time range."}
          </p>
        </EmptyState>
      ) : (
        <div className={logs.isPlaceholderData ? "opacity-60" : undefined}>
          <div className="max-h-96 overflow-auto rounded-md border border-hairline">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Log records, newest first</caption>
              <thead className="sticky top-0 bg-surface text-ink-2">
                <tr>
                  <th scope="col" className="px-3 py-2 font-medium">Time</th>
                  <th scope="col" className="px-3 py-2 font-medium">Severity</th>
                  <th scope="col" className="px-3 py-2 font-medium">Message</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline">
                {logs.data.records.map((record, index) => (
                  <tr key={`${record.timestamp}-${index}`} className="align-top">
                    <td className="whitespace-nowrap px-3 py-1.5 tabular-nums text-ink-2">
                      {formatTimeWithSeconds(Date.parse(record.timestamp))}
                    </td>
                    <td className="px-3 py-1.5">
                      <SeverityLabel severity={record.severity} />
                    </td>
                    {/* Rendered as plain text: log content comes from outside and is untrusted. */}
                    <td className="break-words px-3 py-1.5 font-mono text-[13px] whitespace-pre-wrap text-ink">
                      {record.message}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-sm text-ink-2">
            {logs.data.truncated
              ? `Showing the newest ${logs.data.records.length} matching records. Narrow the time range or the filters to see older ones.`
              : `${logs.data.records.length} record${logs.data.records.length === 1 ? "" : "s"}, newest first.`}
          </p>
        </div>
      )}
    </Card>
  );
}
