import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MetricRange, MetricSeries } from "../lib/types";
import { MetricChart } from "./MetricChart";

function series(region: string, values: number[]): MetricSeries {
  return {
    attributes: { region },
    unit: "ms",
    points: values.map((value, index) => ({
      timestamp: new Date(Date.parse("2026-10-02T10:00:00Z") + index * 10_000).toISOString(),
      value,
    })),
  };
}

function range(items: MetricSeries[], truncated = false): MetricRange {
  return {
    service: "payment-api",
    metric: "latency",
    start: "2026-10-02T10:00:00Z",
    end: "2026-10-02T11:00:00Z",
    truncated,
    series: items,
  };
}

describe("MetricChart", () => {
  it("names every series in a legend and offers the values as a table", () => {
    render(<MetricChart data={range([series("eu", [100, 120]), series("us", [90])])} stale={false} />);

    const legend = screen.getByRole("list", { name: "Series" });
    expect(within(legend).getByText("region=eu")).toBeInTheDocument();
    expect(within(legend).getByText("region=us")).toBeInTheDocument();

    const table = screen.getByRole("table", { hidden: true });
    expect(within(table).getByText("120")).toBeInTheDocument();
    expect(within(table).getByText("90")).toBeInTheDocument();
    expect(screen.getByText("Values in ms.")).toBeInTheDocument();
  });

  it("needs no legend for a single series", () => {
    render(<MetricChart data={range([series("eu", [1, 2])])} stale={false} />);

    expect(screen.queryByRole("list", { name: "Series" })).not.toBeInTheDocument();
    // Its attributes are still stated, in the caption.
    expect(screen.getByText("Series: region=eu.")).toBeInTheDocument();
  });

  it("says so when the range has no points", () => {
    render(<MetricChart data={range([series("eu", [])])} stale={false} />);

    expect(screen.getByRole("heading", { name: "No data in this time range" })).toBeInTheDocument();
  });

  it("names the deployments it marks, and ignores ones outside the range", () => {
    const deployment = (version: string, deployed_at: string) => ({
      id: version,
      service: "payment-api",
      version,
      deployed_at,
      commit_sha: null,
      environment: null,
      deployed_by: null,
      description: null,
    });
    render(
      <MetricChart
        data={range([series("eu", [1, 2])])}
        stale={false}
        deployments={[
          deployment("2.43.0", "2026-10-02T10:30:00Z"),
          deployment("1.0.0", "2026-10-01T10:30:00Z"),
        ]}
      />,
    );

    const caption = screen.getByText(/Vertical lines mark deployments/);
    expect(caption).toHaveTextContent("2.43.0");
    expect(caption).not.toHaveTextContent("1.0.0");
  });

  it("describes the anomaly periods it shades", () => {
    render(
      <MetricChart
        data={range([series("eu", [1, 2])])}
        stale={false}
        anomalies={[
          {
            id: "a1",
            service: "payment-api",
            metric: "latency",
            attributes: {},
            unit: "ms",
            detector: "robust_zscore",
            status: "open",
            severity: "high",
            direction: "above",
            started_at: "2026-10-02T10:20:00Z",
            detected_at: "2026-10-02T10:20:15Z",
            last_anomalous_at: "2026-10-02T10:40:00Z",
            ended_at: null,
            closed_reason: null,
            peak_value: 900,
            peak_at: "2026-10-02T10:40:00Z",
            peak_score: 40,
            baseline_center: 100,
            baseline_spread: 5,
            point_count: 80,
          },
        ]}
      />,
    );

    const caption = screen.getByText(/Shaded: anomaly detected/);
    expect(caption).toHaveTextContent("now (ongoing), high");
  });

  it("is honest about series and points it is not showing", () => {
    const many = Array.from({ length: 10 }, (_, index) => series(`r${index}`, [index]));
    render(<MetricChart data={range(many, true)} stale={false} />);

    expect(screen.getByText("Showing the first 8 of 10 series.")).toBeInTheDocument();
    expect(screen.getByText(/more points than can be shown/)).toBeInTheDocument();
  });
});
