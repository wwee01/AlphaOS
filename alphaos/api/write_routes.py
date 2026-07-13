"""alphaos/api/write_routes.py -- ND-3 write plumbing + first writes, ND-4
crown-jewel writes (docs/roadmap/console-migration-nd.md §4 ND-3/ND-4
scope).

Every route below wraps the EXACT Orchestrator method (or, for the
kill-switch routes, the exact KillSwitch method) the Streamlit sidebar/
Approval Center already calls (alphaos/dashboard/streamlit_app.py:
render_sidebar, render_annunciator, tab_approval_center) -- no business
logic is re-derived here, same discipline as routes.py's read endpoints
(module docstring: "the frontend computes nothing business-critical,
ever"). What IS new here, and genuinely new surface, is the authorization
gate every route shares (``_authorize_write``): PIN (alphaos/api/pin.py) +
nonce replay guard (alphaos/api/nonce.py), on top of the ND-1
origin-allowlist + ``X-AlphaOS-Console`` header middleware, which already
applies to every ``/api/*`` path (including these -- no extra wiring
needed; ``ConsoleSecurityMiddleware`` matches on the URL prefix, not on
which router registered the route).

Seven routes exist. Exactly seven:

* ``POST /api/v1/actions/scan``    -> ``orch.run_scan_once()``                  (ND-3)
* ``POST /api/v1/actions/monitor`` -> ``orch.run_monitor_once()``               (ND-3)
* ``POST /api/v1/actions/report``  -> ``orch.generate_daily_report()``          (ND-3)
* ``POST /api/v1/actions/kill-switch/engage``    -> ``KillSwitch.engage(reason)``   (ND-3)
* ``POST /api/v1/actions/approve`` -> ``orch.approve_proposal(...)``            (ND-4)
* ``POST /api/v1/actions/reject``  -> ``orch.reject_proposal(...)``             (ND-4)
* ``POST /api/v1/actions/kill-switch/disengage`` -> ``KillSwitch.release()``        (ND-4)

ND-4 closes the loop the original (ND-3) version of this docstring left
open: kill-switch RELEASE and proposal APPROVE/REJECT were "deliberately
ABSENT... Streamlit's sidebar/Approval Center keep sole ownership of those
until ND-4" -- this module is that phase. After this change, no
write-capable action anywhere in this app remains Streamlit-only (docs/
roadmap/console-migration-nd.md §4 ND-4: "the crown jewels, last"); "Seed
demo trade" remains the one deliberate, permanent exception -- it is a
dev/demo convenience never named as an ND-1..ND-5 phase deliverable at all,
so its continued absence here is not a gap ND-4 needed to close.

Every successful write lands an audit record: a `system_events` row tagged
`source: "console_api"` in its `detail_json` (see `_log_console_invocation`
below), whose `event_id` is echoed back in the response body under
`audit.event_id` -- distinguishable from a Streamlit-triggered action in
System & Audit's own event log (`/api/v1/system`'s `recent_events`). scan/
monitor ALSO get this for free a second way: `trigger_source=
TriggerSource.CONSOLE_API.value` threads straight through into
`scheduler_runs.trigger_source` (audit-fixup, correctness L3: NOT
`scan_batches.trigger_source` too, as an earlier version of this docstring
claimed -- `run_scan_once()` hardcodes `scan_batches.source = "cli"`
regardless of `trigger_source`, so `scheduler_runs` is the only table this
value actually reaches; already surfaced in `/api/v1/system`'s
`scheduler_runs` list) -- see constants.py's `TriggerSource.CONSOLE_API`
docstring for why that is a DIFFERENT value from `SCHEDULER`, not an alias
of it. `generate_daily_report()`, `approve_proposal()`/`reject_proposal()`,
and both kill-switch routes have no such trigger_source column to ride, so
the explicit system_events write is the only marker they get -- which is
exactly why every route gets the SAME uniform `_log_console_invocation`
call rather than relying on the free channel alone for the routes that lack
a free one.

approve/reject add NO new idempotency logic beyond the shared nonce guard
above: `approve_proposal()`'s own pre-existing belt-and-suspenders check
(an existing live, non-dead entry order for the proposal blocks a second
approval outright, regardless of which nonce carried the second request --
see its docstring in alphaos/orchestrator.py, around the "Idempotency
(belt-and-suspenders on top of the status guard)" comment) is trusted and
verified by test here, never duplicated. `tests/test_api_console_nd4.py`
proves this holds across two DIFFERENT nonces for the same proposal_id (a
genuine double-click racing ahead of any client-side debounce), not merely
a byte-identical replay (which the nonce guard alone already catches).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from alphaos.api.deps import (
    get_kill_switch,
    get_nonce_store,
    get_pin_store,
    get_rate_limiter,
    get_settings,
    get_write_journal,
)
from alphaos.api.nonce import NonceStore
from alphaos.api.pin import PinRateLimiter, PinStore
from alphaos.config.settings import Settings
from alphaos.constants import Severity, TriggerSource
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.safety import KillSwitch
from alphaos.util import timeutils

router = APIRouter(prefix="/api/v1/actions")


def _as_of() -> str:
    return timeutils.to_iso(timeutils.now_utc())


class WriteAuth(BaseModel):
    """Every write request's common envelope. `pin`/`nonce` are REQUIRED
    (no default) -- FastAPI/pydantic already 422s a request missing either,
    before `_authorize_write` ever runs. Sent in the POST body, never a URL
    param/query string (ND-3 plan doc: "no secrets in URLs/query strings/
    logs") -- console/src/api.js's apiPost() is the only call site that
    constructs this shape."""

    pin: str
    nonce: str


class KillSwitchEngageRequest(WriteAuth):
    """`reason` is REQUIRED (matches `KillSwitch.engage(reason)`'s own
    signature) and must be non-empty after stripping whitespace -- checked
    explicitly below because pydantic alone would accept `""` as a valid
    `str`. The console UI supplies a client-side default ("Engaged from
    console") when the operator leaves the field blank, but the WIRE
    contract still requires the field to be present and meaningful; the
    server never fabricates a reason on the operator's behalf."""

    reason: str


class ProposalApproveRequest(WriteAuth):
    """`proposal_id` identifies the target row. `approve_margin` mirrors
    `orch.approve_proposal()`'s own keyword of the exact same name and
    defaults to ``False`` -- same as the method itself, and same as
    Streamlit's checkbox (`st.checkbox(...)` defaults unchecked) -- a
    proposal's margin/short requirement is NEVER silently approved by
    omitting the field. The value is passed straight through to
    `approve_proposal()` unexamined; this route invents no additional
    margin-gating logic of its own (docs/roadmap/console-migration-nd.md §4
    ND-4: "do not invent new blocking logic in the API layer, just pass the
    flag through and assert the existing behavior surfaces correctly")."""

    proposal_id: str
    approve_margin: bool = False


class ProposalRejectRequest(WriteAuth):
    """`proposal_id` identifies the target row. `reason` is OPTIONAL --
    `orch.reject_proposal()` itself defaults to `"user rejected"` when its
    own `reason` keyword is omitted, and this route matches that default
    exactly (see `actions_reject` below) rather than forcing the operator
    to type something Streamlit's own "Reject" button doesn't require
    either."""

    proposal_id: str
    reason: Optional[str] = None


def _authorize_write(
    auth: WriteAuth, pin_store: PinStore, rate_limiter: PinRateLimiter, nonce_store: NonceStore,
) -> None:
    """The one shared gate every write route calls FIRST, before touching
    the journal or the Orchestrator. Order matters and is pinned by the
    ND-3 test matrix (tests/test_api_console_nd3.py):

    1. No PIN configured at all -> 503 (fail CLOSED, never fail open --
       ND-3 plan doc: "No PIN set yet -> all writes return 503... never
       fail open").
    2. Currently locked out (too many recent consecutive failures) -> 429.
    3. Wrong PIN -> 401 (records a failure against the rate limiter) --
       kept a DISTINCT status from the origin/header middleware's 403, per
       the plan doc's own instruction ("403 is the existing origin/header
       middleware's territory").
    4. Nonce already used (replay) -> 409 -- checked AFTER the PIN
       succeeds, so a wrong-PIN attempt never burns a legitimate nonce.
    """
    if not pin_store.is_configured():
        raise HTTPException(status_code=503, detail="PIN not configured — run `alphaos console set-pin`")
    if rate_limiter.is_locked_out():
        raise HTTPException(
            status_code=429, detail="too many failed PIN attempts — locked out, try again shortly",
        )
    if not pin_store.verify(auth.pin):
        rate_limiter.record_failure()
        raise HTTPException(status_code=401, detail="invalid PIN")
    rate_limiter.record_success()
    if not nonce_store.check_and_record(auth.nonce):
        raise HTTPException(status_code=409, detail="nonce already used (replay)")


def _log_console_invocation(
    journal: JournalStore, category: str, message: str, detail: Optional[dict] = None,
    severity: Severity = Severity.INFO,
) -> str:
    """Writes ONE system_events row tagged `source: "console_api"` in its
    detail_json, and returns the new event_id so the caller can echo it
    back in the response body as `audit.event_id` -- see module docstring's
    "Every successful write lands an audit record" section."""
    full_detail = {"source": "console_api", **(detail or {})}
    return journal.log_system_event(severity, category, message, full_detail)


@router.post("/scan")
def actions_scan(
    auth: WriteAuth,
    settings: Settings = Depends(get_settings),
    write_journal: JournalStore = Depends(get_write_journal),
    pin_store: PinStore = Depends(get_pin_store),
    rate_limiter: PinRateLimiter = Depends(get_rate_limiter),
    nonce_store: NonceStore = Depends(get_nonce_store),
) -> dict:
    """`orch.run_scan_once()` -- the exact call streamlit_app.render_sidebar()
    makes on its "Run scan_once" button. Worst case if abused: extra
    journaled scan/candidate data (ND-3 plan doc: "First writes (worst case
    = extra journaled scan data, no order path)") -- proposals created still
    require separate manual approval (ND-4) before anything executes."""
    _authorize_write(auth, pin_store, rate_limiter, nonce_store)
    orch = Orchestrator(settings=settings, journal=write_journal)
    summary = orch.run_scan_once(trigger_source=TriggerSource.CONSOLE_API.value)
    event_id = _log_console_invocation(
        write_journal, "console_api",
        f"scan_once invoked via console API (scan_batch_id={summary.scan_batch_id}, "
        f"{summary.candidates} candidate(s)).",
        {"scan_batch_id": summary.scan_batch_id, "scheduler_run_id": summary.scheduler_run_id},
    )
    return {"result": summary.as_dict(), "audit": {"event_id": event_id}, "as_of": _as_of()}


@router.post("/monitor")
def actions_monitor(
    auth: WriteAuth,
    settings: Settings = Depends(get_settings),
    write_journal: JournalStore = Depends(get_write_journal),
    pin_store: PinStore = Depends(get_pin_store),
    rate_limiter: PinRateLimiter = Depends(get_rate_limiter),
    nonce_store: NonceStore = Depends(get_nonce_store),
) -> dict:
    """`orch.run_monitor_once()` -- the exact call streamlit_app.
    render_sidebar()'s "Run monitor_once" button makes: stop/target/time
    watchdog over open positions, broker reconciliation, protection
    watchdog pass."""
    _authorize_write(auth, pin_store, rate_limiter, nonce_store)
    orch = Orchestrator(settings=settings, journal=write_journal)
    result = orch.run_monitor_once(trigger_source=TriggerSource.CONSOLE_API.value)
    event_id = _log_console_invocation(
        write_journal, "console_api",
        f"monitor_once invoked via console API ({len(result['exits'])} exit(s), "
        f"{result.get('reconciled', 0)} reconciled).",
        {"scheduler_run_id": result.get("scheduler_run_id")},
    )
    return {"result": result, "audit": {"event_id": event_id}, "as_of": _as_of()}


@router.post("/report")
def actions_report(
    auth: WriteAuth,
    settings: Settings = Depends(get_settings),
    write_journal: JournalStore = Depends(get_write_journal),
    pin_store: PinStore = Depends(get_pin_store),
    rate_limiter: PinRateLimiter = Depends(get_rate_limiter),
    nonce_store: NonceStore = Depends(get_nonce_store),
) -> dict:
    """`orch.generate_daily_report()` -- the exact call streamlit_app.
    render_sidebar()'s "Generate daily report" button makes. Unlike scan/
    monitor, this method takes no `trigger_source` parameter and writes to
    `daily_learning_reports` (no scan_batches/scheduler_runs row either), so
    the `_log_console_invocation` system_event below is this action's ONLY
    "invoked via console" marker -- not a belt-and-suspenders duplicate of a
    free signal, an actual necessity here."""
    _authorize_write(auth, pin_store, rate_limiter, nonce_store)
    orch = Orchestrator(settings=settings, journal=write_journal)
    rep = orch.generate_daily_report()
    event_id = _log_console_invocation(
        write_journal, "console_api",
        f"generate_daily_report invoked via console API (report_id={rep['report_id']}).",
        {"report_id": rep["report_id"]},
    )
    return {"result": rep, "audit": {"event_id": event_id}, "as_of": _as_of()}


@router.post("/kill-switch/engage")
def actions_kill_switch_engage(
    auth: KillSwitchEngageRequest,
    write_journal: JournalStore = Depends(get_write_journal),
    pin_store: PinStore = Depends(get_pin_store),
    rate_limiter: PinRateLimiter = Depends(get_rate_limiter),
    nonce_store: NonceStore = Depends(get_nonce_store),
    kill_switch: KillSwitch = Depends(get_kill_switch),
) -> dict:
    """`KillSwitch.engage(reason)` -- ENGAGE half of the pair (docs/roadmap/
    console-migration-nd.md §4 ND-3: "Kill-switch ENGAGE also lands here --
    deliberately early, because its failure mode is safety-increasing
    (false engage = system pauses). Disengage does NOT land here."). ND-3
    shipped this route alone, on purpose, for that asymmetric-risk reason;
    the DISENGAGE counterpart (`actions_kill_switch_disengage` below) is
    ND-4 scope -- same reasoning in reverse: a disengage's failure mode is
    NOT safety-increasing, so it waits for the heaviest audit pass of the
    migration instead of shipping early alongside this one. Mirrors
    alphaos/__main__.py's `cmd_kill(orch, "engage")` CLI path: engage the
    file-backed marker, then log a CRITICAL system_event (same severity
    `cmd_kill` uses for the CLI path) -- unlike scan/monitor/report above,
    engaging the kill switch is not an Orchestrator method at all
    (`KillSwitch` is independent of `Orchestrator` on purpose -- alphaos/
    safety.py's own module docstring: "intentionally independent of the
    order manager so that no single bug can quietly bypass them"), so this
    route (like its disengage counterpart) does not construct an
    Orchestrator."""
    _authorize_write(auth, pin_store, rate_limiter, nonce_store)
    reason = auth.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="reason must not be empty")
    kill_switch.engage(reason)
    event_id = _log_console_invocation(
        write_journal, "kill_switch", f"Kill switch ENGAGED via console API: {reason}",
        {"reason": reason}, severity=Severity.CRITICAL,
    )
    return {
        "kill_switch_engaged": kill_switch.is_engaged(),
        "kill_switch_reason": kill_switch.reason(),
        "audit": {"event_id": event_id},
        "as_of": _as_of(),
    }


# ================================================================== ND-4
#
# The crown jewels (docs/roadmap/console-migration-nd.md §4 ND-4): proposal
# approve/reject and kill-switch disengage. Everything above this divider is
# unchanged from ND-3 except the two docstring corrections noting these
# routes now exist.


@router.post("/approve")
def actions_approve(
    auth: ProposalApproveRequest,
    settings: Settings = Depends(get_settings),
    write_journal: JournalStore = Depends(get_write_journal),
    pin_store: PinStore = Depends(get_pin_store),
    rate_limiter: PinRateLimiter = Depends(get_rate_limiter),
    nonce_store: NonceStore = Depends(get_nonce_store),
) -> dict:
    """`orch.approve_proposal(proposal_id, approve_margin=...)` -- the exact
    call streamlit_app.tab_approval_center()'s "Approve + submit (paper)"
    button makes. `approve_proposal()` re-validates TTL/freshness/price-
    drift/risk/kill-switch/protection-incident state INTERNALLY and returns
    `(ok, message)`; this route does not re-derive or shortcut any of that
    -- see its docstring in alphaos/orchestrator.py (unmodified by this
    build, per this phase's own instruction).

    A `False` `ok` (expired proposal, margin required but not approved,
    risk-blocked, stale data, kill switch engaged, already-approved, ...)
    is NOT translated into an HTTP error status -- it is a normal, expected
    outcome the operator needs to see verbatim, exactly as Streamlit shows
    it inline (`(st.success if ok else st.error)(msg)`) rather than as a
    page error. The route still returns HTTP 200 either way; `ok`/`message`
    in the body is the only signal of the underlying outcome.

    Idempotency: no NEW guard is added here beyond the shared nonce replay
    check in `_authorize_write` above. `approve_proposal()`'s own
    pre-existing idempotency guard (an existing live entry order for this
    proposal blocks a second approval outright) is what actually prevents a
    double-click-with-two-nonces from creating two orders -- proven by
    `tests/test_api_console_nd4.py`'s double-approve race test, not
    reimplemented here."""
    _authorize_write(auth, pin_store, rate_limiter, nonce_store)
    orch = Orchestrator(settings=settings, journal=write_journal)
    ok, message = orch.approve_proposal(auth.proposal_id, approve_margin=auth.approve_margin)
    event_id = _log_console_invocation(
        write_journal, "console_api",
        f"approve_proposal invoked via console API (proposal_id={auth.proposal_id}, "
        f"ok={ok}): {message}",
        {"proposal_id": auth.proposal_id, "ok": ok, "approve_margin": auth.approve_margin, "message": message},
    )
    return {"ok": ok, "message": message, "audit": {"event_id": event_id}, "as_of": _as_of()}


@router.post("/reject")
def actions_reject(
    auth: ProposalRejectRequest,
    settings: Settings = Depends(get_settings),
    write_journal: JournalStore = Depends(get_write_journal),
    pin_store: PinStore = Depends(get_pin_store),
    rate_limiter: PinRateLimiter = Depends(get_rate_limiter),
    nonce_store: NonceStore = Depends(get_nonce_store),
) -> dict:
    """`orch.reject_proposal(proposal_id, reason=...)` -- the exact call
    streamlit_app.tab_approval_center()'s "Reject" button makes. `reason`
    defaults to `"user rejected"` -- the SAME default `reject_proposal()`
    itself already has -- when the operator submits without typing one (see
    `ProposalRejectRequest`'s own docstring); an empty-string `reason` is
    treated the same as an omitted one, never journaled as a blank
    rejection reason."""
    _authorize_write(auth, pin_store, rate_limiter, nonce_store)
    orch = Orchestrator(settings=settings, journal=write_journal)
    reason = (auth.reason or "").strip() or "user rejected"
    ok, message = orch.reject_proposal(auth.proposal_id, reason=reason)
    event_id = _log_console_invocation(
        write_journal, "console_api",
        f"reject_proposal invoked via console API (proposal_id={auth.proposal_id}, "
        f"ok={ok}): {message}",
        {"proposal_id": auth.proposal_id, "ok": ok, "reason": reason},
    )
    return {"ok": ok, "message": message, "audit": {"event_id": event_id}, "as_of": _as_of()}


@router.post("/kill-switch/disengage")
def actions_kill_switch_disengage(
    auth: WriteAuth,
    write_journal: JournalStore = Depends(get_write_journal),
    pin_store: PinStore = Depends(get_pin_store),
    rate_limiter: PinRateLimiter = Depends(get_rate_limiter),
    nonce_store: NonceStore = Depends(get_nonce_store),
    kill_switch: KillSwitch = Depends(get_kill_switch),
) -> dict:
    """`KillSwitch.release()` -- the disengage counterpart to ND-3's engage
    route above, and the mirror of `render_annunciator()`'s "Release kill
    switch" button (`ks.release()`). Takes NO `reason` field -- `release()`
    itself takes none (unlike `engage(reason)`), so the wire contract is
    plain `WriteAuth` (pin + nonce only); inventing a reason field here
    would be exactly the kind of API-layer embellishment this build is
    instructed not to add.

    `release()` is a harmless no-op when the switch is already disengaged
    (its own `try/except FileNotFoundError: pass`) -- this route does not
    special-case that, it just calls `release()` and reports whatever
    `is_engaged()` reads afterward (always `False` either way), matching
    `release()`'s own documented behavior rather than re-deriving an
    "already disengaged" error this method was explicitly written not to
    raise. Logged CRITICAL, same severity as ND-3's engage route -- a
    disengage is exactly as safety-relevant an event as an engage, even
    though its failure mode differs (this docstring, and the module
    docstring above, are explicit that "not safety-increasing" governed
    when this route could SHIP, not how loudly it is logged once it does).
    Unlike engage, there is no `reason` to fold into the audit message."""
    _authorize_write(auth, pin_store, rate_limiter, nonce_store)
    kill_switch.release()
    event_id = _log_console_invocation(
        write_journal, "kill_switch", "Kill switch DISENGAGED via console API.",
        severity=Severity.CRITICAL,
    )
    return {
        "kill_switch_engaged": kill_switch.is_engaged(),
        "audit": {"event_id": event_id},
        "as_of": _as_of(),
    }
