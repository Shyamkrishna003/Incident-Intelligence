import { screen, within } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { describeEvent } from "../lib/timeline";
import type { Anomaly, IncidentDetail } from "../lib/types";
import { projectContext, renderApp } from "../test/render";
import { IncidentPage } from "./IncidentPage";
import { IncidentsPage } from "./IncidentsPage";

function anomaly(overrides: Partial<Anomaly>): Anomaly {
  return {
    id: "a1",
    service: "payments-db",
    metric: "db.query.duration.p95",
    attributes: {},
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
    peak_value: 900,
    peak_at: "2026-10-03T09:58:00Z",
    peak_score: 180,
    baseline_center: 15,
    baseline_spread: 1,
    point_count: 56,
    incident_id: "i1",
    ...overrides,
  };
}

const detail: IncidentDetail = {
  id: "i1",
  title: "Anomalies in payments-db and payment-api",
  status: "open",
  severity: "critical",
  started_at: "2026-10-03T09:45:00Z",
  detected_at: "2026-10-03T09:45:15Z",
  last_activity_at: "2026-10-03T09:59:00Z",
  resolved_at: null,
  merged_into_id: null,
  services: ["payments-db", "payment-api"],
  anomaly_count: 2,
  open_anomaly_count: 2,
  anomalies: [
    anomaly({}),
    anomaly({ id: "a2", service: "payment-api", metric: "http.server.duration.p95", peak_value: 1700, baseline_center: 120 }),
  ],
  candidate_deployments: [
    {
      id: "d1",
      service: "payment-api",
      version: "2.43.0",
      deployed_at: "2026-10-03T09:44:30Z",
      commit_sha: "4f7a9b2c",
      environment: "production",
      deployed_by: "ci",
      description: null,
      timing: "before",
    },
  ],
  timeline: [
    { ts: "2026-10-03T09:44:30Z", kind: "deployment_linked", details: { service: "payment-api", version: "2.43.0", timing: "before" } },
    { ts: "2026-10-03T09:45:15Z", kind: "incident_opened", details: { service: "payments-db", metric: "db.query.duration.p95", rule: "no_related_incident" } },
    {
      ts: "2026-10-03T09:45:15Z",
      kind: "anomaly_attached",
      details: {
        service: "payment-api",
        metric: "http.server.duration.p95",
        rule: "dependency",
        dependency: { service: "payment-api", depends_on: "payments-db" },
      },
    },
  ],
  dependencies: [{ service: "payment-api", depends_on: "payments-db" }],
};

function renderIncident() {
  return renderApp(
    <Routes>
      <Route path="/p/:projectId/incidents/:incidentId" element={<IncidentPage current={projectContext()} />} />
    </Routes>,
    { route: "/p/project-1/incidents/i1" },
  );
}

describe("IncidentPage", () => {
  beforeEach(() => {
    vi.spyOn(api, "listInvestigations").mockResolvedValue({ investigations: [] });
  });

  it("separates what was observed, what changed, and what is not known", async () => {
    vi.spyOn(api, "getIncident").mockResolvedValue(detail);
    renderIncident();

    expect(
      await screen.findByRole("heading", { name: "Anomalies in payments-db and payment-api" }),
    ).toBeInTheDocument();

    const observed = screen.getByRole("heading", { name: "What was observed" }).closest("section");
    if (!observed) throw new Error("observed section not found");
    expect(observed).toHaveTextContent("Rose to 1,700 ms (usually about 120 ms)");
    expect(within(observed).getByRole("link", { name: "payment-api" })).toHaveAttribute(
      "href",
      "/p/project-1/services/payment-api?metric=http.server.duration.p95&range=1440",
    );

    const changed = screen.getByRole("heading", { name: "What changed" }).closest("section");
    if (!changed) throw new Error("changed section not found");
    expect(changed).toHaveTextContent("candidates to check, not confirmed causes");
    expect(changed).toHaveTextContent("payment-api 2.43.0");
    expect(changed).toHaveTextContent("30 s before the first anomaly");

    const unknown = screen.getByRole("heading", { name: "Not yet known" }).closest("section");
    expect(unknown).toHaveTextContent("The cause of this incident has not been confirmed.");
  });

  it("explains in the timeline why each anomaly was grouped", async () => {
    vi.spyOn(api, "getIncident").mockResolvedValue(detail);
    renderIncident();

    expect(
      await screen.findByText(
        "payment-api http.server.duration.p95 became abnormal. Grouped here because payment-api depends on payments-db.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("payment-api deployed version 2.43.0 (before the incident started)."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Declared dependencies among these services: payment-api depends on payments-db/),
    ).toBeInTheDocument();
  });

  it("points a merged incident at the one it was merged into", async () => {
    vi.spyOn(api, "getIncident").mockResolvedValue({
      ...detail,
      status: "merged",
      merged_into_id: "i9",
      anomalies: [],
      candidate_deployments: [],
    });
    renderIncident();

    expect(await screen.findByRole("link", { name: "Open the combined incident" })).toHaveAttribute(
      "href",
      "/p/project-1/incidents/i9",
    );
    expect(screen.getByText(/No deployments of these services were reported/)).toBeInTheDocument();
  });
});

describe("describeEvent", () => {
  it.each([
    [{ kind: "anomaly_attached", details: { service: "a", metric: "m", rule: "same_service" } }, "a m became abnormal. Grouped here because the incident already involves a."],
    [{ kind: "anomaly_ended", details: { service: "a", metric: "m", closed_reason: "recovered" } }, "a m returned to normal."],
    [{ kind: "anomaly_ended", details: { service: "a", metric: "m", closed_reason: "persisted" } }, "a m stayed at its new level long enough to be treated as normal."],
    [{ kind: "incident_resolved", details: {} }, "Incident resolved: every anomaly has ended."],
    [{ kind: "something_new", details: {} }, "something new"],
  ])("%j", (event, expected) => {
    expect(describeEvent({ ts: "2026-10-03T09:45:00Z", ...event })).toBe(expected);
  });
});

describe("IncidentsPage", () => {
  it("lists incidents with a link to each, and explains an empty list", async () => {
    const listIncidents = vi.spyOn(api, "listIncidents").mockResolvedValue({
      start: "2026-09-26T10:00:00Z",
      end: "2026-10-03T10:00:00Z",
      incidents: [detail],
    });
    const first = renderApp(<IncidentsPage current={projectContext("viewer")} />);

    const link = await screen.findByRole("link", { name: "Anomalies in payments-db and payment-api" });
    expect(link).toHaveAttribute("href", "/p/project-1/incidents/i1");
    const row = link.closest("tr");
    if (!row) throw new Error("incident row not found");
    expect(within(row).getByText("Ongoing")).toBeInTheDocument();
    expect(row).toHaveTextContent("2 (2 ongoing)");
    first.unmount();

    listIncidents.mockResolvedValue({ start: "", end: "", incidents: [] });
    renderApp(<IncidentsPage current={projectContext()} />);
    expect(
      await screen.findByRole("heading", { name: "No incidents in the last 7 days" }),
    ).toBeInTheDocument();
  });
});
