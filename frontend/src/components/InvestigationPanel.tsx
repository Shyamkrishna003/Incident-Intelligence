import { useState } from "react";

import { FeedbackSection } from "./FeedbackSection";
import { useLatestInvestigation, useRequestInvestigation } from "../hooks/queries";
import { describeError } from "../lib/api";
import { formatDateTime } from "../lib/format";
import type { Assessment, EvidenceItem, InvestigationDetail } from "../lib/types";
import { Button, Card, ErrorState, FormMessage, LoadingState } from "./ui";

const ASSESSMENT_LABEL: Record<Assessment, string> = {
  supported: "Supported by the evidence",
  weak: "Weakly supported",
  untested: "Untested",
  contradicted: "Contradicted by the evidence",
};

export function InvestigationPanel({
  projectId,
  incidentId,
  canInvestigate,
}: {
  projectId: string;
  incidentId: string;
  canInvestigate: boolean;
}) {
  const { list, latest, detail } = useLatestInvestigation(projectId, incidentId);
  const request = useRequestInvestigation(projectId, incidentId);
  const active = latest?.status === "queued" || latest?.status === "running";

  const button = (label: string) =>
    canInvestigate ? (
      <Button
        variant="primary"
        busy={request.isPending}
        disabled={active}
        onClick={() => { request.mutate(); }}
      >
        {label}
      </Button>
    ) : null;

  return (
    <Card className="mt-5">
      <h2 className="text-base font-semibold text-ink">AI investigation</h2>
      {list.isPending ? (
        <LoadingState label="Loading…" />
      ) : list.isError ? (
        <div className="mt-3">
          <ErrorState
            title="Could not load the investigation"
            error={list.error}
            onRetry={() => void list.refetch()}
          />
        </div>
      ) : latest === null ? (
        <>
          <p className="mt-1 max-w-prose text-sm text-ink-2">
            An AI model can read the evidence for this incident (the anomalies, deployments,
            dependencies and error logs, plus what the deployments changed if GitHub is
            connected) and suggest explanations to check. Every statement it makes must cite
            that evidence.
          </p>
          <div className="mt-3">
            {button("Investigate with AI") ?? (
              <p className="text-sm text-ink-2">
                Starting an investigation needs the member role or higher.
              </p>
            )}
          </div>
        </>
      ) : active ? (
        <div role="status" className="mt-2">
          <LoadingState label="Investigating: collecting the evidence and asking the model. This usually takes a minute or two." />
        </div>
      ) : latest.status === "failed" ? (
        <div className="mt-3">
          <div role="alert" className="rounded-md border border-hairline px-4 py-3">
            <p className="flex items-center gap-2 text-sm font-semibold text-critical-ink">
              <span aria-hidden="true">⚠</span> The investigation did not finish
            </p>
            <p className="mt-1 text-sm text-ink-2">{latest.error ?? "Unknown error."}</p>
          </div>
          <div className="mt-3">{button("Try again")}</div>
        </div>
      ) : detail.isPending ? (
        <LoadingState label="Loading the report…" />
      ) : detail.isError ? (
        <div className="mt-3">
          <ErrorState
            title="Could not load the report"
            error={detail.error}
            onRetry={() => void detail.refetch()}
          />
        </div>
      ) : (
        <>
          <ReportView investigation={detail.data} />
          <FeedbackSection
            projectId={projectId}
            investigation={detail.data}
            canGiveFeedback={canInvestigate}
          />
          <div className="mt-4">{button("Investigate again")}</div>
        </>
      )}
      <div className="mt-2">
        <FormMessage tone="error">
          {request.isError ? describeError(request.error) : null}
        </FormMessage>
      </div>
    </Card>
  );
}

