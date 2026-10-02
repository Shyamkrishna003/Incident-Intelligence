import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Root } from "./App";
import { ApiError, api } from "./lib/api";
import { fakeAuth, meWith, renderApp } from "./test/render";

describe("Root", () => {
  it("shows the sign-in page to a signed-out visitor without calling the API", () => {
    const me = vi.spyOn(api, "me");
    renderApp(<Root />, { auth: fakeAuth({ state: { status: "signedOut" } }) });

    expect(screen.getByRole("button", { name: "Continue with Google" })).toBeInTheDocument();
    expect(me).not.toHaveBeenCalled();
  });

  it("sends a new user to setup", async () => {
    vi.spyOn(api, "me").mockResolvedValue(meWith(null));
    renderApp(<Root />);

    expect(
      await screen.findByRole("heading", { name: "Create your organization" }),
    ).toBeInTheDocument();
  });

  it("opens the first project's incidents for a returning user", async () => {
    vi.spyOn(api, "me").mockResolvedValue(meWith("viewer"));
    vi.spyOn(api, "listIncidents").mockResolvedValue({ start: "", end: "", incidents: [] });
    renderApp(<Root />);

    expect(await screen.findByRole("heading", { name: "Incidents" })).toBeInTheDocument();
    // A viewer gets no API-keys navigation.
    expect(screen.queryByRole("link", { name: "API keys" })).not.toBeInTheDocument();
  });

  it("treats a project the user cannot access as not found", async () => {
    vi.spyOn(api, "me").mockResolvedValue(meWith("owner"));
    renderApp(<Root />, { route: "/p/someone-elses-project/services" });

    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });

  it("offers retry and sign-out when the account cannot be loaded", async () => {
    const auth = fakeAuth();
    vi.spyOn(api, "me").mockRejectedValue(
      new ApiError({ status: 503, code: "service_unavailable", message: "Temporarily unavailable." }),
    );
    renderApp(<Root />, { auth });

    expect(await screen.findByRole("alert")).toHaveTextContent("Temporarily unavailable.");
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(auth.signOut).toHaveBeenCalled();
  });
});
