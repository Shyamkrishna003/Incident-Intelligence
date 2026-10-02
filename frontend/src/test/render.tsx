import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

import { AuthContext, type Account, type AuthContextValue } from "../auth/AuthContext";
import type { ProjectContext } from "../components/AppShell";
import type { Me, Role } from "../lib/types";

export const account: Account = {
  uid: "uid-1",
  email: "priya@example.test",
  emailVerified: true,
  displayName: "Priya",
};

export function fakeAuth(overrides: Partial<AuthContextValue> = {}): AuthContextValue {
  return {
    state: { status: "signedIn", account },
    linkPending: false,
    signInWithEmail: vi.fn(() => Promise.resolve()),
    signUpWithEmail: vi.fn(() => Promise.resolve()),
    signInWithProvider: vi.fn(() => Promise.resolve("ok" as const)),
    sendVerificationEmail: vi.fn(() => Promise.resolve()),
    refreshAccount: vi.fn(() => Promise.resolve()),
    signOut: vi.fn(() => Promise.resolve()),
    ...overrides,
  };
}

/** An error shaped like the ones Firebase Authentication throws. */
export function firebaseError(code: string): Error {
  return Object.assign(new Error(code), { code });
}

export function projectContext(role: Role = "owner"): ProjectContext {
  const project = { id: "project-1", slug: "payments", name: "Payments" };
  return {
    project,
    organization: { id: "org-1", slug: "acme", name: "Acme", role, projects: [project] },
  };
}

export function meWith(role: Role | null): Me {
  return {
    user: { id: "user-1", email: account.email, email_verified: true, display_name: "Priya" },
    organizations: role === null ? [] : [projectContext(role).organization],
  };
}

export function renderApp(
  ui: ReactElement,
  options: { auth?: AuthContextValue; route?: string } = {},
): RenderResult {
  // No retries and no caching between tests: failures surface immediately.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <AuthContext.Provider value={options.auth ?? fakeAuth()}>
        <MemoryRouter initialEntries={[options.route ?? "/"]}>{ui}</MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  );
}
