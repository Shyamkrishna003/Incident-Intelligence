import { describe, expect, it } from "vitest";

import { authErrorMessage } from "./authErrors";

describe("authErrorMessage", () => {
  it("does not reveal whether the email or the password was wrong", () => {
    const wrongPassword = authErrorMessage({ code: "auth/wrong-password" });
    const unknownUser = authErrorMessage({ code: "auth/user-not-found" });

    expect(wrongPassword).toBe("The email or password is incorrect.");
    expect(unknownUser).toBe(wrongPassword);
  });

  it("stays silent when the user closed the pop-up", () => {
    expect(authErrorMessage({ code: "auth/popup-closed-by-user" })).toBeNull();
  });

  it("falls back to a generic message", () => {
    expect(authErrorMessage({ code: "auth/something-new" })).toBe(
      "Sign-in failed. Please try again.",
    );
    expect(authErrorMessage(new Error("boom"))).toBe("Sign-in failed. Please try again.");
  });
});
