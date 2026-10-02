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
import { useApiKeys, useCreateApiKey, useRevokeApiKey } from "../hooks/queries";
import { describeError } from "../lib/api";
import { formatDateTime } from "../lib/format";
import type { ApiKey, ApiKeyCreated, ApiKeyScope } from "../lib/types";
import { roleAtLeast } from "../lib/types";

const SCOPES: { value: ApiKeyScope; label: string; description: string }[] = [
  { value: "ingest:write", label: "Send telemetry", description: "ingest:write" },
  { value: "telemetry:read", label: "Read telemetry", description: "telemetry:read" },
];

const EXPIRY_OPTIONS = [
  { days: null, label: "Never" },
  { days: 30, label: "30 days" },
  { days: 90, label: "90 days" },
  { days: 365, label: "1 year" },
] as const;

export function ApiKeysPage({ current }: { current: ProjectContext }) {
  const allowed = roleAtLeast(current.organization.role, "admin");
  const keys = useApiKeys(current.project.id, allowed);
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);

  if (!allowed) {
    return (
      <>
        <PageHeader title="API keys" />
        <EmptyState title="You don't have access to API keys">
          <p>Only organization admins and owners can view and manage API keys.</p>
        </EmptyState>
      </>
    );
  }

  return (
    <>
      <PageHeader title="API keys" />
      <p className="mb-5 max-w-prose text-sm text-ink-2">
        Services use an API key to send telemetry to <strong>{current.project.name}</strong>. A
        key belongs to this project only.
      </p>

      {created && <NewKeyNotice created={created} onDismiss={() => { setCreated(null); }} />}
      <CreateKeyForm projectId={current.project.id} onCreated={setCreated} />

      <h2 className="mb-3 mt-8 text-base font-semibold text-ink">Existing keys</h2>
      {keys.isPending ? (
        <LoadingState label="Loading API keys…" />
      ) : keys.isError ? (
        <ErrorState
          title="Could not load API keys"
          error={keys.error}
          onRetry={() => void keys.refetch()}
        />
      ) : keys.data.length === 0 ? (
        <EmptyState title="No API keys yet">
          <p>Create one above to start sending telemetry.</p>
        </EmptyState>
      ) : (
        <KeyTable projectId={current.project.id} keys={keys.data} />
      )}
    </>
  );
}

