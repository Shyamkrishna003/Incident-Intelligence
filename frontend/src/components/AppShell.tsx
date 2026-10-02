import { NavLink, useNavigate } from "react-router-dom";

import { useAuth, type Account } from "../auth/AuthContext";
import type { Me, OrganizationMembership, Project } from "../lib/types";
import { roleAtLeast } from "../lib/types";

export interface ProjectContext {
  organization: OrganizationMembership;
  project: Project;
}

export function findProject(me: Me, projectId: string): ProjectContext | null {
  for (const organization of me.organizations) {
    const project = organization.projects.find((candidate) => candidate.id === projectId);
    if (project) return { organization, project };
  }
  return null;
}

const NAV_LINK = "rounded-md px-3 py-1.5 text-sm font-medium";

function navClass({ isActive }: { isActive: boolean }): string {
  return `${NAV_LINK} ${isActive ? "bg-wash text-ink" : "text-ink-2 hover:bg-wash"}`;
}

export function AppShell({
  me,
  account,
  current,
  children,
}: {
  me: Me;
  account: Account;
  current: ProjectContext;
  children: React.ReactNode;
}) {
  const { signOut } = useAuth();
  const navigate = useNavigate();
  const base = `/p/${current.project.id}`;

  return (
    <div className="min-h-screen">
      <header className="border-b border-hairline bg-surface">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
          <span className="text-sm font-semibold text-ink">Incident Intelligence</span>

          <label className="flex items-center gap-2 text-sm text-ink-2">
            <span className="sr-only">Project</span>
            <select
              className="max-w-[16rem] rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm text-ink"
              value={current.project.id}
              onChange={(event) => void navigate(`/p/${event.target.value}/services`)}
            >
              {me.organizations.map((organization) => (
                <optgroup key={organization.id} label={organization.name}>
                  {organization.projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          </label>

          <nav aria-label="Project" className="flex gap-1">
            <NavLink to={`${base}/services`} className={navClass}>
              Services
            </NavLink>
            <NavLink to={`${base}/deployments`} className={navClass}>
              Deployments
            </NavLink>
            {roleAtLeast(current.organization.role, "admin") && (
              <NavLink to={`${base}/api-keys`} className={navClass}>
                API keys
              </NavLink>
            )}
          </nav>

          <div className="ml-auto flex items-center gap-3 text-sm text-ink-2">
            <span className="hidden sm:inline">
              {account.displayName ?? account.email} · {current.organization.role}
            </span>
            <button type="button" className="text-link underline" onClick={() => void signOut()}>
              Sign out
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-6">{children}</main>
    </div>
  );
}
