import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { FeedbackSection } from "../components/FeedbackSection";
import { api } from "../lib/api";
import type { InvestigationDetail } from "../lib/types";
import { projectContext, renderApp } from "../test/render";
import { LearningPage } from "./LearningPage";

const investigation: InvestigationDetail = {
  id: "inv-1",
  incident_id: "i1",
  status: "succeeded",
  created_at: "2026-10-03T10:00:00Z",
  started_at: null,
  finished_at: "2026-10-03T10:02:00Z",
  provider: "gemini",
  model: "gemini-test",
  prompt_version: "2",
  error: null,
  report: null,
  validation_notes: [],
  evidence: [],
  steps: [],
  input_tokens: 0,
  output_tokens: 0,
  feedback: [],
};

describe("FeedbackSection", () => {
  it("saves a verdict with the actual cause", async () => {
    const submit = vi.spyOn(api, "submitFeedback").mockResolvedValue({
      verdict: "partially_correct",
      actual_cause: "A missing index.",
      notes: null,
      author: "Priya",
      updated_at: "2026-10-03T10:05:00Z",
      mine: true,
    });
    renderApp(<FeedbackSection projectId="project-1" investigation={investigation} canGiveFeedback />);

    expect(screen.getByRole("button", { name: "Save feedback" })).toBeDisabled();
    await userEvent.click(screen.getByRole("radio", { name: "Partially correct" }));
    await userEvent.type(screen.getByLabelText(/actual cause/), "A missing index.");
    await userEvent.click(screen.getByRole("button", { name: "Save feedback" }));

    expect(submit).toHaveBeenCalledWith("project-1", "inv-1", {
      verdict: "partially_correct",
      actual_cause: "A missing index.",
      notes: null,
    });
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  it("shows my earlier answer for revision, and other people's answers", () => {
    renderApp(
      <FeedbackSection
        projectId="project-1"
        canGiveFeedback
        investigation={{
          ...investigation,
          feedback: [
            { verdict: "incorrect", actual_cause: "Disk full.", notes: null, author: "Me", updated_at: "2026-10-03T10:05:00Z", mine: true },
            { verdict: "correct", actual_cause: null, notes: null, author: "Sam", updated_at: "2026-10-03T10:06:00Z", mine: false },
          ],
        }}
      />,
    );

    expect(screen.getByRole("radio", { name: "Incorrect" })).toBeChecked();
    expect(screen.getByLabelText(/actual cause/)).toHaveValue("Disk full.");
    expect(screen.getByRole("button", { name: "Update feedback" })).toBeEnabled();
    expect(screen.getByText("Sam")).toBeInTheDocument();
  });

  it("offers no form to a viewer", () => {
    renderApp(
      <FeedbackSection projectId="project-1" investigation={investigation} canGiveFeedback={false} />,
    );

    expect(screen.getByText(/needs the member role/)).toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  });
});

describe("LearningPage", () => {
  it("shows verdict counts as counts, and the reviewed investigations", async () => {
    vi.spyOn(api, "learningRecords").mockResolvedValue({
      counts: { correct: 3, partially_correct: 1, incorrect: 0 },
      records: [
        {
          id: "r1",
          incident_id: "i1",
          incident_title: "Anomalies in payment-api",
          investigation_id: "inv-1",
          verdict: "partially_correct",
          confirmed_cause: "A missing index.",
          model: "gemini-test",
          prompt_version: "2",
          updated_at: "2026-10-03T10:05:00Z",
        },
      ],
    });
    renderApp(<LearningPage current={projectContext("viewer")} />);

    const link = await screen.findByRole("link", { name: "Anomalies in payment-api" });
    expect(link).toHaveAttribute("href", "/p/project-1/incidents/i1");
    const row = link.closest("tr");
    if (!row) throw new Error("record row not found");
    expect(within(row).getByText("Partially correct")).toBeInTheDocument();
    expect(within(row).getByText("A missing index.")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText(/not a measured accuracy rate/)).toBeInTheDocument();
  });

  it("explains how to get the first record", async () => {
    vi.spyOn(api, "learningRecords").mockResolvedValue({
      counts: { correct: 0, partially_correct: 0, incorrect: 0 },
      records: [],
    });
    renderApp(<LearningPage current={projectContext()} />);

    expect(
      await screen.findByRole("heading", { name: "No reviewed investigations yet" }),
    ).toBeInTheDocument();
  });
});
