"""HGEN-1: the Hypothesis Proposer (shadow, registry-first). Covers:
* the draft quarantine's load-bearing safety property -- a hypothesis_drafts
  row never appears in compute_verdicts()'s family, hypothesis_proposals, or
  preregistrations until accept_draft() explicitly runs it through the SAME
  propose_hypothesis() every seeded PR12 hypothesis uses.
* the deterministic substrate (proposer.py) -- schema validation, duplicate
  detection, evidence-availability, mechanical risk classification -- built
  and tested with ZERO LLM calls.
* the accept/reject operator ceremony.
* the LLM generation layer (generator.py) -- mock path, G1 runtime gate
  (re-checked every call), cost-guard integration, caps.
* the no-verdict-filter exemplar-selection query (grep-based, house style).
* the isolation law: proposer/generator never imported by approval/risk/
  execution or referenced by any orchestrator decision method.
* settings joint validation + the config_hash fingerprint fix.
* the daily-brief "Needs you" reporting line.

All offline, in-memory, mock mode. No real money, no network.
"""

from __future__ import annotations

import inspect
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from alphaos.hypotheses import generator as hyp_generator
from alphaos.hypotheses import proposer as hyp_proposer
from alphaos.hypotheses import registry as hyp_registry
from alphaos.hypotheses import resolver as hyp_resolver
from alphaos.hypotheses import queries as hyp_queries
from alphaos.hypotheses.constants import DraftStatus, RiskClass, SEEDED_HYPOTHESES
from alphaos.journal.journal_store import JournalStore
from alphaos.orchestrator import Orchestrator
from alphaos.scheduler.cost_guard import check_hypothesis_gen_budget, hypothesis_gen_calls_today
from conftest import make_settings


def _orch(**over):
    return Orchestrator(settings=make_settings(**over), journal=JournalStore(":memory:"))


def _valid_candidate(**over) -> dict:
    base = {
        "title": "TQS mid-band predicts a modest positive delta",
        "claim_text": "TQS mid-band predicts a modest positive delta in 3-day replay_r",
        "metric_fn_name": "h_tqs_1_rows",
        "proposed_risk_class": "A",
        "direction": "positive",
    }
    base.update(over)
    return base


def _clustered_rows(n: int, start="2026-01-01", values=None):
    from datetime import date as _date
    base = _date.fromisoformat(start)
    vals = values if values is not None else [0.1, 0.1, 0.1, -0.3]
    return [
        {"symbol": f"SYM{i}", "decision_date": (base + timedelta(days=i)).isoformat(),
         "max_holding_days": 1, "centered_delta": vals[i % len(vals)]}
        for i in range(n)
    ]


def _fake_metric_fn(rows):
    return lambda journal: (rows, "centered_delta", None)


def _resolve_win1(journal, monkeypatch, n=35):
    """Seed + resolve H-WIN-1 with a fake metric fn -- the same idiom
    test_pr12_hypotheses.py already uses."""
    spec = next(h for h in SEEDED_HYPOTHESES if h["hypothesis_id"] == "H-WIN-1")
    now = datetime.now(timezone.utc) + timedelta(days=-40)
    hyp_registry.propose_hypothesis(journal, spec, now=now)
    monkeypatch.setitem(hyp_queries.METRIC_FUNCTIONS, "h_win_1_rows", _fake_metric_fn(_clustered_rows(n)))
    return hyp_resolver.resolve_due_hypotheses(journal)


def _mark_resolved_with_verdict(journal, hypothesis_id="H-FAKE-1"):
    """A minimal resolved+verdicted hypothesis_proposals row -- enough to
    clear the G1 gate without going through the full resolver machinery."""
    journal.insert("hypothesis_proposals", {
        "hypothesis_id": hypothesis_id, "risk_class": "A", "claim": "fake seeded fact for G1",
        "status": "resolved", "analysis_not_before": "2020-01-01",
        "resolved_at_utc": "2026-01-01T00:00:00+00:00",
        "last_verdict": "inconclusive", "last_q_value": 0.5,
    })


# ============================================================ quarantine law
def test_hypothesis_drafts_table_exists(journal):
    assert journal.count_rows("hypothesis_drafts") == 0


def test_intake_draft_never_writes_to_hypothesis_proposals_or_preregistrations(journal):
    hyp_proposer.intake_draft(journal, _valid_candidate(), source="manual")
    assert journal.count_rows("hypothesis_drafts") == 1
    assert journal.count_rows("hypothesis_proposals") == 0
    assert journal.count_rows("preregistrations") == 0


