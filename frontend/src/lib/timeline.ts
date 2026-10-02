import type { TimelineEvent } from "./types";

function text(details: Record<string, unknown>, key: string): string {
  const value = details[key];
  return typeof value === "string" ? value : "";
}

function signal(details: Record<string, unknown>): string {
  return `${text(details, "service")} ${text(details, "metric")}`.trim();
}

/** Why an anomaly was placed in this incident, in words. */
function reason(details: Record<string, unknown>): string {
  const rule = text(details, "rule");
  if (rule === "same_service") {
    return `Grouped here because the incident already involves ${text(details, "service")}.`;
  }
  if (rule === "dependency") {
    const dependency = details.dependency;
    if (typeof dependency === "object" && dependency !== null) {
      const edge = dependency as Record<string, unknown>;
      return `Grouped here because ${text(edge, "service")} depends on ${text(edge, "depends_on")}.`;
    }
    return "Grouped here because of a declared dependency.";
  }
  return "";
}

/**
 * One timeline entry as a sentence. Every entry is something the system recorded, with the
 * rule behind any grouping decision; none of them claims a cause.
 */
export function describeEvent(event: TimelineEvent): string {
  const { kind, details } = event;
  switch (kind) {
    case "incident_opened":
      return `Incident opened: ${signal(details)} became abnormal. No related incident was open.`;
    case "anomaly_attached":
      return `${signal(details)} became abnormal. ${reason(details)}`.trim();
    case "anomaly_ended":
      return text(details, "closed_reason") === "persisted"
        ? `${signal(details)} stayed at its new level long enough to be treated as normal.`
        : `${signal(details)} returned to normal.`;
    case "deployment_linked":
      return `${text(details, "service")} deployed version ${text(details, "version")} (${
        text(details, "timing") === "during" ? "during the incident" : "before the incident started"
      }).`;
    case "incident_resolved":
      return "Incident resolved: every anomaly has ended.";
    case "incident_reopened":
      return `Incident reopened: ${signal(details)} became abnormal.`;
    case "incidents_merged":
      return `Another incident was merged into this one, because ${signal(details)} is related to both.`;
    case "merged_into":
      return "This incident was merged into another one.";
    default:
      return kind.replaceAll("_", " ");
  }
}
