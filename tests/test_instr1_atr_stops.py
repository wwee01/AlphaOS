"""INSTR-1 part 2: ATR-scaled stops -- the live-only override inside
OpenAIClient.evaluate()/_apply_atr_stop(). Hermetic: the live HTTP call
itself is monkeypatched out (_live_eval), never real network. Direct
construction throughout.
"""

from __future__ import annotations

from alphaos.ai.openai_client import ATR_STOP_MULTIPLIER_V1, OpenAIClient, OpenAIEvaluation
from alphaos.constants import Decision, ReasonCode, TargetSource, TradeDirection
from alphaos.data.atr import ATR_RULES_V1
from alphaos.orchestrator import Orchestrator
from conftest import make_proposal, make_settings


def _seed_atr(journal, symbol, atr_14, market_date="2026-07-08"):
    journal.insert("atr_history", {
        "atr_id": f"atr_{symbol}_{market_date}", "symbol": symbol, "market_date": market_date,
        "atr_14": atr_14, "rules_version": ATR_RULES_V1, "n_bars_used": 15,
    })


def _eval(decision=Decision.PROPOSE.value, direction=TradeDirection.LONG.value,
          entry=100.0, stop=97.0, target=110.0, symbol="AAPL", expected_r=3.0):
    return OpenAIEvaluation(
        eval_id="ev1", candidate_id="c1", symbol=symbol, model="gpt-5.4-mini",
        direction=direction, entry=entry, stop=stop, target=target, max_holding_days=3,
        expected_r=expected_r, confidence=0.8, decision=decision, reasoning_summary="test",
    )


def _live_client(journal, **overrides):
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test", **overrides)
    return OpenAIClient(settings, journal)


# ------------------------------------------------------ _apply_atr_stop unit
def test_overrides_stop_and_recomputes_expected_r_long(journal):
    _seed_atr(journal, "AAPL", atr_14=2.0)
    client = _live_client(journal)

    result = client._apply_atr_stop(_eval(direction=TradeDirection.LONG.value, entry=100.0, target=110.0),
                                    {"symbol": "AAPL"})

    # k=2.0 * ATR=2.0 = 4.0 distance; long -> stop BELOW entry.
    assert result.stop == 96.0
    assert result.expected_r == 2.5  # |110-100| / |100-96| = 10/4
    assert result.stop_source == TargetSource.ATR_V1.value


def test_overrides_stop_short_direction_stop_above_entry(journal):
    _seed_atr(journal, "AAPL", atr_14=2.0)
    client = _live_client(journal)

    result = client._apply_atr_stop(
        _eval(direction=TradeDirection.SHORT.value, entry=100.0, target=90.0), {"symbol": "AAPL"},
    )

    assert result.stop == 104.0  # short -> stop ABOVE entry
    assert result.expected_r == 2.5


def test_atr_multiplier_is_the_documented_constant(journal):
    assert ATR_STOP_MULTIPLIER_V1 == 2.0


def test_no_op_for_watch_decision(journal):
    _seed_atr(journal, "AAPL", atr_14=2.0)
    client = _live_client(journal)
    ev = _eval(decision=Decision.WATCH.value, stop=97.0)

    result = client._apply_atr_stop(ev, {"symbol": "AAPL"})

    assert result.stop == 97.0  # untouched
    assert result.stop_source is None


def test_no_op_for_reject_decision(journal):
    client = _live_client(journal)
    ev = _eval(decision=Decision.REJECT.value, entry=None, stop=None)

    result = client._apply_atr_stop(ev, {"symbol": "AAPL"})

    assert result.stop is None
    assert result.stop_source is None


def test_rejects_when_no_atr_data_available(journal):
    """The fail-safe law: missing ATR data must NEVER silently fall back to
    the AI's own stop -- that would quietly ship the old behavior under a
    version number that claims to be fixed."""
    client = _live_client(journal)
    ev = _eval(symbol="NEWLISTING")

    result = client._apply_atr_stop(ev, {"symbol": "NEWLISTING", "direction": "long"})

    assert result.decision == Decision.REJECT.value
    assert ReasonCode.NO_ATR_DATA.value in result.risk_flags
    assert "ATR" in result.reasoning_summary


def test_rejects_when_atr_is_zero_or_negative(journal):
    _seed_atr(journal, "AAPL", atr_14=0.0)
    client = _live_client(journal)

    result = client._apply_atr_stop(_eval(symbol="AAPL"), {"symbol": "AAPL", "direction": "long"})

    assert result.decision == Decision.REJECT.value
    assert ReasonCode.NO_ATR_DATA.value in result.risk_flags


def test_uses_the_most_recent_atr_row_when_several_exist(journal):
    _seed_atr(journal, "AAPL", atr_14=1.0, market_date="2026-07-01")
    _seed_atr(journal, "AAPL", atr_14=5.0, market_date="2026-07-08")  # newest
    client = _live_client(journal)

    result = client._apply_atr_stop(_eval(entry=100.0), {"symbol": "AAPL"})

    assert result.stop == 90.0  # 100 - 2.0*5.0, not 100 - 2.0*1.0


