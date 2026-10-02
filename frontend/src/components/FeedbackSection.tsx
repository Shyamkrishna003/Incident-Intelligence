import { useState, type SubmitEvent } from "react";

import { useSubmitFeedback } from "../hooks/queries";
import { describeError } from "../lib/api";
import { formatDateTime } from "../lib/format";
import type { InvestigationDetail, Verdict } from "../lib/types";
import { VERDICTS, VERDICT_LABEL } from "../lib/verdict";
import { Button, FormMessage } from "./ui";

/** Was the report right? Feedback is how the system learns which reports to trust. */
export function FeedbackSection({
  projectId,
  investigation,
  canGiveFeedback,
}: {
  projectId: string;
  investigation: InvestigationDetail;
  canGiveFeedback: boolean;
}) {
  const mine = investigation.feedback.find((item) => item.mine);
  const others = investigation.feedback.filter((item) => !item.mine);
  const submit = useSubmitFeedback(projectId, investigation.id);
  const [verdict, setVerdict] = useState<Verdict | null>(mine?.verdict ?? null);
  const [actualCause, setActualCause] = useState(mine?.actual_cause ?? "");
  const [notes, setNotes] = useState(mine?.notes ?? "");

  function onSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!verdict) return;
    submit.mutate({
      verdict,
      actual_cause: actualCause.trim() || null,
      notes: notes.trim() || null,
    });
  }

  return (
    <section aria-label="Feedback" className="mt-5 border-t border-hairline pt-4 text-sm">
      <h3 className="font-semibold text-ink">Was this analysis right?</h3>
      <p className="mt-1 max-w-prose text-ink-2">
        Your answer is saved with the report and its evidence, and is used to measure how
        reliable these analyses are.
      </p>

      {others.length > 0 && (
        <ul className="mt-2 flex flex-col gap-1 text-ink-2">
          {others.map((item) => (
            <li key={`${item.author}-${item.updated_at}`}>
              <span className="font-medium text-ink">{item.author}</span>:{" "}
              {VERDICT_LABEL[item.verdict]}
              {item.actual_cause && `. Actual cause: ${item.actual_cause}`} (
              {formatDateTime(item.updated_at)})
            </li>
          ))}
        </ul>
      )}

      {canGiveFeedback ? (
        <form onSubmit={onSubmit} className="mt-3 flex flex-col gap-3" noValidate>
          <fieldset>
            <legend className="font-medium text-ink">Your verdict</legend>
            <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
              {VERDICTS.map((option) => (
                <label key={option} className="flex items-center gap-2 text-ink">
                  <input
                    type="radio"
                    name="verdict"
                    checked={verdict === option}
                    onChange={() => { setVerdict(option); }}
                  />
                  {VERDICT_LABEL[option]}
                </label>
              ))}
            </div>
          </fieldset>
          <label className="flex flex-col gap-1 font-medium text-ink">
            What was the actual cause? (optional)
            <textarea
              rows={2}
              maxLength={2000}
              className="rounded-md border border-hairline bg-surface px-3 py-2 font-normal text-ink"
              value={actualCause}
              onChange={(event) => { setActualCause(event.target.value); }}
            />
          </label>
          <label className="flex flex-col gap-1 font-medium text-ink">
            Notes (optional)
            <textarea
              rows={2}
              maxLength={2000}
              className="rounded-md border border-hairline bg-surface px-3 py-2 font-normal text-ink"
              value={notes}
              onChange={(event) => { setNotes(event.target.value); }}
            />
          </label>
          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" busy={submit.isPending} disabled={verdict === null}>
              {mine ? "Update feedback" : "Save feedback"}
            </Button>
            <FormMessage tone={submit.isError ? "error" : "info"}>
              {submit.isError
                ? describeError(submit.error)
                : submit.isSuccess
                  ? "Saved."
                  : mine
                    ? `You answered ${formatDateTime(mine.updated_at)}.`
                    : null}
            </FormMessage>
          </div>
        </form>
      ) : (
        <p className="mt-2 text-ink-2">Giving feedback needs the member role or higher.</p>
      )}
    </section>
  );
}