def test_draft_presence_does_not_alter_a_resolved_hypothesis_q_value(monkeypatch):
    """The load-bearing safety property: a hypothesis_drafts row must never
    shift the seeded family's BH-FDR q-value, because compute_verdicts()'s
    family is 'every EVALUATED preregistration' -- a quarantined draft
    touches none of that. Proven by running the SAME resolution twice, once
    with several hypothesis_drafts rows present (various statuses,
    including 'accepted'-with-a-decoy-link) and once without, and asserting
    identical preregistration family membership + identical q-value."""
    j_without = JournalStore(":memory:")
    summary_without = _resolve_win1(j_without, monkeypatch)
    assert "H-WIN-1" in summary_without["evaluated"]
    q_without = j_without.one(
        "SELECT last_q_value FROM hypothesis_proposals WHERE hypothesis_id = ?", ("H-WIN-1",)
    )["last_q_value"]
    # prereg_id itself is a fresh random uuid per journal, never comparable
    # across two independent runs -- family MEMBERSHIP is what matters
    # (size + which hypothesis_ids it's linked to), not the opaque ids.
    n_family_without = j_without.count_rows("preregistrations", "evaluated_at_utc IS NOT NULL")
    j_without.close()

    j_with = JournalStore(":memory:")
    # Insert drafts BEFORE resolving -- every status, every source, one with
    # a bogus accepted_hypothesis_id link, to prove none of it is visible
    # to the resolver/compute_verdicts() family at all.
    for i, status in enumerate(["draft", "accepted", "rejected"]):
        j_with.insert("hypothesis_drafts", {
            "draft_id": f"hdraft_quarantine_{i}", "title": f"t{i}", "claim_text": f"c{i}",
            "metric_fn_name": "h_win_1_rows", "direction": "positive",
            "proposed_risk_class": "A", "mechanical_risk_class": "A",
            "status": status, "source": "generated" if i else "manual",
            "accepted_hypothesis_id": "H-DECOY-NOT-REAL" if status == "accepted" else None,
        })
    summary_with = _resolve_win1(j_with, monkeypatch)
    assert "H-WIN-1" in summary_with["evaluated"]
    q_with = j_with.one(
        "SELECT last_q_value FROM hypothesis_proposals WHERE hypothesis_id = ?", ("H-WIN-1",)
    )["last_q_value"]
    n_family_with = j_with.count_rows("preregistrations", "evaluated_at_utc IS NOT NULL")
    assert j_with.count_rows("hypothesis_drafts") == 3  # drafts stayed, untouched
    j_with.close()

    assert n_family_with == n_family_without == 1  # exactly one evaluated preregistration, both runs
    assert q_with == q_without  # identical q-value despite the drafts table's own content


# =================================================================== schema
def test_validate_candidate_schema_accepts_a_valid_candidate():
    hyp_proposer.validate_candidate_schema(_valid_candidate())  # must not raise


def test_validate_candidate_schema_collects_every_violation():
    bad = {"title": "", "claim_text": "", "metric_fn_name": "not_whitelisted",
           "proposed_risk_class": "Z", "direction": "sideways"}
    with pytest.raises(hyp_proposer.CandidateSchemaError) as exc_info:
        hyp_proposer.validate_candidate_schema(bad)
    msg = str(exc_info.value)
    assert "title" in msg and "claim_text" in msg and "metric_fn_name" in msg
    assert "proposed_risk_class" in msg and "direction" in msg


def test_intake_draft_raises_and_writes_nothing_on_schema_violation(journal):
    with pytest.raises(hyp_proposer.CandidateSchemaError):
        hyp_proposer.intake_draft(journal, {"title": "x"}, source="manual")
    assert journal.count_rows("hypothesis_drafts") == 0  # never silently coerced or quarantined


def test_validate_candidate_schema_accepts_a_real_card_id():
    hyp_proposer.validate_candidate_schema(_valid_candidate(card_id="catalyst_momentum_v2"))  # must not raise


def test_validate_candidate_schema_rejects_a_phantom_card_id():
    """Audit fixup (scope/safety, LOW): card_id was previously validated for
    TYPE only (str | None), never existence -- a hallucinated/phantom
    card_id from the LLM generator could flow all the way to an accepted
    hypothesis gating a card that doesn't actually exist. Checked against
    the same alphaos.cards.registry.load_card_files() mechanism the rest of
    the codebase already uses (get_default_card()/generator.card_summaries()),
    never a second, independently-maintained card list."""
    with pytest.raises(hyp_proposer.CandidateSchemaError) as exc_info:
        hyp_proposer.validate_candidate_schema(_valid_candidate(card_id="totally_phantom_card_xyz"))
    assert "card_id" in str(exc_info.value)
    assert "totally_phantom_card_xyz" in str(exc_info.value)


def test_intake_draft_raises_and_writes_nothing_on_phantom_card_id(journal):
    """Same 'strict schema, reject at intake, never silently coerce' law as
    every other schema violation (see
    test_intake_draft_raises_and_writes_nothing_on_schema_violation above):
    a phantom card_id fails loudly, no hypothesis_drafts row at all -- never
    quarantined as a rejected row, since there is nothing valid to even
    quarantine."""
    with pytest.raises(hyp_proposer.CandidateSchemaError):
        hyp_proposer.intake_draft(
            journal, _valid_candidate(card_id="totally_phantom_card_xyz"), source="manual",
        )
    assert journal.count_rows("hypothesis_drafts") == 0


def test_metric_whitelist_matches_resolver_dispatch_table():
    """'the same metric functions resolver.py can compute' -- never a second,
    independently-maintained list."""
    assert hyp_proposer.METRIC_WHITELIST == frozenset(hyp_queries.METRIC_FUNCTIONS.keys())
    assert "H-AI-1" not in hyp_proposer.METRIC_WHITELIST  # no metric_fn_name to reuse


# ============================================================== duplicates
def test_duplicate_detection_hard_blocks_a_metric_direction_match_against_seeded(journal):
    hyp_registry.seed_all(journal)  # H-TQS-1 is metric_fn_name=h_tqs_1_rows, direction=positive
    row = hyp_proposer.intake_draft(
        journal, _valid_candidate(title="unique title", claim_text="unique claim text string"),
        source="generated",
    )
    assert row["status"] == DraftStatus.REJECTED.value
    assert "duplicate" in row["rejected_reason"]
    assert "H-TQS-1" in row["rejected_reason"]


