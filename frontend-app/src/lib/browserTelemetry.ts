/**
 * browserTelemetry.ts
 * -------------------
 * Small, dependency-free browser tracing bridge.
 *
 * The browser emits only safe, application-owned telemetry. It never copies
 * cookies, Authorization headers, request/response bodies, form values,
 * passwords, MFA codes, reset tokens, or arbitrary DOM text into spans.
 *
 * TRACE SHAPE:
 *   ui.click.checkout
 *       └── checkout.complete
 *              └── POST /api/assets/:id/checkout_advanced
 *                     └── FastAPI server span
 *                            └── PostgreSQL / Redis / Celery
 *
 * OTEL_ENABLED is a complete runtime off-switch. When disabled, this module
 * does not install click/fetch instrumentation and does not export anything.
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
  attributes: Array<{ key: string; value: { stringValue?: string; intValue?: string; boolValue?: boolean } }>;
  status: { code: SpanStatusCode };
}

export type BrowserTelemetrySpan = SpanData;

interface ActiveInteraction {
  span: SpanData;
  pendingFetches: number;
  pendingOperations: number;
  ended: boolean;
}

const TRACE_EXPORT_PATH = "/api/telemetry/traces";
const EXPORT_BATCH_SIZE = 10;
const EXPORT_INTERVAL_MS = 5000;
const MAX_QUEUED_SPANS = 100;
const MAX_INTERACTION_MS = 10_000;
const SAFE_ACTION_RE = /^ui\.click\.[a-z0-9][a-z0-9._-]{0,78}$/;
const SAFE_OPERATION_RE = /^[a-z][a-z0-9._-]{0,78}$/;

let installed = false;
let queue: SpanData[] = [];
let flushTimer: number | undefined;
let activeInteraction: ActiveInteraction | null = null;
let sampleRatio = 1;
let originalFetch: typeof window.fetch | null = null;

function randomHex(bytes: number): string {
  const values = new Uint8Array(bytes);
  crypto.getRandomValues(values);
  return Array.from(values, (value) => value.toString(16).padStart(2, "0")).join("");
}

function nowUnixNano(): string {
  return `${Date.now()}000000`;
}

function attribute(key: string, value: string | number | boolean) {
  if (typeof value === "number") return { key, value: { intValue: String(value) } };
  if (typeof value === "boolean") return { key, value: { boolValue: value } };
  return { key, value: { stringValue: value } };
}

function shouldSample(ratio: number): boolean {
  if (!Number.isFinite(ratio)) return true;
  return Math.random() < Math.max(0, Math.min(1, ratio));
}

function sanitizeOperationName(name: string, fallback = "app.operation"): string {
  const candidate = name.trim().toLowerCase().slice(0, 80);
  return SAFE_OPERATION_RE.test(candidate) ? candidate : fallback;
}

function safeActionName(target: Element): string {
  // IMPORTANT: do not derive telemetry names from aria-label, visible text,
  // element IDs, input values, or other DOM content. Those can contain names,
  // email addresses, ticket numbers, or other information the user did not
  // intend to put into telemetry. Important actions opt in explicitly.
  const element = target.closest("[data-otel-action]");
  const explicit = element?.getAttribute("data-otel-action")?.trim().toLowerCase() ?? "";
  if (!explicit) return "ui.click";
  const candidate = explicit.startsWith("ui.click.") ? explicit : `ui.click.${explicit}`;
  return SAFE_ACTION_RE.test(candidate) ? candidate : "ui.click";
}

function apiPath(input: RequestInfo | URL): string | null {
  const raw = typeof input === "string"
    ? input
    : input instanceof URL
      ? input.toString()
      : input.url;

  try {
    const url = new URL(raw, window.location.origin);
    if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/")) return null;
    if (url.pathname === TRACE_EXPORT_PATH) return null;
    return safeApiPath(url.pathname);
  } catch {
    return null;
  }
}

function safeApiPath(pathname: string): string {
  // Keep Jaeger useful without copying dynamic identifiers/filenames into the
  // browser trace. Backend HTTP spans remain authoritative for route details.
  return pathname
    .replace(/\/(?:[0-9]+)(?=\/|$)/g, "/:id")
    .replace(/\/[0-9a-f]{8}-[0-9a-f-]{27,36}(?=\/|$)/gi, "/:id")
    .replace(/\/(restore|download)\/[^/]+(?=\/|$)/g, "/$1/:file")
    .slice(0, 240);
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
  if (queue.length >= MAX_QUEUED_SPANS) queue.shift();
  queue.push(span);
  if (queue.length >= EXPORT_BATCH_SIZE) void flush();
}

function endInteraction(interaction: ActiveInteraction): void {
  if (
    interaction.ended ||
    interaction.pendingFetches > 0 ||
    interaction.pendingOperations > 0
  ) return;
  interaction.ended = true;
  finishSpan(interaction.span, 1);
  if (activeInteraction === interaction) activeInteraction = null;
}

function scheduleInteractionExpiry(interaction: ActiveInteraction): void {
  window.setTimeout(() => {
    if (interaction.ended) return;
    interaction.pendingFetches = 0;
    interaction.pendingOperations = 0;
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
            attribute("deployment.environment", "browser"),
          ],
        },
        scopeSpans: [
          {
            scope: {
              name: "asset-mgt-render-free/browser-telemetry",
              version: "1.1.0",
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
    const fetcher = originalFetch ?? window.fetch.bind(window);
    const response = await fetcher(TRACE_EXPORT_PATH, {
      method: "POST",
      credentials: "include",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: otlpBody(batch),
    });

    if (!response.ok) {
      queue = [...batch, ...queue].slice(-MAX_QUEUED_SPANS);
    }
  } catch {
    queue = [...batch, ...queue].slice(-MAX_QUEUED_SPANS);
  }
}

function scheduleFlush(): void {
  if (flushTimer !== undefined) return;
  flushTimer = window.setInterval(() => void flush(), EXPORT_INTERVAL_MS);
}

function createBusinessSpan(name: string, attributes: SpanData["attributes"] = []): {
  span: SpanData | null;
  interaction: ActiveInteraction | null;
} {
  if (!installed) return { span: null, interaction: null };

  const interaction = activeInteraction;
  if (interaction) {
    interaction.pendingOperations += 1;
    return {
      span: makeSpan(sanitizeOperationName(name), 1, interaction.span, attributes),
      interaction,
    };
  }

  if (!shouldSample(sampleRatio)) return { span: null, interaction: null };
  return {
    span: makeSpan(sanitizeOperationName(name), 1, undefined, attributes),
    interaction: null,
  };
}

export async function runBusinessOperation<T>(
  name: string,
  operation: (parentSpan: BrowserTelemetrySpan | null) => Promise<T>,
  attributes: SpanData["attributes"] = [],
): Promise<T> {
  const { span, interaction } = createBusinessSpan(name, attributes);
  if (!span) return operation(null);

  try {
    const result = await operation(span);
    finishSpan(span, 1);
    return result;
  } catch (error) {
    span.attributes.push(
      attribute("error", true),
      attribute("error.type", error instanceof Error ? error.name : "Error"),
    );
    finishSpan(span, 2);
    throw error;
  } finally {
    if (interaction) {
      interaction.pendingOperations = Math.max(0, interaction.pendingOperations - 1);
      endInteraction(interaction);
    }
  }
}

/**
 * API layer entrypoint. `parentSpan === undefined` means "normal tracing
 * rules"; `null` means the enclosing business operation was not sampled;
 * a SpanData value means this fetch MUST continue that sampled trace.
 */
