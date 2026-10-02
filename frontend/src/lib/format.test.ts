import { describe, expect, it } from "vitest";

import { pivotSeries } from "./chart";
import { formatDuration, seriesLabel, slugify } from "./format";
import { roleAtLeast } from "./types";

describe("slugify", () => {
  it.each([
    ["Acme Inc.", "acme-inc"],
    ["  Payments & Billing  ", "payments-billing"],
    ["---", ""],
    ["a".repeat(80), "a".repeat(63)],
  ])("%s -> %s", (input, expected) => {
    expect(slugify(input)).toBe(expected);
  });
});

describe("seriesLabel", () => {
  it("is stable regardless of key order and names the empty set", () => {
    expect(seriesLabel({ region: "eu", host: "a" })).toBe("host=a, region=eu");
    expect(seriesLabel({})).toBe("all");
  });
});

describe("roleAtLeast", () => {
  it("orders roles", () => {
    expect(roleAtLeast("owner", "admin")).toBe(true);
    expect(roleAtLeast("admin", "admin")).toBe(true);
    expect(roleAtLeast("member", "admin")).toBe(false);
    expect(roleAtLeast("viewer", "viewer")).toBe(true);
  });
});

describe("pivotSeries", () => {
  it("merges series by timestamp, sorted, leaving gaps where a series has no point", () => {
    const rows = pivotSeries([
      {
        attributes: { region: "eu" },
        unit: "ms",
        points: [
          { timestamp: "2026-10-02T10:00:10Z", value: 2 },
          { timestamp: "2026-10-02T10:00:00Z", value: 1 },
        ],
      },
      {
        attributes: { region: "us" },
        unit: "ms",
        points: [{ timestamp: "2026-10-02T10:00:10Z", value: 9 }],
      },
    ]);

    expect(rows).toEqual([
      { t: Date.parse("2026-10-02T10:00:00Z"), s0: 1 },
      { t: Date.parse("2026-10-02T10:00:10Z"), s0: 2, s1: 9 },
    ]);
  });
});

describe("formatDuration", () => {
  it.each([
    [15_000, "15 s"],
    [59_400, "59 s"],
    [5 * 60_000, "5 min"],
    [125 * 60_000, "2 h 5 min"],
    [-5_000, "0 s"],
  ])("%d ms -> %s", (elapsed, expected) => {
    const start = "2026-10-03T10:00:00Z";
    expect(formatDuration(start, Date.parse(start) + elapsed)).toBe(expected);
  });
});
