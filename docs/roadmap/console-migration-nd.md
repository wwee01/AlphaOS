# AlphaOS Console — NightDesk-pattern migration (ND-1 … ND-5)

**Status: APPROVED — operator decision 2026-07-12.**
Owner: Fable5 (plan) · Sonnet (build) · Opus ×2 per phase (audit) · CK (merge authority).

---

## 0. Decision record

The operator approved a **full, phased migration** of AlphaOS's human interface
from Streamlit to a NightDesk-pattern console: a hand-authored React frontend
talking to a small local API over the same SQLite journal. Rationale, in the
operator's own framing: AlphaOS is meant to run **near-autonomously with
minimal intervention** once stable — the daily human interaction becomes
*glance, check, occasionally approve*, which favors a purpose-built,
mobile-quality, live-updating console over a widget dashboard. Secondary
driver: the Streamlit substrate has a proven fidelity ceiling (~65–70% of the
Stitch reference designs — see the 2026-07-12 Fable5 ruling in
`alphaos-ui-ux-design.md`); PR-UI-B4 reached that ceiling and further visual
investment there has no headroom.

Precedent: the NightDesk app (sibling project, `../nightdesk`) and the SG card
tracker both run this exact pattern (React + Vite frontend, local server,
SQLite, LaunchAgent) successfully on this machine.

## 1. Architecture ruling

**Frontend — NightDesk's pattern, adopted:**
React 19 + Vite, in a new top-level `console/` directory. One small token CSS
file (design tokens adapted from `ported/stitch-design-tokens.md`, same
palette family the Streamlit theme already uses); component layout
hand-authored in JSX; system font stacks only. Test runner: vitest.

**API — NightDesk's runtime, deliberately NOT adopted:**
The API is **Python (FastAPI + uvicorn), not Express/Node**, living in
`alphaos/api/`. Reason, and this is the load-bearing decision of the whole
migration: every safety gate (approval-time freshness/drift/spread re-checks,
kill switch, risk caps) and every view computation (`build_daily_brief`,
positions-health verdicts, `governance_report`, TTL math) already exists in
twice-audited Python. A Node API would force reimplementing exactly the logic
we must never fork. The FastAPI endpoints are thin wrappers over the same
functions `streamlit_app.py` calls today — **the frontend computes nothing
business-critical, ever; it formats and displays.**

**Data:** same journal SQLite DB. During read-only phases the API opens it
with SQLite's native read-only mode (`file:...?mode=ro`) so writes are
*structurally impossible*, not merely absent.

**Serving:** loopback only (same OPS-A posture as the dashboard), default port
**8601**. The built `console/dist/` is served by the same FastAPI process —
one process, one port, one LaunchAgent (added in ND-2).

## 2. Non-negotiable invariants (carried over from the UX doc & rulings)

These survive the substrate change; the UX doc's visual/content grammar
applies to ANY frontend:

1. **Loopback-only**, verified at runtime, plus API startup is
   **side-effect-free** (no scheduler start, no provider call, no scan — import
   and bind, nothing else).
2. **Zero external browser calls** — no CDN, no webfonts, no analytics.
   Everything vendored. (B1 audit-fixup precedent.)
