import "./index.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { configError } from "./lib/config";

const container = document.getElementById("root");
if (!container) throw new Error("Missing #root element");
const root = createRoot(container);

if (configError) {
  // Firebase cannot start without its configuration; say so instead of a blank page.
  root.render(
    <div role="alert" style={{ maxWidth: "36rem", margin: "4rem auto", padding: "0 1rem" }}>
      <h1 style={{ fontSize: "1.25rem", fontWeight: 600 }}>The app is not configured</h1>
      <p>{configError}</p>
    </div>,
  );
} else {
  // Loaded only once configuration is known to be present.
  void import("./App").then(({ App }) => {
    root.render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
  });
}
