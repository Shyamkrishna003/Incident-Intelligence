import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { Suspense, lazy, useEffect } from "react";
import { BrowserRouter, Link, Navigate, Route, Routes, useParams } from "react-router-dom";

import { AuthProvider, useAuth, type Account } from "./auth/AuthContext";
import { AppShell, findProject } from "./components/AppShell";
import { Button, EmptyState, ErrorState, LoadingState } from "./components/ui";
import { useMe } from "./hooks/queries";
import { ApiError } from "./lib/api";
import type { Me } from "./lib/types";
import { AnomaliesPage } from "./pages/AnomaliesPage";
import { ApiKeysPage } from "./pages/ApiKeysPage";
import { DeploymentsPage } from "./pages/DeploymentsPage";
import { OnboardingPage } from "./pages/OnboardingPage";
import { ServicesPage } from "./pages/ServicesPage";
import { SignInPage } from "./pages/SignInPage";

// The chart library is large; load it only when a chart is opened.
const ServicePage = lazy(() =>
  import("./pages/ServicePage").then((module) => ({ default: module.ServicePage })),
);

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        // Retrying cannot fix a 4xx (not found, forbidden, invalid): fail fast on those.
        retry: (failures, error) =>
          !(error instanceof ApiError && error.status >= 400 && error.status < 500) &&
          failures < 2,
      },
    },
  });
}

const queryClient = createQueryClient();

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <BrowserRouter>
          <Root />
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  );
}

export function Root() {
  const { state } = useAuth();
  const client = useQueryClient();

  useEffect(() => {
    // Drop every cached response when the user signs out.
    if (state.status === "signedOut") client.clear();
  }, [state.status, client]);

  if (state.status === "loading") {
    return (
      <div className="mx-auto max-w-sm px-4 py-10">
        <LoadingState label="Loading…" />
      </div>
    );
  }
  if (state.status === "signedOut") return <SignInPage />;
  return <SignedIn account={state.account} />;
}

function SignedIn({ account }: { account: Account }) {
  const { signOut } = useAuth();
  const me = useMe();

  if (me.isPending) {
    return (
      <div className="mx-auto max-w-sm px-4 py-10">
        <LoadingState label="Loading your account…" />
      </div>
    );
  }
  if (me.isError) {
    return (
      <div className="mx-auto max-w-lg px-4 py-10">
        <ErrorState
          title="Could not load your account"
          error={me.error}
          onRetry={() => void me.refetch()}
        />
        <Button className="mt-3" onClick={() => void signOut()}>
          Sign out
        </Button>
      </div>
    );
  }

  const firstProject = me.data.organizations.flatMap((org) => org.projects)[0];
  return (
    <Routes>
      <Route
        path="/"
        element={
          firstProject ? (
            <Navigate to={`/p/${firstProject.id}/services`} replace />
          ) : (
            <OnboardingPage me={me.data} account={account} />
          )
        }
      />
      <Route path="/p/:projectId/*" element={<ProjectRoutes me={me.data} account={account} />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}

function ProjectRoutes({ me, account }: { me: Me; account: Account }) {
  const { projectId = "" } = useParams();
  const current = findProject(me, projectId);
  if (!current) return <NotFound />;

  return (
    <AppShell me={me} account={account} current={current}>
      <Routes>
        <Route path="services" element={<ServicesPage current={current} />} />
        <Route
          path="services/:service"
          element={
            <Suspense fallback={<LoadingState label="Loading…" />}>
              <ServicePage current={current} />
            </Suspense>
          }
        />
        <Route path="anomalies" element={<AnomaliesPage current={current} />} />
        <Route path="deployments" element={<DeploymentsPage current={current} />} />
        <Route path="api-keys" element={<ApiKeysPage current={current} />} />
        <Route path="*" element={<Navigate to="services" replace />} />
      </Routes>
    </AppShell>
  );
}

function NotFound() {
  return (
    <div className="mx-auto max-w-lg px-4 py-10">
      <EmptyState title="Page not found">
        <p>
          This page doesn't exist, or you don't have access to it.{" "}
          <Link className="text-link underline" to="/">
            Go to the start page
          </Link>
        </p>
      </EmptyState>
    </div>
  );
}
