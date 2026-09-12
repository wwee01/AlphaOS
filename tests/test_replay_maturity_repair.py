"""Real lifecycle regression and history-preserving repair acceptance."""

from datetime import date, datetime, timezone
import hashlib
import json

import pytest

from alphaos.journal.journal_store import JournalStore
from alphaos.learning.outcomes_engine import REPLAY_METHOD_VERSION, replay_bracket_with_maturity
from alphaos.learning.outcomes_tracker import seed_pending_outcomes, update_pending_outcomes
from alphaos.learning.replay_corrections import append_replay_corrections, build_replay_corrections
from alphaos.reports.baseline_report import build_baseline_report
from alphaos.util.market_calendar import nth_trading_day_after
from alphaos.util import timeutils
from scripts.replay_corrections import main as corrections_main


class Bars:
    def __init__(self, rows):
        self.rows = rows

    def get_daily_bars(self, symbol, start, end):
        return self.rows


def bars(n=10):
    return [{"date": nth_trading_day_after(date(2026, 1, 5), i).isoformat(),
             "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0,
             "volume": 10000} for i in range(1, n + 1)]


def seed(j, *, legacy=False, candidate_id="candidate", outcome_id="outcome"):
    j.insert("candidates", {"candidate_id": candidate_id, "symbol": "AAPL", "direction": "long",
                             "card_id": "catalyst_momentum_v3"})
    j.insert("candidate_outcomes", {
        "outcome_id": outcome_id, "candidate_id": candidate_id, "symbol": "AAPL",
        "candidate_type": "candidate", "decision_at_utc": "2026-01-05T16:00:00+00:00",
        "entry_reference_price": 100.0, "stop_price": 90.0, "target_price": 120.0,
        "direction_hint": "long", "outcome_status": "complete" if legacy else "pending",
        "replay_result": "neither" if legacy else None, "replay_r": 0.1 if legacy else None,
        "replay_exit_reason": "window_exhausted" if legacy else None,
        "replay_window_days": 10,
        "replay_status": None if legacy else "pending",
        "replay_method_version": None if legacy else REPLAY_METHOD_VERSION,
        "data_quality_status": "ok",
    })


@pytest.mark.parametrize("barrier,expected,r_value", [
    ("target", "target_hit", 2.0), ("stop", "stop_hit", -1.0),
    ("both", "ambiguous_same_bar", None),
])
def test_ten_day_replay_survives_five_day_completion_and_observes_day_six(monkeypatch, barrier, expected, r_value):
    j = JournalStore(":memory:")
    monkeypatch.setattr(timeutils, "now_utc", lambda: datetime(2026, 2, 1, tzinfo=timezone.utc))
    seed(j)
    source = Bars(bars(5))
    update_pending_outcomes(j, source)
    day5 = j.one("SELECT * FROM candidate_outcomes WHERE outcome_id='outcome'")
    assert day5["outcome_status"] == "complete"  # unchanged five-day family contract
    assert day5["replay_status"] == "pending"
    assert day5["replay_r"] is None and day5["replay_result"] is None
    assert day5["replay_bars_used"] == 5
    source.rows = bars(6)
    if barrier in ("target", "both"):
        source.rows[-1]["high"] = 121.0
    if barrier in ("stop", "both"):
        source.rows[-1]["low"] = 89.0
    result = update_pending_outcomes(j, source)
    after = j.one("SELECT * FROM candidate_outcomes WHERE outcome_id='outcome'")
    assert after["replay_status"] == "complete"
    assert after["replay_result"] == expected and after["replay_r"] == r_value
    assert after["forward_5d_r"] == day5["forward_5d_r"]
    assert result["completed"] == 0  # does not report completing the five-day family again
    assert update_pending_outcomes(j, source)["updated"] == 0
    j.close()


def test_open_bracket_requires_ten_usable_bars_and_final_close():
    source = bars(10)
    for n in (1, 5, 9):
        replay = replay_bracket_with_maturity(100, 90, 120, "long", source[:n], max_days=10)
        assert replay["replay_status"] == "pending" and replay["replay_r"] is None
    source[-1]["close"] = None
    assert replay_bracket_with_maturity(100, 90, 120, "long", source, max_days=10)["replay_status"] == "pending"
    source[-1]["close"] = 105.0
    replay = replay_bracket_with_maturity(100, 90, 120, "long", source, max_days=10)
    assert replay["replay_status"] == "complete" and replay["replay_r"] == 0.5
    source[-1]["high"] = None
    assert replay_bracket_with_maturity(100, 90, 120, "long", source, max_days=10)["replay_status"] == "pending"


def test_seed_stamps_method_but_never_promotes_an_existing_legacy_row():
    j = JournalStore(":memory:")
    seed(j, legacy=True)
    j.insert("candidates", {"candidate_id": "new", "symbol": "MSFT", "direction": "long"})
    seed_pending_outcomes(j)
    assert j.one("SELECT replay_method_version FROM candidate_outcomes WHERE candidate_id='new'")["replay_method_version"] == REPLAY_METHOD_VERSION
    assert j.one("SELECT replay_method_version FROM candidate_outcomes WHERE outcome_id='outcome'")["replay_method_version"] is None
    update_pending_outcomes(j, Bars(bars()))
    old = j.one("SELECT * FROM candidate_outcomes WHERE outcome_id='outcome'")
    assert (old["replay_r"], old["replay_result"], old["replay_status"]) == (0.1, "neither", None)
    j.close()


