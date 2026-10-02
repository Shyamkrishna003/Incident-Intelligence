/** Build-time configuration. Everything here is a public identifier, not a secret. */

const env = import.meta.env;

export interface FirebaseWebConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  appId: string;
}

const required: Record<string, string | undefined> = {
  VITE_FIREBASE_API_KEY: env.VITE_FIREBASE_API_KEY,
  VITE_FIREBASE_AUTH_DOMAIN: env.VITE_FIREBASE_AUTH_DOMAIN,
  VITE_FIREBASE_PROJECT_ID: env.VITE_FIREBASE_PROJECT_ID,
  VITE_FIREBASE_APP_ID: env.VITE_FIREBASE_APP_ID,
};

const missing = Object.entries(required)
  .filter(([, value]) => !value)
  .map(([name]) => name);

/** Human-readable reason the app cannot start, or null when configuration is complete. */
export const configError: string | null =
  missing.length > 0 ? `Missing configuration: ${missing.join(", ")}. Set them in .env.` : null;

export const firebaseConfig: FirebaseWebConfig = {
  apiKey: env.VITE_FIREBASE_API_KEY ?? "",
  authDomain: env.VITE_FIREBASE_AUTH_DOMAIN ?? "",
  projectId: env.VITE_FIREBASE_PROJECT_ID ?? "",
  appId: env.VITE_FIREBASE_APP_ID ?? "",
};

/** Set only for local development against the Firebase Auth emulator. */
export const authEmulatorUrl: string | undefined =
  env.VITE_FIREBASE_AUTH_EMULATOR_URL || undefined;
