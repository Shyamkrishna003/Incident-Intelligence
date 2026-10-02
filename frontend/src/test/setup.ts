import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// Tests never talk to Firebase: the auth context is supplied directly (see test/render.tsx).
vi.mock("../lib/firebase", () => ({ auth: {} }));

afterEach(() => {
  cleanup();
});

// jsdom has no layout engine; the chart library needs this to exist.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver = ResizeObserverStub;
