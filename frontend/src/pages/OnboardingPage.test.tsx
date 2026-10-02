import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../lib/api";
import { account, fakeAuth, meWith, renderApp } from "../test/render";
import { OnboardingPage } from "./OnboardingPage";

describe("OnboardingPage", () => {
  it("asks an unverified user to verify their email first", async () => {
    const auth = fakeAuth();
    renderApp(
      <OnboardingPage me={meWith(null)} account={{ ...account, emailVerified: false }} />,
      { auth },
    );

    expect(screen.getByRole("heading", { name: "Verify your email" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Organization name")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Send the email again" }));
    expect(auth.sendVerificationEmail).toHaveBeenCalled();
    expect(await screen.findByText(/Verification email sent/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "I've verified my email" }));
    expect(auth.refreshAccount).toHaveBeenCalled();
  });

  it("creates an organization and its first project", async () => {
    const createOrganization = vi
      .spyOn(api, "createOrganization")
      .mockResolvedValue({ id: "org-1", slug: "acme-inc", name: "Acme Inc.", role: "owner" });
    const createProject = vi
      .spyOn(api, "createProject")
      .mockResolvedValue({ id: "project-1", slug: "payments", name: "Payments" });
    vi.spyOn(api, "me").mockResolvedValue(meWith("owner"));
    renderApp(<OnboardingPage me={meWith(null)} account={account} />);

    await userEvent.type(screen.getByLabelText("Organization name"), "Acme Inc.");
    await userEvent.type(screen.getByLabelText("First project name"), "Payments");
    expect(screen.getByText("Identifier: acme-inc")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Create organization" }));

    await waitFor(() => {
      expect(createProject).toHaveBeenCalledWith("org-1", { slug: "payments", name: "Payments" });
    });
    expect(createOrganization).toHaveBeenCalledWith({ slug: "acme-inc", name: "Acme Inc." });
  });

  it("shows the server's reason when creation fails", async () => {
    vi.spyOn(api, "createOrganization").mockRejectedValue(
      new ApiError({ status: 409, code: "conflict", message: "Organization 'acme' already exists." }),
    );
    vi.spyOn(api, "me").mockResolvedValue(meWith(null));
    renderApp(<OnboardingPage me={meWith(null)} account={account} />);

    await userEvent.type(screen.getByLabelText("Organization name"), "Acme");
    await userEvent.type(screen.getByLabelText("First project name"), "Payments");
    await userEvent.click(screen.getByRole("button", { name: "Create organization" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Organization 'acme' already exists.",
    );
  });

  it("resumes an interrupted setup by asking only for the project", () => {
    const me = meWith("owner");
    me.organizations = me.organizations.map((organization) => ({ ...organization, projects: [] }));
    renderApp(<OnboardingPage me={me} account={account} />);

    expect(screen.getByRole("heading", { name: "Create a project in Acme" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Organization name")).not.toBeInTheDocument();
  });
});
