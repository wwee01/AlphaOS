# ALPHAOS SPEC — INSTR-3: Honest Trend + Multi-Day Context (prompt v3) — drafted 2026-07-24

Same laws as every spec in this directory: spec → build → independent review →
**merge only on explicit human instruction**. Shadow-first. Additive migrations
only (`SCHEMA_VERSION` stays 3 — additive columns/tables never bump it, per
`schema.py`'s governing law). §H.1 test discipline. Successor to INSTR-1
(honest instruments, 2026-07-09) and INSTR-2 (ATR-coherent prompt v2, merged
`e90efb3` 2026-07-23): same doctrine — give the evaluator MORE HONEST
INFORMATION, never a lower bar.

## Motivating evidence (proof-gate run `abrun_ec8085b50775`, 2026-07-24)

INSTR-2's four-arm proof gate passed all P-gates (RR_FLOOR downgrades 100%→0%
both models, zero anti-coaching flips, 240/240 rows). But under v2, luna
raw-proposes 1/60 vs mini 33/60, and luna's rejection text names two evidence
gaps — both real, both ours:

1. **`trend_quality` is dishonest.** `candidate_scanner.py:367`:
   `trend_quality = round(min(1.0, abs(change) * 10), 3)` — the same intraday
   change_pct rescaled and relabeled as a trend measure. Ledger proof: NFLX
   rejection cites "trend quality is weak at 0.278" = |−2.78%|×10; META
   "0.398" = 3.98%×10. The model penalizes candidates using a field whose
   name promises multi-day trend and whose value is a number it already has.
2. **No multi-day history reaches the prompt.** The evaluator gets one
   intraday snapshot. Luna's rejections are literal: "no multi-bar
   continuation structure is supplied" (DIA), "lacks confirmed multi-session
   continuation structure" (META), "available structure does not sufficiently
   support that distance" (NVDA, re the 2.4×ATR minimum target distance).
   Meanwhile the nightly ATR job (`reports/atr_service.py:55`) already
   fetches ~25 calendar days of daily OHLCV per core symbol per day via
   `alpaca_bars.get_daily_bars`, computes one ATR scalar, and discards the
   bars. The evidence the evaluator demands is downloaded daily and thrown
   away.

R5 first-pass (candidate_outcomes join, n=36 packets with full 5-day data,
descriptive only): on this corpus mini's 17 v2-proposes bracket-replayed to
net ≈ −2.6R while the 19 packets both models declined netted −9.2R. Luna's
near-total rejection was the more profitable verdict this week. INSTR-3 is
therefore NOT a "make luna propose more" ticket — it is an evidence-honesty
ticket whose success is measured by the model no longer citing missing
evidence, with propose-rate movement REPORTED but never targeted.

## Goal

Persist the daily bars we already fetch; compute a real, versioned trend
measure from them; surface both (plus derived structure levels) into a
settings-gated prompt v3 — proven through the same four-arm AB-EVAL ceremony
INSTR-2 used, with anti-coaching gates.

## Non-goals (frozen)

- No change to `MIN_REWARD_RISK` (1.2), `ATR_STOP_MULTIPLIER_V1` (2.0), any
  risk floor, position sizing, scanner thresholds, or the 1–5 day holding
  window (separately flagged operator question; NOT this ticket).
- No new data vendor, no new network calls on the live scan/eval path (bars
  are read from the DB table this ticket adds; the fetch stays in the nightly
  ATR job where it already lives).
- v1 AND v2 prompt outputs stay **byte-identical** (two golden tests). The
  INSTR-2 containment and stale-fixture-leak behaviors survive untouched.
- No propose-rate target anywhere in code, prompt, or gates. The v3 section
  must carry the same neutrality sentence discipline as ATR_STOP_POLICY.
- Existing `trend_quality` column semantics unchanged for existing consumers
  (grep them; do not repurpose the column). The fix is ADDITIVE fields; v3
  simply stops showing the dishonest one.
- Mock path untouched (never builds a prompt).

## Design

1. **`daily_bars` table (additive; SCHEMA_VERSION stays 3).** Columns:
   symbol, market_date, open, high, low, close, volume, source_feed,
   created stamps; UNIQUE(symbol, market_date). Populated inside the existing
   nightly ATR job at the point `atr_service.py` already holds the fetched
   bars — persist before discarding, idempotent upsert (INSERT OR IGNORE
   against the unique key). Note the deliberate boundary crossing:
   `alpaca_bars.py`'s module docstring declares itself measurement-layer-only;
   INSTR-3 does NOT change that — the live scan/eval path never calls the
   provider; it reads `daily_bars`. The ATR job (already a measurement-layer
   consumer) simply stops discarding what it fetched. Update the docstring to
   say exactly that. Accumulation starts at merge; ~17 trading bars arrive
   with the first post-merge ATR job run (the fetch window is ~25 calendar
   days), so v3's data floor is met on day one for core symbols.

