import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { projectContext, renderApp } from "../test/render";
import { DeploymentsPage } from "./DeploymentsPage";

const empty = { start: "2026-09-25T10:00:00Z", end: "2026-10-02T10:00:00Z", deployments: [] };

describe("DeploymentsPage", () => {
  it("lists deployments with a link to each service", async () => {
    vi.spyOn(api, "listDeployments").mockResolvedValue({
      ...empty,
      deployments: [
        {
          id: "d1",
          service: "payment-api",
          version: "2.43.0",
          deployed_at: "2026-10-02T09:45:00Z",
          commit_sha: "4f7a9b2c0ffee",
          environment: "production",
          deployed_by: "ci",
          description: null,
        },
      ],
    });
    renderApp(<DeploymentsPage current={projectContext("viewer")} />);

    const row = (await screen.findByRole("link", { name: "payment-api" })).closest("tr");
    if (!row) throw new Error("the deployment's row was not found");
    expect(within(row).getByText("2.43.0")).toBeInTheDocument();
    expect(within(row).getByText("4f7a9b2")).toBeInTheDocument();
    expect(within(row).getByRole("link")).toHaveAttribute(
      "href",
      "/p/project-1/services/payment-api",
    );
  });

  it("explains how to report deployments when there are none", async () => {
    vi.spyOn(api, "listDeployments").mockResolvedValue(empty);
    renderApp(<DeploymentsPage current={projectContext()} />);

    expect(
      await screen.findByRole("heading", { name: "No deployments in the last 7 days" }),
    ).toBeInTheDocument();
    expect(screen.getByText("POST /v1/ingest/deployments")).toBeInTheDocument();
  });
});
