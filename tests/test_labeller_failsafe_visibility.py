"""Labeller fail-safe VISIBILITY (health only). Proves the fail-safe rate is
counted + surfaced, that warnings fire only above threshold and never on a small
sample, and that NOTHING about the fail-safe behaviour, gates, approval, or
execution changes. Hermetic — no API calls."""

from __future__ import annotations

from types import SimpleNamespace

from alphaos.ai.labeller_health import evaluate_failsafe_health
from alphaos.ai.playbook_classifier import PlaybookClassifier, _classify_exception
from alphaos.constants import LABEL_OTHER
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.reports.daily_recon import DailyRecon
from alphaos.util.ids import new_id
from conftest import make_settings


def _s(**over):
    from alphaos.config.settings import load_settings
    return load_settings(load_env_file=False, env={"ALPHAOS_MODE": "mock", **over})


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


def _add_labels(o, n_failsafe=0, n_openai=0, n_mock=0, reason="live_exception"):
    def add(source, status, decision):
        o.journal.insert("candidate_labels", {
            "label_id": new_id("lbl"), "candidate_id": new_id("cand"), "symbol": "X",
            "label_source": source, "validation_status": status, "label_decision": decision})
    for _ in range(n_failsafe):
        add("fail_safe", reason, "reject")
    for _ in range(n_openai):
        add("openai", "ok", "propose")
    for _ in range(n_mock):
        add("mock", "ok", "watch")


def _summary(total, fail_safe, reasons=None):
    return {
        "total": total, "fail_safe": fail_safe,
        "fail_safe_rate": round(fail_safe / total, 3) if total else 0.0,
        "by_failsafe_reason": reasons or {},
    }


# ------------------------------------------------------------------ counting
def test_summary_counts_sources_and_reasons():
    o = _orch()
    _add_labels(o, n_failsafe=6, reason="truncated_output")
    _add_labels(o, n_openai=3, n_mock=1)
    s = o.journal.labeller_source_summary(50)
    assert s["total"] == 10
    assert s["fail_safe"] == 6 and s["openai"] == 3 and s["mock"] == 1
    assert s["fail_safe_rate"] == 0.6
    assert s["by_failsafe_reason"] == {"truncated_output": 6}
    o.close()


def test_summary_empty_is_zero_not_error():
    o = _orch()
    s = o.journal.labeller_source_summary(50)
    assert s["total"] == 0 and s["fail_safe"] == 0 and s["fail_safe_rate"] == 0.0
    assert s["by_failsafe_reason"] == {}
    o.close()


# ------------------------------------------------------------ threshold logic
def test_health_ok_below_warn():
    h = evaluate_failsafe_health(_summary(10, 1), 0.25, 0.50, 5)
    assert h["level"] == "ok" and h["message"] is None


def test_health_warn_between_thresholds():
    h = evaluate_failsafe_health(_summary(10, 3, {"live_exception": 3}), 0.25, 0.50, 5)
    assert h["level"] == "warn" and h["message"]
    assert "30%" in h["message"] and "WARN" in h["message"]


def test_health_critical_above_critical():
    h = evaluate_failsafe_health(_summary(10, 8, {"truncated_output": 8}), 0.25, 0.50, 5)
    assert h["level"] == "critical"
    assert "80%" in h["message"] and "truncated_output" in h["message"]


def test_health_exactly_on_threshold_triggers():
    # rate == warn_rate should warn; rate == critical_rate should be critical
    assert evaluate_failsafe_health(_summary(4, 1), 0.25, 0.50, 4)["level"] == "warn"
    assert evaluate_failsafe_health(_summary(4, 2), 0.25, 0.50, 4)["level"] == "critical"


def test_health_no_false_alarm_on_low_sample():
    # 4/4 = 100% fail-safe, but below min_sample=5 -> stays ok (no alarm)
    h = evaluate_failsafe_health(_summary(4, 4, {"live_exception": 4}), 0.25, 0.50, 5)
    assert h["level"] == "ok" and h["message"] is None
    assert "insufficient sample" in h["note"]


def test_health_zero_sample_is_ok():
    h = evaluate_failsafe_health(_summary(0, 0), 0.25, 0.50, 5)
    assert h["level"] == "ok" and h["message"] is None


# ------------------------------------------------------- system_health surface
def test_system_health_includes_labeller_block():
    o = _orch()
    _add_labels(o, n_failsafe=8, n_openai=2, reason="truncated_output")
    lf = o.system_health()["labeller_failsafe"]
    assert lf["level"] == "critical" and lf["fail_safe"] == 8 and lf["total"] == 10
    assert lf["top_reason"] == "truncated_output"
    assert "80%" in lf["message"]
    o.close()


def test_system_health_ok_when_clean():
    o = _orch()
    _add_labels(o, n_openai=10)
    lf = o.system_health()["labeller_failsafe"]
    assert lf["level"] == "ok" and lf["message"] is None and lf["fail_safe"] == 0
    o.close()


# ------------------------------------------------------------- daily report
def test_daily_report_includes_labeller_line_with_warning():
    o = _orch()
    _add_labels(o, n_failsafe=8, n_openai=2, reason="truncated_output")
    md = DailyRecon(o.settings, o.journal).generate()["content_md"]
    assert "AI labeller:" in md
    assert "fail-safe rate" in md and "CRITICAL" in md
    o.close()


# -------------------------------------------------- fail-safe behaviour UNCHANGED
def test_failsafe_label_still_rejects_and_records_reason():
    c = PlaybookClassifier(_s())
    pkt = SimpleNamespace(candidate_id="c1", symbol="AAPL", direction="long")
    fs = c._fail_safe(pkt, "truncated_output")
    assert fs.label_decision == "reject"          # STILL fails safe to reject
    assert fs.label_source == "fail_safe"
    assert fs.primary_label == LABEL_OTHER
    assert fs.validation_status == "truncated_output"   # reason stored (visibility)


def test_classify_exception_maps_reasons():
    assert _classify_exception(TimeoutError("slow")) == "timeout"
    assert _classify_exception(ValueError("no JSON object found in LLM response")) == "parse_error"
    assert _classify_exception(RuntimeError("boom")) == "live_exception"
