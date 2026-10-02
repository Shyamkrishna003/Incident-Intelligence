/** Typed client for the backend. Every request carries the signed-in user's ID token. */

import type {
  ApiKey,
  ApiKeyCreated,
  AnomalyList,
  ApiKeyScope,
  DeploymentList,
  IncidentDetail,
  IncidentList,
  LogList,
  Me,
  Metric,
  MetricRange,
  OrganizationCreated,
  Project,
  Service,
  Severity,
} from "./types";

export interface ErrorDetail {
  loc: (string | number)[];
  msg: string;
  type: string;
}

/** A non-2xx response, carrying the server's error envelope. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly details: ErrorDetail[];
  readonly retryAfterSeconds: number | null;

  constructor(init: {
    status: number;
    code: string;
    message: string;
    requestId?: string | null;
    details?: ErrorDetail[];
    retryAfterSeconds?: number | null;
  }) {
    super(init.message);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.requestId = init.requestId ?? null;
    this.details = init.details ?? [];
    this.retryAfterSeconds = init.retryAfterSeconds ?? null;
  }
}

/** Returns the current ID token; `forceRefresh` asks the identity provider for a new one. */
export type TokenProvider = (forceRefresh: boolean) => Promise<string | null>;

let tokenProvider: TokenProvider = () => Promise.resolve(null);

export function setTokenProvider(provider: TokenProvider): void {
  tokenProvider = provider;
}

interface ErrorEnvelope {
  error?: {
    code?: unknown;
    message?: unknown;
    request_id?: unknown;
    details?: unknown;
  };
}

async function toApiError(response: Response): Promise<ApiError> {
  let envelope: ErrorEnvelope = {};
  try {
    envelope = (await response.json()) as ErrorEnvelope;
  } catch {
    // Not JSON (for example a proxy error page): fall back to the status alone.
  }
  const error = envelope.error ?? {};
  const retryAfter = Number(response.headers.get("Retry-After"));
  return new ApiError({
    status: response.status,
    code: typeof error.code === "string" ? error.code : "http_error",
    message:
      typeof error.message === "string" ? error.message : `Request failed (${response.status}).`,
    requestId:
      typeof error.request_id === "string"
        ? error.request_id
        : response.headers.get("X-Request-ID"),
    details: Array.isArray(error.details) ? (error.details as ErrorDetail[]) : [],
    retryAfterSeconds: Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
  });
}

async function send(path: string, init: RequestInit, forceRefresh: boolean): Promise<Response> {
  const token = await tokenProvider(forceRefresh);
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  try {
    return await fetch(path, { ...init, headers });
  } catch {
    throw new ApiError({
      status: 0,
      code: "network_error",
      message: "Could not reach the server. Check your connection and try again.",
    });
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response = await send(path, init, false);
  if (response.status === 401) {
    // The cached token may have just expired: retry once with a fresh one.
    response = await send(path, init, true);
  }
  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const json = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });
const seg = encodeURIComponent;

export const api = {
  me: () => request<Me>("/v1/me"),

  createOrganization: (body: { slug: string; name: string }) =>
    request<OrganizationCreated>("/v1/organizations", json(body)),

  createProject: (organizationId: string, body: { slug: string; name: string }) =>
    request<Project>(`/v1/organizations/${seg(organizationId)}/projects`, json(body)),

  listServices: (projectId: string) =>
    request<{ services: Service[] }>(`/v1/projects/${seg(projectId)}/services`),

  listMetrics: (projectId: string, service: string) =>
    request<{ service: string; metrics: Metric[] }>(
      `/v1/projects/${seg(projectId)}/services/${seg(service)}/metrics`,
    ),

  metricRange: (
    projectId: string,
    service: string,
    metric: string,
    range: { start: Date; end: Date },
  ) => {
    const query = new URLSearchParams({
      start: range.start.toISOString(),
      end: range.end.toISOString(),
      limit: "5000",
    });
    return request<MetricRange>(
      `/v1/projects/${seg(projectId)}/services/${seg(service)}/metrics/${seg(metric)}?${query.toString()}`,
    );
  },

  listLogs: (
    projectId: string,
    service: string,
    filter: { start: Date; end: Date; severity: Severity | null; search: string },
  ) => {
    const query = new URLSearchParams({
      start: filter.start.toISOString(),
      end: filter.end.toISOString(),
      limit: "200",
    });
    if (filter.severity) query.set("severity", filter.severity);
    if (filter.search) query.set("q", filter.search);
    return request<LogList>(
      `/v1/projects/${seg(projectId)}/services/${seg(service)}/logs?${query.toString()}`,
    );
  },

  listDeployments: (
    projectId: string,
    filter: { start: Date; end: Date; service?: string },
  ) => {
    const query = new URLSearchParams({
      start: filter.start.toISOString(),
      end: filter.end.toISOString(),
    });
    if (filter.service) query.set("service", filter.service);
    return request<DeploymentList>(
      `/v1/projects/${seg(projectId)}/deployments?${query.toString()}`,
    );
  },

  listAnomalies: (
    projectId: string,
    filter: { start: Date; end: Date; status?: "open" | "closed"; service?: string; metric?: string },
  ) => {
    const query = new URLSearchParams({
      start: filter.start.toISOString(),
      end: filter.end.toISOString(),
    });
    if (filter.status) query.set("status", filter.status);
    if (filter.service) query.set("service", filter.service);
    if (filter.metric) query.set("metric", filter.metric);
    return request<AnomalyList>(`/v1/projects/${seg(projectId)}/anomalies?${query.toString()}`);
  },

  listIncidents: (
    projectId: string,
    filter: { start: Date; end: Date; status?: "open" | "resolved" },
  ) => {
    const query = new URLSearchParams({
      start: filter.start.toISOString(),
      end: filter.end.toISOString(),
    });
    if (filter.status) query.set("status", filter.status);
    return request<IncidentList>(`/v1/projects/${seg(projectId)}/incidents?${query.toString()}`);
  },

  getIncident: (projectId: string, incidentId: string) =>
    request<IncidentDetail>(`/v1/projects/${seg(projectId)}/incidents/${seg(incidentId)}`),

  listApiKeys: (projectId: string) =>
    request<{ api_keys: ApiKey[] }>(`/v1/projects/${seg(projectId)}/api-keys`),

  createApiKey: (
    projectId: string,
    body: { name: string; scopes: ApiKeyScope[]; expires_in_days: number | null },
  ) => request<ApiKeyCreated>(`/v1/projects/${seg(projectId)}/api-keys`, json(body)),

  revokeApiKey: (projectId: string, prefix: string) =>
    request<undefined>(`/v1/projects/${seg(projectId)}/api-keys/${seg(prefix)}`, {
      method: "DELETE",
    }),
};

/** A message suitable for showing to the user, with the request id for support. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429 && error.retryAfterSeconds !== null) {
      return `Too many requests. Try again in ${error.retryAfterSeconds} seconds.`;
    }
    const first = error.details[0];
    const detail = first ? ` ${first.msg}` : "";
    return `${error.message}${detail}`;
  }
  return error instanceof Error ? error.message : "Something went wrong.";
}
