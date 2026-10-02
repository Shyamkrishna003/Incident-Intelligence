/** The API contract, mirroring the backend's response schemas. */

export type Role = "viewer" | "member" | "admin" | "owner";

const ROLE_RANK: Record<Role, number> = { viewer: 0, member: 1, admin: 2, owner: 3 };

/** For showing or hiding controls only. The server enforces roles on every request. */
export function roleAtLeast(role: Role, minimum: Role): boolean {
  return ROLE_RANK[role] >= ROLE_RANK[minimum];
}

export interface User {
  id: string;
  email: string | null;
  email_verified: boolean;
  display_name: string | null;
}

export interface Project {
  id: string;
  slug: string;
  name: string;
}

export interface OrganizationMembership {
  id: string;
  slug: string;
  name: string;
  role: Role;
  projects: Project[];
}

export interface Me {
  user: User;
  organizations: OrganizationMembership[];
}

export interface OrganizationCreated {
  id: string;
  slug: string;
  name: string;
  role: Role;
}

export interface Service {
  id: string;
  name: string;
  created_at: string;
}

export interface Metric {
  name: string;
  unit: string | null;
  series_count: number;
}

export interface MetricPoint {
  timestamp: string;
  value: number;
}

export interface MetricSeries {
  attributes: Record<string, string>;
  unit: string | null;
  points: MetricPoint[];
}

export interface MetricRange {
  service: string;
  metric: string;
  start: string;
  end: string;
  truncated: boolean;
  series: MetricSeries[];
}

export type Severity = "trace" | "debug" | "info" | "warn" | "error" | "fatal";

export interface LogRecord {
  timestamp: string;
  severity: Severity;
  /** Stored exactly as the service sent it: untrusted text, rendered as text only. */
  message: string;
  attributes: Record<string, string>;
  trace_id: string | null;
}

export interface LogList {
  service: string;
  start: string;
  end: string;
  truncated: boolean;
  records: LogRecord[];
}

export interface Deployment {
  id: string;
  service: string;
  version: string;
  deployed_at: string;
  commit_sha: string | null;
  environment: string | null;
  deployed_by: string | null;
  description: string | null;
}

export interface DeploymentList {
  start: string;
  end: string;
  deployments: Deployment[];
}

export type AnomalySeverity = "low" | "medium" | "high" | "critical";

export interface Anomaly {
  id: string;
  service: string;
  metric: string;
  attributes: Record<string, string>;
  unit: string | null;
  detector: string;
  status: "open" | "closed";
  /** A documented rule on the score, not a probability. */
  severity: AnomalySeverity;
  direction: "above" | "below";
  started_at: string;
  detected_at: string;
  last_anomalous_at: string;
  ended_at: string | null;
  closed_reason: "recovered" | "persisted" | null;
  peak_value: number;
  peak_at: string;
  /** Distance from normal in units of the baseline's spread. Not a probability. */
  peak_score: number;
  /** What "normal" was when the anomaly opened. */
  baseline_center: number;
  baseline_spread: number;
  point_count: number;
  incident_id: string | null;
}

export interface AnomalyList {
  start: string;
  end: string;
  anomalies: Anomaly[];
}

export interface Incident {
  id: string;
  title: string;
  status: "open" | "resolved" | "merged";
  /** The worst severity among its anomalies. A rule, not a probability. */
  severity: AnomalySeverity;
  started_at: string;
  detected_at: string;
  last_activity_at: string;
  resolved_at: string | null;
  merged_into_id: string | null;
  services: string[];
  anomaly_count: number;
  open_anomaly_count: number;
}

export interface IncidentList {
  start: string;
  end: string;
  incidents: Incident[];
}

/** A deployment linked as a candidate for "what changed". Not a confirmed cause. */
export interface CandidateDeployment extends Deployment {
  timing: "before" | "during";
}

export interface TimelineEvent {
  ts: string;
  kind: string;
  details: Record<string, unknown>;
}

export interface Dependency {
  service: string;
  depends_on: string;
}

export interface IncidentDetail extends Incident {
  anomalies: Anomaly[];
  candidate_deployments: CandidateDeployment[];
  timeline: TimelineEvent[];
  dependencies: Dependency[];
}

export type Assessment = "supported" | "weak" | "untested" | "contradicted";

export interface ReportFact {
  statement: string;
  evidence: string[];
}

export interface ReportHypothesis {
  statement: string;
  /** How well the evidence supports it. A word, never a probability. */
  assessment: Assessment;
  supporting_evidence: string[];
  contradicting_evidence: string[];
  reasoning: string;
  review: string | null;
}

export interface InvestigationReport {
  summary: string;
  observed_facts: ReportFact[];
  hypotheses: ReportHypothesis[];
  unknowns: string[];
  next_steps: { action: string; expected_evidence: string }[];
}

export interface Investigation {
  id: string;
  incident_id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  provider: string | null;
  model: string | null;
  prompt_version: string | null;
  error: string | null;
}

export interface EvidenceItem {
  ref: string;
  kind: string;
  title: string;
  /** Exactly what the model was shown. */
  data: Record<string, unknown>;
}

export interface InvestigationDetail extends Investigation {
  report: InvestigationReport | null;
  validation_notes: string[];
  evidence: EvidenceItem[];
  steps: { kind: string; status: string; duration_ms: number; error: string | null }[];
  input_tokens: number;
  output_tokens: number;
}

export type ApiKeyScope = "ingest:write" | "telemetry:read";

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

/** Returned once, at creation. `key` cannot be retrieved again. */
export interface ApiKeyCreated extends ApiKey {
  key: string;
}