3. **Quarantine the script** — Stitch `code.html` markup/layout may now be
   adapted (that's the point), but no mockup **text, values, labels, or
   states** ever ship: no "LIVE" badge, no fabricated limits, no
   auto-liquidation copy, no wrong autonomy level, no inverted ΔR.
4. **§13 calm-console rules**: no flashing/pulsing; live updates are honest
   (data actually refreshed) with a visible "as of" stamp; if the API is
   unreachable the console says so — it never silently shows stale numbers.
5. **Unknown-never-zero**, R-not-dollars in positions, TQS score+confidence
   always paired.
6. **All gates server-side.** The browser is untrusted. No client-side
   validation ever substitutes for a server check.
7. **Every write endpoint requires the PIN** (see §3) and lands an audit
   record in the journal.
8. **Streamlit stays runnable and unmodified** (bugfixes only) until ND-5
   retirement criteria are met. It is the break-glass fallback.

## 3. Security model (the one genuinely new surface)

A localhost HTTP API is reachable by any web page the operator's browser
visits (`fetch("http://localhost:8601/...")`). Streamlit had built-in XSRF
protection; we must build our own, from day one, even for reads:

- **Origin/Host allowlist middleware**: requests must carry no Origin (CLI/
  curl) or an allowlisted loopback Origin; anything else → 403. Applied in
  ND-1, before any write exists.
- **Custom header requirement** (`X-AlphaOS-Console: 1`) on all `/api/*`
  routes — defeats simple-form CSRF, forces a CORS preflight that the
  allowlist then kills.
- **PIN for writes** (ND-3+): set via `alphaos console set-pin`, stored
  hashed (scrypt/argon2-style KDF, never plaintext), required per write
  request. NightDesk's own design, adopted because it is doing real security
  work against exactly this threat.
- **Idempotency**: write endpoints take a client nonce; server rejects
  replays and re-validates state transitions (an already-decided proposal
  cannot be re-approved regardless of nonce).
- **Read-only DB mode** in ND-1/ND-2 (structural write impossibility).

## 4. Phases

Each phase = one PR, own branch, own worktree, full T4 protocol (Sonnet
build → 2 independent parallel Opus audits: correctness + scope/safety → fix →
full pytest + ruff + mypy + vitest → commit → **hold for explicit operator
merge instruction**). Swap-test discipline applies to new guards.

### ND-1 — Read-only cockpit: Tonight screen (STARTS NOW)
- `alphaos/api/` FastAPI app, read-only DB, endpoints:
  - `GET /api/v1/health` — server self-check + DB path + `as_of` timestamp.
  - `GET /api/v1/annunciator` — mode, kill-switch state, autonomy level line,
    heartbeat, open R, approvals pending (the strip's exact fields).
  - `GET /api/v1/tonight` — the `build_daily_brief()` dict verbatim.
  - `GET /api/v1/positions` — the positions-health view (verdicts, R fields).
- Security middleware from §3 (origin allowlist + custom header), loopback
  bind, side-effect-free startup.
- `console/` scaffold: Vite + React 19 + vitest, token CSS, **Tonight page
  only** — annunciator strip, ①–⑦ blocks, positions summary; polling ~10s
  with "as of" stamp + unreachable banner; deep links to the Streamlit app
  (`http://localhost:8502`) for every action.
- Tests: Python contract tests via FastAPI TestClient in the existing suite
  (security middleware tests included: bad origin → 403, missing header →
  403, write verb → 405); vitest for formatting helpers.
- Explicit non-goals: no LaunchAgent, no writes, no second screen, no PIN yet.
- Exit: operator looks at it and says continue/adjust/stop.

### ND-2 — Read coverage: the full 7-view IA
- Views per the B5 information architecture (now built HERE, not in
  Streamlit): Tonight · Positions · Approvals (view-only + TTL bars) ·
  Decisions · Learning · Autonomy & Risk · System & Audit.
- Read endpoints to power them (still `mode=ro`).
- LaunchAgent `com.ck.alphaos.console.plist` + deploy script (`npm ci &&
  npm run build` + uvicorn), README/OPERATIONS notes.
- Mobile pass per UX doc §16 (its principles transfer; its Streamlit
  implementation details do not).

### ND-3 — Write plumbing + least-dangerous writes
- PIN infrastructure (§3), audit-record-per-write, nonce/idempotency
  framework. DB connection becomes read-write **for named routes only**.
- First writes (worst case = extra journaled scan data, no order path):
  run scan_once · run monitor_once · generate daily report.
- **Kill-switch ENGAGE** also lands here — deliberately early, because its
  failure mode is safety-increasing (false engage = system pauses). Disengage
  does NOT land here.
- Streamlit sidebar remains fully functional in parallel.

### ND-4 — The crown jewels, last
- Kill-switch **disengage** (PIN).
- **Approve / Reject** (PIN): endpoint calls the same
  `orch.approve_proposal()` / `reject_proposal()` — gates re-run server-side
  inside the same functions as today. Margin-approval checkbox semantics
  preserved. Double-submit, TTL-expired, and replay tests are mandatory and
  swap-tested. Heaviest audit pass of the migration; audits should actively
  attempt CSRF/replay/double-approve against a running instance.

### ND-5 — Parallel run + retirement decision
- ≥10 trading days both consoles live. Exit criteria: zero write-path
  discrepancies between consoles; no unexplained API errors in normal
  operation; operator states preference for the console.
- Then: Streamlit demoted to break-glass (kept in repo, kept tested, its
  LaunchAgent/port documentation marked "fallback"). **Never deleted.**
- Rollback at ANY phase = stop the console process; Streamlit was never
  touched.

## 5. Supersessions & scope notes

- **PR-UI-B5 (Streamlit nav consolidation) is CANCELLED** — its 7-view IA
  ships in ND-2 instead. Do not build it twice.
- UX doc §16 (mobile) now targets the console; the Streamlit mobile pass
  (M1) stays as-shipped for the fallback.
- Streamlit dashboard is **feature-frozen** (bugfixes only) from this date.
- New deps: `fastapi`, `uvicorn` (new optional-dependency group `api` in
  pyproject), `httpx` (test group). Node >=22 already on the machine
  (NightDesk requires it).

## 6. Risk register (honest)

| Risk | Mitigation |
|---|---|
| Localhost CSRF from browser tabs | §3 stack: origin allowlist + custom header + PIN + preflight kill — in place before any write exists |
| Forked business logic drifting | API wraps the exact Python functions Streamlit calls; frontend displays only — enforced at audit |
| Double-submit / replay on approvals | Nonce + server-side state-transition guard + mandatory swap-tested tests (ND-4) |
| Dishonest staleness on a "live" console | "As of" stamp + unreachable banner (§2.4); no fabricated freshness |
| Migration limbo (half-built, half-audited) | Strict phase gates; Streamlit untouched and running until ND-5 criteria met |
| Two-language maintenance tax | Accepted knowingly (operator decision); bounded by "frontend computes nothing" rule |
