import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useAuth } from "../auth/AuthContext";
import { api } from "../lib/api";
import type { ApiKeyScope, Severity } from "../lib/types";

/** Keys are namespaced by user id so one person never sees another's cached data. */
function useUid(): string {
  const { state } = useAuth();
  return state.status === "signedIn" ? state.account.uid : "anonymous";
}

export function useMe() {
  const uid = useUid();
  return useQuery({ queryKey: [uid, "me"], queryFn: api.me, enabled: uid !== "anonymous" });
}

export function useServices(projectId: string) {
  const uid = useUid();
  return useQuery({
    queryKey: [uid, "services", projectId],
    queryFn: () => api.listServices(projectId),
    select: (data) => data.services,
    // New services appear when they first send data, which happens outside this app:
    // refresh whenever the page is opened and while it stays open.
    staleTime: 0,
    refetchInterval: 30_000,
  });
}

export function useMetrics(projectId: string, service: string) {
  const uid = useUid();
  return useQuery({
    queryKey: [uid, "metrics", projectId, service],
    queryFn: () => api.listMetrics(projectId, service),
    select: (data) => data.metrics,
    staleTime: 0,
  });
}

export function useMetricRange(
  projectId: string,
  service: string,
  metric: string | null,
  rangeMinutes: number,
) {
  const uid = useUid();
  return useQuery({
    queryKey: [uid, "metric-range", projectId, service, metric, rangeMinutes],
    queryFn: () => {
      const end = new Date();
      const start = new Date(end.getTime() - rangeMinutes * 60_000);
      return api.metricRange(projectId, service, metric ?? "", { start, end });
    },
    enabled: metric !== null,
    refetchInterval: 30_000,
    // While a new range loads, keep showing the previous chart instead of a blank frame.
    placeholderData: keepPreviousData,
  });
}

function lastMinutes(minutes: number): { start: Date; end: Date } {
  const end = new Date();
  return { start: new Date(end.getTime() - minutes * 60_000), end };
}

export function useLogs(
  projectId: string,
  service: string,
  rangeMinutes: number,
  severity: Severity | null,
  search: string,
) {
  const uid = useUid();
  return useQuery({
    queryKey: [uid, "logs", projectId, service, rangeMinutes, severity, search],
    queryFn: () =>
      api.listLogs(projectId, service, { ...lastMinutes(rangeMinutes), severity, search }),
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  });
}

/** Deployments in the last `rangeMinutes`, for the whole project or one service. */
export function useDeployments(projectId: string, rangeMinutes: number, service?: string) {
  const uid = useUid();
  return useQuery({
    queryKey: [uid, "deployments", projectId, rangeMinutes, service ?? null],
    queryFn: () =>
      api.listDeployments(projectId, {
        ...lastMinutes(rangeMinutes),
        ...(service ? { service } : {}),
      }),
    select: (data) => data.deployments,
    staleTime: 0,
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  });
}

/** Anomalies overlapping the last `rangeMinutes`. */
export function useAnomalies(
  projectId: string,
  rangeMinutes: number,
  filter: { status?: "open" | "closed"; service?: string; metric?: string | null } = {},
) {
  const uid = useUid();
  const { status, service, metric } = filter;
  return useQuery({
    queryKey: [uid, "anomalies", projectId, rangeMinutes, status ?? null, service ?? null, metric ?? null],
    queryFn: () =>
      api.listAnomalies(projectId, {
        ...lastMinutes(rangeMinutes),
        ...(status ? { status } : {}),
        ...(service ? { service } : {}),
        ...(metric ? { metric } : {}),
      }),
    select: (data) => data.anomalies,
    enabled: metric !== null,
    staleTime: 0,
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  });
}

/** Incidents overlapping the last `rangeMinutes`. */
export function useIncidents(projectId: string, rangeMinutes: number, onlyOpen: boolean) {
  const uid = useUid();
  return useQuery({
    queryKey: [uid, "incidents", projectId, rangeMinutes, onlyOpen],
    queryFn: () =>
      api.listIncidents(projectId, {
        ...lastMinutes(rangeMinutes),
        ...(onlyOpen ? { status: "open" as const } : {}),
      }),
    select: (data) => data.incidents,
    staleTime: 0,
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  });
}

export function useIncident(projectId: string, incidentId: string) {
  const uid = useUid();
  return useQuery({
    queryKey: [uid, "incident", projectId, incidentId],
    queryFn: () => api.getIncident(projectId, incidentId),
    staleTime: 0,
    refetchInterval: 30_000,
  });
}

export function useApiKeys(projectId: string, enabled: boolean) {
  const uid = useUid();
  return useQuery({
    queryKey: [uid, "api-keys", projectId],
    queryFn: () => api.listApiKeys(projectId),
    select: (data) => data.api_keys,
    enabled,
  });
}

export function useCreateApiKey(projectId: string) {
  const uid = useUid();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; scopes: ApiKeyScope[]; expires_in_days: number | null }) =>
      api.createApiKey(projectId, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [uid, "api-keys", projectId] }),
  });
}

export function useRevokeApiKey(projectId: string) {
  const uid = useUid();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (prefix: string) => api.revokeApiKey(projectId, prefix),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [uid, "api-keys", projectId] }),
  });
}

/** Creates an organization and its first project, then refreshes the account overview. */
export function useCreateWorkspace() {
  const uid = useUid();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      organization: { slug: string; name: string };
      project: { slug: string; name: string };
    }) => {
      const organization = await api.createOrganization(input.organization);
      const project = await api.createProject(organization.id, input.project);
      return { organization, project };
    },
    // Also on error: the organization may exist even if creating the project failed.
    onSettled: () => queryClient.invalidateQueries({ queryKey: [uid, "me"] }),
  });
}

export function useCreateProject(organizationId: string) {
  const uid = useUid();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { slug: string; name: string }) => api.createProject(organizationId, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [uid, "me"] }),
  });
}