def test_duplicate_detection_hard_blocks_a_text_match_against_seeded(journal):
    hyp_registry.seed_all(journal)
    h_tqs_1 = next(h for h in SEEDED_HYPOTHESES if h["hypothesis_id"] == "H-TQS-1")
    row = hyp_proposer.intake_draft(
        journal,
        _valid_candidate(title=h_tqs_1["claim"], metric_fn_name="h_pol_1_rows", direction="negative"),
        source="generated",
    )
    assert row["status"] == DraftStatus.REJECTED.value


def test_duplicate_detection_is_never_silently_dropped_it_is_recorded(journal):
    hyp_registry.seed_all(journal)
    before = journal.count_rows("hypothesis_drafts")
    hyp_proposer.intake_draft(journal, _valid_candidate(), source="generated")
    after = journal.count_rows("hypothesis_drafts")
    assert after == before + 1  # a row WAS written, just status='rejected'


def test_duplicate_detection_exact_direction_not_wildcard(journal):
    """An 'either'-direction seeded hypothesis (H-WIN-1) must NOT block a
    differently-directed generated claim on the same metric_fn_name --
    exact match only, no wildcard (see proposer.py's own module comment on
    why a wildcard was rejected)."""
    hyp_registry.seed_all(journal)  # H-WIN-1: h_win_1_rows, reference direction 'either'
    row = hyp_proposer.intake_draft(
        journal,
        _valid_candidate(
            title="a genuinely different morning-window claim",
            claim_text="a genuinely different morning-window claim text",
            metric_fn_name="h_win_1_rows", direction="positive",
        ),
        source="generated",
    )
    assert row["status"] == DraftStatus.DRAFT.value  # not blocked


def test_duplicate_detection_against_existing_non_rejected_draft(journal):
    first = hyp_proposer.intake_draft(journal, _valid_candidate(), source="manual")
    assert first["status"] == DraftStatus.DRAFT.value
    second = hyp_proposer.intake_draft(
        journal, _valid_candidate(title="different title", claim_text="different claim text string"),
        source="manual",
    )
    assert second["status"] == DraftStatus.REJECTED.value  # metric_fn_name+direction match vs first


def test_duplicate_detection_ignores_a_rejected_draft(journal):
    first = hyp_proposer.intake_draft(journal, _valid_candidate(), source="manual")
    hyp_proposer.reject_draft(journal, first["draft_id"], decided_by="ck", reason="not now")
    # Same metric_fn_name+direction as the now-REJECTED first draft -- must
    # NOT be blocked (a rejected draft is not "still in flight").
    second = hyp_proposer.intake_draft(journal, _valid_candidate(), source="manual")
    assert second["status"] == DraftStatus.DRAFT.value


# ========================================================= evidence check
def test_evidence_availability_true_for_a_whitelisted_metric(journal):
    check = hyp_proposer.check_evidence_availability(journal, "h_tqs_1_rows")
    assert check["available"] is True
    assert check["missing_tables"] == []
    assert set(check["required_tables"]) == {"tqs_scores", "candidate_outcomes", "trade_proposals"}


def test_evidence_availability_false_for_a_non_whitelisted_metric(journal):
    check = hyp_proposer.check_evidence_availability(journal, "not_a_real_metric")
    assert check["available"] is False


# ======================================================= risk classification
def test_classify_risk_inherits_base_class_from_seeded_metric():
    result = hyp_proposer.classify_risk(_valid_candidate(metric_fn_name="h_win_1_rows"))  # seeded Class A
    assert result["mechanical_risk_class"] == RiskClass.A.value
    assert result["ambiguous"] is False


def test_classify_risk_steps_up_one_class_on_card_id_ambiguity():
    result = hyp_proposer.classify_risk(
        _valid_candidate(metric_fn_name="h_win_1_rows", card_id="catalyst_momentum_v2")
    )
    assert result["mechanical_risk_class"] == RiskClass.B.value  # A -> B
    assert result["ambiguous"] is True


def test_classify_risk_step_up_caps_at_class_c():
    result = hyp_proposer.classify_risk(
        _valid_candidate(metric_fn_name="h_ttl_1_rows", card_id="some_card")  # seeded Class C
    )
    assert result["mechanical_risk_class"] == RiskClass.C.value  # C -> C, never overflows


def test_classify_risk_mechanical_always_wins_over_proposed():
    result = hyp_proposer.classify_risk(
        _valid_candidate(metric_fn_name="h_tqs_1_rows", proposed_risk_class="A")  # seeded Class B
    )
    assert result["mechanical_risk_class"] == RiskClass.B.value
    assert result["proposed_risk_class"] == "A"  # both recorded, mechanical wins


def test_intake_draft_stores_both_proposed_and_mechanical_class(journal):
    row = hyp_proposer.intake_draft(
        journal, _valid_candidate(metric_fn_name="h_tqs_1_rows", proposed_risk_class="A"), source="manual",
    )
    assert row["proposed_risk_class"] == "A"
    assert row["mechanical_risk_class"] == "B"  # inherited from h_tqs_1_rows's own seeded class


