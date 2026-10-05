import { useState, type SubmitEvent } from "react";

import type { ProjectContext } from "../components/AppShell";
import {
  Button,
  Card,
  EmptyState,
  ErrorState,
  FormMessage,
  LoadingState,
  PageHeader,
  TextField,
} from "../components/ui";
import {
  useConnectGitHub,
  useDisconnectGitHub,
  useGitHub,
  useServices,
  useSetGitHubRepositories,
} from "../hooks/queries";
import { ApiError, describeError } from "../lib/api";
import { formatDateTime } from "../lib/format";
import type { GitHubStatus, RepositoryMapping } from "../lib/types";
import { roleAtLeast } from "../lib/types";

const REPOSITORY = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9._-]{1,100}$/;

export function GitHubPage({ current }: { current: ProjectContext }) {
  const allowed = roleAtLeast(current.organization.role, "admin");
  const github = useGitHub(current.project.id, allowed);

  if (!allowed) {
    return (
      <>
        <PageHeader title="GitHub" />
        <EmptyState title="You don't have access to GitHub settings">
          <p>Only organization admins and owners can connect GitHub.</p>
        </EmptyState>
      </>
    );
  }

  return (
    <>
      <PageHeader title="GitHub" />
      <p className="mb-5 max-w-prose text-sm text-ink-2">
        Connect GitHub so an AI investigation of <strong>{current.project.name}</strong> can see
        what a deployment changed. It reads commit messages and the names of changed files for
        deployments linked to an incident. File contents are never read or sent to the AI model.
      </p>

      {github.isPending ? (
        <LoadingState label="Loading GitHub settings…" />
      ) : github.isError ? (
        <ErrorState
          title="Could not load GitHub settings"
          error={github.error}
          onRetry={() => void github.refetch()}
        />
      ) : !github.data.available ? (
        <EmptyState title="GitHub integration is not set up on this server">
          <p>
            The server has no encryption key for stored tokens. Whoever runs it needs to set
            SECRETS_ENCRYPTION_KEY and restart the API and the investigation worker.
          </p>
        </EmptyState>
      ) : (
        <>
          <ConnectionCard projectId={current.project.id} status={github.data} />
          {github.data.connected && (
            <RepositoriesCard projectId={current.project.id} saved={github.data.repositories} />
          )}
        </>
      )}
    </>
  );
}

