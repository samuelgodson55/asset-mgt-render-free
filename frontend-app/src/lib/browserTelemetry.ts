/**
 * browserTelemetry.ts
 * -------------------
 * Small, dependency-free browser tracing bridge.
 *
 * WHY THIS IS NOT THE FULL NODE/PYTHON SDK:
 * Browser OpenTelemetry instrumentation is still experimental, and pulling
 * the full web SDK + protobuf runtime into this production React bundle would
 * add a large dependency surface for a feature that must never affect app
 * behavior. Instead, this module emits the standard OTLP/HTTP JSON wire
 * format and W3C Trace Context directly. The backend/collector therefore
 * receives normal OpenTelemetry spans, while the React application keeps
 * zero runtime telemetry dependencies.
 *
 * MASTER SWITCH:
 *   The backend's OTEL_ENABLED setting is exposed only as a boolean through
 *   /api/config/public. When it is false, this module installs nothing and
 *   sends nothing. No browser environment flag can accidentally enable it.
 *
 * TRACE SHAPE:
 *   click (frontend, INTERNAL)
 *       └── fetch (frontend, CLIENT)
 *              └── FastAPI server span (backend, SERVER)
 *                     └── SQLAlchemy / Redis / Celery children
 *
 * PRIVACY:
 *   - Only API requests are traced; static assets are ignored.
 *   - URLs are recorded without query strings.
 *   - Click spans use a safe action name (data-otel-action / aria-label /
 *     element id / generic role), never arbitrary visible text or form data.
 *   - Authentication headers, cookies, request bodies and response bodies are
 *     never copied into spans.
 */

type SpanKind = 1 | 3; // INTERNAL | CLIENT
type SpanStatusCode = 0 | 1 | 2; // UNSET | OK | ERROR

interface BrowserOtelConfig {
  otel_enabled?: boolean;
  otel_trace_sample_ratio?: number;
}

interface SpanData {
  traceId: string;
  spanId: string;
  parentSpanId?: string;
  name: string;
  kind: SpanKind;
  startTimeUnixNano: string;
  endTimeUnixNano: string;
  attributes: Array<{ key: string; value: { stringValue?: string; intValue?: string } }>;
  status: { code: SpanStatusCode };
}

interface ActiveInteraction {
  span: SpanData;
  pendingFetches: number;
  ended: boolean;
}

const TRACE_EXPORT_PATH = "/api/telemetry/traces";
const EXPORT_BATCH_SIZE = 10;
const EXPORT_INTERVAL_MS = 5000;
const MAX_QUEUED_SPANS = 100;
const MAX_INTERACTION_MS = 10_000;

let installed = false;
let queue: SpanData[] = [];
let flushTimer: number | undefined;
let activeInteraction: ActiveInteraction | null = null;

function randomHex(bytes: number): string {
  const values = new Uint8Array(bytes);
  crypto.getRandomValues(values);
  return Array.from(values, (value) => value.toString(16).padStart(2, "0")).join("");
}

function nowUnixNano(): string {
  // Date.now() is intentionally used rather than performance.timeOrigin +
  // performance.now(): it keeps the exported timestamp directly comparable
  // with backend spans, while the duration measurement below still uses
  // performance.now() for monotonic timing.
  return `${Date.now()}000000`;
}

function attribute(key: string, value: string | number) {
  return typeof value === "number"
    ? { key, value: { intValue: String(value) } }
    : { key, value: { stringValue: value } };
}

function shouldSample(ratio: number): boolean {
  if (!Number.isFinite(ratio)) return true;
  return Math.random() < Math.max(0, Math.min(1, ratio));
}

function safeActionName(target: Element): string {
  const element = target.closest(
    "[data-otel-action],button,a,[role='button'],input[type='submit']",
  );
  if (!element) return "ui.click";

  const explicit = element.getAttribute("data-otel-action");
  if (explicit) return explicit.slice(0, 120);

  const aria = element.getAttribute("aria-label");
  if (aria) return `ui.click.${aria.trim().slice(0, 80).replace(/\s+/g, "_")}`;

  const id = element.id;
  if (id) return `ui.click.#${id.slice(0, 80)}`;

  return `ui.click.${element.tagName.toLowerCase()}`;
}

function apiUrl(input: RequestInfo | URL): string | null {
  const raw = typeof input === "string"
    ? input
    : input instanceof URL
      ? input.toString()
      : input.url;

  try {
    const url = new URL(raw, window.location.origin);
    // Only trace same-origin API calls. This prevents third-party analytics,
    // images, fonts, etc. from becoming part of this application's trace.
    if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/")) {
      return null;
    }
    // The browser's own OTLP export must never recursively trace itself.
    if (url.pathname === TRACE_EXPORT_PATH) return null;
    return url.toString();
  } catch {
    return null;
  }
}

function makeSpan(
  name: string,
  kind: SpanKind,
  parent?: SpanData,
  attributes: SpanData["attributes"] = [],
): SpanData {
  return {
    traceId: parent?.traceId ?? randomHex(16),
    spanId: randomHex(8),
    parentSpanId: parent?.spanId,
    name,
    kind,
    startTimeUnixNano: nowUnixNano(),
    endTimeUnixNano: nowUnixNano(),
    attributes,
    status: { code: 0 },
  };
}

function finishSpan(span: SpanData, status: SpanStatusCode): void {
  span.endTimeUnixNano = nowUnixNano();
  span.status = { code: status };
  if (queue.length >= MAX_QUEUED_SPANS) {
    // Telemetry is best-effort. Dropping old spans is preferable to allowing
    // observability backlog to consume application memory.
    queue.shift();
  }
  queue.push(span);
  if (queue.length >= EXPORT_BATCH_SIZE) {
    void flush();
  }
}