2. **Honest trend fields (`trend_rules_v1`).** Computed at candidate creation
   from `daily_bars` (completed sessions only, excluding scan day):
   - `consistency = (up_days − down_days) / 10` over the last 10 completed
     sessions (close vs prior close), range −1..+1.
   - `extension = clamp((close_last − close_10_sessions_ago) / (2.4 × ATR14),
     −1, +1)` — deliberately denominated in the trade's own minimum target
     distance: "has this name demonstrated it can cover a target-sized move
     in ~2 weeks?" ATR14 from `atr_history` (same `_latest_atr` source the
     evaluator uses — one ruler).
   - `signed_trend = round(0.5×consistency + 0.5×extension, 3)`.
   - Stored NEW creation fields: `trend_score` = signed_trend for longs,
     −signed_trend for shorts (alignment with trade direction, −1..+1), and
     `trend_rules_version = "trend_rules_v1"`. Fewer than 10 completed
     sessions in `daily_bars`, or no ATR → `trend_score = NULL` (honest
     absence; v3 prompt then omits the trend line — never a fake fallback,
     and NEVER falling back to the old `abs(change)*10`).
   - Worked examples (must appear as unit tests): 10 sessions of closes
     [100,101,102,101,103,104,105,104,106,107], ATR14=2.5 → up_days=7,
     down_days=2 (one flat... use distinct closes in the real fixture),
     consistency=0.5 exactly with 8 up / 3 down replaced by exact fixture
     the builder constructs; extension = (107−100)/(2.4×2.5)=7/6→clamped
     1.0; signed_trend = 0.75; short direction → trend_score −0.75. Builder:
     construct exact fixtures where every intermediate is asserted, both
     directions.
   - The scanner's creation dict gains these two fields → the AB-EVAL corpus
     whitelist (`CANDIDATE_CREATION_FIELDS`) and its lockstep AST test must
     be extended in the same commit.

3. **`MULTI_DAY_CONTEXT` prompt section (v3 only).** Rendered between
   `MARKET_SNAPSHOT` and `ATR_STOP_POLICY` when the augmented snapshot
   carries a `multi_day_context` block. `_augment_snapshot_for_prompt`
   computes it fresh under (live AND version=="v3"): up to the last 15
   completed daily bars from `daily_bars` (date, O, H, L, C, V, compact),
   plus derived: `recent_high_10d`, `recent_low_10d` (excluding scan day),
   `dist_to_recent_high_atr = (recent_high − last_price)/ATR14` (and low
   analogue), `trend_score` + `trend_rules_version` (from the candidate row),
   and nothing else. Under v3 the dishonest legacy `trend_quality` key is
   POPPED from the serialized MARKET_SNAPSHOT (v1/v2 serialization unchanged
   — byte-identity). Fewer than 5 bars → no block, no section, no mention.
   Framing (testable, INSTR-2 discipline): the section states data only,
   ends with "This context does not make proposing more or less desirable;
   apply your usual evidence standards unchanged." It must NOT characterize
   the bars (no "supports continuation", no "strong/weak" adjectives).
   All block contents are frozen into `evaluation.snapshot` → `snapshot_json`
   (replayability, same law as `atr_policy`).

4. **Version mechanics.** `OPENAI_PROMPT_VERSION` validation set becomes
   `{"v1","v2","v3"}` (`settings.py:1394`). **Two existing `== "v2"` gates
   MUST become membership checks `in ("v2","v3")`** or v3 silently loses the
   ATR policy block (incoherent by construction):
   `openai_client.py:537` (`atr_policy` kwarg gate — this is the INSTR-2
   audit-MEDIUM fix; regressing it re-opens that finding) and the
   `_augment_snapshot_for_prompt` early-return (`!= "v2"` at ~line 252).
   Pin with a test asserting a v3 prompt contains BOTH `ATR_STOP_POLICY` and
   `MULTI_DAY_CONTEXT`, in that order... (order per Design 3: MULTI_DAY_CONTEXT
   first, then ATR_STOP_POLICY, then DATA_FRESHNESS). The stale-fixture leak
   regression test extends to the new block: a v3-era fixture (snapshot
   carrying `multi_day_context` AND `atr_policy`) replayed under v1 and v2
   arms must leak NEITHER section (v2 arm shows ATR_STOP_POLICY from its own
   fresh augment, never the archived block).

