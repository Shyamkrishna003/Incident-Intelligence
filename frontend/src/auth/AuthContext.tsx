import {
  GithubAuthProvider,
  GoogleAuthProvider,
  OAuthProvider,
  createUserWithEmailAndPassword,
  linkWithCredential,
  onIdTokenChanged,
  sendEmailVerification,
  signInWithEmailAndPassword,
  signInWithPopup,
  signOut as firebaseSignOut,
  updateProfile,
  type AuthCredential,
  type User as FirebaseUser,
} from "firebase/auth";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import { setTokenProvider } from "../lib/api";
import { auth } from "../lib/firebase";
import { ACCOUNT_EXISTS, authErrorCode } from "./authErrors";

export type OAuthProviderName = "google" | "github";

export interface Account {
  uid: string;
  email: string | null;
  emailVerified: boolean;
  displayName: string | null;
}

export type AuthState =
  | { status: "loading" }
  | { status: "signedOut" }
  | { status: "signedIn"; account: Account };

export interface AuthActions {
  signInWithEmail: (email: string, password: string) => Promise<void>;
  signUpWithEmail: (email: string, password: string, displayName: string) => Promise<void>;
  /** Resolves to "link_required" when the email already uses another sign-in method. */
  signInWithProvider: (provider: OAuthProviderName) => Promise<"ok" | "link_required">;
  sendVerificationEmail: () => Promise<void>;
  /** Re-reads the account (for example after the user clicked the verification link). */
  refreshAccount: () => Promise<void>;
  signOut: () => Promise<void>;
}

export interface AuthContextValue extends AuthActions {
  state: AuthState;
  /** True while a sign-in with another method is waiting to be linked to this account. */
  linkPending: boolean;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

function toAccount(user: FirebaseUser): Account {
  return {
    uid: user.uid,
    email: user.email,
    emailVerified: user.emailVerified,
    displayName: user.displayName,
  };
}

function providerFor(name: OAuthProviderName): GoogleAuthProvider | GithubAuthProvider {
  return name === "google" ? new GoogleAuthProvider() : new GithubAuthProvider();
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  // A credential from a provider whose email already belongs to an account. It is linked
  // to that account after the user proves ownership by signing in the original way.
  const pendingCredential = useRef<AuthCredential | null>(null);
  const [linkPending, setLinkPending] = useState(false);

  useEffect(() => {
    setTokenProvider(async (forceRefresh) =>
      auth.currentUser ? auth.currentUser.getIdToken(forceRefresh) : null,
    );
    return onIdTokenChanged(auth, (user) => {
      setState(user ? { status: "signedIn", account: toAccount(user) } : { status: "signedOut" });
    });
  }, []);

  const linkIfPending = useCallback(async (user: FirebaseUser) => {
    const credential = pendingCredential.current;
    if (!credential) return;
    pendingCredential.current = null;
    setLinkPending(false);
    await linkWithCredential(user, credential);
  }, []);

  const actions = useMemo<AuthActions>(
    () => ({
      signInWithEmail: async (email, password) => {
        const result = await signInWithEmailAndPassword(auth, email, password);
        await linkIfPending(result.user);
      },

      signUpWithEmail: async (email, password, displayName) => {
        const result = await createUserWithEmailAndPassword(auth, email, password);
        if (displayName.trim()) {
          await updateProfile(result.user, { displayName: displayName.trim() });
        }
        await sendEmailVerification(result.user);
        // The ID token was minted before the name was set: refresh so the API sees it.
        await result.user.getIdToken(true);
        setState({ status: "signedIn", account: toAccount(result.user) });
      },

      signInWithProvider: async (name) => {
        try {
          const result = await signInWithPopup(auth, providerFor(name));
          await linkIfPending(result.user);
          return "ok";
        } catch (error) {
          if (authErrorCode(error) !== ACCOUNT_EXISTS) throw error;
          const credential = OAuthProvider.credentialFromError(error as never);
          if (!credential) throw error;
          pendingCredential.current = credential;
          setLinkPending(true);
          return "link_required";
        }
      },

      sendVerificationEmail: async () => {
        if (auth.currentUser) await sendEmailVerification(auth.currentUser);
      },

      refreshAccount: async () => {
        const user = auth.currentUser;
        if (!user) return;
        await user.reload();
        // A fresh token carries the updated email_verified claim to the API.
        await user.getIdToken(true);
        setState({ status: "signedIn", account: toAccount(user) });
      },

      signOut: async () => {
        pendingCredential.current = null;
        setLinkPending(false);
        await firebaseSignOut(auth);
      },
    }),
    [linkIfPending],
  );

  const value = useMemo<AuthContextValue>(
    () => ({ state, linkPending, ...actions }),
    [state, linkPending, actions],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}