# =============================================================== accept/reject
def test_accept_draft_registers_via_the_real_propose_hypothesis(journal):
    row = hyp_proposer.intake_draft(journal, _valid_candidate(), source="manual")
    accepted = hyp_proposer.accept_draft(journal, row["draft_id"], decided_by="ck")

    assert accepted["status"] == DraftStatus.ACCEPTED.value
    assert accepted["accepted_hypothesis_id"]
    hyp_row = journal.one(
        "SELECT * FROM hypothesis_proposals WHERE hypothesis_id = ?", (accepted["accepted_hypothesis_id"],)
    )
    assert hyp_row is not None
    assert hyp_row["risk_class"] == accepted["mechanical_risk_class"]
    assert hyp_row["status"] == "testing"
    prereg = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (hyp_row["prereg_id"],))
    assert prereg["floor_effective_n"] is not None
    assert prereg["hypothesis"] == row["claim_text"]


def test_accept_draft_floors_are_mechanically_derived_never_settable(journal):
    from alphaos.hypotheses.constants import RISK_CLASS_FLOORS

    row = hyp_proposer.intake_draft(
        journal, _valid_candidate(metric_fn_name="h_tqs_1_rows", proposed_risk_class="A"), source="manual",
    )
    assert row["mechanical_risk_class"] == "B"
    accepted = hyp_proposer.accept_draft(journal, row["draft_id"], decided_by="ck")
    hyp_row = journal.one(
        "SELECT * FROM hypothesis_proposals WHERE hypothesis_id = ?", (accepted["accepted_hypothesis_id"],)
    )
    prereg = journal.one("SELECT * FROM preregistrations WHERE prereg_id = ?", (hyp_row["prereg_id"],))
    assert prereg["floor_effective_n"] == RISK_CLASS_FLOORS["B"]["min_sample"]
    assert prereg["floor_span_days"] == RISK_CLASS_FLOORS["B"]["min_span_days"]


def test_accept_draft_refuses_a_non_draft_status(journal):
    row = hyp_proposer.intake_draft(journal, _valid_candidate(), source="manual")
    hyp_proposer.accept_draft(journal, row["draft_id"], decided_by="ck")
    with pytest.raises(ValueError, match="not 'draft'"):
        hyp_proposer.accept_draft(journal, row["draft_id"], decided_by="ck")


def test_accept_draft_refuses_decided_by_system(journal):
    row = hyp_proposer.intake_draft(journal, _valid_candidate(), source="manual")
    with pytest.raises(ValueError, match="system"):
        hyp_proposer.accept_draft(journal, row["draft_id"], decided_by="system")


def test_accept_draft_enforces_concurrent_testing_cap_for_generated_source(journal, monkeypatch):
    """Swap-tested guard (build protocol): hard-block acceptance when 4
    generated-source hypotheses are already concurrently 'testing'."""
    monkeypatch.setattr(hyp_proposer, "MAX_CONCURRENT_TESTING_GENERATED", 1)
    metrics = ["h_tqs_1_rows", "h_cat_1_rows"]
    first = hyp_proposer.intake_draft(
        journal, _valid_candidate(metric_fn_name=metrics[0], direction="positive"), source="generated",
    )
    hyp_proposer.accept_draft(journal, first["draft_id"], decided_by="ck")  # now 1 generated testing

    second = hyp_proposer.intake_draft(
        journal,
        _valid_candidate(
            title="second candidate", claim_text="second unique claim text",
            metric_fn_name=metrics[1], direction="positive",
        ),
        source="generated",
    )
    with pytest.raises(ValueError, match="CONCURRENT_TESTING_CAP"):
        hyp_proposer.accept_draft(journal, second["draft_id"], decided_by="ck")


def test_accept_draft_concurrent_testing_cap_does_not_block_manual_source(journal, monkeypatch):
    """The cap is scoped to source='generated' only -- a manual draft must
    never be blocked by it (swap-test's negative counterpart)."""
    monkeypatch.setattr(hyp_proposer, "MAX_CONCURRENT_TESTING_GENERATED", 0)
    row = hyp_proposer.intake_draft(journal, _valid_candidate(), source="manual")
    accepted = hyp_proposer.accept_draft(journal, row["draft_id"], decided_by="ck")
    assert accepted["status"] == DraftStatus.ACCEPTED.value


def test_reject_draft_records_reason(journal):
    row = hyp_proposer.intake_draft(journal, _valid_candidate(), source="manual")
    rejected = hyp_proposer.reject_draft(journal, row["draft_id"], decided_by="ck", reason="not compelling")
    assert rejected["status"] == DraftStatus.REJECTED.value
    assert rejected["rejected_reason"] == "not compelling"


def test_reject_draft_refuses_decided_by_system(journal):
    row = hyp_proposer.intake_draft(journal, _valid_candidate(), source="manual")
    with pytest.raises(ValueError, match="system"):
        hyp_proposer.reject_draft(journal, row["draft_id"], decided_by="system", reason="x")


def test_reject_draft_requires_a_reason(journal):
    row = hyp_proposer.intake_draft(journal, _valid_candidate(), source="manual")
    with pytest.raises(ValueError, match="reason"):
        hyp_proposer.reject_draft(journal, row["draft_id"], decided_by="ck", reason="")