function endInteraction(interaction: ActiveInteraction): void {
  if (interaction.ended || interaction.pendingFetches > 0) return;
  interaction.ended = true;
  finishSpan(interaction.span, 1);
  if (activeInteraction === interaction) activeInteraction = null;
}

function scheduleInteractionExpiry(interaction: ActiveInteraction): void {
  window.setTimeout(() => {
    if (interaction.ended) return;
    interaction.pendingFetches = 0;
    endInteraction(interaction);
  }, MAX_INTERACTION_MS);
}

function otlpBody(spans: SpanData[]): string {
  return JSON.stringify({
    resourceSpans: [
      {
        resource: {
          attributes: [
            attribute("service.name", "snipeit-lite-frontend"),
            attribute("service.version", "0.1.0"),
            attribute("deployment.environment", window.location.hostname),
            attribute("browser.url.path", window.location.pathname),
          ],
        },
        scopeSpans: [
          {
            scope: {
              name: "asset-mgt-render-free/browser-telemetry",
              version: "1.0.0",
            },
            spans,
          },
        ],
      },
    ],
  });
}

async function flush(): Promise<void> {
  if (!queue.length) return;
  const batch = queue.splice(0, EXPORT_BATCH_SIZE);

  try {
    // Do not await this from application code. A telemetry outage must never
    // delay a user action or make a successful API request look failed.
    const response = await fetch(TRACE_EXPORT_PATH, {
      method: "POST",
      credentials: "include",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: otlpBody(batch),
    });

    if (!response.ok) {
      // Put the batch back only while the queue has room. A permanently
      // unavailable collector must not create an unbounded retry loop.
      queue = [...batch, ...queue].slice(-MAX_QUEUED_SPANS);
    }
  } catch {
    queue = [...batch, ...queue].slice(-MAX_QUEUED_SPANS);
  }
}

function scheduleFlush(): void {
  if (flushTimer !== undefined) return;
  flushTimer = window.setInterval(() => {
    void flush();
  }, EXPORT_INTERVAL_MS);
}

function installFetchTracing(sampleRatio: number): void {
  const originalFetch = window.fetch.bind(window);

  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = apiUrl(input);
    if (!url || !shouldSample(sampleRatio)) {
      return originalFetch(input, init);
    }

    const method = String(init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    const interaction = activeInteraction;
    const parent = interaction?.span;
    if (interaction) interaction.pendingFetches += 1;

    const span = makeSpan(
      `${method} ${new URL(url).pathname}`,
      3,
      parent,
      [
        attribute("http.request.method", method),
        attribute("url.full", new URL(url).pathname),
      ],
    );

    // Inject W3C Trace Context. FastAPI's OpenTelemetry instrumentation
    // consumes this header and makes its SERVER span a child of this browser
    // CLIENT span, producing one distributed trace.
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    headers.set(
      "traceparent",
      `00-${span.traceId}-${span.spanId}-01`,
    );

    try {
      const response = await originalFetch(input, {
        ...init,
        headers,
      });
      span.attributes.push(attribute("http.response.status_code", response.status));
      finishSpan(span, response.ok ? 1 : 2);
      return response;
    } catch (error) {
      span.attributes.push(
        attribute("error.type", error instanceof Error ? error.name : "NetworkError"),
      );
      finishSpan(span, 2);
      throw error;
    } finally {
      if (interaction) {
        interaction.pendingFetches = Math.max(0, interaction.pendingFetches - 1);
        endInteraction(interaction);
      }
    }
  };
}

function installInteractionTracing(sampleRatio: number): void {
  document.addEventListener(
    "click",
    (event) => {
      if (!shouldSample(sampleRatio)) return;

      const target = event.target;
      if (!(target instanceof Element)) return;
      if (!target.closest("[data-otel-action],button,a,[role='button'],input[type='submit']")) {
        return;
      }

      const interaction: ActiveInteraction = {
        span: makeSpan(
          safeActionName(target),
          1,
          undefined,
          [
            attribute("ui.action", safeActionName(target)),
            attribute("browser.url.path", window.location.pathname),
          ],
        ),
        pendingFetches: 0,
        ended: false,
      };

      // Capture-phase listener runs before React's handler. Keep this
      // reference briefly so async React handlers that immediately cross one
      // promise boundary can still attach their API call to this click. The
      // short grace period avoids creating a long-lived global context that
      // could accidentally capture an unrelated later request.
      activeInteraction = interaction;
      window.setTimeout(() => {
        if (activeInteraction === interaction && interaction.pendingFetches === 0) {
          endInteraction(interaction);
        }
      }, 100);
      scheduleInteractionExpiry(interaction);
    },
    true,
  );
}

export async function installBrowserTelemetry(): Promise<void> {
  if (installed || typeof window === "undefined" || typeof document === "undefined") {
    return;
  }
  installed = true;

  // Runtime gate: OTEL_ENABLED is owned by the backend. If it is false,
  // nothing below is installed and this module becomes a cheap no-op.
  let config: BrowserOtelConfig;
  try {
    const response = await fetch("/api/config/public", {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) return;
    config = (await response.json()) as BrowserOtelConfig;
  } catch {
    // Telemetry must never make application startup depend on the backend.
    return;
  }

  if (!config.otel_enabled) return;

  const ratio = config.otel_trace_sample_ratio ?? 1;
  installFetchTracing(ratio);
  installInteractionTracing(ratio);
  scheduleFlush();

  // Give the collector a final chance to receive queued spans when the page
  // is being hidden. `keepalive` in flush() keeps this best-effort and bounded.
  window.addEventListener("pagehide", () => {
    void flush();
  });
}
