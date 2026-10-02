/** Maps Firebase Authentication error codes to messages a person can act on. */

const MESSAGES: Record<string, string> = {
  "auth/invalid-credential": "The email or password is incorrect.",
  "auth/wrong-password": "The email or password is incorrect.",
  "auth/user-not-found": "The email or password is incorrect.",
  "auth/invalid-email": "Enter a valid email address.",
  "auth/email-already-in-use": "An account with this email already exists. Sign in instead.",
  "auth/weak-password": "Choose a stronger password (at least 6 characters).",
  "auth/too-many-requests": "Too many attempts. Wait a few minutes and try again.",
  "auth/user-disabled": "This account has been disabled.",
  "auth/network-request-failed": "Could not reach the sign-in service. Check your connection.",
  "auth/popup-blocked": "Your browser blocked the sign-in window. Allow pop-ups and try again.",
  "auth/operation-not-allowed": "This sign-in method is not enabled for this application.",
  "auth/unauthorized-domain": "This site is not authorized for sign-in.",
};

/** The user closed the pop-up themselves: not an error worth showing. */
const SILENT = new Set(["auth/popup-closed-by-user", "auth/cancelled-popup-request"]);

export const ACCOUNT_EXISTS = "auth/account-exists-with-different-credential";

export function authErrorCode(error: unknown): string | null {
  if (typeof error === "object" && error !== null && "code" in error) {
    const code = (error).code;
    return typeof code === "string" ? code : null;
  }
  return null;
}

/** Returns null when nothing should be shown. */
export function authErrorMessage(error: unknown): string | null {
  const code = authErrorCode(error);
  if (code !== null && SILENT.has(code)) return null;
  if (code !== null && code in MESSAGES) return MESSAGES[code] ?? null;
  return "Sign-in failed. Please try again.";
}
