import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../lib/api";
import type { Investigation, InvestigationDetail } from "../lib/types";
import { renderApp } from "../test/render";
import { InvestigationPanel } from "./InvestigationPanel";

const base: Investigation = {
  id: "inv-1",
  incident_id: "i1",
  status: "succeeded",
  created_at: "2026-10-03T10:00:00Z",
  started_at: "2026-10-03T10:00:01Z",
  finished_at: "2026-10-03T10:00:20Z",
  provider: "gemini",
  model: "gemini-test",
  prompt_version: "1",
  error: null,
};

const detail: InvestigationDetail = {
  ...base,
  report: {
    summary: "Latency rose right after 2.43.0 was deployed.",
    observed_facts: [{ statement: "payment-api latency rose to 900 ms.", evidence: ["E2"] }],
    hypotheses: [
      {
        statement: "Version 2.43.0 introduced the slowdown.",
        assessment: "supported",
        supporting_evidence: ["E2", "E3"],
        contradicting_evidence: [],
        reasoning: "The rise began seconds after the deployment.",
        review: "Nothing contradicts it.",
      },
      {
        statement: "A traffic surge.",
        assessment: "contradicted",
        supporting_evidence: [],
        contradicting_evidence: ["E6"],
        reasoning: "Would affect all services.",
        review: null,
      },
    ],
    unknowns: ["Whether the database was slow."],
    next_steps: [{ action: "Diff the release.", expected_evidence: "A changed query." }],
  },
  validation_notes: ["Removed observed fact 2: it cited no evidence."],
  evidence: [
    { ref: "E2", kind: "anomaly", title: "Anomaly: payment-api latency", data: { most_extreme_value: 900 } },
    { ref: "E3", kind: "deployment", title: "Deployment: payment-api 2.43.0", data: { note: "<b>not markup</b>" } },
  ],
  steps: [],
  input_tokens: 1200,
  output_tokens: 300,
  feedback: [],
};

const panel = (canInvestigate = true) => (
  <InvestigationPanel projectId="project-1" incidentId="i1" canInvestigate={canInvestigate} />
);

describe("InvestigationPanel", () => {
  it("starts an investigation on request, never by itself", async () => {
    vi.spyOn(api, "listInvestigations").mockResolvedValue({ investigations: [] });
    const request = vi
      .spyOn(api, "requestInvestigation")
      .mockResolvedValue({ ...base, status: "queued", model: null, finished_at: null });
    renderApp(panel());

    const button = await screen.findByRole("button", { name: "Investigate with AI" });
    expect(request).not.toHaveBeenCalled();
    await userEvent.click(button);

    expect(request).toHaveBeenCalledWith("project-1", "i1");
  });

  it("offers no button to a viewer", async () => {
    vi.spyOn(api, "listInvestigations").mockResolvedValue({ investigations: [] });
    renderApp(panel(false));

    expect(await screen.findByText(/needs the member role/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Investigate with AI" })).not.toBeInTheDocument();
  });

  it("shows progress while the investigation runs", async () => {
    vi.spyOn(api, "listInvestigations").mockResolvedValue({
      investigations: [{ ...base, status: "running", finished_at: null }],
    });
    renderApp(panel());

    expect(await screen.findByText(/Investigating: collecting the evidence/)).toBeInTheDocument();
  });

  it("shows the report as AI-written, with assessments in words and citations", async () => {
    vi.spyOn(api, "listInvestigations").mockResolvedValue({ investigations: [base] });
    vi.spyOn(api, "getInvestigation").mockResolvedValue(detail);
    const { container } = renderApp(panel());

    const note = await screen.findByRole("note");
    expect(note).toHaveTextContent("Written by an AI model (gemini-test)");
    expect(note).toHaveTextContent("not confirmed findings");
    expect(screen.getByText("Supported by the evidence.")).toBeInTheDocument();
    expect(screen.getByText("Contradicted by the evidence.")).toBeInTheDocument();
    expect(screen.getByText("Second check: Nothing contradicts it.")).toBeInTheDocument();
    expect(screen.getByText("Whether the database was slow.")).toBeInTheDocument();
    expect(screen.getByText(/Removed observed fact 2/)).toBeInTheDocument();
    // No probabilities or percentages anywhere in the report.
    expect(container.textContent).not.toMatch(/\d+\s?%|probab|confidence/i);

    // A citation opens the evidence it points to; evidence is shown as text.
    const evidence = container.querySelector("#evidence-E3 details");
    expect(evidence).not.toHaveAttribute("open");
    const [citation] = screen.getAllByRole("button", { name: "Show evidence E3" });
    if (!citation) throw new Error("no citation button for E3");
    await userEvent.click(citation);
    await waitFor(() => { expect(evidence).toHaveAttribute("open"); });
    expect(container.querySelector("#evidence-E3 b")).toBeNull();
  });

  it("shows why an investigation failed and lets a member retry", async () => {
    vi.spyOn(api, "listInvestigations").mockResolvedValue({
      investigations: [{ ...base, status: "failed", error: "Gemini answered 429: quota" }],
    });
    const request = vi
      .spyOn(api, "requestInvestigation")
      .mockRejectedValue(new ApiError({ status: 429, code: "rate_limited", message: "Too many requests." }));
    renderApp(panel());

    expect(await screen.findByRole("alert")).toHaveTextContent("Gemini answered 429: quota");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(request).toHaveBeenCalled();
    expect(await screen.findByText("Too many requests.")).toBeInTheDocument();
  });
});
