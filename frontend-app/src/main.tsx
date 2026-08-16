// React entrypoint: mounts the application into the single #root element.
// Keeping this file tiny makes startup failures easy to diagnose: if the app
// does not render, check this mount point and the App import first.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.tsx";
import { installGlobalErrorBeacon } from "./lib/errorbeacon";

installGlobalErrorBeacon();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
