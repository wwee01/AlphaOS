"""alphaos/api/write_routes.py -- ND-3 write plumbing + first writes
(docs/roadmap/console-migration-nd.md §4 ND-3 scope).

Every route below wraps the EXACT Orchestrator method the Streamlit sidebar
already calls (alphaos/dashboard/streamlit_app.py:render_sidebar) -- no
business logic is re-derived here, same discipline as routes.py's read
endpoints (module docstring: "the frontend computes nothing
business-critical, ever"). What IS new here, and genuinely new surface, is
the authorization gate every route shares (``_authorize_write``): PIN
(alphaos/api/pin.py) + nonce replay guard (alphaos/api/nonce.py), on top of
the ND-1 origin-allowlist + ``X-AlphaOS-Console`` header middleware, which
already applies to every ``/api/*`` path (including these -- no extra wiring
needed; ``ConsoleSecurityMiddleware`` matches on the URL prefix, not on
which router registered the route).

Four routes exist. Exactly four:

* ``POST /api/v1/actions/scan``    -> ``orch.run_scan_once()``
* ``POST /api/v1/actions/monitor`` -> ``orch.run_monitor_once()``
* ``POST /api/v1/actions/report``  -> ``orch.generate_daily_report()``
* ``POST /api/v1/actions/kill-switch/engage`` -> ``KillSwitch.engage(reason)``

Deliberately ABSENT, both here and anywhere else in this API: kill-switch
RELEASE, and proposal APPROVE/REJECT. Streamlit's sidebar/Approval Center
keep sole ownership of those until ND-4 (plan doc: "Disengage does NOT land
here"; "ND-4 -- The crown jewels, last"). "Seed demo trade" is also
deliberately not ported -- it is a dev/demo convenience, not one of the
three named ND-3 writes.

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
of it. `generate_daily_report()` and the kill-switch route have no such
trigger_source column to ride, so the explicit system_events write is the
only marker they get -- which is exactly why every route gets the SAME
uniform `_log_console_invocation` call rather than relying on the free channel
alone for two of the four.
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
    """`KillSwitch.engage(reason)` -- ENGAGE only (docs/roadmap/console-
    migration-nd.md §4 ND-3: "Kill-switch ENGAGE also lands here --
    deliberately early, because its failure mode is safety-increasing
    (false engage = system pauses). Disengage does NOT land here."). There
    is deliberately no corresponding release/disengage route anywhere in
    this API -- that is ND-4 (approve/reject land alongside it, same phase,
    same reasoning: those failure modes are NOT safety-increasing, so they
    get the heaviest audit pass of the migration instead of shipping early).
    Mirrors alphaos/__main__.py's `cmd_kill(orch, "engage")` CLI path:
    engage the file-backed marker, then log a CRITICAL system_event (same
    severity `cmd_kill` uses for the CLI path) -- unlike scan/monitor/report
    above, engaging the kill switch is not an Orchestrator method at all
    (`KillSwitch` is independent of `Orchestrator` on purpose -- alphaos/
    safety.py's own module docstring: "intentionally independent of the
    order manager so that no single bug can quietly bypass them"), so this
    route is the only one of the four that does not construct an
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
