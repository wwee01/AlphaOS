"""ND-3 write-plumbing contract tests (docs/roadmap/console-migration-nd.md
§4 ND-3 scope): PIN infrastructure, nonce/idempotency framework, the four
named write routes, and the audit trail each successful write lands.

FastAPI TestClient against a temp, FILE-BASED, WRITE-CAPABLE journal (unlike
ND-1/ND-2's read-only fixtures -- these routes need get_write_journal, not
get_journal). Every PIN/rate-limiter/nonce-store/kill-switch dependency is
overridden per-test (app.dependency_overrides, the SAME pattern
test_api_console.py already uses for get_settings/get_journal) so:

* the process-wide PinRateLimiter/NonceStore singletons (alphaos/api/pin.py,
  alphaos/api/nonce.py) never leak lockout/replay state BETWEEN tests, and
* no test ever touches this repo's real data/console_pin.hash or
  data/KILL_SWITCH files.

Covers the ND-3 build-doc test matrix verbatim: no-PIN-configured -> 503;
wrong PIN -> 401 (never 403 -- that status stays the origin/header
middleware's alone); correct PIN + valid nonce -> 200 AND the underlying
write actually lands (asserted against the DB, not just the HTTP response --
this is the one phase where "writes nothing" would be the WRONG assertion);
replayed nonce -> 409; a failed PIN does NOT consume the nonce (a legitimate
retry with the same nonce must still work); rate-limit lockout after N
consecutive failures -> 429 (this build's chosen status -- documented in
alphaos/api/write_routes.py's _authorize_write docstring), and recovers
after the cooldown elapses; security middleware (origin allowlist + custom
header) still enforced on every new route; PIN comparison is constant-time
(hmac.compare_digest, never `==`); kill-switch release/approve/reject exist
NOWHERE in this API (404, not merely "not tested").
"""

from __future__ import annotations

import inspect
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
from alphaos.safety import KillSwitch
from conftest import make_settings

HEADERS = {"X-AlphaOS-Console": "1"}

WRITE_ENDPOINTS = [
    "/api/v1/actions/scan",
    "/api/v1/actions/monitor",
    "/api/v1/actions/report",
    "/api/v1/actions/kill-switch/engage",
]

TEST_PIN = "4242"


def _min_body(path: str, pin: str = TEST_PIN, nonce: str = "n") -> dict:
    body = {"pin": pin, "nonce": nonce}
    if path.endswith("/kill-switch/engage"):
        body["reason"] = "test reason"
    return body


def _seed(tmp_path) -> tuple:
    """A file-based (not :memory:) settings + DB path -- get_write_journal
    needs a real path (a fresh non-read-only JournalStore auto-inits the
    schema in __init__, so no separate seeding step is required for these
    routes to run cleanly against an otherwise-empty ledger)."""
    db_path = str(tmp_path / "console_nd3_test.db")
    settings = make_settings(ALPHAOS_DB_PATH=db_path)
    return settings, db_path


def _configured_pin_store(tmp_path, pin: str = TEST_PIN) -> PinStore:
    store = PinStore(path=str(tmp_path / "pin.hash"))
    store.set_pin(pin)
    return store


def _client(
    settings, tmp_path, *, pin_store=None, rate_limiter=None, nonce_store=None, kill_switch=None,
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    # Default pin_store points at a file that does not exist -- "PIN never
    # configured" is the correct default for any test that doesn't pass one.
    app.dependency_overrides[get_pin_store] = (
        lambda: pin_store if pin_store is not None else PinStore(path=str(tmp_path / "unset_pin.hash"))
    )
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter or PinRateLimiter()
    app.dependency_overrides[get_nonce_store] = lambda: nonce_store or NonceStore()
    # Never the real data/KILL_SWITCH -- every test gets its own tmp_path marker file.
    app.dependency_overrides[get_kill_switch] = (
        lambda: kill_switch if kill_switch is not None else KillSwitch(path=str(tmp_path / "KILL_SWITCH"))
    )
    return TestClient(app)


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
    """401, distinct from the origin/header middleware's 403 -- ND-3 plan
    doc: keep these distinguishable."""
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)
    r = client.post(path, json=_min_body(path, pin="0000", nonce="wrong-pin-1"), headers=HEADERS)
    assert r.status_code == 401


