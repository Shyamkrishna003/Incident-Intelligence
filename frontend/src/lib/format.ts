const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const timeOnly = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const timeWithSeconds = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});
const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

export function formatDateTime(iso: string | null): string {
  return iso ? dateTime.format(new Date(iso)) : "—";
}

export function formatTime(epochMs: number): string {
  return timeOnly.format(new Date(epochMs));
}

export function formatTimeWithSeconds(epochMs: number): string {
  return timeWithSeconds.format(new Date(epochMs));
}

export function formatNumber(value: number): string {
  return number.format(value);
}

export function formatValue(value: number, unit: string | null): string {
  return unit ? `${formatNumber(value)} ${unit}` : formatNumber(value);
}

/** "region=eu, host=a" for a series' attributes; "all" when it has none. */
export function seriesLabel(attributes: Record<string, string>): string {
  const entries = Object.entries(attributes).sort(([a], [b]) => a.localeCompare(b));
  return entries.length === 0 ? "all" : entries.map(([k, v]) => `${k}=${v}`).join(", ");
}

/** Turns a display name into a URL-safe slug the API accepts. */
export function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63)
    .replace(/-+$/g, "");
}
