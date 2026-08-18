// =============================================================================
// js/api.js
// -----------------------------------------------------------------------------
// Pure networking layer. Nothing in this file knows about the DOM, forms, or
// which page it's running on -- it only knows how to reach the FastAPI
// backend and how to attach the current session's Authorization header.
//
// Every other module imports `apiRequest` (or `API_URL` directly, for the
// one raw-blob download in components/audit.js) from here instead of calling
// `fetch()` itself, so there's exactly one place that knows the backend's
// base URL and exactly one place that knows how 401s should be handled.
//
// API_URL IS A RELATIVE PATH ON PURPOSE -- see nginx/default.conf.template.
// This app is always served BY the same nginx reverse proxy that also
// forwards /api/* to the FastAPI backend under the hood. That means this
// file never needs to know the backend's real hostname, and never needs to
// change between local Docker Compose, Render, and cloud -- nginx is the
// only thing that knows where the backend actually lives in each
// environment. Do NOT hardcode a host/port here; if you ever need to point
// this frontend at a backend that ISN'T behind this same reverse proxy,
// fix that in nginx (BACKEND_HOST/BACKEND_PORT), not here.
// =============================================================================

import { getSession, logout } from './auth.js';
import { reportClientError, setLastRequestId } from './errorbeacon.js';

export const API_URL = '/api';

export class MaintenanceModeError extends Error { constructor(message = "The application is currently undergoing maintenance.") { super(message); this.name = "MaintenanceModeError"; this.status = 503; } }

function dispatchMaintenanceIfNeeded(response, body) {
  if (response?.status === 503 && body?.code === "MAINTENANCE_MODE") {
    window.dispatchEvent(new Event("asset-app:maintenance"));
    return true;
  }
  return false;
}

// Small fetch wrapper that automatically attaches the Authorization header
// and JSON-parses the response, throwing a readable Error on failure.
export async function apiRequest(path, options = {}) {
  const session = getSession();
  const headers = Object.assign({}, options.headers || {});
  if (session && session.token) headers['Authorization'] = `Bearer ${session.token}`;
  // Don't force a JSON content-type when sending FormData (CSV upload) --
  // the browser needs to set its own multipart boundary header for that.
  if (!(options.body instanceof FormData) && options.body) {
    headers['Content-Type'] = 'application/json';
  }

  let response;
  try {
    response = await fetch(`${API_URL}${path}`, { ...options, headers, credentials: 'include' });
  } catch (err) {
    reportClientError(err, { source: 'api.network', endpoint: path });
    throw err;
  }

  const requestId = response.headers.get('x-request-id');
  if (requestId) setLastRequestId(requestId);

  if (response.status === 401) {
    // Session expired or invalid -- force a clean re-login.
    logout();
    throw new Error('Your session has expired. Please log in again.');
  }

  // Some endpoints (CSV export) return a raw file, not JSON.
  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('application/json')) {
    if (!response.ok) { if (response.status >= 500) reportClientError(new Error(`HTTP ${response.status} ${path}`), { source: 'api.http', endpoint: path, status: response.status }); throw new Error(buildErrorMessage(response, {})); }
    return response;
  }

  const data = await response.json();
  if (!response.ok) {
    if (response.status >= 500) reportClientError(new Error(buildErrorMessage(response, data)), { source: 'api.http', endpoint: path, status: response.status });
    throw new Error(buildErrorMessage(response, data));
  }
  return data;
}

// FastAPI returns `detail` as a plain string for most errors (e.g. our own
// `raise HTTPException(status_code=400, detail="...")` calls), but as an
// ARRAY of {loc, msg, type} objects for Pydantic validation failures (422s)
// -- e.g. the password-strength or due-date validators in schemas/*.py.
// Without this, `new Error(anArray)` would stringify to something useless
// like "[object Object]" instead of the actual validation message.
function formatErrorDetail(detail, fallback = 'Request failed.') {
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => (item && typeof item === 'object' && 'msg' in item ? item.msg : String(item)))
      .join(' ');
  }
  return fallback;
}

// Builds the message actually shown to the person, with the request's
// correlation ID appended when one is available -- see
// backend/middleware/error_handling.py's module docstring: its whole point
// is to hand the caller a `request_id` they can give to support, but that
// only helps if the UI actually surfaces it instead of quietly dropping it
// (which is exactly what this function used to do before this fix).
// backend/middleware/request_context.py stamps an `X-Request-ID` response
// header onto EVERY response (success or failure -- not just the generic
// unhandled-exception 500 whose JSON body also happens to carry
// `request_id`), so reading it off the response here works uniformly for
// any failed request. Exported so js/auth.js's few raw (non-apiRequest)
// fetch calls -- login(), confirmMfaSetup(), verifyMfa(), which run before
// a session/Authorization header exists -- can build the same kind of
// message instead of dropping the ID like they used to.
export function buildErrorMessage(response, data, fallback) {
  const detailMessage = formatErrorDetail(data && data.detail, fallback);
  const requestId = (data && data.request_id) || (response && response.headers && response.headers.get('x-request-id'));
  return requestId ? `${detailMessage} (Reference: ${requestId})` : detailMessage;
}
