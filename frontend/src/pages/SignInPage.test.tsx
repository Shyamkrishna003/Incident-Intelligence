import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fakeAuth, firebaseError, renderApp } from "../test/render";
import { SignInPage } from "./SignInPage";

describe("SignInPage", () => {
  it("offers Google, GitHub, and email sign-in", () => {
    renderApp(<SignInPage />);

    expect(screen.getByRole("button", { name: "Continue with Google" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Continue with GitHub" })).toBeEnabled();
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("signs in with email and password", async () => {
    const auth = fakeAuth();
    renderApp(<SignInPage />, { auth });

    await userEvent.type(screen.getByLabelText("Email"), "priya@example.test");
    await userEvent.type(screen.getByLabelText("Password"), "secret123");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(auth.signInWithEmail).toHaveBeenCalledWith("priya@example.test", "secret123");
  });

  it("shows a clear message when sign-in fails", async () => {
    const auth = fakeAuth({
      signInWithEmail: vi.fn(() => Promise.reject(firebaseError("auth/invalid-credential"))),
    });
    renderApp(<SignInPage />, { auth });

    await userEvent.type(screen.getByLabelText("Email"), "priya@example.test");
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The email or password is incorrect.",
    );
  });

  it("creates an account with a name", async () => {
    const auth = fakeAuth();
    renderApp(<SignInPage />, { auth });

    await userEvent.click(screen.getByRole("button", { name: "Create an account" }));
    await userEvent.type(screen.getByLabelText("Name"), "Priya");
    await userEvent.type(screen.getByLabelText("Email"), "priya@example.test");
    await userEvent.type(screen.getByLabelText("Password"), "secret123");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(auth.signUpWithEmail).toHaveBeenCalledWith("priya@example.test", "secret123", "Priya");
  });

  it("shows nothing when the user closes the provider pop-up", async () => {
    const auth = fakeAuth({
      signInWithProvider: vi.fn(() => Promise.reject(firebaseError("auth/popup-closed-by-user"))),
    });
    renderApp(<SignInPage />, { auth });

    await userEvent.click(screen.getByRole("button", { name: "Continue with GitHub" }));

    expect(auth.signInWithProvider).toHaveBeenCalledWith("github");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("explains how to link when the email already uses another method", () => {
    renderApp(<SignInPage />, { auth: fakeAuth({ linkPending: true }) });

    expect(screen.getByRole("status")).toHaveTextContent(
      "An account with this email already exists",
    );
  });
});