def test_list_drafts_filters_by_status(journal):
    a = hyp_proposer.intake_draft(journal, _valid_candidate(), source="manual")
    hyp_proposer.reject_draft(journal, a["draft_id"], decided_by="ck", reason="x")
    hyp_proposer.intake_draft(
        journal, _valid_candidate(title="t2", claim_text="c2", metric_fn_name="h_cat_1_rows"), source="manual",
    )
    assert len(hyp_proposer.list_drafts(journal, status="rejected")) == 1
    assert len(hyp_proposer.list_drafts(journal, status="draft")) == 1
    assert len(hyp_proposer.list_drafts(journal)) == 2


# =================================================================== CLI
def test_cli_hypothesis_accept_and_reject_ceremony():
    from alphaos.__main__ import cmd_hypothesis_accept, cmd_hypothesis_drafts, cmd_hypothesis_reject

    o = _orch()
    row = hyp_proposer.intake_draft(o.journal, _valid_candidate(), source="manual")
    assert cmd_hypothesis_drafts(o, None) == 0

    exit_code = cmd_hypothesis_accept(o, row["draft_id"], "ck")
    assert exit_code == 0
    accepted = o.journal.one("SELECT status FROM hypothesis_drafts WHERE draft_id = ?", (row["draft_id"],))
    assert accepted["status"] == "accepted"

    row2 = hyp_proposer.intake_draft(
        o.journal, _valid_candidate(title="t2", claim_text="c2 unique", metric_fn_name="h_cat_1_rows"),
        source="manual",
    )
    exit_code2 = cmd_hypothesis_reject(o, row2["draft_id"], "ck", "no thanks")
    assert exit_code2 == 0
    o.close()


def test_cli_hypothesis_accept_refused_exit_code_1():
    from alphaos.__main__ import cmd_hypothesis_accept

    o = _orch()
    exit_code = cmd_hypothesis_accept(o, "hdraft_does_not_exist", "ck")
    assert exit_code == 1
    o.close()


def test_cli_hypothesis_generate_wired():
    from alphaos.__main__ import cmd_hypothesis_generate

    o = _orch()
    assert cmd_hypothesis_generate(o) == 0  # default-off -> safe no-op, exit 0
    o.close()


# ============================================================ generator/G1
def test_default_off():
    o = _orch()
    assert o.settings.hypothesis_gen_shadow_enabled is False
    result = o.hypothesis_generate()
    assert result["status"] == "skipped"
    assert "SHADOW_ENABLED" in result["reason"]
    assert o.journal.count_rows("hypothesis_drafts") == 0
    o.close()


def test_g1_gate_blocks_generation_when_zero_hypotheses_resolved():
    o = _orch(HYPOTHESIS_GEN_SHADOW_ENABLED="true")
    ok, detail = hyp_generator.check_g1_gate(o.journal)
    assert ok is False
    assert "0 resolved" in detail
    result = o.hypothesis_generate()
    assert result["status"] == "skipped"
    assert "G1 gate" in result["reason"]
    o.close()


def test_g1_gate_clears_once_one_hypothesis_has_resolved_with_a_verdict():
    o = _orch(HYPOTHESIS_GEN_SHADOW_ENABLED="true")
    _mark_resolved_with_verdict(o.journal)
    ok, detail = hyp_generator.check_g1_gate(o.journal)
    assert ok is True
    result = o.hypothesis_generate()
    assert result["status"] == "completed"
    o.close()


def test_g1_gate_requires_resolved_at_utc_and_last_verdict_both_set():
    """A row that is status='resolved' but has NOT yet had its verdict
    cache refreshed (last_verdict NULL) must not clear G1 -- matches the
    exact SQL predicate in the build spec."""
    o = _orch(HYPOTHESIS_GEN_SHADOW_ENABLED="true")
    o.journal.insert("hypothesis_proposals", {
        "hypothesis_id": "H-HALF-1", "risk_class": "A", "claim": "x", "status": "resolved",
        "analysis_not_before": "2020-01-01", "resolved_at_utc": "2026-01-01T00:00:00+00:00",
        "last_verdict": None,
    })
    ok, _ = hyp_generator.check_g1_gate(o.journal)
    assert ok is False
    o.close()


def test_generation_produces_deterministic_mock_drafts():
    o = _orch(HYPOTHESIS_GEN_SHADOW_ENABLED="true", HYPOTHESIS_GEN_MAX_PROPOSALS_PER_RUN="2")
    _mark_resolved_with_verdict(o.journal)
    result = o.hypothesis_generate()
    assert result["is_mock"] is True
    assert result["generated"] == 2
    drafts = o.journal.query("SELECT * FROM hypothesis_drafts")
    assert len(drafts) == 2
    for d in drafts:
        assert d["source"] == "generated"
        assert d["model_provider"] is None  # mock path never stamps a real provider
        assert d["status"] in ("draft", "rejected")
    o.close()


def test_unreviewed_draft_ceiling_hard_blocks_generation(journal, monkeypatch):
    monkeypatch.setattr(hyp_generator, "UNREVIEWED_DRAFT_CEILING", 2)
    o = _orch(HYPOTHESIS_GEN_SHADOW_ENABLED="true")
    monkeypatch.setattr(hyp_generator, "UNREVIEWED_DRAFT_CEILING", 2)
    _mark_resolved_with_verdict(o.journal)
    for i in range(2):
        o.journal.insert("hypothesis_drafts", {
            "draft_id": f"hdraft_ceiling_{i}", "title": f"t{i}", "claim_text": f"c{i}",
            "metric_fn_name": "h_tqs_1_rows", "direction": "positive",
            "proposed_risk_class": "A", "mechanical_risk_class": "A",
            "status": "draft", "source": "manual",
        })
    result = o.hypothesis_generate()
    assert result["status"] == "skipped"
    assert "UNREVIEWED_DRAFT_CEILING" in result["reason"]
    o.close()


