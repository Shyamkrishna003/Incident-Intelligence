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
