import { useState, type SubmitEvent } from "react";
import { useNavigate } from "react-router-dom";

import { useAuth, type Account } from "../auth/AuthContext";
import { authErrorMessage } from "../auth/authErrors";
import { Button, Card, FormMessage, TextField } from "../components/ui";
import { useCreateProject, useCreateWorkspace } from "../hooks/queries";
import { describeError } from "../lib/api";
import { slugify } from "../lib/format";
import type { Me, OrganizationMembership } from "../lib/types";
import { roleAtLeast } from "../lib/types";

/** First-run setup. Shown until the user can reach at least one project. */
export function OnboardingPage({ me, account }: { me: Me; account: Account }) {
  const { signOut } = useAuth();
  // An organization without a project (for example setup was interrupted half-way).
  const unfinished = me.organizations.find(
    (org) => org.projects.length === 0 && roleAtLeast(org.role, "admin"),
  );

  return (
    <main className="mx-auto max-w-lg px-4 py-10">
      <h1 className="text-2xl font-semibold text-ink">Welcome</h1>
      <p className="mb-6 mt-1 text-sm text-ink-2">
        Signed in as {account.email ?? "your account"}.{" "}
        <button type="button" className="text-link underline" onClick={() => void signOut()}>
          Sign out
        </button>
      </p>
      {unfinished ? (
        <ProjectForm organization={unfinished} />
      ) : !account.emailVerified ? (
        <VerifyEmail email={account.email} />
      ) : (
        <WorkspaceForm />
      )}
    </main>
  );
}

function VerifyEmail({ email }: { email: string | null }) {
  const { sendVerificationEmail, refreshAccount } = useAuth();
  const [message, setMessage] = useState<{ tone: "error" | "info"; text: string } | null>(null);
  const [busy, setBusy] = useState<"send" | "check" | null>(null);

  async function send() {
    setBusy("send");
    try {
      await sendVerificationEmail();
      setMessage({ tone: "info", text: "Verification email sent. Check your inbox." });
    } catch (error) {
      setMessage({ tone: "error", text: authErrorMessage(error) ?? "Could not send the email." });
    } finally {
      setBusy(null);
    }
  }

  async function check() {
    setBusy("check");
    try {
      await refreshAccount();
      // If verified, the parent re-renders with the next step and this panel unmounts.
      setMessage({ tone: "info", text: "Not verified yet. Open the link in the email first." });
    } catch (error) {
      setMessage({ tone: "error", text: authErrorMessage(error) ?? "Could not check." });
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card>
      <h2 className="text-base font-semibold text-ink">Verify your email</h2>
      <p className="mt-2 text-sm text-ink-2">
        Before you can create an organization, confirm that {email ?? "your email address"} is
        yours. Open the link in the verification email, then come back here.
      </p>
      <div className="mt-4 flex flex-wrap gap-2">
        <Button variant="primary" busy={busy === "check"} disabled={busy !== null} onClick={() => void check()}>
          I've verified my email
        </Button>
        <Button busy={busy === "send"} disabled={busy !== null} onClick={() => void send()}>
          Send the email again
        </Button>
      </div>
      <div className="mt-3">
        <FormMessage tone={message?.tone ?? "info"}>{message?.text}</FormMessage>
      </div>
    </Card>
  );
}

function WorkspaceForm() {
  const navigate = useNavigate();
  const create = useCreateWorkspace();
  const [organizationName, setOrganizationName] = useState("");
  const [projectName, setProjectName] = useState("");
  const organizationSlug = slugify(organizationName);
  const projectSlug = slugify(projectName);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    create.mutate(
      {
        organization: { slug: organizationSlug, name: organizationName.trim() },
        project: { slug: projectSlug, name: projectName.trim() },
      },
      { onSuccess: ({ project }) => void navigate(`/p/${project.id}/services`) },
    );
  }

  return (
    <Card>
      <h2 className="text-base font-semibold text-ink">Create your organization</h2>
      <p className="mt-2 text-sm text-ink-2">
        An organization is your company or team. A project groups the services you monitor
        together, for example one per product or environment.
      </p>
      <form onSubmit={submit} className="mt-4 flex flex-col gap-4" noValidate>
        <TextField
          label="Organization name"
          required
          value={organizationName}
          onChange={(event) => { setOrganizationName(event.target.value); }}
          hint={organizationSlug ? `Identifier: ${organizationSlug}` : "For example: Acme"}
        />
        <TextField
          label="First project name"
          required
          value={projectName}
          onChange={(event) => { setProjectName(event.target.value); }}
          hint={projectSlug ? `Identifier: ${projectSlug}` : "For example: Payments"}
        />
        <FormMessage tone="error">{create.isError ? describeError(create.error) : null}</FormMessage>
        <Button
          type="submit"
          variant="primary"
          busy={create.isPending}
          disabled={!organizationSlug || !projectSlug}
        >
          Create organization
        </Button>
      </form>
    </Card>
  );
}

function ProjectForm({ organization }: { organization: OrganizationMembership }) {
  const navigate = useNavigate();
  const create = useCreateProject(organization.id);
  const [name, setName] = useState("");
  const slug = slugify(name);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    create.mutate(
      { slug, name: name.trim() },
      { onSuccess: (project) => void navigate(`/p/${project.id}/services`) },
    );
  }

  return (
    <Card>
      <h2 className="text-base font-semibold text-ink">Create a project in {organization.name}</h2>
      <p className="mt-2 text-sm text-ink-2">
        A project groups the services you monitor together, for example one per product or
        environment.
      </p>
      <form onSubmit={submit} className="mt-4 flex flex-col gap-4" noValidate>
        <TextField
          label="Project name"
          required
          value={name}
          onChange={(event) => { setName(event.target.value); }}
          hint={slug ? `Identifier: ${slug}` : "For example: Payments"}
        />
        <FormMessage tone="error">{create.isError ? describeError(create.error) : null}</FormMessage>
        <Button type="submit" variant="primary" busy={create.isPending} disabled={!slug}>
          Create project
        </Button>
      </form>
    </Card>
  );
}