def test_unreviewed_draft_ceiling_does_not_block_below_threshold(monkeypatch):
    monkeypatch.setattr(hyp_generator, "UNREVIEWED_DRAFT_CEILING", 2)
    o = _orch(HYPOTHESIS_GEN_SHADOW_ENABLED="true")
    _mark_resolved_with_verdict(o.journal)
    o.journal.insert("hypothesis_drafts", {
        "draft_id": "hdraft_below_1", "title": "t", "claim_text": "c",
        "metric_fn_name": "h_tqs_1_rows", "direction": "positive",
        "proposed_risk_class": "A", "mechanical_risk_class": "A",
        "status": "draft", "source": "manual",
    })
    result = o.hypothesis_generate()
    assert result["status"] == "completed"
    o.close()


def test_real_call_lineage_and_model_id_stamped_on_generated_drafts(monkeypatch):
    """The live path is never exercised in tests (no network), but the
    stamping contract from HypothesisGenerator.generate()'s meta dict into
    the resulting draft rows is exercised by monkeypatching generate() to
    return a live-shaped meta -- proving intake_draft actually threads
    model_id/model_provider/prompt_hash/lineage_id through."""
    o = _orch(HYPOTHESIS_GEN_SHADOW_ENABLED="true", OPENAI_PRIMARY_MODEL="gpt-5.6-luna")
    _mark_resolved_with_verdict(o.journal)

    def _fake_generate(self, exemplars, cards, n):
        return (
            [_valid_candidate(title="live one", claim_text="live claim text unique")],
            {"model_provider": "openai", "prompt_hash": "abc123hash", "system_prompt_hash": "sys456hash",
             "is_mock": False},
        )

    monkeypatch.setattr(hyp_generator.HypothesisGenerator, "generate", _fake_generate)
    result = o.hypothesis_generate()
    assert result["intaken"] == 1
    row = o.journal.one("SELECT * FROM hypothesis_drafts")
    assert row["model_id"] == "gpt-5.6-luna"
    assert row["model_provider"] == "openai"
    assert row["prompt_hash"] == "abc123hash"
    assert row["lineage_id"] is not None
    o.close()


def test_generated_candidate_failing_schema_is_skipped_not_fatal(monkeypatch):
    """Fail-safe, per-item isolation -- one bad LLM-produced candidate must
    never crash the whole batch."""
    o = _orch(HYPOTHESIS_GEN_SHADOW_ENABLED="true")
    _mark_resolved_with_verdict(o.journal)

    def _fake_generate(self, exemplars, cards, n):
        return (
            [{"title": "", "claim_text": "", "metric_fn_name": "nope", "proposed_risk_class": "Z",
              "direction": "sideways"}],
            {"model_provider": None, "prompt_hash": None, "system_prompt_hash": None, "is_mock": False},
        )

    monkeypatch.setattr(hyp_generator.HypothesisGenerator, "generate", _fake_generate)
    result = o.hypothesis_generate()
    assert result["status"] == "completed"
    assert result["schema_errors"] == 1
    assert result["intaken"] == 0
    o.close()


# ================================================== exemplar no-verdict-filter
def test_exemplar_selection_query_has_no_verdict_predicate():
    """Grep-based, house style (test_alerts_module_never_imported_by_
    approval_or_risk_engine's own convention): isolate the WHERE clause and
    assert it names no verdict column. last_verdict IS a SELECTED column
    (the prompt shows it for context) -- only the FILTER predicate matters."""
    sql = hyp_generator.EXEMPLAR_SELECT_SQL
    where_clause = re.search(r"WHERE\s+(.*?)\s+ORDER BY", sql, re.IGNORECASE).group(1)
    assert "verdict" not in where_clause.lower()
    assert where_clause.strip().lower() == "status = 'resolved'"


def test_select_exemplars_includes_hypotheses_regardless_of_verdict(journal, monkeypatch):
    for hid, verdict in (("H-A", "rejected"), ("H-B", "forward-test-candidate"), ("H-C", "inconclusive")):
        journal.insert("hypothesis_proposals", {
            "hypothesis_id": hid, "risk_class": "A", "claim": f"claim {hid}", "status": "resolved",
            "analysis_not_before": "2020-01-01", "resolved_at_utc": "2026-01-01T00:00:00+00:00",
            "last_verdict": verdict,
        })
    exemplars = hyp_generator.select_exemplars(journal)
    ids = {e["hypothesis_id"] for e in exemplars}
    assert ids == {"H-A", "H-B", "H-C"}  # every verdict present, none filtered


# ==================================================================== caps
def test_cost_guard_counts_real_hypothesis_gen_calls(journal, settings):
    ok, detail = check_hypothesis_gen_budget(settings, journal)
    assert ok is True
    assert "0/" in detail

    journal.insert("hypothesis_drafts", {
        "draft_id": "hdraft_costcheck", "title": "t", "claim_text": "c",
        "metric_fn_name": "h_tqs_1_rows", "direction": "positive",
        "proposed_risk_class": "A", "mechanical_risk_class": "A",
        "status": "draft", "source": "generated", "model_provider": "openai",
    })
    assert hypothesis_gen_calls_today(journal) == 1


