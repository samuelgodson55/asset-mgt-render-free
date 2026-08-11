import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});

// jsdom doesn't implement matchMedia -- lib/theme.tsx's ThemeProvider
// reads it on mount to pick a default light/dark theme, so any test that
// renders through that provider needs a stub or it throws.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

// framer-motion's layout animations lean on ResizeObserver, which jsdom
// also doesn't implement.
if (!("ResizeObserver" in window)) {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  // @ts-expect-error -- test-environment polyfill, not a real ResizeObserver
  window.ResizeObserver = ResizeObserverStub;
}