function NewKeyNotice({ created, onDismiss }: { created: ApiKeyCreated; onDismiss: () => void }) {
  const [copied, setCopied] = useState<boolean | null>(null);

  async function copy() {
    try {
      await navigator.clipboard.writeText(created.key);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <Card className="mb-5">
      <h2 className="text-base font-semibold text-ink">Copy your new key now</h2>
      <p className="mt-1 text-sm text-ink-2">
        This is the only time the key “{created.name}” is shown. It is not stored and cannot be
        recovered. If you lose it, revoke it and create a new one.
      </p>
      <code
        aria-label="New API key"
        className="mt-3 block overflow-x-auto rounded-md bg-wash px-3 py-2 text-sm text-ink"
      >
        {created.key}
      </code>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Button variant="primary" onClick={() => void copy()}>
          Copy key
        </Button>
        <Button onClick={onDismiss}>I've saved it</Button>
        <FormMessage tone={copied === false ? "error" : "info"}>
          {copied === true
            ? "Copied."
            : copied === false
              ? "Could not copy. Select the key and copy it manually."
              : null}
        </FormMessage>
      </div>
    </Card>
  );
}

function CreateKeyForm({
  projectId,
  onCreated,
}: {
  projectId: string;
  onCreated: (key: ApiKeyCreated) => void;
}) {
  const create = useCreateApiKey(projectId);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<ApiKeyScope[]>(["ingest:write"]);
  const [expiryDays, setExpiryDays] = useState<number | null>(null);

  function toggle(scope: ApiKeyScope) {
    setScopes((selected) =>
      selected.includes(scope) ? selected.filter((item) => item !== scope) : [...selected, scope],
    );
  }

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    create.mutate(
      { name: name.trim(), scopes, expires_in_days: expiryDays },
      {
        onSuccess: (key) => {
          onCreated(key);
          setName("");
        },
      },
    );
  }

  return (
    <Card>
      <h2 className="text-base font-semibold text-ink">Create a key</h2>
      <form onSubmit={submit} className="mt-4 flex flex-col gap-4" noValidate>
        <TextField
          label="Name"
          required
          maxLength={100}
          value={name}
          onChange={(event) => { setName(event.target.value); }}
          hint="Where the key will be used, for example: payment-api production"
        />

        <fieldset>
          <legend className="text-sm font-medium text-ink">Permissions</legend>
          <div className="mt-1 flex flex-col gap-1">
            {SCOPES.map((scope) => (
              <label key={scope.value} className="flex items-center gap-2 text-sm text-ink">
                <input
                  type="checkbox"
                  checked={scopes.includes(scope.value)}
                  onChange={() => { toggle(scope.value); }}
                />
                {scope.label} <span className="text-ink-2">({scope.description})</span>
              </label>
            ))}
          </div>
          <p className="mt-1 text-sm text-ink-2">
            Give a key only what it needs. A service that only sends data needs only “Send
            telemetry”.
          </p>
        </fieldset>

        <label className="flex flex-col gap-1 text-sm font-medium text-ink">
          Expires
          <select
            className="w-40 rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm font-normal text-ink"
            value={expiryDays ?? ""}
            onChange={(event) => {
              setExpiryDays(event.target.value === "" ? null : Number(event.target.value));
            }}
          >
            {EXPIRY_OPTIONS.map((option) => (
              <option key={option.label} value={option.days ?? ""}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        <FormMessage tone="error">{create.isError ? describeError(create.error) : null}</FormMessage>
        <div>
          <Button
            type="submit"
            variant="primary"
            busy={create.isPending}
            disabled={!name.trim() || scopes.length === 0}
          >
            Create key
          </Button>
        </div>
      </form>
    </Card>
  );
}

function keyStatus(key: ApiKey): "Revoked" | "Expired" | "Active" {
  if (key.revoked_at) return "Revoked";
  if (key.expires_at && Date.parse(key.expires_at) <= Date.now()) return "Expired";
  return "Active";
}

function KeyTable({ projectId, keys }: { projectId: string; keys: ApiKey[] }) {
  const revoke = useRevokeApiKey(projectId);
  const [confirming, setConfirming] = useState<string | null>(null);

  return (
    <>
      <FormMessage tone="error">{revoke.isError ? describeError(revoke.error) : null}</FormMessage>
      <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">API keys for this project</caption>
          <thead className="text-ink-2">
            <tr>
              {["Name", "Key starts with", "Permissions", "Status", "Last used", "Expires", ""].map(
                (heading) => (
                  <th key={heading} scope="col" className="px-4 py-2 font-medium">
                    {heading || <span className="sr-only">Actions</span>}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody className="divide-y divide-hairline">
            {keys.map((key) => {
              const status = keyStatus(key);
              return (
                <tr key={key.id}>
                  <th scope="row" className="px-4 py-2 font-medium text-ink">
                    {key.name}
                  </th>
                  <td className="px-4 py-2 text-ink-2">
                    <code>ii_{key.prefix}</code>
                  </td>
                  <td className="px-4 py-2 text-ink-2">{key.scopes.join(", ")}</td>
                  <td className="px-4 py-2 text-ink-2">{status}</td>
                  <td className="px-4 py-2 text-ink-2">
                    {key.last_used_at ? formatDateTime(key.last_used_at) : "Never"}
                  </td>
                  <td className="px-4 py-2 text-ink-2">
                    {key.expires_at ? formatDateTime(key.expires_at) : "Never"}
                  </td>
                  <td className="px-4 py-2 text-right">
                    {status !== "Active" ? null : confirming === key.prefix ? (
                      <span className="inline-flex gap-2">
                        <Button
                          variant="danger"
                          busy={revoke.isPending}
                          onClick={() => {
                            revoke.mutate(key.prefix, { onSettled: () => { setConfirming(null); } });
                          }}
                        >
                          Confirm revoke
                        </Button>
                        <Button onClick={() => { setConfirming(null); }}>Cancel</Button>
                      </span>
                    ) : (
                      <Button
                        variant="danger"
                        aria-label={`Revoke ${key.name}`}
                        onClick={() => { setConfirming(key.prefix); }}
                      >
                        Revoke
                      </Button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-sm text-ink-2">
        Revoking takes effect immediately. Services using that key will be rejected.
      </p>
    </>
  );
}