def test_cost_guard_ignores_mock_generation_calls(journal, settings):
    """model_provider stays NULL on the mock path (see HypothesisGenerator's
    own convention) -- mock calls must never count against a real spend cap."""
    journal.insert("hypothesis_drafts", {
        "draft_id": "hdraft_mockcheck", "title": "t", "claim_text": "c",
        "metric_fn_name": "h_tqs_1_rows", "direction": "positive",
        "proposed_risk_class": "A", "mechanical_risk_class": "A",
        "status": "draft", "source": "generated", "model_provider": None,
    })
    assert hypothesis_gen_calls_today(journal) == 0


def test_generation_calls_in_last_30_days_includes_hypothesis_drafts(journal):
    from alphaos.scheduler.cost_guard import calls_in_last_30_days

    before = calls_in_last_30_days(journal)
    journal.insert("hypothesis_drafts", {
        "draft_id": "hdraft_30d", "title": "t", "claim_text": "c",
        "metric_fn_name": "h_tqs_1_rows", "direction": "positive",
        "proposed_risk_class": "A", "mechanical_risk_class": "A",
        "status": "draft", "source": "generated", "model_provider": "openai",
    })
    after = calls_in_last_30_days(journal)
    assert after == before + 1


# ============================================================== settings
def test_hypothesis_gen_recurring_requires_shadow_enabled():
    from alphaos.config.settings import SettingsError

    with pytest.raises(SettingsError, match="HYPOTHESIS_GEN_RECURRING_ENABLED"):
        make_settings(HYPOTHESIS_GEN_RECURRING_ENABLED="true")
    s = make_settings(HYPOTHESIS_GEN_SHADOW_ENABLED="true", HYPOTHESIS_GEN_RECURRING_ENABLED="true")
    assert s.hypothesis_gen_recurring_enabled is True


def test_hypothesis_gen_daily_cap_cannot_exceed_25pct_of_shared_30day_cap():
    from alphaos.config.settings import SettingsError

    with pytest.raises(SettingsError):
        make_settings(HYPOTHESIS_GEN_MAX_CALLS_PER_DAY=100, SCHEDULER_AI_COST_CAP_CALLS_PER_30D=50)
    with pytest.raises(SettingsError):
        make_settings(HYPOTHESIS_GEN_MAX_CALLS_PER_DAY=13, SCHEDULER_AI_COST_CAP_CALLS_PER_30D=50)  # 13 > 12.5
    s = make_settings(HYPOTHESIS_GEN_MAX_CALLS_PER_DAY=12, SCHEDULER_AI_COST_CAP_CALLS_PER_30D=50)
    assert s.hypothesis_gen_max_calls_per_day == 12
    s = make_settings()  # defaults: 5 <= 0.25 * 2000 = 500
    assert s.hypothesis_gen_max_calls_per_day == 5


def test_hypothesis_gen_max_proposals_per_run_bounds():
    from alphaos.config.settings import SettingsError

    with pytest.raises(SettingsError):
        make_settings(HYPOTHESIS_GEN_MAX_PROPOSALS_PER_RUN=0)
    with pytest.raises(SettingsError):
        make_settings(HYPOTHESIS_GEN_MAX_PROPOSALS_PER_RUN=21)
    s = make_settings(HYPOTHESIS_GEN_MAX_PROPOSALS_PER_RUN=20)
    assert s.hypothesis_gen_max_proposals_per_run == 20


# ============================================================= config_hash
def test_config_hash_changes_when_primary_model_changes():
    """Operator-directed fixup: record_config_version()'s safe snapshot
    previously captured only has_openai_key, not WHICH model it drives -- a
    same-day model switch (gpt-5.6-luna) moved zero bits of config_hash.
    Swap-tested: reverting the journal_store.py fix reproduces this
    failure (see the build report)."""
    base = {"ALPHAOS_MODE": "mock", "APPROVAL_MODE": "manual", "REAL_TRADING_ENABLED": "false",
            "ALPHAOS_DB_PATH": ":memory:"}
    from alphaos.config.settings import load_settings

    s1 = load_settings(load_env_file=False, env=dict(base, OPENAI_PRIMARY_MODEL="gpt-4o-mini"))
    s2 = load_settings(load_env_file=False, env=dict(base, OPENAI_PRIMARY_MODEL="gpt-5.6-luna"))
    j1, j2 = JournalStore(":memory:"), JournalStore(":memory:")
    j1.record_config_version(s1)
    j2.record_config_version(s2)
    hash1 = j1.one("SELECT config_hash FROM config_versions")["config_hash"]
    hash2 = j2.one("SELECT config_hash FROM config_versions")["config_hash"]
    assert hash1 != hash2
    assert "openai_api_key" not in j1.one("SELECT config_json FROM config_versions")["config_json"]
    j1.close()
    j2.close()


def test_config_hash_changes_when_hgen_flags_change():
    base = {"ALPHAOS_MODE": "mock", "APPROVAL_MODE": "manual", "REAL_TRADING_ENABLED": "false",
            "ALPHAOS_DB_PATH": ":memory:"}
    from alphaos.config.settings import load_settings

    s_off = load_settings(load_env_file=False, env=dict(base, HYPOTHESIS_GEN_SHADOW_ENABLED="false"))
    s_on = load_settings(load_env_file=False, env=dict(base, HYPOTHESIS_GEN_SHADOW_ENABLED="true"))
    j1, j2 = JournalStore(":memory:"), JournalStore(":memory:")
    j1.record_config_version(s_off)
    j2.record_config_version(s_on)
    hash_off = j1.one("SELECT config_hash FROM config_versions")["config_hash"]
    hash_on = j2.one("SELECT config_hash FROM config_versions")["config_hash"]
    assert hash_off != hash_on
    j1.close()
    j2.close()