function ConnectionCard({ projectId, status }: { projectId: string; status: GitHubStatus }) {
  const connect = useConnectGitHub(projectId);
  const disconnect = useDisconnectGitHub(projectId);
  const [token, setToken] = useState("");
  // Once connected, the token field stays closed until someone chooses to replace the token.
  const [replacing, setReplacing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [saved, setSaved] = useState<"connected" | "replaced" | null>(null);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const replaced = status.connected;
    connect.mutate(token.trim(), {
      onSuccess: () => {
        setReplacing(false);
        setSaved(replaced ? "replaced" : "connected");
      },
      // Cleared whether or not it was accepted: the token is not kept on the page.
      onSettled: () => { setToken(""); },
    });
  }

  function closeForm() {
    setReplacing(false);
    setToken("");
    connect.reset();
  }

  const tokenForm = (
    <form onSubmit={submit} className="mt-4 flex flex-col gap-3" noValidate>
      <TextField
        label={status.connected ? "New token" : "Token"}
        type="password"
        autoComplete="off"
        spellCheck={false}
        value={token}
        onChange={(event) => { setToken(event.target.value); }}
        hint="Starts with github_pat_. It is sent once, stored encrypted, and not shown again."
      />
      <FormMessage tone="error">{connect.isError ? describeError(connect.error) : null}</FormMessage>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" variant="primary" busy={connect.isPending} disabled={!token.trim()}>
          {status.connected ? "Save new token" : "Connect"}
        </Button>
        {status.connected && <Button onClick={closeForm}>Cancel</Button>}
      </div>
    </form>
  );

  if (!status.connected) {
    return (
      <Card>
        <h2 className="text-base font-semibold text-ink">Access token</h2>
        <div className="mt-1 max-w-prose text-sm text-ink-2">
          <p>Create a fine-grained personal access token on GitHub with:</p>
          <ul className="mt-1 list-disc pl-5">
            <li>Repository access: only the repositories of this project's services</li>
            <li>Permissions: Contents, read-only (nothing else)</li>
            <li>An expiry date</li>
          </ul>
        </div>
        {tokenForm}
      </Card>
    );
  }

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-ink">Access token</h2>
          <p className="mt-1 text-sm text-ink">
            <span aria-hidden="true" className="text-good-ink">●</span> Connected
          </p>
          <p className="mt-0.5 text-sm text-ink-2">
            Token ending in <code>{status.token_hint}</code>
            {status.connected_at ? ` · saved ${formatDateTime(status.connected_at)}` : ""}
          </p>
        </div>
        {!replacing && !confirming && (
          <div className="flex flex-wrap gap-2">
            <Button onClick={() => { setSaved(null); setReplacing(true); }}>Replace token</Button>
            <Button variant="danger" onClick={() => { setSaved(null); setConfirming(true); }}>
              Disconnect
            </Button>
          </div>
        )}
      </div>

      <FormMessage tone="info">
        {saved === "connected"
          ? "GitHub is connected. Next, add your repositories below."
          : saved === "replaced"
            ? "The token was replaced."
            : null}
      </FormMessage>

      {replacing && tokenForm}

      {confirming && (
        <div className="mt-4">
          <p className="max-w-prose text-sm text-ink-2">
            Disconnecting deletes the stored token. Investigations will no longer include code
            changes. Your repository list is kept.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button
              variant="danger"
              busy={disconnect.isPending}
              onClick={() => {
                disconnect.mutate(undefined, { onSettled: () => { setConfirming(false); } });
              }}
            >
              Confirm disconnect
            </Button>
            <Button onClick={() => { setConfirming(false); }}>Cancel</Button>
          </div>
        </div>
      )}
      <FormMessage tone="error">
        {disconnect.isError ? describeError(disconnect.error) : null}
      </FormMessage>
    </Card>
  );
}

/** Which row a server validation error points at (`body.repositories.<row>.<field>`). */
function rowErrors(error: unknown): Map<number, string> {
  const errors = new Map<number, string>();
  if (error instanceof ApiError) {
    for (const detail of error.details) {
      const [, list, row] = detail.loc;
      if (list === "repositories" && typeof row === "number") errors.set(row, detail.msg);
    }
  }
  return errors;
}

const EMPTY_ROW: RepositoryMapping = { service: "", repository: "" };

