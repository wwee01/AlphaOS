"""2026-07-17 Research-tab split (Fable 5 architecture consult; Sonnet
build; Opus audit). Covers:

* journal.shadow_instrument_health() -- capture-day/coverage counting,
  unknown-never-zero on an empty DB.
* GET /api/v1/research -- 200 shape, field-for-field parity against the
  JournalStore reads it wraps, provisional-constants flag.
* The hard-filter fix: proposed/watch/rejected candidates must be core-only
  by CONSTRUCTION now (a shadow row was previously kept out only because
  SHADOW_LABELLING_ENABLED happens to be off -- nothing structurally
  stopped one from reaching 'watch'/'proposed'/appearing in the rejected
  list).
* The dedup fix: watch_candidates_latest()/rejected_candidates_latest()
  collapse an append-only per-scan log to one row per symbol, with a true
  (never history-capped) occurrence_count.

Offline, in-memory, no network -- same conventions as test_api_console_nd2.py
(imports _client/_seed/HEADERS/_json_roundtrip from test_api_console) and
test_exp1_shadow_labelling.py (direct-insert fixtures, §H.1: never a
component this journal_store method doesn't itself construct)."""

from __future__ import annotations

from alphaos.journal.journal_store import JournalStore
from alphaos.util.ids import new_id
from conftest import make_settings
from test_api_console import HEADERS, _client, _json_roundtrip, _seed


def _insert_candidate(journal, symbol, status, shadow_tier=0, created_at_utc=None, **extra):
    row = {
        "candidate_id": new_id("cand"), "symbol": symbol, "status": status,
        "shadow_tier": shadow_tier, **extra,
    }
    if created_at_utc is not None:
        row["created_at_utc"] = created_at_utc
        row["created_at_sgt"] = created_at_utc
    return journal.insert("candidates", row)


def _insert_rejected(journal, symbol, stage=None, reason_code="TEST_REASON", created_at_utc=None):
    row = {
        "rejection_id": new_id("rej"), "symbol": symbol, "stage": stage, "reason_code": reason_code,
    }
    if created_at_utc is not None:
        row["created_at_utc"] = created_at_utc
        row["created_at_sgt"] = created_at_utc
    return journal.insert("rejected_candidates", row)


def _insert_universe_day(journal, symbol, market_date, freshness_status="usable",
                         candidate_found=0, instrument_version="instr1"):
    return journal.insert("universe_days", {
        "universe_day_id": new_id("univday"), "symbol": symbol, "market_date": market_date,
        "tier": "watchlist", "freshness_status": freshness_status,
        "candidate_found": candidate_found, "instrument_version": instrument_version,
    })


# --------------------------------------------------- shadow_instrument_health

def test_shadow_instrument_health_counts_days_and_coverage(tmp_path):
    journal = JournalStore(str(tmp_path / "t.db"))
    _insert_universe_day(journal, "AAA", "2026-07-10", freshness_status="usable", candidate_found=1)
    _insert_universe_day(journal, "BBB", "2026-07-10", freshness_status="stale")
    _insert_universe_day(journal, "AAA", "2026-07-13", freshness_status="usable")
    _insert_universe_day(journal, "BBB", "2026-07-13", freshness_status="usable", candidate_found=1)

    health = journal.shadow_instrument_health()
    assert health["capture_days"] == 2
    assert health["first_market_date"] == "2026-07-10"
    assert health["last_market_date"] == "2026-07-13"
    assert health["audit_min_trading_days"] == 20
    assert health["audit_days_remaining"] == 18
    assert health["audit_viable"] is False

    by_date = {row["market_date"]: row for row in health["coverage_by_day"]}
    assert health["coverage_by_day"][0]["market_date"] == "2026-07-13"  # newest first
    assert by_date["2026-07-10"]["symbols"] == 2
    assert by_date["2026-07-10"]["usable"] == 1
    assert by_date["2026-07-10"]["stale"] == 1
    assert by_date["2026-07-10"]["usable_pct"] == 50.0
    assert by_date["2026-07-10"]["candidates_found"] == 1
    assert by_date["2026-07-13"]["candidates_found"] == 1
    journal.close()


def test_shadow_instrument_health_empty_db_unknown_never_zero(tmp_path):
    journal = JournalStore(str(tmp_path / "t.db"))
    health = journal.shadow_instrument_health()
    assert health["capture_days"] == 0
    assert health["first_market_date"] is None
    assert health["last_market_date"] is None
    assert health["coverage_by_day"] == []
    assert health["audit_days_remaining"] == 20
    assert health["audit_viable"] is False
    assert health["universe_size_latest"] is None
    assert health["shadow_candidate_rows_total"] == 0
    assert health["screen_rejects_total"] == 0
    assert health["shadow_labels_total"] == 0
    journal.close()