# ================================================================ isolation
def test_proposer_and_generator_never_imported_by_approval_or_risk_or_execution():
    """Audit fixup (scope/safety, MEDIUM): the original grep checked only
    three dotted-path tokens (hypotheses.proposer / hypotheses.generator /
    hypothesis_drafts). But alphaos/hypotheses/__init__.py re-exports
    run_hypothesis_generate/accept_draft/reject_draft/list_drafts at the
    package top level, so `from alphaos.hypotheses import
    run_hypothesis_generate` contains NONE of those three tokens and would
    evade the check entirely. A blanket "this decision-path file never
    touches the hypotheses package at all, in any import form" check is
    more robust than a token whitelist that has to be kept in sync with
    every new exported symbol -- confirmed safe today: approval.py/
    risk_engine.py/order_manager.py contain zero references to
    alphaos.hypotheses in any form. (PR12's registry/resolver modules are
    legitimately imported elsewhere -- orchestrator.py's own hypothesis_seed/
    hypothesis_resolve/hypothesis_generate registry/reporting methods --
    never by these three decision-path files.) The hypothesis_drafts check
    stays separate: it guards against a raw SQL/string table reference,
    a different vector than a Python import statement."""
    import alphaos.approval as approval_mod
    import alphaos.execution.order_manager as order_mod
    import alphaos.risk.risk_engine as risk_mod

    for mod, name in (
        (approval_mod, "approval.py"),
        (risk_mod, "risk_engine.py"),
        (order_mod, "order_manager.py"),
    ):
        text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "alphaos.hypotheses" not in text, f"{name} imports the hypotheses package"
        assert "from alphaos import hypotheses" not in text, f"{name} imports the hypotheses package"
        assert "hypothesis_drafts" not in text, f"{name} references hypothesis_drafts"


def test_proposer_and_generator_never_referenced_by_orchestrator_decision_methods():
    """Orchestrator LEGITIMATELY references the hypotheses package elsewhere
    (hypothesis_seed/hypothesis_resolve/hypothesis_generate -- registry/
    reporting methods), so a whole-file grep would false-positive. This
    scopes the check to the actual decision-path methods only.

    Audit fixup (correctness, LOW): the original list checked only 7 method
    names. PR9's established sibling test
    (test_decision_functions_never_reference_alerts_or_fuse_state in
    tests/test_scheduler.py) checks a longer list for the same class of
    concern -- match that SAME set of decision methods here (union of both
    lists), not a narrower subset, so a future decision-path method doesn't
    slip past this check just because HGEN-1's own list happened to omit
    it."""
    import alphaos.orchestrator as orch_mod

    decision_methods = [
        "run_scan_once", "run_monitor_once", "approve_proposal", "reject_proposal",
        "_handle_proposal", "_execute", "_reject_candidate",
        "_resolve_decision", "_combine_decision", "_real_decision_driver",
        "_label_candidate", "_freeze_label",
    ]
    for name in decision_methods:
        fn = getattr(orch_mod.Orchestrator, name)
        src = inspect.getsource(fn)
        assert "proposer" not in src, f"Orchestrator.{name} references 'proposer'"
        assert "hypotheses.generator" not in src, f"Orchestrator.{name} references hypotheses.generator"
        assert "hypothesis_drafts" not in src, f"Orchestrator.{name} references hypothesis_drafts"


# ============================================================= daily brief
def test_daily_brief_omits_drafts_pending_line_when_queue_empty(settings, journal):
    from alphaos.reports.daily_brief import _hypothesis_drafts_pending

    assert _hypothesis_drafts_pending(journal) is None


def test_daily_brief_reports_pending_drafts(settings, journal):
    from alphaos.reports.daily_brief import _hypothesis_drafts_pending

    journal.insert("hypothesis_drafts", {
        "draft_id": "hdraft_brief_1", "title": "t", "claim_text": "c",
        "metric_fn_name": "h_tqs_1_rows", "direction": "positive",
        "proposed_risk_class": "A", "mechanical_risk_class": "A",
        "status": "draft", "source": "manual",
    })
    pending = _hypothesis_drafts_pending(journal)
    assert pending == {"count": 1, "draft_ids": ["hdraft_brief_1"]}


def test_daily_brief_renders_pending_drafts_line():
    from alphaos.reports.daily_brief import build_daily_brief, render_markdown
    from alphaos.safety import KillSwitch

    o = _orch()
    o.journal.insert("hypothesis_drafts", {
        "draft_id": "hdraft_brief_2", "title": "t", "claim_text": "c",
        "metric_fn_name": "h_tqs_1_rows", "direction": "positive",
        "proposed_risk_class": "A", "mechanical_risk_class": "A",
        "status": "draft", "source": "manual",
    })
    ks = KillSwitch("/tmp/alphaos-hgen1-test-kill-switch-marker")
    brief = build_daily_brief(o.journal, o.settings, ks)
    md = render_markdown(brief)
    assert "Hypothesis drafts awaiting review" in md
    assert "hdraft_brief_2" in md
    o.close()