function RepositoriesCard({ projectId, saved }: { projectId: string; saved: RepositoryMapping[] }) {
  // With a saved list, the card shows it read-only until someone chooses to edit.
  const [editing, setEditing] = useState(false);
  const [justSaved, setJustSaved] = useState(false);
  const showForm = editing || saved.length === 0;

  return (
    <Card className="mt-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-ink">Repositories</h2>
          <p className="mt-1 max-w-prose text-sm text-ink-2">
            {showForm
              ? "Say which repository holds each service's code. Use the service name exactly as it appears in the telemetry. Each repository is checked with the token when you save."
              : "Which repository holds each service's code. Deployments need a commit for their changes to be found."}
          </p>
        </div>
        {!showForm && (
          <Button onClick={() => { setJustSaved(false); setEditing(true); }}>
            Edit repositories
          </Button>
        )}
      </div>

      {showForm ? (
        <RepositoriesForm
          projectId={projectId}
          saved={saved}
          onSaved={() => { setEditing(false); setJustSaved(true); }}
          onCancel={saved.length > 0 ? () => { setEditing(false); } : null}
        />
      ) : (
        <>
          <FormMessage tone="info">{justSaved ? "Repositories saved." : null}</FormMessage>
          <div className="mt-3 overflow-x-auto rounded-lg border border-hairline">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Repository of each service</caption>
              <thead className="text-ink-2">
                <tr>
                  <th scope="col" className="px-4 py-2 font-medium">Service</th>
                  <th scope="col" className="px-4 py-2 font-medium">Repository</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline">
                {saved.map((row) => (
                  <tr key={row.service}>
                    <th scope="row" className="px-4 py-2 font-medium text-ink">{row.service}</th>
                    <td className="px-4 py-2 text-ink-2">
                      <code>{row.repository}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Card>
  );
}

function RepositoriesForm({
  projectId,
  saved,
  onSaved,
  onCancel,
}: {
  projectId: string;
  saved: RepositoryMapping[];
  onSaved: () => void;
  /** Null when there is no saved list to go back to. */
  onCancel: (() => void) | null;
}) {
  const save = useSetGitHubRepositories(projectId);
  const services = useServices(projectId);
  const [rows, setRows] = useState<RepositoryMapping[]>(saved.length > 0 ? saved : [EMPTY_ROW]);

  const filled = rows
    .map((row) => ({ service: row.service.trim(), repository: row.repository.trim() }))
    .filter((row) => row.service !== "" || row.repository !== "");
  const names = filled.map((row) => row.service);
  const problem = filled.some((row) => row.service === "" || row.repository === "")
    ? "Fill in both the service and the repository, or clear the row."
    : filled.some((row) => !REPOSITORY.test(row.repository))
      ? "Write each repository as owner/name, for example acme/payment-api."
      : new Set(names).size !== names.length
        ? "Each service can have one repository only."
        : null;
  const fromServer = save.isError ? rowErrors(save.error) : new Map<number, string>();

  function update(index: number, change: Partial<RepositoryMapping>) {
    save.reset();
    setRows((all) => all.map((row, at) => (at === index ? { ...row, ...change } : row)));
  }

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    // Sent as shown, minus empty rows, so the server's row numbers match the page.
    setRows(filled.length > 0 ? filled : [EMPTY_ROW]);
    save.mutate(filled, { onSuccess: onSaved });
  }

  return (
    <>
      <datalist id="github-service-names">
        {(services.data ?? []).map((service) => (
          <option key={service.id} value={service.name} />
        ))}
      </datalist>

      <form onSubmit={submit} className="mt-4 flex flex-col gap-3" noValidate>
        {rows.map((row, index) => (
          <div key={index} className="flex flex-wrap items-start gap-3">
            <TextField
              label={`Service ${index + 1}`}
              list="github-service-names"
              placeholder="payment-api"
              value={row.service}
              onChange={(event) => { update(index, { service: event.target.value }); }}
            />
            <TextField
              label={`Repository ${index + 1}`}
              placeholder="owner/name"
              spellCheck={false}
              value={row.repository}
              error={fromServer.get(index)}
              onChange={(event) => { update(index, { repository: event.target.value }); }}
            />
            <div className="pt-6">
              <Button
                aria-label={`Remove row ${index + 1}`}
                onClick={() => {
                  save.reset();
                  setRows((all) => {
                    const rest = all.filter((_, at) => at !== index);
                    return rest.length > 0 ? rest : [EMPTY_ROW];
                  });
                }}
              >
                Remove
              </Button>
            </div>
          </div>
        ))}
        <div>
          <Button
            onClick={() => { setRows((all) => [...all, EMPTY_ROW]); }}
            disabled={rows.length >= 50}
          >
            Add a service
          </Button>
        </div>

        <FormMessage tone="error">
          {problem ?? (save.isError && fromServer.size === 0 ? describeError(save.error) : null)}
        </FormMessage>
        {/* Only seen when the list was saved empty: otherwise the form closes on save. */}
        <FormMessage tone="info">{save.isSuccess ? "Saved. No repositories are set." : null}</FormMessage>
        <div className="flex flex-wrap gap-2">
          <Button type="submit" variant="primary" busy={save.isPending} disabled={problem !== null}>
            Save repositories
          </Button>
          {onCancel && <Button onClick={onCancel}>Cancel</Button>}
        </div>
      </form>
    </>
  );
}
