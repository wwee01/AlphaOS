"""ND-2 read-only console API contract tests (docs/roadmap/
console-migration-nd.md §4 ND-2).

Covers the 5 new endpoints (/approvals /decisions /learning /governance
/system + /system/trade-packet): 200 + expected shape, field-for-field
parity against the exact function/query each wraps (same discipline
test_api_console.py already established for ND-1's four endpoints), and the
reporting-law floor-gate at the API layer (mirrors the swap-tested guard in
console/src/learning.test.js, one level down the stack). Security-middleware
and read-only-guarantee coverage for these paths was added directly to
test_api_console.py's existing parametrized/snapshot tests rather than
duplicated here (see that file's `test_disallowed_origin_returns_403` and
`test_serving_every_endpoint_writes_nothing`).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.reports.attribution import build_attribution_report
from alphaos.reports.governance_report import build_governance_report
from alphaos.reports.hypothesis_report import build_hypothesis_report
from alphaos.reports.journal_feed import build_journal_feed
from alphaos.reports.metrics import compute_metrics
from alphaos.reports.tqs_report import build_tqs_report
from alphaos.reports.trade_packet import assemble_trade_packet
from alphaos.safety import KillSwitch
from conftest import inject_pending_proposal
from test_api_console import HEADERS, _client, _json_roundtrip, _seed

AUTONOMY_LEVEL_LABEL = "L1 — unattended cadence"


def _round_proposal_seconds_remaining(obj):
    """Same wall-clock-jitter rounding as test_api_console.py's
    `_round_seconds_remaining`, but for `proposal_seconds_remaining` -- the
    field name `list_open_proposals()` actually uses (distinct from
    `build_daily_brief()`'s `seconds_remaining`, which that helper targets)."""
    if isinstance(obj, dict):
        return {
            k: (
                round(v) if k == "proposal_seconds_remaining" and isinstance(v, (int, float))
                else _round_proposal_seconds_remaining(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_round_proposal_seconds_remaining(v) for v in obj]
    return obj


# ------------------------------------------------------------------ approvals

def test_approvals_matches_list_open_proposals_field_for_field(tmp_path):
    settings, journal, _ = _seed(tmp_path, symbol="AAPL")
    r = _client(settings).get("/api/v1/approvals", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert "as_of" in body
    assert len(body["proposals"]) == 1

    orch = Orchestrator(settings=settings, journal=journal)
    expected = orch.list_open_proposals()
    # proposal_seconds_remaining is time-of-call-dependent (expires_at - now()),
    # legitimately a few ms apart between the API's own computation and this
    # test's -- rounded the same way test_api_console.py's tonight test does,
    # not masking a real discrepancy (e.g. a completely wrong TTL).
    assert _round_proposal_seconds_remaining(_json_roundtrip(body["proposals"])) == _round_proposal_seconds_remaining(_json_roundtrip(expected))
    journal.close()


def test_approvals_empty_journal_returns_empty_list(tmp_path):
    from conftest import make_settings

    db_path = str(tmp_path / "empty.db")
    settings = make_settings(ALPHAOS_DB_PATH=db_path)
    journal = JournalStore(db_path)
    r = _client(settings).get("/api/v1/approvals", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["proposals"] == []
    journal.close()


# ------------------------------------------------------------------ decisions

def test_decisions_returns_expected_shape(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/decisions", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    for key in (
        "label_summary", "proposed", "watch", "rejected", "blocked",
        "open_trades", "closed_trades", "closed_trade_metrics", "as_of",
    ):
        assert key in body, f"missing {key!r} in decisions response: {body}"
    # seed_demo() opens exactly one DEMO position -- the funnel's "filled" end.
    assert len(body["open_trades"]) == 1
    assert isinstance(body["closed_trade_metrics"]["trades"], int)
    journal.close()


def test_decisions_proposed_excludes_candidates_whose_proposal_already_resolved(tmp_path):
    """`candidates.status='proposed'` is set once, at proposal-creation time,
    and never updated again when the proposal itself later resolves (see
    JournalStore.proposed_candidates()'s docstring) -- so a candidate whose
    trade_proposal already reached a terminal state (filled here) must NOT
    keep showing up as an actionable "proposed candidate" on the Decisions
    tab days/weeks after the fact."""
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/decisions", headers=HEADERS)
    before = r.json()["proposed"]
    assert len(before) == 1  # the pending proposal _seed() injected

    journal.conn.execute("UPDATE trade_proposals SET status = 'filled'")
    journal.conn.commit()

    r = _client(settings).get("/api/v1/decisions", headers=HEADERS)
    after = r.json()["proposed"]
    assert after == []
    journal.close()


def test_decisions_rejected_blocked_carry_raw_hindsight_not_formatted(tmp_path):
    """The API attaches the RAW attribution row (or None) under
    `hindsight_raw` -- it must never pre-format it into "pending"/"+N.NNR"
    text (that's console/src/decisions.js:formatHindsight()'s job, done
    client-side)."""
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/decisions", headers=HEADERS)
    body = r.json()
    for row in body["rejected"] + body["blocked"]:
        assert "hindsight_raw" in row
        assert row["hindsight_raw"] is None or isinstance(row["hindsight_raw"], dict)
    journal.close()


def test_decisions_closed_trade_metrics_matches_compute_metrics(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/decisions", headers=HEADERS)
    body = r.json()
    expected = compute_metrics(journal.closed_outcomes(500))
    assert _json_roundtrip(body["closed_trade_metrics"]) == _json_roundtrip(expected)
    journal.close()


# ------------------------------------------------------------------- learning

def test_learning_returns_expected_shape(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/learning", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    for key in ("tqs", "attribution", "hypotheses", "hypothesis_drafts", "journal_feed", "as_of"):
        assert key in body, f"missing {key!r} in learning response: {body}"
    journal.close()


def test_learning_matches_report_builders_field_for_field(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/learning", headers=HEADERS)
    body = r.json()

    ro = JournalStore(journal.db_path, read_only=True)
    try:
        assert _json_roundtrip(body["tqs"]) == _json_roundtrip(build_tqs_report(ro, limit=1000))
        assert _json_roundtrip(body["attribution"]) == _json_roundtrip(build_attribution_report(ro, settings, limit=1000))
        assert _json_roundtrip(body["hypotheses"]) == _json_roundtrip(build_hypothesis_report(ro))
        assert _json_roundtrip(body["journal_feed"]) == _json_roundtrip(build_journal_feed(ro, limit=50))
    finally:
        ro.close()
    journal.close()


def test_learning_reporting_law_never_leaks_a_below_floor_delta_r(tmp_path):
    """The reporting-law guard, verified at the API/data layer (one level
    below the swap-tested console/src/learning.test.js guard): every
    attribution v2 aggregate with `status != "ok"` must carry `None` for
    BOTH mean_delta_r and sum_delta_r -- alphaos/reports/attribution.py's
    own floor gate is what makes this structurally true; this test proves
    the API passes that dict through unchanged rather than accidentally
    populating either field on the way out."""
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/learning", headers=HEADERS)
    v2 = r.json()["attribution"]["v2"]

    def _check_agg(agg):
        if agg.get("status") != "ok":
            assert agg.get("mean_delta_r") is None, f"leaked mean_delta_r below floor: {agg}"
            assert agg.get("sum_delta_r") is None, f"leaked sum_delta_r below floor: {agg}"

    for by_agent in v2["aggregate_delta_r_by_type_and_agent"].values():
        for agg in by_agent.values():
            _check_agg(agg)
    for agg in v2["aggregate_delta_r_by_card"].values():
        _check_agg(agg)
    _check_agg(v2["execution_gap_propose_approved_executed"])
    journal.close()


# ----------------------------------------------------------------- governance

def test_governance_matches_build_governance_report_field_for_field(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/governance", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert "as_of" in body

    ro = JournalStore(journal.db_path, read_only=True)
    try:
        expected = build_governance_report(
            ro, settings, KillSwitch(), autonomy_level_label=AUTONOMY_LEVEL_LABEL
        )
    finally:
        ro.close()
    got = {k: v for k, v in body.items() if k != "as_of"}
    assert _json_roundtrip(got) == _json_roundtrip(expected)
    journal.close()


def test_governance_real_money_lock_is_display_only(tmp_path):
    """Binding content ruling carried over from governance_report.py's own
    docstring: the real-money lock panel DISCLOSES that no unlock control
    exists (honest transparency); this payload must never additionally
    carry an actionable field (e.g. an `unlock` boolean/endpoint) that would
    turn that disclosure into a control surface."""
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/governance", headers=HEADERS)
    lock = r.json()["real_money_lock"]
    assert set(lock.keys()) == {
        "real_trading_enabled_raw", "allow_real_orders_raw", "mode",
        "structural_statement", "no_unlock_note",
    }
    journal.close()


# -------------------------------------------------------------------- system

def test_system_health_matches_orchestrator_system_health_field_for_field(tmp_path):
    """Proves the deliberate deviation documented in routes.py's `/system`
    docstring (avoiding an `OrderManager`/full `Orchestrator` construction
    for the one `broker_connected` field) produces an IDENTICAL dict to the
    real `Orchestrator.system_health()`, not just a plausible-looking one."""
    settings, journal, _ = _seed(tmp_path)
    orch = Orchestrator(settings=settings, journal=journal)
    expected = orch.system_health()

    r = _client(settings).get("/api/v1/system", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    for key in (
        "health", "startup_checks", "recent_snapshots", "recent_events",
        "scan_batches", "scheduler_runs", "recent_candidates", "as_of",
    ):
        assert key in body, f"missing {key!r} in system response: {body}"

    assert _json_roundtrip(body["health"]) == _json_roundtrip(expected)
    journal.close()


def test_system_startup_checks_matches_settings_validate_startup(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/system", headers=HEADERS)
    body = r.json()
    expected = [c.as_dict() for c in settings.validate_startup()]
    assert _json_roundtrip(body["startup_checks"]) == _json_roundtrip(expected)
    journal.close()


def test_system_trade_packet_no_id_returns_null_packet(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/system/trade-packet", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["packet"] is None
    assert "as_of" in body
    journal.close()


def test_system_trade_packet_by_candidate_id_matches_assemble_trade_packet(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    orch = Orchestrator(settings=settings, journal=journal)
    proposal_id, _entry = inject_pending_proposal(orch, symbol="MSFT")
    row = journal.one("SELECT candidate_id FROM trade_proposals WHERE proposal_id = ?", (proposal_id,))
    candidate_id = row["candidate_id"]

    r = _client(settings).get(
        "/api/v1/system/trade-packet", headers=HEADERS, params={"candidate_id": candidate_id}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["packet"] is not None

    ro = JournalStore(journal.db_path, read_only=True)
    try:
        expected = assemble_trade_packet(ro, candidate_id=candidate_id)
    finally:
        ro.close()
    assert _json_roundtrip(body["packet"]) == _json_roundtrip(expected)
    journal.close()


# --------------------------------------------------------- approvals shim proof

def test_list_open_proposals_shim_only_touches_journal(tmp_path):
    """Swap-test-adjacent guard for the SimpleNamespace(journal=...) trick
    routes.approvals() relies on (see its docstring): proves the method
    genuinely works against a minimal stand-in, not secretly reaching for
    `self.settings`/`self.claude`/etc. If a future change to
    `list_open_proposals()` starts touching another attribute, this fails
    loudly (AttributeError) instead of routes.py silently 500ing in
    production."""
    settings, journal, _ = _seed(tmp_path)
    shim = cast(Orchestrator, SimpleNamespace(journal=journal))
    result = Orchestrator.list_open_proposals(shim)
    assert isinstance(result, list)
    assert len(result) == 1
    journal.close()


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_write_verb_to_nd2_paths_refused(tmp_path, method):
    settings, journal, _ = _seed(tmp_path)
    client = _client(settings)
    for path in ("/api/v1/approvals", "/api/v1/decisions", "/api/v1/learning", "/api/v1/governance", "/api/v1/system"):
        r = getattr(client, method)(path, headers=HEADERS)
        assert r.status_code in (403, 405), f"{method.upper()} {path} got {r.status_code}"
    journal.close()
