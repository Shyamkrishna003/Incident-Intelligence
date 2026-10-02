import { useState, type SubmitEvent } from "react";

import { useAuth, type OAuthProviderName } from "../auth/AuthContext";
import { authErrorMessage } from "../auth/authErrors";
import { Button, Card, FormMessage, TextField } from "../components/ui";

type Mode = "signIn" | "signUp";

export function SignInPage() {
  const { signInWithEmail, signUpWithEmail, signInWithProvider, linkPending } = useAuth();
  const [mode, setMode] = useState<Mode>("signIn");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState<"email" | OAuthProviderName | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(kind: "email" | OAuthProviderName, action: () => Promise<unknown>) {
    setBusy(kind);
    setError(null);
    try {
      await action();
    } catch (caught) {
      setError(authErrorMessage(caught));
    } finally {
      setBusy(null);
    }
  }

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    void run("email", () =>
      mode === "signIn" ? signInWithEmail(email, password) : signUpWithEmail(email, password, name),
    );
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-4 py-10">
      <h1 className="mb-1 text-2xl font-semibold text-ink">Incident Intelligence</h1>
      <p className="mb-6 text-sm text-ink-2">
        {mode === "signIn" ? "Sign in to your account." : "Create your account."}
      </p>

      <Card>
        {linkPending && (
          <div role="status" className="mb-4 rounded-md bg-wash px-3 py-2 text-sm text-ink">
            An account with this email already exists, using a different sign-in method. Sign in
            the way you did before, and the new method will be added to your account.
          </div>
        )}

        <div className="flex flex-col gap-2">
          <Button
            busy={busy === "google"}
            disabled={busy !== null}
            onClick={() => void run("google", () => signInWithProvider("google"))}
          >
            Continue with Google
          </Button>
          <Button
            busy={busy === "github"}
            disabled={busy !== null}
            onClick={() => void run("github", () => signInWithProvider("github"))}
          >
            Continue with GitHub
          </Button>
        </div>

        <div className="my-5 flex items-center gap-3 text-xs text-ink-2">
          <span className="h-px flex-1 bg-hairline" />
          or with email
          <span className="h-px flex-1 bg-hairline" />
        </div>

        <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
          {mode === "signUp" && (
            <TextField
              label="Name"
              autoComplete="name"
              value={name}
              onChange={(event) => { setName(event.target.value); }}
            />
          )}
          <TextField
            label="Email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => { setEmail(event.target.value); }}
          />
          <TextField
            label="Password"
            type="password"
            autoComplete={mode === "signIn" ? "current-password" : "new-password"}
            required
            hint={mode === "signUp" ? "At least 6 characters." : undefined}
            value={password}
            onChange={(event) => { setPassword(event.target.value); }}
          />
          <FormMessage tone="error">{error}</FormMessage>
          <Button
            type="submit"
            variant="primary"
            busy={busy === "email"}
            disabled={busy !== null || !email || !password}
          >
            {mode === "signIn" ? "Sign in" : "Create account"}
          </Button>
        </form>
      </Card>

      <p className="mt-4 text-sm text-ink-2">
        {mode === "signIn" ? "New here? " : "Already have an account? "}
        <button
          type="button"
          className="font-medium text-link underline"
          onClick={() => {
            setMode(mode === "signIn" ? "signUp" : "signIn");
            setError(null);
          }}
        >
          {mode === "signIn" ? "Create an account" : "Sign in"}
        </button>
      </p>
    </main>
  );
}
