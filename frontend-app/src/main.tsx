// React entrypoint: mounts the application into the single #root element.
// Keeping this file tiny makes startup failures easy to diagnose: if the app
// does not render, check this mount point and the App import first.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.tsx";
import { installGlobalErrorBeacon } from "./lib/errorbeacon";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { installBrowserTelemetry } from "./lib/browserTelemetry";

installGlobalErrorBeacon();
// Telemetry initialization is deliberately fire-and-forget. The application
// renders immediately even if /api/config/public or the collector is down.
void installBrowserTelemetry();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>
);
