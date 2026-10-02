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
  });

  it("says so when the range has no points", () => {
    render(<MetricChart data={range([series("eu", [])])} stale={false} />);

    expect(screen.getByRole("heading", { name: "No data in this time range" })).toBeInTheDocument();
  });

  it("is honest about series and points it is not showing", () => {
    const many = Array.from({ length: 10 }, (_, index) => series(`r${index}`, [index]));
    render(<MetricChart data={range(many, true)} stale={false} />);

    expect(screen.getByText("Showing the first 8 of 10 series.")).toBeInTheDocument();
    expect(screen.getByText(/more points than can be shown/)).toBeInTheDocument();
  });
});