def test_shadow_instrument_health_segments_by_instrument_version_never_pooled(tmp_path):
    journal = JournalStore(str(tmp_path / "t.db"))
    _insert_candidate(journal, "AAA", "detected", shadow_tier=1, instrument_version="instr1")
    _insert_candidate(journal, "BBB", "detected", shadow_tier=1, instrument_version="pre_instr1")
    health = journal.shadow_instrument_health()
    by_version = {row["instrument_version"]: row["rows"] for row in health["rows_by_instrument_version"]}
    assert by_version["instr1"] == 1
    assert by_version["pre_instr1"] == 1
    journal.close()


def test_shadow_instrument_health_screen_reject_reasons(tmp_path):
    journal = JournalStore(str(tmp_path / "t.db"))
    _insert_rejected(journal, "AAA", stage="shadow_scan", reason_code="LOW_LIQUIDITY")
    _insert_rejected(journal, "BBB", stage="shadow_scan", reason_code="LOW_LIQUIDITY")
    _insert_rejected(journal, "CCC", stage="shadow_scan", reason_code="WIDE_SPREAD")
    _insert_rejected(journal, "DDD", stage="scan", reason_code="WIDE_SPREAD")  # core -- must not count
    health = journal.shadow_instrument_health()
    assert health["screen_rejects_total"] == 3
    by_reason = {row["reason_code"]: row["n"] for row in health["screen_rejects_by_reason"]}
    assert by_reason == {"LOW_LIQUIDITY": 2, "WIDE_SPREAD": 1}
    journal.close()


# ---------------------------------------------------------------- /research

