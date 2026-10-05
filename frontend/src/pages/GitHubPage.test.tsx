import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../lib/api";
import type { GitHubStatus } from "../lib/types";
import { projectContext, renderApp } from "../test/render";
import { GitHubPage } from "./GitHubPage";

const TOKEN = "github_pat_" + "a1B2c3D4".repeat(5);

const notConnected: GitHubStatus = {
  available: true,
  connected: false,
  token_hint: null,
  connected_at: null,
  repositories: [],
};

const connected: GitHubStatus = {
  ...notConnected,
  connected: true,
  token_hint: "c3D4",
  connected_at: "2026-10-05T10:00:00Z",
};

describe("GitHubPage", () => {
  beforeEach(() => {
    vi.spyOn(api, "listServices").mockResolvedValue({
      services: [{ id: "s1", name: "payment-api", created_at: "2026-10-01T10:00:00Z" }],
    });
  });

  it("does not request settings for someone below admin", () => {
    const getGitHub = vi.spyOn(api, "getGitHub");
    renderApp(<GitHubPage current={projectContext("member")} />);

    expect(
      screen.getByRole("heading", { name: "You don't have access to GitHub settings" }),
    ).toBeInTheDocument();
    expect(getGitHub).not.toHaveBeenCalled();
  });

  it("says so when the server cannot store tokens", async () => {
    vi.spyOn(api, "getGitHub").mockResolvedValue({ ...notConnected, available: false });
    renderApp(<GitHubPage current={projectContext("owner")} />);

    expect(
      await screen.findByRole("heading", {
        name: "GitHub integration is not set up on this server",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Token")).not.toBeInTheDocument();
  });

  it("sends the token once and does not keep it on the page", async () => {
    vi.spyOn(api, "getGitHub").mockResolvedValue(notConnected);
    const connectGitHub = vi.spyOn(api, "connectGitHub").mockResolvedValue(connected);
    renderApp(<GitHubPage current={projectContext("admin")} />);

    const field = await screen.findByLabelText("Token");
    expect(field).toHaveAttribute("type", "password");
    expect(screen.queryByRole("heading", { name: "Repositories" })).not.toBeInTheDocument();
    await userEvent.type(field, `  ${TOKEN} `);
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByText(/Token ending in/)).toHaveTextContent("c3D4");
    expect(connectGitHub).toHaveBeenCalledWith("project-1", TOKEN);
    // Once connected, there is no token field until someone asks to replace the token.
    expect(screen.queryByLabelText(/token/i)).not.toBeInTheDocument();
    expect(screen.getByText(/GitHub is connected. Next, add your repositories/)).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(TOKEN);
    expect(screen.getByRole("heading", { name: "Repositories" })).toBeInTheDocument();
  });

  it("clears a rejected token and shows why it was rejected", async () => {
    vi.spyOn(api, "getGitHub").mockResolvedValue(notConnected);
    vi.spyOn(api, "connectGitHub").mockRejectedValue(
      new ApiError({ status: 422, code: "validation_error", message: "Request validation failed." }),
    );
    renderApp(<GitHubPage current={projectContext("admin")} />);

    await userEvent.type(await screen.findByLabelText("Token"), "not-a-token");
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByText("Request validation failed.")).toBeInTheDocument();
    expect(screen.getByLabelText("Token")).toHaveValue("");
  });

  it("opens the token field only to replace the token, and closes it again", async () => {
    vi.spyOn(api, "getGitHub").mockResolvedValue(connected);
    const connectGitHub = vi
      .spyOn(api, "connectGitHub")
      .mockResolvedValue({ ...connected, token_hint: "W6x7" });
    renderApp(<GitHubPage current={projectContext("owner")} />);

    await userEvent.click(await screen.findByRole("button", { name: "Replace token" }));
    expect(screen.getByLabelText("New token")).toHaveValue("");
    expect(screen.queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("New token"), "half-typed");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("New token")).not.toBeInTheDocument();
    expect(connectGitHub).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Replace token" }));
    expect(screen.getByLabelText("New token")).toHaveValue("");
    await userEvent.type(screen.getByLabelText("New token"), TOKEN);
    await userEvent.click(screen.getByRole("button", { name: "Save new token" }));

    expect(await screen.findByText("The token was replaced.")).toBeInTheDocument();
    expect(connectGitHub).toHaveBeenCalledWith("project-1", TOKEN);
    expect(screen.getByText(/Token ending in/)).toHaveTextContent("W6x7");
    expect(screen.queryByLabelText("New token")).not.toBeInTheDocument();
  });

  it("disconnects only after confirmation", async () => {
    vi.spyOn(api, "getGitHub").mockResolvedValue(connected);
    const disconnectGitHub = vi.spyOn(api, "disconnectGitHub").mockResolvedValue(undefined);
    renderApp(<GitHubPage current={projectContext("owner")} />);

    await userEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
    expect(disconnectGitHub).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Confirm disconnect" }));
    expect(disconnectGitHub).toHaveBeenCalledWith("project-1");
  });

  it("shows saved repositories read-only, and edits them only on request", async () => {
    vi.spyOn(api, "getGitHub").mockResolvedValue({
      ...connected,
      repositories: [{ service: "payment-api", repository: "acme/payment-api" }],
    });
    const setGitHubRepositories = vi
      .spyOn(api, "setGitHubRepositories")
      .mockImplementation((_project, repositories) =>
        Promise.resolve({ ...connected, repositories }),
      );
    renderApp(<GitHubPage current={projectContext("owner")} />);

    // Saved list: shown as text, with no fields and no save button.
    expect(await screen.findByRole("rowheader", { name: "payment-api" })).toBeInTheDocument();
    expect(screen.getByText("acme/payment-api")).toBeInTheDocument();
    expect(screen.queryByLabelText("Repository 1")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save repositories" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Edit repositories" }));
    expect(screen.getByLabelText("Repository 1")).toHaveValue("acme/payment-api");
    await userEvent.click(screen.getByRole("button", { name: "Add a service" }));
    await userEvent.type(screen.getByLabelText("Service 2"), "payments-db");
    await userEvent.type(screen.getByLabelText("Repository 2"), "acme/payments-db ");
    await userEvent.click(screen.getByRole("button", { name: "Add a service" }));
    await userEvent.click(screen.getByRole("button", { name: "Save repositories" }));

    expect(setGitHubRepositories).toHaveBeenCalledWith("project-1", [
      { service: "payment-api", repository: "acme/payment-api" },
      { service: "payments-db", repository: "acme/payments-db" },
    ]);
    // Saving closes the form and shows the new list.
    expect(await screen.findByText("Repositories saved.")).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "payments-db" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Repository 1")).not.toBeInTheDocument();
  });

  it("discards edits on cancel", async () => {
    vi.spyOn(api, "getGitHub").mockResolvedValue({
      ...connected,
      repositories: [{ service: "payment-api", repository: "acme/payment-api" }],
    });
    const setGitHubRepositories = vi.spyOn(api, "setGitHubRepositories");
    renderApp(<GitHubPage current={projectContext("owner")} />);

    await userEvent.click(await screen.findByRole("button", { name: "Edit repositories" }));
    await userEvent.clear(screen.getByLabelText("Repository 1"));
    await userEvent.type(screen.getByLabelText("Repository 1"), "acme/something-else");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.getByText("acme/payment-api")).toBeInTheDocument();
    expect(setGitHubRepositories).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Edit repositories" }));
    expect(screen.getByLabelText("Repository 1")).toHaveValue("acme/payment-api");
  });

  it("opens the form straight away when no repository is set, and closes it on save", async () => {
    vi.spyOn(api, "getGitHub").mockResolvedValue(connected);
    vi.spyOn(api, "setGitHubRepositories").mockImplementation((_project, repositories) =>
      Promise.resolve({ ...connected, repositories }),
    );
    renderApp(<GitHubPage current={projectContext("owner")} />);

    await userEvent.type(await screen.findByLabelText("Service 1"), "payment-api");
    // Nothing saved yet, so there is nothing to cancel back to.
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Repository 1"), "acme/payment-api");
    await userEvent.click(screen.getByRole("button", { name: "Save repositories" }));

    expect(await screen.findByText("Repositories saved.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit repositories" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Service 1")).not.toBeInTheDocument();
  });

  it("explains an incomplete or malformed row before anything is sent", async () => {
    vi.spyOn(api, "getGitHub").mockResolvedValue(connected);
    const setGitHubRepositories = vi.spyOn(api, "setGitHubRepositories");
    renderApp(<GitHubPage current={projectContext("owner")} />);

    await userEvent.type(await screen.findByLabelText("Service 1"), "payment-api");
    expect(screen.getByText(/Fill in both the service and the repository/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save repositories" })).toBeDisabled();

    await userEvent.type(screen.getByLabelText("Repository 1"), "just-a-name");
    expect(screen.getByText(/Write each repository as owner\/name/)).toBeInTheDocument();
    expect(setGitHubRepositories).not.toHaveBeenCalled();
  });

  it("shows a repository the token cannot read on its own row", async () => {
    vi.spyOn(api, "getGitHub").mockResolvedValue(connected);
    vi.spyOn(api, "setGitHubRepositories").mockRejectedValue(
      new ApiError({
        status: 422,
        code: "validation_error",
        message: "One or more repositories cannot be read.",
        details: [
          {
            loc: ["body", "repositories", 0, "repository"],
            msg: "The repository was not found, or the token cannot read it.",
            type: "repository_not_readable",
          },
        ],
      }),
    );
    renderApp(<GitHubPage current={projectContext("owner")} />);

    await userEvent.type(await screen.findByLabelText("Service 1"), "payment-api");
    await userEvent.type(screen.getByLabelText("Repository 1"), "acme/missing");
    await userEvent.click(screen.getByRole("button", { name: "Save repositories" }));

    expect(await screen.findByLabelText("Repository 1")).toHaveAccessibleDescription(
      "The repository was not found, or the token cannot read it.",
    );
  });
});
