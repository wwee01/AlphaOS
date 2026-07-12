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

// ND-1/ND-2 have zero write affordances (docs/roadmap/console-migration-nd.md
// ND-1 non-goals: "no writes"; ND-2 Approvals is explicitly view-only). Every
// action-suggesting element links out to the Streamlit app instead, which
// still owns Approve/Reject/kill-switch/etc. until ND-3+ moves writes into
// this console.
export const STREAMLIT_URL = 'http://localhost:8502';