def test_no_journal_available_rejects_rather_than_crashing():
    """A client with journal=None (rare, but the constructor allows it)
    must still fail safe, never raise."""
    settings = make_settings(ALPHAOS_MODE="paper", OPENAI_API_KEY="fake-key-for-test")
    client = OpenAIClient(settings, journal=None)

    result = client._apply_atr_stop(_eval(), {"symbol": "AAPL", "direction": "long"})

    assert result.decision == Decision.REJECT.value


# ---------------------------------------------------- evaluate() integration
def test_evaluate_live_path_applies_atr_override_end_to_end(journal, monkeypatch):
    _seed_atr(journal, "AAPL", atr_14=2.0)
    client = _live_client(journal)
    monkeypatch.setattr(client, "_live_eval", lambda *a, **k: _eval(entry=100.0, target=110.0, stop=97.0))

    result = client.evaluate({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0})

    assert result.stop == 96.0  # ATR override fired, NOT the raw AI stop of 97.0
    assert result.stop_source == TargetSource.ATR_V1.value


def test_evaluate_mock_path_never_applies_atr_override(journal):
    """mock != real (INSTR-1's own explicit design choice) -- even with ATR
    data seeded, the mock baseline's own deterministic formula must be
    completely untouched."""
    _seed_atr(journal, "AAPL", atr_14=2.0)
    settings = make_settings()  # mock mode by default
    client = OpenAIClient(settings, journal)

    result = client.evaluate({"symbol": "AAPL", "direction": "long", "momentum_score": 0.9}, {"last_price": 100.0})

    assert result.stop_source is None
    assert result.is_mock is True


def test_evaluate_live_reward_risk_recheck_uses_the_atr_widened_stop(journal, monkeypatch):
    """Regression guard for the ordering: _apply_atr_stop must run BEFORE
    _enforce_min_reward_risk inside evaluate(), so a stop ATR widens enough
    to break the reward:risk floor gets correctly downgraded to reject --
    never silently approved on the AI's OWN (now-stale) reward:risk number."""
    _seed_atr(journal, "AAPL", atr_14=20.0)  # huge ATR -> very wide stop
    client = _live_client(journal, MIN_REWARD_RISK="2.0")
    # AI's own numbers looked fine (RR=3.0 at entry=100/stop=97/target=110.5),
    # but a 20-point ATR distance blows the risk basis wide open.
    monkeypatch.setattr(
        client, "_live_eval",
        lambda *a, **k: _eval(entry=100.0, stop=97.0, target=110.5, expected_r=4.5),
    )

    result = client.evaluate({"symbol": "AAPL", "direction": "long"}, {"last_price": 100.0})

    assert result.decision == Decision.REJECT.value
    assert ReasonCode.REWARD_RISK_TOO_LOW.value in result.risk_flags


def test_evaluate_stamps_snapshot_even_through_the_atr_override(journal, monkeypatch):
    """EVAL-1 addendum regression: the ATR override constructs no new
    OpenAIEvaluation object on its success path (mutates in place), so the
    snapshot stamp at the end of evaluate() must still land correctly."""
    _seed_atr(journal, "AAPL", atr_14=2.0)
    client = _live_client(journal)
    monkeypatch.setattr(client, "_live_eval", lambda *a, **k: _eval())
    snapshot = {"last_price": 100.0, "freshness_status": "usable"}

    result = client.evaluate({"symbol": "AAPL", "direction": "long"}, snapshot)

    assert result.snapshot == snapshot


# --------------------------------------------- orchestrator: _tag_target_profile
def test_tag_target_profile_uses_atr_stop_source_when_present(journal, settings):
    orch = Orchestrator(settings=settings, journal=journal)
    proposal = make_proposal()
    ev = _eval()
    ev.stop_source = TargetSource.ATR_V1.value

    orch._tag_target_profile(proposal, from_config=False, evaluation=ev)

    assert proposal.stop_price_source == TargetSource.ATR_V1.value
    assert proposal.target_price_source == TargetSource.OPENAI.value  # unchanged -- AI still sets the target


def test_tag_target_profile_falls_back_to_config_openai_split_without_atr(journal, settings):
    """No evaluation, or an evaluation with no stop_source (mock path, or a
    watch/reject evaluation later force-approved via user override) -- the
    ORIGINAL config/openai split is preserved exactly, unchanged behavior."""
    orch = Orchestrator(settings=settings, journal=journal)

    live_proposal = make_proposal()
    orch._tag_target_profile(live_proposal, from_config=False, evaluation=_eval())  # stop_source=None
    assert live_proposal.stop_price_source == TargetSource.OPENAI.value

    mock_proposal = make_proposal()
    orch._tag_target_profile(mock_proposal, from_config=True)  # no evaluation at all (seed_demo's own call shape)
    assert mock_proposal.stop_price_source == TargetSource.CONFIG.value
