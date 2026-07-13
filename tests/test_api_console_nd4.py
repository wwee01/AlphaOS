"""ND-4 crown-jewel write tests (docs/roadmap/console-migration-nd.md §4
ND-4 scope): proposal approve/reject and kill-switch disengage.

Same harness discipline as tests/test_api_console_nd3.py (FastAPI TestClient
against a temp, FILE-BASED, WRITE-CAPABLE journal; every PIN/rate-limiter/
nonce-store/kill-switch dependency overridden per-test so the process-wide
singletons never leak state between tests and no test ever touches this
repo's real data/console_pin.hash or data/KILL_SWITCH files).

Covers, beyond the ND-3 status-code matrix (503/401/409/429) re-run against
the three new routes: a real seeded proposal approved end-to-end (DB state
asserted, not just the HTTP response); an EXPIRED proposal approved -> 200
with ok:False and NO order created (the re-validation gate did its job); a
margin-required proposal approved without approve_margin -> the exact
existing orch.approve_proposal() behavior surfaces, no new API-layer
blocking logic; a rejected proposal -> status changed, no order created,
default reason applied when omitted; the double-approve race (two DIFFERENT
nonces, same proposal_id) -> exactly one order, proving approve_proposal()'s
own pre-existing idempotency guard (not a new one added here) holds even
when the nonce guard alone would have let both requests through; kill-switch
disengage -> is_engaged() flips False, and is a harmless no-op when already
disengaged (matches KillSwitch.release()'s own FileNotFoundError -> pass
behavior, never an error); security middleware still enforced on all three
new routes.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from alphaos.api.app import create_app
from alphaos.api.deps import (
    get_kill_switch,
    get_nonce_store,
    get_pin_store,
    get_rate_limiter,
    get_settings,
)
from alphaos.api.nonce import NonceStore
from alphaos.api.pin import PinRateLimiter, PinStore
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.safety import KillSwitch
from conftest import inject_pending_proposal, make_settings

HEADERS = {"X-AlphaOS-Console": "1"}

WRITE_ENDPOINTS = [
    "/api/v1/actions/approve",
    "/api/v1/actions/reject",
    "/api/v1/actions/kill-switch/disengage",
]

TEST_PIN = "4242"


def _min_body(path: str, pin: str = TEST_PIN, nonce: str = "n") -> dict:
    body = {"pin": pin, "nonce": nonce}
    if path in ("/api/v1/actions/approve", "/api/v1/actions/reject"):
        body["proposal_id"] = "prop_does_not_exist"
    return body


def _seed(tmp_path) -> tuple:
    """A file-based (not :memory:) settings + DB path, same reasoning as
    test_api_console_nd3.py's own _seed(): get_write_journal needs a real
    path, and seeding a proposal below needs a SEPARATE write-capable
    connection to the same file before the app ever opens its own."""
    db_path = str(tmp_path / "console_nd4_test.db")
    settings = make_settings(ALPHAOS_DB_PATH=db_path)
    return settings, db_path


def _seed_proposal(settings, db_path, *, symbol="AAPL", requires_margin=False, expired=False) -> tuple:
    """Seed a pending, approvable proposal directly via inject_pending_proposal
    (fresh-by-construction TTL, per test_proposal_ttl_flow.py's own fixture
    contract) on a REAL file-backed DB, then optionally mutate it in place
    via direct SQL -- the same technique tests/test_proposal_ttl_flow.py
    already uses to force an expired row (`UPDATE trade_proposals SET
    proposal_expires_at_utc = ...`), since inject_pending_proposal() itself
    takes no TTL/margin override parameters. Returns (proposal_id, entry).
    The seeding connection is closed before returning so the app's own
    get_write_journal dependency opens a fresh connection per request,
    exactly as it does against a normally-created DB."""
    journal = JournalStore(db_path, read_only=False)
    orch = Orchestrator(settings=settings, journal=journal)
    pid, entry = inject_pending_proposal(orch, symbol=symbol)
    if requires_margin:
        journal.conn.execute(
            "UPDATE trade_proposals SET requires_margin = 1 WHERE proposal_id = ?", (pid,)
        )
        journal.conn.commit()
    if expired:
        journal.conn.execute(
            "UPDATE trade_proposals SET proposal_expires_at_utc = '2000-01-01T00:00:00+00:00' "
            "WHERE proposal_id = ?", (pid,),
        )
        journal.conn.commit()
    orch.close()
    return pid, entry


def _configured_pin_store(tmp_path, pin: str = TEST_PIN) -> PinStore:
    store = PinStore(path=str(tmp_path / "pin.hash"))
    store.set_pin(pin)
    return store


def _client(
    settings, tmp_path, *, pin_store=None, rate_limiter=None, nonce_store=None, kill_switch=None,
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_pin_store] = (
        lambda: pin_store if pin_store is not None else PinStore(path=str(tmp_path / "unset_pin.hash"))
    )
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter or PinRateLimiter()
    app.dependency_overrides[get_nonce_store] = lambda: nonce_store or NonceStore()
    app.dependency_overrides[get_kill_switch] = (
        lambda: kill_switch if kill_switch is not None else KillSwitch(path=str(tmp_path / "KILL_SWITCH"))
    )
    return TestClient(app)


def _counts(db_path) -> tuple:
    journal = JournalStore(db_path, read_only=True)
    try:
        return (
            journal.count_rows("paper_orders"),
            journal.count_rows("paper_fills"),
            journal.count_rows("positions", "status = 'open'"),
        )
    finally:
        journal.close()


# --------------------------------------------------------------- 503: no PIN

@pytest.mark.parametrize("path", WRITE_ENDPOINTS)
def test_no_pin_configured_returns_503_fail_closed(tmp_path, path):
    settings, _ = _seed(tmp_path)
    client = _client(settings, tmp_path)
    r = client.post(path, json=_min_body(path), headers=HEADERS)
    assert r.status_code == 503
    assert "set-pin" in r.json()["detail"]


# ------------------------------------------------------------- 401: wrong PIN

@pytest.mark.parametrize("path", WRITE_ENDPOINTS)
def test_wrong_pin_returns_401_not_403(tmp_path, path):
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)
    r = client.post(path, json=_min_body(path, pin="0000", nonce="wrong-pin-1"), headers=HEADERS)
    assert r.status_code == 401


# --------------------------------------------------------------- 409: replay

@pytest.mark.parametrize("path", WRITE_ENDPOINTS)
def test_replayed_nonce_returns_409(tmp_path, path):
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    nonce_store = NonceStore()
    client = _client(settings, tmp_path, pin_store=pin_store, nonce_store=nonce_store)

    body = _min_body(path, nonce="replay-nonce-nd4")
    r1 = client.post(path, json=body, headers=HEADERS)
    assert r1.status_code == 200, r1.text
    r2 = client.post(path, json=body, headers=HEADERS)
    assert r2.status_code == 409


# ---------------------------------------------------------- 429: rate limit

@pytest.mark.parametrize("path", WRITE_ENDPOINTS)
def test_rate_limit_lockout_after_five_consecutive_failures(tmp_path, path):
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    rate_limiter = PinRateLimiter(max_attempts=5, cooldown_seconds=300)
    client = _client(settings, tmp_path, pin_store=pin_store, rate_limiter=rate_limiter)

    for i in range(5):
        r = client.post(path, json=_min_body(path, pin="wrong", nonce=f"lock-{i}"), headers=HEADERS)
        assert r.status_code == 401, f"attempt {i}: expected 401, got {r.status_code}"

    r = client.post(path, json=_min_body(path, nonce="lock-final"), headers=HEADERS)
    assert r.status_code == 429


# ------------------------------------------------------- security middleware

@pytest.mark.parametrize("path", WRITE_ENDPOINTS)
def test_write_route_disallowed_origin_returns_403(tmp_path, path):
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)
    r = client.post(path, json=_min_body(path), headers={**HEADERS, "Origin": "http://evil.example"})
    assert r.status_code == 403


@pytest.mark.parametrize("path", WRITE_ENDPOINTS)
def test_write_route_missing_console_header_returns_403(tmp_path, path):
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)
    r = client.post(path, json=_min_body(path))  # no X-AlphaOS-Console header
    assert r.status_code == 403


@pytest.mark.parametrize("path", WRITE_ENDPOINTS)
def test_write_route_get_verb_refused(tmp_path, path):
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)
    r = client.get(path, headers=HEADERS)
    assert r.status_code in (404, 405)


# ------------------------------------------------------------------ approve

def test_approve_fresh_proposal_creates_order_and_fills(tmp_path):
    settings, db_path = _seed(tmp_path)
    pid, _ = _seed_proposal(settings, db_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)

    before = _counts(db_path)
    assert before == (0, 0, 0)

    r = client.post(
        "/api/v1/actions/approve",
        json={"pin": TEST_PIN, "nonce": "approve-n1", "proposal_id": pid, "approve_margin": False},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["audit"]["event_id"]

    after = _counts(db_path)
    assert after == (1, 1, 1)  # exactly one order/fill/open position -- a genuine execution, not a no-op

    journal = JournalStore(db_path, read_only=True)
    try:
        assert journal.proposal_by_id(pid)["status"] == "filled"
        evt = journal.one(
            "SELECT category, detail_json FROM system_events WHERE event_id = ?", (body["audit"]["event_id"],),
        )
        assert evt is not None
        assert '"source": "console_api"' in evt["detail_json"]
        assert f'"proposal_id": "{pid}"' in evt["detail_json"]
    finally:
        journal.close()


def test_approve_expired_proposal_returns_ok_false_and_creates_no_order(tmp_path):
    """The critical re-validation-gate test: an expired proposal must come
    back as a normal HTTP 200 with ok:False and a clear message -- NOT an
    HTTP error status (docs/roadmap/console-migration-nd.md §4 ND-4: "surface
    that message verbatim... do NOT translate it into an HTTP error
    status") -- and, decisively, must create NO order at all."""
    settings, db_path = _seed(tmp_path)
    pid, _ = _seed_proposal(settings, db_path, expired=True)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)

    r = client.post(
        "/api/v1/actions/approve",
        json={"pin": TEST_PIN, "nonce": "approve-expired-1", "proposal_id": pid, "approve_margin": False},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "expired" in body["message"].lower()

    assert _counts(db_path) == (0, 0, 0)
    journal = JournalStore(db_path, read_only=True)
    try:
        assert journal.proposal_by_id(pid)["status"] == "expired"
    finally:
        journal.close()


def test_approve_margin_required_without_flag_matches_orchestrator_behavior(tmp_path):
    """No new API-layer blocking logic: `approve_margin` is passed straight
    through to orch.approve_proposal(), and the existing method's own
    margin-required-but-not-approved behavior (ok:False, a margin-approval
    message, no order) surfaces unmodified."""
    settings, db_path = _seed(tmp_path)
    pid, _ = _seed_proposal(settings, db_path, requires_margin=True)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)

    r = client.post(
        "/api/v1/actions/approve",
        json={"pin": TEST_PIN, "nonce": "approve-margin-1", "proposal_id": pid, "approve_margin": False},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "margin" in body["message"].lower()

    assert _counts(db_path) == (0, 0, 0)
    journal = JournalStore(db_path, read_only=True)
    try:
        # Still pending -- a margin-required refusal is not a terminal block.
        assert journal.proposal_by_id(pid)["status"] == "pending_approval"
    finally:
        journal.close()


def test_approve_margin_required_with_flag_succeeds(tmp_path):
    """The explicit-approval counterpart to the test above: WITH
    approve_margin=True, the same proposal executes normally -- proves the
    flag is genuinely threaded through, not just always refused."""
    settings, db_path = _seed(tmp_path)
    pid, _ = _seed_proposal(settings, db_path, requires_margin=True)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)

    r = client.post(
        "/api/v1/actions/approve",
        json={"pin": TEST_PIN, "nonce": "approve-margin-2", "proposal_id": pid, "approve_margin": True},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body["message"]
    assert _counts(db_path) == (1, 1, 1)


# ------------------------------------------------------------------- reject

def test_reject_proposal_changes_status_and_creates_no_order(tmp_path):
    settings, db_path = _seed(tmp_path)
    pid, _ = _seed_proposal(settings, db_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)

    r = client.post(
        "/api/v1/actions/reject",
        json={"pin": TEST_PIN, "nonce": "reject-n1", "proposal_id": pid, "reason": "not convinced"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True

    assert _counts(db_path) == (0, 0, 0)
    journal = JournalStore(db_path, read_only=True)
    try:
        assert journal.proposal_by_id(pid)["status"] == "rejected"
        rows = journal.query("SELECT * FROM approvals WHERE proposal_id = ? AND label = 'REJECTED'", (pid,))
        assert len(rows) == 1 and rows[0]["reason"] == "not convinced"
    finally:
        journal.close()


def test_reject_without_reason_defaults_to_user_rejected(tmp_path):
    """`reason` is OPTIONAL on the wire -- omitted entirely must default to
    the exact same `"user rejected"` orch.reject_proposal() itself already
    defaults to, matching Streamlit's own no-required-reason button."""
    settings, db_path = _seed(tmp_path)
    pid, _ = _seed_proposal(settings, db_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)

    r = client.post(
        "/api/v1/actions/reject",
        json={"pin": TEST_PIN, "nonce": "reject-n2", "proposal_id": pid},  # no "reason" key at all
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    journal = JournalStore(db_path, read_only=True)
    try:
        rows = journal.query("SELECT * FROM approvals WHERE proposal_id = ? AND label = 'REJECTED'", (pid,))
        assert len(rows) == 1 and rows[0]["reason"] == "user rejected"
    finally:
        journal.close()


def test_reject_nonexistent_proposal_returns_ok_false(tmp_path):
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)

    r = client.post(
        "/api/v1/actions/reject",
        json={"pin": TEST_PIN, "nonce": "reject-missing-1", "proposal_id": "prop_does_not_exist"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "not found" in body["message"].lower()


# --------------------------------------------------- double-approve race

def test_double_approve_two_different_nonces_creates_exactly_one_order(tmp_path):
    """The primary audit target of this phase (docs/roadmap/console-
    migration-nd.md §4 ND-4): two approve requests for the SAME proposal_id,
    carrying two DIFFERENT valid nonces (simulating a genuine double-click
    racing ahead of any client-side debounce, or two browser tabs) -- the
    nonce guard alone does NOT stop this (each nonce is fresh), so this
    proves orch.approve_proposal()'s OWN pre-existing idempotency guard (an
    existing live entry order blocks a second approval) is what actually
    prevents a duplicate. No new idempotency logic was added to the API
    layer to make this pass -- see write_routes.py's actions_approve
    docstring."""
    settings, db_path = _seed(tmp_path)
    pid, _ = _seed_proposal(settings, db_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)

    body_1 = {"pin": TEST_PIN, "nonce": "race-nonce-A", "proposal_id": pid, "approve_margin": False}
    body_2 = {"pin": TEST_PIN, "nonce": "race-nonce-B", "proposal_id": pid, "approve_margin": False}

    r1 = client.post("/api/v1/actions/approve", json=body_1, headers=HEADERS)
    r2 = client.post("/api/v1/actions/approve", json=body_2, headers=HEADERS)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text  # NOT a nonce-replay 409 -- both nonces are individually fresh

    outcomes = [r1.json()["ok"], r2.json()["ok"]]
    # Exactly one of the two requests actually executed; the other observes
    # the pre-existing order and is refused.
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 1
    loser_message = (r1.json() if not r1.json()["ok"] else r2.json())["message"]
    assert "already" in loser_message.lower() or "not approvable" in loser_message.lower()

    # The swap-testable assertion: regardless of what either individual
    # response said, the DATABASE shows exactly ONE order -- never two.
    #
    # Swap-test performed during development (per this build's own
    # instruction to prove this assertion would catch a real regression):
    # temporarily changed `== 1` to `== 2` below, re-ran this test, and
    # confirmed it FAILED against the real (correct) orch.approve_proposal()
    # behavior (actual count stayed 1, so `== 2` mismatched) -- then
    # reverted to `== 1` and confirmed it passes again. This proves the
    # assertion is load-bearing, not a tautology that would pass either way.
    orders, fills, open_positions = _counts(db_path)
    assert orders == 1, f"expected exactly one order, found {orders}"
    assert fills == 1
    assert open_positions == 1


# ---------------------------------------------------------- kill-switch

def test_kill_switch_disengage_flips_is_engaged_false(tmp_path):
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    kill_switch = KillSwitch(path=str(tmp_path / "KILL_SWITCH"))
    kill_switch.engage("pre-test setup")
    assert kill_switch.is_engaged() is True
    client = _client(settings, tmp_path, pin_store=pin_store, kill_switch=kill_switch)

    r = client.post(
        "/api/v1/actions/kill-switch/disengage",
        json={"pin": TEST_PIN, "nonce": "disengage-n1"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kill_switch_engaged"] is False
    assert body["audit"]["event_id"]
    # The swap-testable assertion: KillSwitch.is_engaged() actually flipped.
    assert kill_switch.is_engaged() is False


def test_kill_switch_disengage_no_reason_field_required(tmp_path):
    """Unlike engage, disengage's wire contract has NO `reason` field --
    KillSwitch.release() itself takes none. A bare {pin, nonce} body must
    be accepted (422 would mean this route wrongly requires one)."""
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    kill_switch = KillSwitch(path=str(tmp_path / "KILL_SWITCH"))
    kill_switch.engage("pre-test setup")
    client = _client(settings, tmp_path, pin_store=pin_store, kill_switch=kill_switch)

    r = client.post(
        "/api/v1/actions/kill-switch/disengage",
        json={"pin": TEST_PIN, "nonce": "disengage-n2"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text


def test_kill_switch_disengage_when_already_disengaged_is_a_harmless_no_op(tmp_path):
    """Matches KillSwitch.release()'s own documented behavior (`try:
    os.remove(...) except FileNotFoundError: pass`) -- disengaging an
    already-disengaged switch is NOT an error, it is a no-op that still
    returns 200 with kill_switch_engaged: False."""
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    kill_switch = KillSwitch(path=str(tmp_path / "KILL_SWITCH"))
    assert kill_switch.is_engaged() is False
    client = _client(settings, tmp_path, pin_store=pin_store, kill_switch=kill_switch)

    r = client.post(
        "/api/v1/actions/kill-switch/disengage",
        json={"pin": TEST_PIN, "nonce": "disengage-noop-1"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kill_switch_engaged"] is False
    assert kill_switch.is_engaged() is False


def test_kill_switch_disengage_is_logged_critical(tmp_path):
    settings, db_path = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    kill_switch = KillSwitch(path=str(tmp_path / "KILL_SWITCH"))
    kill_switch.engage("pre-test setup")
    client = _client(settings, tmp_path, pin_store=pin_store, kill_switch=kill_switch)

    r = client.post(
        "/api/v1/actions/kill-switch/disengage",
        json={"pin": TEST_PIN, "nonce": "disengage-critical-1"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    event_id = r.json()["audit"]["event_id"]

    journal = JournalStore(db_path, read_only=True)
    try:
        evt = journal.one("SELECT severity, category FROM system_events WHERE event_id = ?", (event_id,))
        assert evt is not None
        assert evt["severity"] == "critical"
        assert evt["category"] == "kill_switch"
    finally:
        journal.close()


# ------------------------------------------------------ engage/disengage e2e

def test_engage_then_disengage_round_trip_via_both_console_routes(tmp_path):
    """End-to-end sanity across BOTH ND-3's engage and ND-4's disengage
    routes on the same running client -- not just KillSwitch called
    directly, proving the two routes agree on the same underlying file."""
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    kill_switch = KillSwitch(path=str(tmp_path / "KILL_SWITCH"))
    client = _client(settings, tmp_path, pin_store=pin_store, kill_switch=kill_switch)

    engaged = client.post(
        "/api/v1/actions/kill-switch/engage",
        json={"pin": TEST_PIN, "nonce": "roundtrip-engage-1", "reason": "roundtrip test"},
        headers=HEADERS,
    )
    assert engaged.status_code == 200, engaged.text
    assert engaged.json()["kill_switch_engaged"] is True
    assert kill_switch.is_engaged() is True

    disengaged = client.post(
        "/api/v1/actions/kill-switch/disengage",
        json={"pin": TEST_PIN, "nonce": "roundtrip-disengage-1"},
        headers=HEADERS,
    )
    assert disengaged.status_code == 200, disengaged.text
    assert disengaged.json()["kill_switch_engaged"] is False
    assert kill_switch.is_engaged() is False


# --------------------------------------------------------- misc regression

def test_wrong_pin_does_not_consume_the_nonce_for_approve(tmp_path):
    """Same ND-3-established contract, re-verified on a ND-4 route: a
    failed-PIN attempt must not burn the nonce -- a legitimate retry with
    the SAME nonce but the CORRECT pin must still succeed."""
    settings, db_path = _seed(tmp_path)
    pid, _ = _seed_proposal(settings, db_path)
    pin_store = _configured_pin_store(tmp_path)
    nonce_store = NonceStore()
    client = _client(settings, tmp_path, pin_store=pin_store, nonce_store=nonce_store)

    nonce = "retry-nonce-approve-1"
    bad = client.post(
        "/api/v1/actions/approve",
        json={"pin": "0000", "nonce": nonce, "proposal_id": pid, "approve_margin": False},
        headers=HEADERS,
    )
    assert bad.status_code == 401
    good = client.post(
        "/api/v1/actions/approve",
        json={"pin": TEST_PIN, "nonce": nonce, "proposal_id": pid, "approve_margin": False},
        headers=HEADERS,
    )
    assert good.status_code == 200, good.text
    assert good.json()["ok"] is True


def test_rate_limit_recovers_after_cooldown_elapses_for_reject(tmp_path):
    settings, db_path = _seed(tmp_path)
    pid, _ = _seed_proposal(settings, db_path)
    pin_store = _configured_pin_store(tmp_path)
    rate_limiter = PinRateLimiter(max_attempts=3, cooldown_seconds=0.05)
    client = _client(settings, tmp_path, pin_store=pin_store, rate_limiter=rate_limiter)

    for i in range(3):
        client.post(
            "/api/v1/actions/reject",
            json={"pin": "wrong", "nonce": f"cd-{i}", "proposal_id": pid}, headers=HEADERS,
        )
    locked = client.post(
        "/api/v1/actions/reject",
        json={"pin": TEST_PIN, "nonce": "cd-locked", "proposal_id": pid}, headers=HEADERS,
    )
    assert locked.status_code == 429

    time.sleep(0.1)
    ok = client.post(
        "/api/v1/actions/reject",
        json={"pin": TEST_PIN, "nonce": "cd-ok", "proposal_id": pid}, headers=HEADERS,
    )
    assert ok.status_code == 200, ok.text
