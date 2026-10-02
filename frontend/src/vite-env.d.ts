/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_FIREBASE_API_KEY?: string;
  readonly VITE_FIREBASE_AUTH_DOMAIN?: string;
  readonly VITE_FIREBASE_PROJECT_ID?: string;
  readonly VITE_FIREBASE_APP_ID?: string;
  /** Local development only, e.g. http://localhost:9099 (the Firebase Auth emulator). */
  readonly VITE_FIREBASE_AUTH_EMULATOR_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