export async function telemetryFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
  parentSpan?: BrowserTelemetrySpan | null,
): Promise<Response> {
  const fetcher = originalFetch ?? window.fetch.bind(window);
  const path = apiPath(input);

  if (!installed || !path || parentSpan === null) {
    return fetcher(input, init);
  }

  let spanParent = parentSpan;
  let interactionForFetch: ActiveInteraction | null = null;
  if (spanParent === undefined) {
    const interaction = activeInteraction;
    if (interaction) {
      interactionForFetch = interaction;
      spanParent = interaction.span;
      interaction.pendingFetches += 1;
    } else if (shouldSample(sampleRatio)) {
      spanParent = undefined;
    } else {
      return fetcher(input, init);
    }
  } else if (spanParent) {
    // Business operation owns the trace. This fetch is not independently
    // sampled, preventing click/business/fetch trace splitting.
  }

  const method = String(init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
  const span = makeSpan(
    `${method} ${path}`,
    3,
    spanParent ?? undefined,
    [
      attribute("http.request.method", method),
      attribute("url.path", path),
    ],
  );

  const headers = new Headers(
    init?.headers ?? (input instanceof Request ? input.headers : undefined),
  );
  headers.set("traceparent", `00-${span.traceId}-${span.spanId}-01`);

  try {
    const response = await fetcher(input, { ...init, headers });
    span.attributes.push(attribute("http.response.status_code", response.status));
    if (!response.ok) span.attributes.push(attribute("error", true));
    finishSpan(span, response.ok ? 1 : 2);
    return response;
  } catch (error) {
    span.attributes.push(
      attribute("error", true),
      attribute("error.type", error instanceof Error ? error.name : "NetworkError"),
    );
    finishSpan(span, 2);
    throw error;
  } finally {
    if (interactionForFetch) {
      interactionForFetch.pendingFetches = Math.max(0, interactionForFetch.pendingFetches - 1);
      endInteraction(interactionForFetch);
    }
  }
}

function installInteractionTracing(): void {
  document.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (!target.closest("[data-otel-action]")) return;

      const action = safeActionName(target);
      if (action === "ui.click" || !shouldSample(sampleRatio)) return;

      const interaction: ActiveInteraction = {
        span: makeSpan(
          action,
          1,
          undefined,
          [
            attribute("ui.action", action),
            attribute("browser.url.path", safeApiPath(window.location.pathname)),
          ],
        ),
        pendingFetches: 0,
        pendingOperations: 0,
        ended: false,
      };

      activeInteraction = interaction;
      window.setTimeout(() => {
        if (
          activeInteraction === interaction &&
          interaction.pendingFetches === 0 &&
          interaction.pendingOperations === 0
        ) {
          endInteraction(interaction);
        }
      }, 100);
      scheduleInteractionExpiry(interaction);
    },
    true,
  );
}

export async function installBrowserTelemetry(): Promise<void> {
  if (installed || typeof window === "undefined" || typeof document === "undefined") return;

  // Runtime gate is owned by the backend. The application continues rendering
  // immediately; this initialization is deliberately fire-and-forget.
  let config: BrowserOtelConfig;
  try {
    const response = await window.fetch("/api/config/public", {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) return;
    config = (await response.json()) as BrowserOtelConfig;
  } catch {
    return;
  }

  if (!config.otel_enabled) return;

  sampleRatio = config.otel_trace_sample_ratio ?? 1;
  originalFetch = window.fetch.bind(window);
  installed = true;
  installInteractionTracing();
  scheduleFlush();

  window.addEventListener("pagehide", () => void flush());
}
