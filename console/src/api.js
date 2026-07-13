// ND-1 API client: a thin fetch wrapper for alphaos/api's read-only FastAPI
// backend. Every request carries the custom X-AlphaOS-Console header
// ConsoleSecurityMiddleware requires on every /api/* route (alphaos/api/
// security.py) -- this is the ONE place that header is set, so every call
// site below gets it for free, satisfying "every fetch sends
// X-AlphaOS-Console: 1" without repeating it at each call site.
const HEADERS = { 'X-AlphaOS-Console': '1' };

export async function apiGet(path) {
  const res = await fetch(`/api/v1${path}`, { headers: HEADERS });
  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

export const getHealth = () => apiGet('/health');
export const getAnnunciator = () => apiGet('/annunciator');
export const getTonight = () => apiGet('/tonight');
export const getPositions = () => apiGet('/positions');

// ND-2: the remaining 5 views of the 7-view IA (docs/roadmap/
// console-migration-nd.md ND-2 scope). Approvals is VIEW-ONLY -- see
// getApprovals' call site (pages/Approvals.jsx): no approve/reject request
// is ever made from this console; every decision deep-links to Streamlit.
export const getApprovals = () => apiGet('/approvals');
export const getDecisions = () => apiGet('/decisions');
export const getLearning = () => apiGet('/learning');
export const getGovernance = () => apiGet('/governance');
export const getSystem = () => apiGet('/system');
export const getTradePacket = (params) => {
  const qs = new URLSearchParams(params).toString();
  return apiGet(`/system/trade-packet${qs ? `?${qs}` : ''}`);
};

// The Streamlit app's own address -- kept as a deep link for the general
// "open the full app" affordance (Tonight's footer link) and as the
// documented break-glass fallback (docs/roadmap/console-migration-nd.md §2
// item 8: "Streamlit stays runnable and unmodified... until ND-5
// retirement"). As of ND-4, no write-capable action in this console still
// requires following this link -- scan/monitor/report and kill-switch
// ENGAGE moved here in ND-3; approve/reject and kill-switch DISENGAGE move
// here in ND-4 (postApprove/postReject/postKillSwitchDisengage below).
export const STREAMLIT_URL = 'http://localhost:8502';

// ND-3 write routes (docs/roadmap/console-migration-nd.md §4 ND-3 scope).
// Every write POSTs `{ pin, nonce, ...extra }` in the request BODY (never a
// URL param/query string -- ND-3 plan doc §5) and carries the same
// X-AlphaOS-Console header every read already sends. A non-2xx response's
// JSON `detail` (FastAPI's own error shape) becomes the thrown Error's
// `.detail`/`.message`, and its HTTP status becomes `.status` -- callers
// (PinPrompt.jsx) use `.status`/`.detail` to show a specific message
// (invalid PIN / replay / locked out / PIN not configured) rather than a
// generic "failed".
export async function apiPost(path, body) {
  const res = await fetch(`/api/v1${path}`, {
    method: 'POST',
    headers: { ...HEADERS, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  let data = {};
  try {
    data = await res.json();
  } catch {
    /* a non-JSON error body (rare) still falls through to the generic message below */
  }
  if (!res.ok) {
    const err = new Error(data.detail || `HTTP ${res.status}`);
    err.status = res.status;
    err.detail = data.detail;
    throw err;
  }
  return data;
}

export const postScan = (pin, nonce) => apiPost('/actions/scan', { pin, nonce });
export const postMonitor = (pin, nonce) => apiPost('/actions/monitor', { pin, nonce });
export const postReport = (pin, nonce) => apiPost('/actions/report', { pin, nonce });
export const postKillSwitchEngage = (pin, nonce, reason) =>
  apiPost('/actions/kill-switch/engage', { pin, nonce, reason });

// ND-4 write routes (docs/roadmap/console-migration-nd.md §4 ND-4 scope).
// Same envelope discipline as the ND-3 writes above: pin/nonce in the POST
// body, never a URL param. `approveMargin` is passed straight through to
// `POST /actions/approve`'s `approve_margin` field -- the server neither
// invents nor defaults it away from what the caller (Approvals.jsx's
// per-proposal checkbox state) explicitly supplies.
export const postApprove = (pin, nonce, proposalId, approveMargin) =>
  apiPost('/actions/approve', { pin, nonce, proposal_id: proposalId, approve_margin: approveMargin });
// `reason` may be omitted (undefined) -- JSON.stringify drops an undefined
// field entirely, so the server sees no `reason` key at all and applies
// its own "user rejected" default (see write_routes.py's actions_reject),
// exactly matching Streamlit's own no-required-reason "Reject" button.
export const postReject = (pin, nonce, proposalId, reason) =>
  apiPost('/actions/reject', { pin, nonce, proposal_id: proposalId, reason });
export const postKillSwitchDisengage = (pin, nonce) =>
  apiPost('/actions/kill-switch/disengage', { pin, nonce });