5. **Harness + proof gate (pre-registered).** Arms validation gains v3 (CLI
   token check). After merge-dark, one four-arm run over the SAME frozen
   corpus (do not rebuild): `--arms gpt-5.4-mini:v2 gpt-5.6-luna:v2
   gpt-5.4-mini:v3 gpt-5.6-luna:v3` (240 calls; v2 arms are the concurrent
   controls; anchor run `abrun_ec8085b50775`). Gates:
   - **Q0 validity:** both v2 arms reproduce RR_FLOOR ≈ 0 on raw proposes
     (anchor: 0%/0%); corpus errors 0; 240 rows. Else uninterpretable.
   - **Q1 primary (evidence uptake):** the fraction of luna:v3 rejects whose
     reasoning cites missing history — pre-registered patterns, matched
     case-insensitively over `reasoning_summary`: "multi-bar", "multi-
     session", "structure is supplied", "snapshot lacks", "single-session",
     "single snapshot" — drops by ≥50% vs luna:v2's fraction on the same
     packets. This is the primary: we closed an information gap; success is
     the model no longer citing the gap.
   - **Q2 propose movement (REPORTED, NOT GATED):** propose counts per arm
     vs anchors (mini 33, luna 1). Explicitly no target in either direction.
   - **Q3 anti-coaching:** any v2-reject→v3-propose flip must cite concrete
     bar-derived structure (a level, a multi-day pattern) as its reason;
     a flip citing the mere presence of the context section, or the trend
     score alone without price structure, fails Q3. Every flip listed with
     both reasonings for operator reading.
   - **Q4 fail-safes:** NO_ATR downgrade counts unchanged v2 vs v3 per model.
   Cutover to v3 remains a separate explicit operator decision (decision
   row), sequenced with the pending model ruling.

## Tests (hermetic, §H.1; the builder implements ALL of these)

1. Golden v1: byte-identical prompt (existing test stays green unmodified).
2. Golden v2: NEW — byte-identical to INSTR-2's v2 output for a fixed
   candidate/snapshot with atr_policy present (pin before touching builder).
3. v3 renders MULTI_DAY_CONTEXT then ATR_STOP_POLICY then DATA_FRESHNESS;
   both blocks' interpolated values correct.
4. v3 with <5 daily bars: no MULTI_DAY_CONTEXT section, ATR_STOP_POLICY
   still present, no error.
5. trend_rules_v1 worked examples: exact fixtures, every intermediate
   asserted (consistency, extension incl. clamp both ends, signed_trend,
   long AND short alignment).
6. trend_score NULL when <10 sessions or no ATR; v3 omits the trend line;
   never falls back to abs(change)*10.
7. `daily_bars` additive migration on an old DB; SCHEMA_VERSION stays 3;
   idempotent re-persist (unique key, second run adds 0 rows).
8. ATR job persists bars: run the job against a mocked provider, assert
   rows land AND ATR result unchanged vs pre-INSTR-3 for identical bars.
9. Structural (AST): live scan/eval modules do not import the bars provider;
   `multi_day_context` is computed only inside `_augment_snapshot_for_prompt`
   from the `daily_bars` table.
10. Version gates: v3 prompt contains atr_policy block (regression on the
    two `== "v2"` sites); settings rejects "v4"; `--arms x:v3` parses,
    `x:v4` refused.
11. Stale-fixture leak extended: v3-era fixture under v1 arm → neither
    section, no stale values; under v2 arm → fresh ATR_STOP_POLICY only,
    no MULTI_DAY_CONTEXT, no stale values.
12. Snapshot journaling: v3 rows' snapshot_json carries the full
    multi_day_context shown; v2 rows carry none.
13. Whitelist lockstep: creation-dict additions flow through the AST
    lockstep test; frozen pre-INSTR-3 fixtures (no trend fields) still load.
14. Neutrality: v3 section contains the exact neutrality sentence and no
    characterizing adjectives (test the renderer's literal output against a
    banned-word list: "supports", "strong", "weak", "bullish", "bearish"
    within the MULTI_DAY_CONTEXT block).
15. INSTR-2 containment test stays green unmodified; augment-time bars/ATR
    read failure under v3 degrades to a v2-shaped prompt (journaled ERROR,
    never propagates — same law as the v2 augment read).

## Out of scope
Holding-window extension (operator question, separate pre-registered
measurement first); TRIP-1; model keep-vs-revert ruling; SIP feed upgrade;
EVENT-SCAN-1 / shadow graduation (already on the roadmap).