function ReportView({ investigation }: { investigation: InvestigationDetail }) {
  const [openRef, setOpenRef] = useState<string | null>(null);
  const report = investigation.report;
  if (!report) return <p className="mt-2 text-sm text-ink-2">This investigation has no report.</p>;

  function Refs({ refs }: { refs: string[] }) {
    return (
      <span className="ml-1 inline-flex flex-wrap gap-1 align-baseline">
        {refs.map((ref) => (
          <button
            key={ref}
            type="button"
            className="rounded border border-hairline px-1 text-xs text-link hover:bg-wash"
            aria-label={`Show evidence ${ref}`}
            onClick={() => {
              setOpenRef(ref);
              document.getElementById(`evidence-${ref}`)?.scrollIntoView({ block: "center" });
            }}
          >
            {ref}
          </button>
        ))}
      </span>
    );
  }

  return (
    <div className="mt-2 flex flex-col gap-4 text-sm">
      <p role="note" className="rounded-md bg-wash px-3 py-2 text-ink">
        Written by an AI model ({investigation.model ?? "unknown"}) on{" "}
        {formatDateTime(investigation.finished_at)}, using only the evidence listed below. Code
        checked that every reference points to real evidence. The hypotheses are suggestions to
        verify, not confirmed findings.
      </p>

      <section aria-label="Summary">
        <h3 className="font-semibold text-ink">Summary</h3>
        <p className="mt-1 text-ink">{report.summary}</p>
      </section>

      <section aria-label="Observed facts">
        <h3 className="font-semibold text-ink">Observed facts</h3>
        {report.observed_facts.length === 0 ? (
          <p className="mt-1 text-ink-2">None that cite evidence.</p>
        ) : (
          <ul className="mt-1 list-disc pl-5 text-ink">
            {report.observed_facts.map((fact, index) => (
              <li key={index}>
                {fact.statement}
                <Refs refs={fact.evidence} />
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label="Hypotheses">
        <h3 className="font-semibold text-ink">Hypotheses</h3>
        {report.hypotheses.length === 0 ? (
          <p className="mt-1 text-ink-2">
            The model proposed none: the evidence was not enough to suggest an explanation.
          </p>
        ) : (
          <ol className="mt-1 flex flex-col gap-3">
            {report.hypotheses.map((hypothesis, index) => (
              <li key={index} className="rounded-md border border-hairline px-3 py-2">
                <p className="font-medium text-ink">{hypothesis.statement}</p>
                <p className="mt-1 text-ink-2">
                  <span className="font-semibold text-ink">
                    {ASSESSMENT_LABEL[hypothesis.assessment]}.
                  </span>{" "}
                  {hypothesis.reasoning}
                </p>
                {hypothesis.supporting_evidence.length > 0 && (
                  <p className="mt-1 text-ink-2">
                    Supporting evidence:
                    <Refs refs={hypothesis.supporting_evidence} />
                  </p>
                )}
                {hypothesis.contradicting_evidence.length > 0 && (
                  <p className="mt-1 text-ink-2">
                    Contradicting evidence:
                    <Refs refs={hypothesis.contradicting_evidence} />
                  </p>
                )}
                {hypothesis.review && (
                  <p className="mt-1 text-ink-2">Second check: {hypothesis.review}</p>
                )}
              </li>
            ))}
          </ol>
        )}
      </section>

      <section aria-label="Unknowns">
        <h3 className="font-semibold text-ink">What the evidence does not show</h3>
        {report.unknowns.length === 0 ? (
          <p className="mt-1 text-ink-2">Nothing listed.</p>
        ) : (
          <ul className="mt-1 list-disc pl-5 text-ink">
            {report.unknowns.map((unknown, index) => (
              <li key={index}>{unknown}</li>
            ))}
          </ul>
        )}
      </section>

      {report.next_steps.length > 0 && (
        <section aria-label="Suggested checks">
          <h3 className="font-semibold text-ink">Suggested checks</h3>
          <ul className="mt-1 list-disc pl-5 text-ink">
            {report.next_steps.map((step, index) => (
              <li key={index}>
                {step.action}{" "}
                <span className="text-ink-2">Would show: {step.expected_evidence}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {investigation.validation_notes.length > 0 && (
        <section aria-label="Corrections">
          <h3 className="font-semibold text-ink">Corrections made to the model's draft</h3>
          <ul className="mt-1 list-disc pl-5 text-ink-2">
            {investigation.validation_notes.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        </section>
      )}

      <section aria-label="Evidence">
        <h3 className="font-semibold text-ink">Evidence the model was given</h3>
        <ul className="mt-1 flex flex-col gap-1">
          {investigation.evidence.map((item) => (
            <EvidenceRow
              key={item.ref}
              item={item}
              open={openRef === item.ref}
              onToggle={(open) => { setOpenRef(open ? item.ref : null); }}
            />
          ))}
        </ul>
      </section>
    </div>
  );
}

function EvidenceRow({
  item,
  open,
  onToggle,
}: {
  item: EvidenceItem;
  open: boolean;
  onToggle: (open: boolean) => void;
}) {
  return (
    <li id={`evidence-${item.ref}`}>
      <details
        open={open}
        onToggle={(event) => { onToggle(event.currentTarget.open); }}
        className="rounded-md border border-hairline px-3 py-1.5"
      >
        <summary className="cursor-pointer text-ink">
          <span className="font-semibold">{item.ref}</span> {item.title}
        </summary>
        {/* Rendered as text: it contains log lines and descriptions from outside. */}
        <pre className="mt-2 overflow-x-auto text-xs whitespace-pre-wrap text-ink-2">
          {JSON.stringify(item.data, null, 2)}
        </pre>
      </details>
    </li>
  );
}