def legacy_with_cache(j):
    seed(j, legacy=True)
    source = bars()
    source[5]["high"] = 121.0
    for i, b in enumerate(source):
        j.insert("daily_bars", {"bar_id": f"bar{i}", "symbol": "AAPL", "market_date": b["date"],
                                 **{k: b[k] for k in ("open", "high", "low", "close", "volume")},
                                 "source_feed": "test"})
    j.insert("preregistrations", {"prereg_id": "frozen", "hypothesis": "prior", "metric": "r",
        "floor_effective_n": 30, "floor_span_days": 28, "analysis_not_before": "2026-01-01",
        "registered_at_utc": "2026-01-01", "registered_at_sgt": "2026-01-01",
        "evaluated_at_utc": "2026-02-01", "point_estimate": 0.1, "ci_low": -0.5, "ci_high": 0.5})


def test_correction_append_is_idempotent_and_preserves_all_original_and_frozen_fields():
    j = JournalStore(":memory:")
    legacy_with_cache(j)
    original = j.query("SELECT * FROM candidate_outcomes")
    frozen = j.query("SELECT * FROM preregistrations")
    report = build_replay_corrections(j.conn, as_of_date="2026-02-01")
    assert report["n_value_changed"] == 1 and report["n_corrections"] == 1
    assert json.loads(report["records"][0]["corrected_json"])["replay_r"] == 2.0
    assert j.query("SELECT * FROM replay_evidence_corrections") == []
    assert append_replay_corrections(j.conn, report) == 1
    assert append_replay_corrections(j.conn, report) == 0
    assert j.query("SELECT * FROM candidate_outcomes") == original
    assert j.query("SELECT * FROM preregistrations") == frozen
    saved = j.one("SELECT * FROM replay_evidence_corrections")
    assert hashlib.sha256(saved["input_json"].encode()).hexdigest() == saved["input_sha256"]
    j.close()


def test_correction_refuses_changed_original_and_tampered_output():
    j = JournalStore(":memory:")
    legacy_with_cache(j)
    report = build_replay_corrections(j.conn, as_of_date="2026-02-01")
    original_output = report["records"][0]["corrected_json"]
    report["records"][0]["corrected_json"] = "{}"
    with pytest.raises(ValueError, match="does not match"):
        append_replay_corrections(j.conn, report)
    report["records"][0]["corrected_json"] = original_output
    j.conn.execute("UPDATE candidate_outcomes SET replay_r=999 WHERE outcome_id='outcome'")
    j.conn.commit()
    with pytest.raises(ValueError, match="original outcome changed"):
        append_replay_corrections(j.conn, report)
    assert j.query("SELECT * FROM replay_evidence_corrections") == []
    j.close()


def test_correction_cli_dry_run_is_byte_preserving_and_historical_cutoff_keeps_replay_pending(tmp_path, capsys):
    path = tmp_path / "copy.db"
    j = JournalStore(str(path))
    legacy_with_cache(j)
    j.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert corrections_main(["--db", str(path), "--as-of-date", bars(5)[-1]["date"]]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["n_pending"] == 1 and report["inserted"] == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert report["evidence_kind"] == "retrospective_cached_bars_not_confirmatory"


def paired(j, name, *, method=REPLAY_METHOD_VERSION, status="complete", ai_window=10, baseline_window=10, cash=False):
    seed(j, candidate_id=name, outcome_id=name)
    j.conn.execute(
        "UPDATE candidate_outcomes SET outcome_status='complete',replay_status=?,replay_method_version=?,"
        "replay_window_days=?,replay_r=1,replay_result='neither' WHERE outcome_id=?",
        (status, method, ai_window, name),
    )
    j.conn.commit()
    j.insert("shadow_baseline_decisions", {
        "baseline_decision_id": name, "candidate_id": name, "symbol": "AAPL",
        "rule_version": "threshold_v1_hold10", "decision": "no_action" if cash else "propose",
        "input_sha": "fixture_input",
        "decision_at_utc": "2026-01-05T16:00:00+00:00", "replay_status": "complete",
        "replay_r": 0.0, "replay_result": "no_action" if cash else "neither",
        "replay_window_days": baseline_window,
    })


def test_baseline_exposes_quarantine_preserves_abstentions_and_ignores_override_and_corrections():
    j = JournalStore(":memory:")
    paired(j, "valid")
    paired(j, "legacy", method=None)
    paired(j, "partial", status="pending")
    paired(j, "mismatch", ai_window=3)
    paired(j, "cash", baseline_window=None, cash=True)
    # A newer manual override must not become the AI leg.
    j.insert("candidate_outcomes", {"outcome_id": "override", "candidate_id": "valid", "symbol": "AAPL",
        "candidate_type": "user_override", "outcome_status": "complete", "replay_r": 999.0})
    rep = build_baseline_report(j, None, rule_versions=("threshold_v1_hold10",))
    assert rep["n_paired_total"] == 2
    assert rep["validity"]["exclusion_reasons"] == {
        "legacy_or_unverified_ai_replay": 1, "ai_outcome_or_replay_incomplete": 1, "horizon_mismatch": 1,
    }
    assert rep["validity"]["formal_evaluation_allowed"] is False
    assert rep["validity"]["historical_corrections_used"] is False
    j.close()
