import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../lib/api";
import type { LogList, LogRecord } from "../lib/types";
import { renderApp } from "../test/render";
import { LogsPanel } from "./LogsPanel";

function record(message: string, severity: LogRecord["severity"] = "info"): LogRecord {
  return { timestamp: "2026-10-02T10:00:00Z", severity, message, attributes: {}, trace_id: null };
}

function page(records: LogRecord[], truncated = false): LogList {
  return {
    service: "payment-api",
    start: "2026-10-02T09:00:00Z",
    end: "2026-10-02T10:00:00Z",
    truncated,
    records,
  };
}

const panel = <LogsPanel projectId="project-1" service="payment-api" rangeMinutes={60} />;

describe("LogsPanel", () => {
  it("shows records with their severity spelled out", async () => {
    vi.spyOn(api, "listLogs").mockResolvedValue(
      page([record("connection pool exhausted", "error"), record("handled 200 requests")]),
    );
    renderApp(panel);

    expect(await screen.findByText("connection pool exhausted")).toBeInTheDocument();
    expect(screen.getByText("error")).toBeInTheDocument();
    expect(screen.getByText("2 records, newest first.")).toBeInTheDocument();
  });

  it("renders log content as text, never as markup", async () => {
    const hostile = '<img src=x onerror="alert(1)"> <b>bold</b>';
    vi.spyOn(api, "listLogs").mockResolvedValue(page([record(hostile)]));
    const { container } = renderApp(panel);

    expect(await screen.findByText(hostile)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("td b")).toBeNull();
  });

  it("asks the server for the chosen severity and search text", async () => {
    const listLogs = vi.spyOn(api, "listLogs").mockResolvedValue(page([record("x")]));
    renderApp(panel);
    await screen.findByText("x");

    await userEvent.selectOptions(screen.getByLabelText("Severity"), "Errors only");
    await userEvent.type(screen.getByLabelText("Message contains"), "pool");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() => {
      expect(listLogs).toHaveBeenLastCalledWith(
        "project-1",
        "payment-api",
        expect.objectContaining({ severity: "error", search: "pool" }),
      );
    });
  });

  it("explains an empty result differently with and without filters", async () => {
    vi.spyOn(api, "listLogs").mockResolvedValue(page([]));
    renderApp(panel);

    expect(await screen.findByText(/sent no logs in this time range/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Severity"), "Errors only");
    expect(await screen.findByText(/Nothing matches these filters/)).toBeInTheDocument();
  });

  it("says when older matching records are not shown", async () => {
    vi.spyOn(api, "listLogs").mockResolvedValue(page([record("a"), record("b")], true));
    renderApp(panel);

    expect(await screen.findByText(/Showing the newest 2 matching records/)).toBeInTheDocument();
  });

  it("shows an error with a retry", async () => {
    vi.spyOn(api, "listLogs")
      .mockRejectedValueOnce(
        new ApiError({ status: 503, code: "service_unavailable", message: "Temporarily unavailable." }),
      )
      .mockResolvedValue(page([record("recovered")]));
    renderApp(panel);

    expect(await screen.findByRole("alert")).toHaveTextContent("Temporarily unavailable.");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("recovered")).toBeInTheDocument();
  });
});
