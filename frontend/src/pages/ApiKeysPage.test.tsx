import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import type { ApiKey } from "../lib/types";
import { projectContext, renderApp } from "../test/render";
import { ApiKeysPage } from "./ApiKeysPage";

const activeKey: ApiKey = {
  id: "k1",
  name: "payment-api production",
  prefix: "abcdefghijkl",
  scopes: ["ingest:write"],
  created_at: "2026-10-01T10:00:00Z",
  expires_at: null,
  last_used_at: null,
  revoked_at: null,
};

describe("ApiKeysPage", () => {
  it("does not request keys for someone below admin", () => {
    const listApiKeys = vi.spyOn(api, "listApiKeys");
    renderApp(<ApiKeysPage current={projectContext("member")} />);

    expect(
      screen.getByRole("heading", { name: "You don't have access to API keys" }),
    ).toBeInTheDocument();
    expect(listApiKeys).not.toHaveBeenCalled();
  });

  it("shows a new key once, then hides it for good", async () => {
    vi.spyOn(api, "listApiKeys").mockResolvedValue({ api_keys: [] });
    const createApiKey = vi
      .spyOn(api, "createApiKey")
      .mockResolvedValue({ ...activeKey, key: "ii_abcdefghijkl_SECRETSECRET" });
    renderApp(<ApiKeysPage current={projectContext("admin")} />);

    await userEvent.type(screen.getByLabelText("Name"), "payment-api production");
    await userEvent.click(screen.getByRole("button", { name: "Create key" }));

    expect(await screen.findByLabelText("New API key")).toHaveTextContent(
      "ii_abcdefghijkl_SECRETSECRET",
    );
    expect(createApiKey).toHaveBeenCalledWith("project-1", {
      name: "payment-api production",
      scopes: ["ingest:write"],
      expires_in_days: null,
    });

    await userEvent.click(screen.getByRole("button", { name: "I've saved it" }));
    expect(screen.queryByText(/SECRETSECRET/)).not.toBeInTheDocument();
  });

  it("requires at least one permission", async () => {
    vi.spyOn(api, "listApiKeys").mockResolvedValue({ api_keys: [] });
    renderApp(<ApiKeysPage current={projectContext("owner")} />);

    await userEvent.type(screen.getByLabelText("Name"), "ci");
    await userEvent.click(screen.getByRole("checkbox", { name: /Send telemetry/ }));

    expect(screen.getByRole("button", { name: "Create key" })).toBeDisabled();
  });

  it("revokes only after confirmation", async () => {
    vi.spyOn(api, "listApiKeys").mockResolvedValue({ api_keys: [activeKey] });
    const revokeApiKey = vi.spyOn(api, "revokeApiKey").mockResolvedValue(undefined);
    renderApp(<ApiKeysPage current={projectContext("owner")} />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Revoke payment-api production" }),
    );
    expect(revokeApiKey).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Confirm revoke" }));
    expect(revokeApiKey).toHaveBeenCalledWith("project-1", "abcdefghijkl");
  });

  it("lists a revoked key without a revoke button or any key material", async () => {
    vi.spyOn(api, "listApiKeys").mockResolvedValue({
      api_keys: [{ ...activeKey, revoked_at: "2026-10-02T10:00:00Z" }],
    });
    renderApp(<ApiKeysPage current={projectContext("owner")} />);

    const row = (await screen.findByRole("rowheader", { name: "payment-api production" }))
      .closest("tr");
    if (!row) throw new Error("the key's table row was not found");
    expect(within(row).getByText("Revoked")).toBeInTheDocument();
    expect(within(row).getByText("ii_abcdefghijkl")).toBeInTheDocument();
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
  });
});
