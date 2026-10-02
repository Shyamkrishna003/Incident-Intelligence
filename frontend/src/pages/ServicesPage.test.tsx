import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { AuthContext } from "../auth/AuthContext";

import { ApiError, api } from "../lib/api";
import { fakeAuth, projectContext, renderApp } from "../test/render";
import { ServicesPage } from "./ServicesPage";

describe("ServicesPage", () => {
  it("shows loading, then the services", async () => {
    vi.spyOn(api, "listServices").mockResolvedValue({
      services: [{ id: "s1", name: "payment-api", created_at: "2026-10-01T10:00:00Z" }],
    });
    renderApp(<ServicesPage current={projectContext()} />);

    expect(screen.getByRole("status")).toHaveTextContent("Loading services…");
    const link = await screen.findByRole("link", { name: /payment-api/ });
    expect(link).toHaveAttribute("href", "/p/project-1/services/payment-api");
  });

  it("tells an admin how to get data flowing when there are no services", async () => {
    vi.spyOn(api, "listServices").mockResolvedValue({ services: [] });
    renderApp(<ServicesPage current={projectContext("admin")} />);

    expect(await screen.findByRole("heading", { name: "No services yet" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create an API key" })).toHaveAttribute(
      "href",
      "/p/project-1/api-keys",
    );
  });

  it("does not point a viewer at a page they cannot use", async () => {
    vi.spyOn(api, "listServices").mockResolvedValue({ services: [] });
    renderApp(<ServicesPage current={projectContext("viewer")} />);

    expect(await screen.findByText(/Ask an organization admin/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Create an API key" })).not.toBeInTheDocument();
  });

  it("fetches again each time the page is opened, so new services show up", async () => {
    const listServices = vi.spyOn(api, "listServices").mockResolvedValue({ services: [] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const page = (
      <QueryClientProvider client={client}>
        <AuthContext.Provider value={fakeAuth()}>
          <MemoryRouter>
            <ServicesPage current={projectContext()} />
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>
    );

    const first = render(page);
    await screen.findByRole("heading", { name: "No services yet" });
    first.unmount();
    // The user leaves, a service sends its first batch, and the user comes back.
    listServices.mockResolvedValue({
      services: [{ id: "s1", name: "payment-api", created_at: "2026-10-01T10:00:00Z" }],
    });
    render(page);

    expect(await screen.findByRole("link", { name: /payment-api/ })).toBeInTheDocument();
  });

  it("shows the error and lets the user retry", async () => {
    const listServices = vi
      .spyOn(api, "listServices")
      .mockRejectedValueOnce(
        new ApiError({ status: 503, code: "service_unavailable", message: "Temporarily unavailable." }),
      )
      .mockResolvedValue({ services: [] });
    renderApp(<ServicesPage current={projectContext()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Temporarily unavailable.");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("heading", { name: "No services yet" })).toBeInTheDocument();
    expect(listServices).toHaveBeenCalledTimes(2);
  });
});
