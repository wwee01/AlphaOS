"""ND-1 read-only console API contract tests (docs/roadmap/
console-migration-nd.md §4).

FastAPI TestClient against a temp, FILE-BASED, seeded journal -- unlike most
of this suite's `:memory:` fixtures, the API's own `mode=ro` SQLite URI
connection needs a real path on disk (see JournalStore.__init__'s
`read_only` branch), so every fixture here writes to `tmp_path`.

Covers:
* health/annunciator/tonight/positions return 200 + the expected shape;
  tonight's payload matches `build_daily_brief()` field-for-field, positions
  matches `assess_positions()` field-for-field (docs/roadmap/
  console-migration-nd.md §1: "the frontend computes nothing
  business-critical" -- proven here at the API layer too: nothing is
  reshaped or re-derived).
* Security (§3): a disallowed Origin -> 403; a missing
  `X-AlphaOS-Console` header -> 403; a write verb to any `/api/*` path ->
  403 or 405 (no write routes exist in ND-1).
* Read-only guarantee: serving every endpoint leaves every table's row
  count unchanged; a direct write through the exact dependency the API
  hands every request (`get_journal`) raises `sqlite3.OperationalError` --
  the swap-test-worthy guard.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from alphaos.api.app import create_app
from alphaos.api.deps import get_journal, get_settings
from alphaos.data.market_data import MarketDataClient
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.reports.daily_brief import build_daily_brief
from alphaos.reports.position_health import assess_positions
from alphaos.safety import KillSwitch
from conftest import inject_pending_proposal, make_settings

HEADERS = {"X-AlphaOS-Console": "1"}


def _seed(tmp_path, symbol="AAPL"):
    """A file-based (not :memory:) journal + orchestrator, seeded with one
    open position (via seed_demo) and one pending proposal -- exercises the
    annunciator's open-R/approvals-pending fields and tonight/positions'
    non-empty paths, not just the empty-journal case."""
    db_path = str(tmp_path / "console_test.db")
    settings = make_settings(ALPHAOS_DB_PATH=db_path)
    journal = JournalStore(db_path)
    orch = Orchestrator(settings=settings, journal=journal)
    orch.seed_demo()
    inject_pending_proposal(orch, symbol=symbol)
    return settings, journal, db_path


def _client(settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def _all_table_counts(journal) -> dict:
    tables = [
        r["name"] for r in journal.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {t: journal.count_rows(t) for t in tables}


def _json_roundtrip(obj):
    """Normalizes a plain Python dict the same way it comes back through an
    HTTP JSON response (e.g. no tuples, no non-JSON-native types), so the
    directly-computed 'expected' value and the API's actual response body
    compare equal field-for-field."""
    return json.loads(json.dumps(obj, default=str))


def _round_seconds_remaining(obj):
    """`seconds_remaining` (proposals.seconds_remaining, computed as
    expires_at - now()) is genuinely time-of-call-dependent: two build_daily_
    brief() calls a few milliseconds apart -- one inside the running API
    process, one in this test -- legitimately produce slightly different
    values. Rounded to the nearest whole second so the field-for-field
    comparison isn't flaky on wall-clock jitter, without masking a REAL
    discrepancy (e.g. a completely wrong TTL)."""
    if isinstance(obj, dict):
        return {
            k: (round(v) if k == "seconds_remaining" and isinstance(v, (int, float)) else _round_seconds_remaining(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_round_seconds_remaining(v) for v in obj]
    return obj


# ------------------------------------------------------------------ endpoints

def test_health_returns_status_db_path_as_of(tmp_path):
    settings, journal, db_path = _seed(tmp_path)
    r = _client(settings).get("/api/v1/health", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_path"] == db_path
    assert "as_of" in body and body["as_of"]
    journal.close()


def test_annunciator_returns_expected_fields(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/annunciator", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    for key in (
        "mode", "autonomy_level_label", "kill_switch_engaged", "kill_switch_reason",
        "heartbeat_age_seconds", "open_position_count", "total_open_r",
        "unmeasurable_positions", "approvals_pending_count", "as_of",
    ):
        assert key in body, f"missing {key!r} in annunciator response: {body}"
    assert body["mode"] == "mock"
    assert body["kill_switch_engaged"] is False
    assert body["kill_switch_reason"] is None
    assert body["open_position_count"] == 1
    assert body["approvals_pending_count"] == 1
    journal.close()


def test_tonight_matches_build_daily_brief_field_for_field(tmp_path):
    """'Expected' is computed on a SEPARATE read-only handle to the same DB
    file, not the write-capable seed handle -- apples-to-apples with what
    the API itself does internally. This matters here specifically (unlike
    positions/annunciator): build_daily_brief() constructs its OWN
    MarketDataClient internally from whatever journal it is given
    (alphaos/reports/daily_brief.py, unmodified/reused verbatim per the ND-1
    plan doc -- this API never re-derives its logic), so in MOCK MODE the
    client's one-time "market data is mocked" notice attempts a write
    through that journal. Against a read-only journal that write fails and
    is caught by assess_positions()'s own, pre-existing broad exception
    handler (its docstring: "never raises... reports itself with
    current_r=None" for ANY per-position measurement failure) -- so the
    first position touched shows as briefly unmeasurable, consistently, on
    every call with a read-only journal. This is a real, documented ND-1
    characteristic (see alphaos/api/deps.py's get_market docstring for how
    /api/v1/positions avoids it by constructing its MarketDataClient with
    journal=None instead) -- comparing against a write-capable journal here
    would silently pass a broken assertion by comparing non-equivalent
    conditions."""
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/tonight", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert "as_of" in body

    ro_journal = JournalStore(journal.db_path, read_only=True)
    try:
        expected = build_daily_brief(ro_journal, settings, KillSwitch())
    finally:
        ro_journal.close()
    got = {k: v for k, v in body.items() if k != "as_of"}
    assert _round_seconds_remaining(got) == _round_seconds_remaining(_json_roundtrip(expected))
    journal.close()


def test_positions_matches_assess_positions_field_for_field(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/positions", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert "as_of" in body
    assert len(body["positions"]) == 1

    market = MarketDataClient(settings, journal=None)  # matches deps.get_market
    expected = assess_positions(journal, settings, market)
    assert body["positions"] == _json_roundtrip(expected)
    journal.close()


def test_positions_empty_journal_returns_empty_list(tmp_path):
    db_path = str(tmp_path / "empty.db")
    settings = make_settings(ALPHAOS_DB_PATH=db_path)
    journal = JournalStore(db_path)
    r = _client(settings).get("/api/v1/positions", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["positions"] == []
    journal.close()


# ------------------------------------------------------------------- security

@pytest.mark.parametrize("path", ["/api/v1/health", "/api/v1/annunciator", "/api/v1/tonight", "/api/v1/positions"])
def test_disallowed_origin_returns_403(tmp_path, path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get(path, headers={**HEADERS, "Origin": "http://evil.example"})
    assert r.status_code == 403
    journal.close()


def test_allowlisted_origin_passes(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get(
        "/api/v1/health", headers={**HEADERS, "Origin": "http://localhost:8601"},
    )
    assert r.status_code == 200
    r2 = _client(settings).get(
        "/api/v1/health", headers={**HEADERS, "Origin": "http://127.0.0.1:8601"},
    )
    assert r2.status_code == 200
    journal.close()


def test_missing_console_header_returns_403(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/health")  # no X-AlphaOS-Console header
    assert r.status_code == 403
    journal.close()


def test_no_origin_header_passes_like_curl(tmp_path):
    """A request with no Origin header at all (curl, the CLI) is the honest
    signature of a non-browser caller and must pass -- only a present,
    disallowed Origin is refused."""
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/health", headers=HEADERS)
    assert r.status_code == 200
    journal.close()


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_write_verb_to_api_path_refused(tmp_path, method):
    settings, journal, _ = _seed(tmp_path)
    client = _client(settings)
    r = getattr(client, method)("/api/v1/health", headers=HEADERS)
    assert r.status_code in (403, 405), f"{method.upper()} /api/v1/health got {r.status_code}"
    journal.close()


# ---------------------------------------------------------------- read-only

def test_serving_every_endpoint_writes_nothing(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    before = _all_table_counts(journal)

    client = _client(settings)
    for path in ("/api/v1/health", "/api/v1/annunciator", "/api/v1/tonight", "/api/v1/positions"):
        r = client.get(path, headers=HEADERS)
        assert r.status_code == 200, f"{path} returned {r.status_code}: {r.text}"

    after = _all_table_counts(journal)
    assert after == before, f"serving wrote rows: before={before} after={after}"
    journal.close()


def test_api_journal_handle_write_raises(tmp_path):
    """The swap-test-worthy guard: the EXACT dependency the API hands every
    request (get_journal) is structurally incapable of a write, at the
    SQLite driver level -- proven here by attempting one directly, not just
    inferred from an absence of write routes."""
    settings, journal, _ = _seed(tmp_path)
    journal.close()

    gen = get_journal(settings)
    ro_journal = next(gen)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro_journal.insert(
                "system_events",
                {
                    "event_id": "evt_should_never_land", "severity": "info",
                    "category": "test", "message": "this write must never land",
                },
            )
    finally:
        with pytest.raises(StopIteration):
            next(gen)  # drains the generator's `finally: journal.close()`
