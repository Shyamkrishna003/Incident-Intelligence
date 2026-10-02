import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import type { Anomaly } from "../lib/types";
import { projectContext, renderApp } from "../test/render";
import { AnomaliesPage } from "./AnomaliesPage";

const window24h = { start: "2026-10-02T10:00:00Z", end: "2026-10-03T10:00:00Z" };

function anomaly(overrides: Partial<Anomaly>): Anomaly {
  return {
    id: "a1",
    service: "payment-api",
    metric: "http.server.duration.p95",
    attributes: { source: "simulator" },
    unit: "ms",
    detector: "robust_zscore",
    status: "open",
    severity: "critical",
    direction: "above",
    started_at: "2026-10-03T09:45:00Z",
    detected_at: "2026-10-03T09:45:15Z",
    last_anomalous_at: "2026-10-03T09:59:00Z",
    ended_at: null,
    closed_reason: null,
    peak_value: 1700,
    peak_at: "2026-10-03T09:58:00Z",
    peak_score: 180,
    baseline_center: 120,
    baseline_spread: 6,
    point_count: 56,
    ...overrides,
  };
}

describe("AnomaliesPage", () => {
  it("shows each anomaly with its evidence and a link to the chart", async () => {
    vi.spyOn(api, "listAnomalies").mockResolvedValue({
      ...window24h,
      anomalies: [
        anomaly({}),
        anomaly({
          id: "a2",
          metric: "requests",
          attributes: {},
          unit: null,
          status: "closed",
          closed_reason: "recovered",
          severity: "medium",
          direction: "below",
          ended_at: "2026-10-03T08:10:00Z",
          last_anomalous_at: "2026-10-03T08:05:00Z",
          started_at: "2026-10-03T08:00:00Z",
          peak_value: 30,
          baseline_center: 100,
        }),
      ],
    });
    renderApp(<AnomaliesPage current={projectContext("viewer")} />);

    const [ongoing, recovered] = (await screen.findAllByRole("row")).slice(1);
    if (!ongoing || !recovered) throw new Error("expected two anomaly rows");
    expect(within(ongoing).getByText("Ongoing")).toBeInTheDocument();
    expect(within(ongoing).getByText("critical")).toBeInTheDocument();
    expect(ongoing).toHaveTextContent("Rose to 1,700 ms (usually about 120 ms)");
    expect(ongoing).toHaveTextContent("http.server.duration.p95 (source=simulator)");
    expect(within(ongoing).getByRole("link", { name: "payment-api" })).toHaveAttribute(
      "href",
      "/p/project-1/services/payment-api?metric=http.server.duration.p95&range=1440",
    );
    expect(within(recovered).getByText("Recovered")).toBeInTheDocument();
    expect(recovered).toHaveTextContent("Fell to 30 (usually about 100)");
    expect(recovered).toHaveTextContent("5 min");
    // Never framed as a probability.
    expect(screen.getByText(/not a probability/)).toBeInTheDocument();
  });

  it("can show ongoing anomalies only", async () => {
    const listAnomalies = vi
      .spyOn(api, "listAnomalies")
      .mockResolvedValue({ ...window24h, anomalies: [] });
    renderApp(<AnomaliesPage current={projectContext()} />);
    expect(
      await screen.findByRole("heading", { name: "No anomalies in the last 24 hours" }),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("checkbox", { name: "Ongoing only" }));

    await waitFor(() => {
      expect(listAnomalies).toHaveBeenLastCalledWith(
        "project-1",
        expect.objectContaining({ status: "open" }),
      );
    });
    expect(await screen.findByRole("heading", { name: "No ongoing anomalies" })).toBeInTheDocument();
  });
});
