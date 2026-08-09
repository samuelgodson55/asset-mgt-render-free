import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Deliberately its own config file rather than reusing vite.config.ts's
// `test` field -- keeps the prod build config free of test-only settings
// (jsdom environment, setupFiles) that have no bearing on `npm run build`.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: true,
    globals: false,
    restoreMocks: true,
  },
});
