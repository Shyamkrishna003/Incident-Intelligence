import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The API the dev server forwards to. Proxying keeps the browser on one origin, so the
// API needs no cross-origin (CORS) configuration.
const apiTarget = process.env.API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // One .env for the whole repo. Vite exposes ONLY variables prefixed with VITE_ to
  // browser code; everything else in that file (database password, API-key pepper)
  // stays out of the bundle.
  envDir: "..",
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/v1": apiTarget,
      "/healthz": apiTarget,
      "/readyz": apiTarget,
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
  },
});