def test_research_endpoint_shape_and_parity(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    _insert_universe_day(journal, "AAA", "2026-07-16", candidate_found=1)

    r = _client(settings).get("/api/v1/research", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    for key in (
        "shadow_tier_enabled", "shadow_labelling_enabled", "constants",
        "universe_config", "capture", "recent_captures", "as_of",
    ):
        assert key in body, f"missing {key!r} in research response: {body}"

    assert body["shadow_labelling_enabled"] is False  # SHADOW_LABELLING_ENABLED default
    assert body["constants"]["interest_score_version"] == "interest_score_shadow_v1"
    assert body["constants"]["provisional"] is True  # "_v1" suffix -- not yet audit-replaced
    assert set(body["constants"]["values"].keys()) == {
        "change_scale", "rel_vol_scale", "day_range_min", "momentum_change_cap", "momentum_relvol_cap",
    }

    expected_capture = _json_roundtrip(journal.shadow_instrument_health())
    assert _json_roundtrip(body["capture"]) == expected_capture
    expected_recent = _json_roundtrip(journal.shadow_recent_captures(25))
    assert _json_roundtrip(body["recent_captures"]) == expected_recent
    journal.close()


def test_research_endpoint_reflects_universe_config_from_settings(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    r = _client(settings).get("/api/v1/research", headers=HEADERS)
    uc = r.json()["universe_config"]
    assert uc["min_adv_usd"] == settings.shadow_tier_min_adv_usd
    assert uc["max_adv_usd"] == settings.shadow_tier_max_adv_usd
    assert uc["min_price"] == settings.shadow_tier_min_price
    assert uc["max_price"] == settings.shadow_tier_max_price
    journal.close()


# -------------------------------------------------------- hard-filter (core-only)

def test_watch_candidates_excludes_shadow_even_when_status_is_watch(tmp_path):
    """The read guard, not just the creation-time chokepoint: a shadow-tier
    row FORCED to status='watch' (bypassing every normal write path) must
    still never appear -- proves the filter lives at the read, not merely
    relies on nothing ever writing a shadow row to 'watch' today."""
    journal = JournalStore(str(tmp_path / "t.db"))
    _insert_candidate(journal, "CORE1", "watch", shadow_tier=0)
    _insert_candidate(journal, "SHADOW1", "watch", shadow_tier=1)
    rows = journal.watch_candidates()
    assert {r["symbol"] for r in rows} == {"CORE1"}
    journal.close()


def test_proposed_candidates_excludes_shadow_even_when_status_is_proposed(tmp_path):
    journal = JournalStore(str(tmp_path / "t.db"))
    _insert_candidate(journal, "CORE1", "proposed", shadow_tier=0)
    _insert_candidate(journal, "SHADOW1", "proposed", shadow_tier=1)
    rows = journal.proposed_candidates()
    assert {r["symbol"] for r in rows} == {"CORE1"}
    journal.close()


def test_rejected_candidates_recent_excludes_shadow_scan_stage(tmp_path):
    journal = JournalStore(str(tmp_path / "t.db"))
    _insert_rejected(journal, "CORE1", stage="scan")
    _insert_rejected(journal, "SHADOW1", stage="shadow_scan")
    _insert_rejected(journal, "LEGACY1", stage=None)  # absence of evidence is not evidence of shadow-ness
    rows = journal.rejected_candidates_recent()
    assert {r["symbol"] for r in rows} == {"CORE1", "LEGACY1"}
    journal.close()


def test_decisions_endpoint_never_returns_a_shadow_row(tmp_path):
    settings, journal, _ = _seed(tmp_path)
    _insert_candidate(journal, "SHADOWWATCH", "watch", shadow_tier=1)
    _insert_rejected(journal, "SHADOWREJ", stage="shadow_scan")
    r = _client(settings).get("/api/v1/decisions", headers=HEADERS)
    body = r.json()
    watch_symbols = {row["symbol"] for row in body["watch"]}
    rejected_symbols = {row["symbol"] for row in body["rejected"]}
    assert "SHADOWWATCH" not in watch_symbols
    assert "SHADOWREJ" not in rejected_symbols
    journal.close()


# ------------------------------------------------------- dedup latest-per-symbol

def test_watch_candidates_latest_collapses_to_one_row_per_symbol(tmp_path):
    journal = JournalStore(str(tmp_path / "t.db"))
    _insert_candidate(journal, "AAA", "watch", created_at_utc="2026-07-10T10:00:00+00:00")
    _insert_candidate(journal, "AAA", "watch", created_at_utc="2026-07-12T10:00:00+00:00")
    _insert_candidate(journal, "AAA", "watch", created_at_utc="2026-07-14T10:00:00+00:00")  # latest
    _insert_candidate(journal, "BBB", "watch", created_at_utc="2026-07-11T10:00:00+00:00")

    rows = journal.watch_candidates_latest()
    assert len(rows) == 2
    by_symbol = {r["symbol"]: r for r in rows}

    aaa = by_symbol["AAA"]
    assert aaa["created_at_utc"] == "2026-07-14T10:00:00+00:00"  # the newest row is "latest"
    assert aaa["occurrence_count"] == 3
    assert aaa["first_seen_at_utc"] == "2026-07-10T10:00:00+00:00"
    assert [h["created_at_utc"] for h in aaa["history"]] == ["2026-07-12T10:00:00+00:00", "2026-07-10T10:00:00+00:00"]

    bbb = by_symbol["BBB"]
    assert bbb["occurrence_count"] == 1
    assert bbb["history"] == []
    journal.close()


def test_watch_candidates_latest_occurrence_count_is_never_capped_by_history_window(tmp_path):
    journal = JournalStore(str(tmp_path / "t.db"))
    for i in range(15):
        _insert_candidate(journal, "AAA", "watch", created_at_utc=f"2026-07-{i+1:02d}T10:00:00+00:00")

    rows = journal.watch_candidates_latest(history_per_symbol=10)
    assert len(rows) == 1
    assert rows[0]["occurrence_count"] == 15  # true total, not capped
    assert len(rows[0]["history"]) == 10       # capped detail window
    journal.close()


def test_rejected_candidates_latest_dedupes_and_excludes_shadow(tmp_path):
    journal = JournalStore(str(tmp_path / "t.db"))
    _insert_rejected(journal, "AAA", stage="scan", created_at_utc="2026-07-10T10:00:00+00:00")
    _insert_rejected(journal, "AAA", stage="scan", created_at_utc="2026-07-11T10:00:00+00:00")
    _insert_rejected(journal, "SHADOW1", stage="shadow_scan")

    rows = journal.rejected_candidates_latest()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAA"
    assert rows[0]["occurrence_count"] == 2
    assert len(rows[0]["history"]) == 1
    journal.close()


def test_decisions_watch_and_rejected_rows_carry_dedup_fields_and_hindsight_survives(tmp_path):
    """The nd2 hindsight contract (raw attribution row under `hindsight_raw`,
    never pre-formatted -- see decisions.js:formatHindsight()) must keep
    working on the LATEST row after the dedup change."""
    settings, journal, _ = _seed(tmp_path)
    _insert_candidate(journal, "REPEATWATCH", "watch", created_at_utc="2026-07-10T10:00:00+00:00")
    _insert_candidate(journal, "REPEATWATCH", "watch", created_at_utc="2026-07-15T10:00:00+00:00")
    _insert_rejected(journal, "REPEATREJ", stage="scan", created_at_utc="2026-07-10T10:00:00+00:00")
    _insert_rejected(journal, "REPEATREJ", stage="scan", created_at_utc="2026-07-15T10:00:00+00:00")

    r = _client(settings).get("/api/v1/decisions", headers=HEADERS)
    body = r.json()

    watch_row = next(row for row in body["watch"] if row["symbol"] == "REPEATWATCH")
    assert watch_row["occurrence_count"] == 2
    assert "first_seen_at_utc" in watch_row
    assert len(watch_row["history"]) == 1

    rej_row = next(row for row in body["rejected"] if row["symbol"] == "REPEATREJ")
    assert rej_row["occurrence_count"] == 2
    assert "hindsight_raw" in rej_row  # unchanged nd2 contract, still on the latest row
    journal.close()