# --------------------------------------------------- 200 + underlying write

def test_scan_correct_pin_runs_and_writes_scan_batch(tmp_path):
    settings, db_path = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)

    r = client.post("/api/v1/actions/scan", json={"pin": TEST_PIN, "nonce": "scan-n1"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["scan_batch_id"]
    assert body["audit"]["event_id"]

    journal = JournalStore(db_path, read_only=True)
    try:
        assert journal.count_rows("scan_batches") == 1
        # trigger_source lives on scheduler_runs, not scan_batches (see
        # Orchestrator.run_scan_once's own two inserts) -- run_scan_once()
        # mints both a scan_batch_id and a scheduler_run_id per call.
        row = journal.one(
            "SELECT trigger_source FROM scheduler_runs WHERE scheduler_run_id = ?",
            (body["result"]["scheduler_run_id"],),
        )
        assert row["trigger_source"] == "console_api"
        evt = journal.one(
            "SELECT category, detail_json FROM system_events WHERE event_id = ?",
            (body["audit"]["event_id"],),
        )
        assert evt is not None
        assert '"source": "console_api"' in evt["detail_json"]
    finally:
        journal.close()


def test_monitor_correct_pin_runs_and_writes_scheduler_run(tmp_path):
    settings, db_path = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)

    r = client.post("/api/v1/actions/monitor", json={"pin": TEST_PIN, "nonce": "mon-n1"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["audit"]["event_id"]
    assert "exits" in body["result"]

    journal = JournalStore(db_path, read_only=True)
    try:
        assert journal.count_rows("scheduler_runs", "run_type = 'monitor'") == 1
        row = journal.one(
            "SELECT trigger_source FROM scheduler_runs WHERE scheduler_run_id = ?",
            (body["result"]["scheduler_run_id"],),
        )
        assert row["trigger_source"] == "console_api"
    finally:
        journal.close()


def test_report_correct_pin_runs_and_writes_daily_report(tmp_path):
    settings, db_path = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)

    r = client.post("/api/v1/actions/report", json={"pin": TEST_PIN, "nonce": "rep-n1"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["report_id"]
    assert body["audit"]["event_id"]

    journal = JournalStore(db_path, read_only=True)
    try:
        assert journal.count_rows("daily_learning_reports") == 1
        evt = journal.one(
            "SELECT category FROM system_events WHERE event_id = ?", (body["audit"]["event_id"],),
        )
        assert evt is not None and evt["category"] == "console_api"
    finally:
        journal.close()


def test_kill_switch_engage_correct_pin_engages_with_reason(tmp_path):
    settings, db_path = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    kill_switch = KillSwitch(path=str(tmp_path / "KILL_SWITCH"))
    assert not kill_switch.is_engaged()
    client = _client(settings, tmp_path, pin_store=pin_store, kill_switch=kill_switch)

    r = client.post(
        "/api/v1/actions/kill-switch/engage",
        json={"pin": TEST_PIN, "nonce": "ks-n1", "reason": "test engage"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kill_switch_engaged"] is True
    assert body["kill_switch_reason"] == "test engage"
    # The swap-testable assertion: KillSwitch.is_engaged() actually flipped.
    assert kill_switch.is_engaged() is True
    assert kill_switch.reason() == "test engage"


def test_kill_switch_engage_blank_reason_refused_and_stays_disengaged(tmp_path):
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    kill_switch = KillSwitch(path=str(tmp_path / "KILL_SWITCH"))
    client = _client(settings, tmp_path, pin_store=pin_store, kill_switch=kill_switch)

    r = client.post(
        "/api/v1/actions/kill-switch/engage",
        json={"pin": TEST_PIN, "nonce": "ks-n2", "reason": "   "},
        headers=HEADERS,
    )
    assert r.status_code == 422
    assert kill_switch.is_engaged() is False


# --------------------------------------------------------------- 409: replay

def test_replayed_nonce_returns_409(tmp_path):
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    nonce_store = NonceStore()
    client = _client(settings, tmp_path, pin_store=pin_store, nonce_store=nonce_store)

    body = {"pin": TEST_PIN, "nonce": "replay-nonce-1"}
    r1 = client.post("/api/v1/actions/report", json=body, headers=HEADERS)
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/v1/actions/report", json=body, headers=HEADERS)
    assert r2.status_code == 409


def test_wrong_pin_does_not_consume_the_nonce(tmp_path):
    """A failed-PIN attempt must not burn the nonce -- a legitimate retry
    with the SAME nonce but the CORRECT pin must still succeed (the nonce
    check runs AFTER the PIN check in _authorize_write, on purpose)."""
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    nonce_store = NonceStore()
    client = _client(settings, tmp_path, pin_store=pin_store, nonce_store=nonce_store)

    nonce = "retry-nonce-1"
    bad = client.post("/api/v1/actions/report", json={"pin": "0000", "nonce": nonce}, headers=HEADERS)
    assert bad.status_code == 401
    good = client.post("/api/v1/actions/report", json={"pin": TEST_PIN, "nonce": nonce}, headers=HEADERS)
    assert good.status_code == 200, good.text


# ---------------------------------------------------------- 429: rate limit

def test_rate_limit_lockout_after_five_consecutive_failures(tmp_path):
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    rate_limiter = PinRateLimiter(max_attempts=5, cooldown_seconds=300)
    client = _client(settings, tmp_path, pin_store=pin_store, rate_limiter=rate_limiter)

    for i in range(5):
        r = client.post(
            "/api/v1/actions/report", json={"pin": "wrong", "nonce": f"lock-{i}"}, headers=HEADERS,
        )
        assert r.status_code == 401, f"attempt {i}: expected 401, got {r.status_code}"

    # 6th request, even with the CORRECT PIN -- locked out, never reaches verify().
    r = client.post(
        "/api/v1/actions/report", json={"pin": TEST_PIN, "nonce": "lock-final"}, headers=HEADERS,
    )
    assert r.status_code == 429


def test_rate_limit_recovers_after_cooldown_elapses(tmp_path):
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    rate_limiter = PinRateLimiter(max_attempts=3, cooldown_seconds=0.05)
    client = _client(settings, tmp_path, pin_store=pin_store, rate_limiter=rate_limiter)

    for i in range(3):
        client.post("/api/v1/actions/report", json={"pin": "wrong", "nonce": f"cd-{i}"}, headers=HEADERS)
    locked = client.post(
        "/api/v1/actions/report", json={"pin": TEST_PIN, "nonce": "cd-locked"}, headers=HEADERS,
    )
    assert locked.status_code == 429

    time.sleep(0.1)
    ok = client.post("/api/v1/actions/report", json={"pin": TEST_PIN, "nonce": "cd-ok"}, headers=HEADERS)
    assert ok.status_code == 200, ok.text


def test_successful_pin_resets_the_failure_counter(tmp_path):
    """A success in between failures resets the CONSECUTIVE counter -- 2
    failures, 1 success, then 4 more failures must NOT lock out (would need
    5 in a row, never accumulated)."""
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    rate_limiter = PinRateLimiter(max_attempts=5, cooldown_seconds=300)
    client = _client(settings, tmp_path, pin_store=pin_store, rate_limiter=rate_limiter)

    client.post("/api/v1/actions/report", json={"pin": "wrong", "nonce": "rc-1"}, headers=HEADERS)
    client.post("/api/v1/actions/report", json={"pin": "wrong", "nonce": "rc-2"}, headers=HEADERS)
    reset = client.post("/api/v1/actions/report", json={"pin": TEST_PIN, "nonce": "rc-3"}, headers=HEADERS)
    assert reset.status_code == 200, reset.text

    for i in range(4):
        r = client.post(
            "/api/v1/actions/report", json={"pin": "wrong", "nonce": f"rc-post-{i}"}, headers=HEADERS,
        )
        assert r.status_code == 401
    still_ok = client.post(
        "/api/v1/actions/report", json={"pin": TEST_PIN, "nonce": "rc-final"}, headers=HEADERS,
    )
    assert still_ok.status_code == 200, still_ok.text


# ------------------------------------------------------- security middleware

@pytest.mark.parametrize("path", WRITE_ENDPOINTS)
def test_write_route_disallowed_origin_returns_403(tmp_path, path):
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)
    r = client.post(
        path, json=_min_body(path), headers={**HEADERS, "Origin": "http://evil.example"},
    )
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
    """These are POST-only routes; a stray GET must not accidentally trigger
    a write (405, matching ND-1's own "write verb refused" contract in
    reverse -- the read routes refuse writes, these refuse reads)."""
    settings, _ = _seed(tmp_path)
    pin_store = _configured_pin_store(tmp_path)
    client = _client(settings, tmp_path, pin_store=pin_store)
    r = client.get(path, headers=HEADERS)
    assert r.status_code in (404, 405)


# ----------------------------------------------------- constant-time compare

def test_pin_verify_uses_constant_time_compare_not_equality():
    """Code-review-style guard (ND-3 plan doc §5 mandate): hmac.compare_digest,
    never `==`, on the PIN hash comparison."""
    src = inspect.getsource(PinStore.verify)
    assert "hmac.compare_digest" in src
    assert "candidate_digest == expected" not in src
    assert "expected == candidate_digest" not in src


def test_pin_verify_rejects_wrong_pin_and_accepts_right_one(tmp_path):
    store = PinStore(path=str(tmp_path / "pin.hash"))
    assert store.is_configured() is False
    assert store.verify("anything") is False  # fail closed before a PIN even exists
    store.set_pin("998877")
    assert store.is_configured() is True
    assert store.verify("998877") is True
    assert store.verify("000000") is False


# -------------------------------------------------- absent-by-design (ND-4)
#
# 404 if console/dist hasn't been built yet in this checkout (no static
# catch-all mounted -- see alphaos/api/app.py); 405 if it has (an undefined
# path under a `StaticFiles(..., html=True)` mount at "/" answers a non-GET
# method with 405 rather than falling through to a bare 404) -- either way,
# NO WRITE was accepted, which is the actual property under test. Same
# acceptance already used by tests/test_api_console.py::
# test_write_verb_to_api_path_refused for the identical reason.

def test_no_kill_switch_release_route_exists(tmp_path):
    settings, _ = _seed(tmp_path)
    client = _client(settings, tmp_path)
    r = client.post(
        "/api/v1/actions/kill-switch/release",
        json={"pin": "x", "nonce": "n"},
        headers=HEADERS,
    )
    assert r.status_code in (404, 405)


@pytest.mark.parametrize("path", ["/api/v1/actions/approve", "/api/v1/actions/reject"])
def test_no_approve_or_reject_route_exists(tmp_path, path):
    settings, _ = _seed(tmp_path)
    client = _client(settings, tmp_path)
    r = client.post(path, json={"pin": "x", "nonce": "n"}, headers=HEADERS)
    assert r.status_code in (404, 405)


def test_no_seed_demo_route_exists(tmp_path):
    """ND-3 plan doc is explicit: "Seed demo trade" is a dev/demo action,
    NOT one of the three ported writes -- must not exist as a route."""
    settings, _ = _seed(tmp_path)
    client = _client(settings, tmp_path)
    r = client.post(
        "/api/v1/actions/seed-demo", json={"pin": "x", "nonce": "n"}, headers=HEADERS,
    )
    assert r.status_code in (404, 405)
