import { initializeApp } from "firebase/app";
import { connectAuthEmulator, getAuth } from "firebase/auth";

import { authEmulatorUrl, firebaseConfig } from "./config";

const app = initializeApp(firebaseConfig);

export const auth = getAuth(app);

if (authEmulatorUrl) {
  // The emulator issues unsigned tokens; the API only accepts them in local development.
  connectAuthEmulator(auth, authEmulatorUrl, { disableWarnings: true });
}
