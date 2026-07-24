# ALPHAOS SPEC — HOLD-1: 10-Trading-Day Shadow Outcome Horizon — drafted 2026-07-24

Same laws as every spec in this directory: spec → build → independent review →
**merge only on explicit human instruction**. Additive migrations only
(`SCHEMA_VERSION` stays 3). §H.1 test discipline. Measurement-only: ZERO
live-path change.

## Why (operator decision, 2026-07-24 — see master reference §9 decision log)

The operator is open to extending the 1–5 day holding window (the 2.4×ATR
minimum target distance needs ~5–8 sessions of typical trend progress), but
ruled measurement-first: extending live holds halves closed-observations-per-
week on a 5-slot book and doubles earnings-window collisions, so the case
must be made by data. HOLD-1 is that instrument.

**Pre-registered question (stated before any data is examined):** what
fraction of quality setups reach the 2.4×ATR favorable-excursion distance in
days 6–10, having failed to reach it by day 5? Revisit condition: ≥30
resolved 10-day observations (independent-cluster counting per the PORT-1
effective-N law). The answer drives an operator ruling on extension (likely
8–10 trading days if material); it never auto-changes anything.

## Non-goals
- `max_holding_days` and every live gate/floor/sizing parameter unchanged.
- No new data fetches — the tracker's existing bar source covers 10 days the
  same way it covers 5.
- No backfill fabrication: candidates whose 10-day window predates HOLD-1's
  merge get 10-day rows only where the bar data genuinely covers the window
  (it does — bars are historical); but rows are stamped so the pre-
  registration date is provable against `created_at`/`updated_at`.
- Not this ticket: any change to the 1/3/5-day columns or their consumers.

## Design
1. **Additive columns on `candidate_outcomes`** (SCHEMA_VERSION stays 3):
   `forward_10d_return_pct`, `forward_10d_r`, `max_favorable_10d_r`,
   `max_adverse_10d_r`, `bars_to_favorable_10d`, `bars_to_adverse_10d` —
   exact same semantics as the existing 5d family, horizon extended.
2. **Tracker extension** (`alphaos/learning/outcomes_tracker.py` +
   `mfe_mae_backfill` machinery): resolve the 10d family wherever the 5d
   family resolves, same bar source, same direction/R conventions
   (R denominated in the same per-candidate risk basis the 5d columns use —
   verify and mirror, do not re-derive). A row whose 10-day window has not
   yet elapsed stays partial for the 10d family exactly as the 5d family
   handles its own not-yet-elapsed windows.
3. **The pre-registered report line.** One section in the existing outcomes/
   attribution report surface: among rows with BOTH 5d and 10d families
   resolved and `max_favorable_5d_r < 2.4`: count and fraction with
   `max_favorable_10d_r >= 2.4`, split by candidate cohort (proposed /
   watch / rejected), with the explicit caveat line "pre-registered
   2026-07-24; descriptive until n≥30 independent clusters; no significance
   claimed." Also report the same fraction for `>= 1.2` (halfway) as
   context, labeled as such.
4. **Digest visibility:** one line in the daily brief reporting accumulation
   toward the floor ("HOLD-1: N/30 resolved 10d observations"), so the
   revisit condition is watched without anyone remembering to query.

## Tests (hermetic, §H.1)
1. Additive migration on an old DB; SCHEMA_VERSION stays 3; old rows NULL.
2. 10d resolution correctness on constructed bar fixtures: exact MFE/MAE/
   bars-to arithmetic for long AND short, including a case where the target
   distance is first reached on day 7 (the exact scenario the question
   cares about: 5d family says <2.4, 10d family says >=2.4 with
   bars_to_favorable_10d = 7).
3. Not-yet-elapsed 10d window stays pending/partial without touching the 5d
   family's status.
4. Report section arithmetic on a seeded ledger: known cohort counts in →
   exact fractions out; caveat line present.
5. Structural: no live scan/eval/risk/execution module imports or reads the
   new columns (zero-decision-surface law, grep/AST — same pattern as
   AB-EVAL-1).
6. Digest line renders the accumulation count.

## Sequencing
Build AFTER INSTR-3 merges (both tickets add to `schema.py`; sequential
avoids a pointless conflict). Small ticket: one builder, standard two-audit
ceremony, merge on explicit operator instruction.
