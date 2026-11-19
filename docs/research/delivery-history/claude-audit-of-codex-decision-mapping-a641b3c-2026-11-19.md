# Audit of Codex decision-time mapping `a641b3c` — Claude, 19 November 2026

**Exact SHA:** `a641b3caa95c325c6ad97547dd62f0b9e16a30a7` (implementation `4470356` + upper-bound fix),
base `3f1721c`, 17 files, +3,197/−14. **Spec:** `docs/research/explore1-decision-time-mapping-2026-11-18.md`.
**Method:** my own clone at the exact SHA (Codex checkout untouched); my own read-only production
snapshot (SQLite backup API, 22:33 SGT, latest evaluator row 18 Nov 20:52:17Z); migration + enrollment
rehearsal on a copy; 21 independent probes (`claude-audit-a641b3c-decision-mapping-probes-2026-11-19.py`) plus
the D2 two-pass script (`claude-audit-a641b3c-transient-proof-probe-2026-11-19.py`); Codex's 195 focused tests; full
suite in my own clone. No production write, no provider call, nothing merged or deployed.

**Verdict: NOT ACCEPTED as delivered — two small defects (D1, D2) must be fixed before deployment,
because both freeze permanently at the first production enrollment.** Everything else below is
confirmed against the spec.

## 1. Confirmed

| Area | Result |
|---|---|
| Routing stamp | One `UPDATE` writes draw + `explore_routed_at_utc` for owner, copier (own stamp, window key NULL, no pool) and off-window scans; write-once; re-entry never restamps; a stamp without a draw refuses. Probes G4–G6: stamp 15:59:58 ET stays the event although the commit lands at 16:00:04; a crash before commit persists nothing and the re-run stamps its own later time |
| §6 examples | All 13 exact examples (16:01 opinion, exactly 16:00:00.000000, 15:59:59.999999, both early closes incl. 13:00:00 and the post-close 15:45 scan, Thanksgiving, Saturday, 24 Dec) give my spec's D / D+5 / D+10 (G1). Bounded straddle → `session_unproven`; L>U → `session_unproven`; pre-open → `out_of_scope`; straddling open → `boundary_unproven`; naive timestamp → `timestamp_unavailable`; exact outside interval → `identity_mismatch` (G2) |
| Ordering / dedupe | Overlapping interval vs exact → both `order_unproven`; disjoint → first `ready`, later `duplicate_in_session`; 16:02 decision + next-morning decision on the same symbol → one decision (CK ruling 1) (G3) |
| Pinned calendar | 155 sessions 2026-11-16 → 2027-06-30, byte-identical to `market_calendar.is_trading_day` days with 16:00 ET closes except 27 Nov and 24 Dec at 13:00 ET; DST change 14 Mar 2027 correct; SHA-256 `de912084…f57c` pinned and enforced |
| Legacy bounds | L = latest live evaluator stamp of the scan; U = min(completion, earliest same-scan `explore_1` proposal). In campaign mode every evaluated candidate is queued and routed after the loop, so no campaign proposal can precede the draw. Producer bytes: `orchestrator.py` at `ea036e1`/`70e0063`/`d78ad87`/`3f1721c` all hash `161329c3…`, the tip `0ad26c4d…`; both in the approved set, and a test fails if the HEAD file's digest is not |
| Production rehearsal (my snapshot) | All nine campaign scans enrol; the three 16 Nov scans have exactly my spec's L/U and D = 16 Nov; owner pools 1/2/1/0/0/0/0/0/0; 103 rows, 91 ready, 12 duplicates (same as Codex) |
| Preservation | Every original table identical in its original columns; only additions are `scan_batches.explore_routed_at_utc` (288 rows, values intact), one `schema_migration` system event, and the new table. `preregistrations` unchanged |
| Prices | Candidate and SPY legs independent; request must follow endpoint close; missing exact bar pending until +10 days then `unavailable`; leg + scalar committed together (G9) |
| Tests | Codex focused 195 passed; my probes 21 passed; ruff clean on new modules |

## 2. Defects

### D1 — provider-failure fallbacks enter the ranking roster (fix before deploy)

The evaluator's exception fallback (`openai_client.py:412`, and `:461` after a post-process exception)
writes `decision='reject'`, `confidence=0.0`, no levels, `risk_flags=["OPENAI_REJECT"]`, text "OpenAI
call failed; rejected for safety." — and `validation_status='passed'` from `_rejection`'s default.
`close_ref_mapping._ranking_reason` keys on `validation_status`, so these rows are ranking-eligible.
In production, five 18-Nov fallbacks (AAPL, AMZN, AVGO, DIS, AMD) are ranking-`ready` and five more
are ranking duplicates (G7 reproduces). Ranking puts reject-by-confidence-ascending first, so a
confidence-0 fallback would rank as the *most* promising reject — a systematic artifact, not noise.
Eligibility is frozen into `decision_json` at enrollment, so the first production run would make it
permanent. **Treatment: §4.**

### D2 — a transient source-proof failure becomes a permanent exclusion (fix before deploy)

`close_ref_lineage.committed_ordering` runs `git show` (2 s timeout). On `OSError`/timeout it returns
`source_commit_unavailable`; the tracker then enrols every legacy arm/pool record of that scan as
`unavailable: ordering_source_unproven` and every evaluator row as ranking-ineligible
(`non_degraded_scan_unproven`), and the enrollment is immutable. Probe (`transient.py` on a snapshot
copy): one timeout during the first pass, git healthy on the second → 5 arm + 4 pool records stay
unavailable and 94 of 94 evaluator rows stay ranking-ineligible. All future scans' ranking rows depend
on the same proof. **Fix:** treat `source_commit_unavailable` (and any exception) as *defer this scan*,
like an open scan; only a verified digest mismatch, missing lineage or disagreeing lineage commits are
permanent reasons. Add the two-pass test.

## 3. Observations (not defects of this SHA)

- **O1 — raw prices and splits (CK decision before the first leg is written, 23 Nov after 21:00Z).**
  Legs use `adjustment=raw`, which the contract allows ("same basis, recorded"). A split inside a
  window freezes as a return: a 4:1 split gives r ≈ −0.75 (G8), irreversibly. Recommendation: switch
  both legs to split-adjusted closes from the same response (the ratio is invariant to later splits;
  dividends stay unadjusted, so it is still a price return) and record `adjustment=split`; the
  alternative is raw plus a split check that marks the leg quality-failed (v1 already excludes and
  counts quality-failed rows).
- **O2 — ranking test feasibility.** The scanner has turned short-heavy: of 94 campaign evaluations
  only 16 are long, 10 of them fallbacks. A ranking session needs ≥ 10 long rows; no campaign session
  has reached it yet. Not actionable in code; CK should know the 20-session floor is at risk if this
  persists.

## 4. The quota diagnosis and the five fallback records

**Diagnosis — agreed.** HTTP 429 `insufficient_quota` / `credit_balance_exhausted` since
16 Nov 20:57:52Z, same signature as 7 August. My spec §8 wrongly said the evaluator was passing on
17 Nov; it was failing too (correction appended there). All 142 `OPENAI_REJECT` rows since August
share one signature (flag + fixed text + null tokens + confidence 0 + `passed`); no non-fallback live
row since 1 Oct lacks tokens. Nothing has been routed since 16 Nov 20:57Z; **CK must restore credits**
— every session until then is lost to every arm.

**Treatment of the five ranking-ready fallbacks (and their duplicates).** The registered roster is
"a passed, non-mock *evaluator opinion*". A fallback is not an opinion: no model response exists. The
stored `passed` is a producer default. So:
1. **Nothing is rewritten.** `openai_evaluations` and `candidate_labels` rows stay exactly as stored.
2. **Fixed, existing predicate.** A row is a fail-safe iff `risk_flags_json` contains `OPENAI_REJECT`
   — the codebase's own definition (`ai/labeller_health.is_openai_reject_row`, used by the daily brief).
   Apply it identically to historical and future rows, so the roster is the same before and after any
   status fix.
3. **Additive exclusion.** Such a row is enrolled (measurement kept) with `ranking_eligible=false`,
   `ranking_exclusion_reason='evaluator_failsafe'`, counted in every output, and never reported as an
   evaluator decision. It consumes no ranking key, so a later genuine opinion of the same symbol in the
   same session becomes the roster row (filter-then-earliest, as the addendum says). Arms and pool are
   unaffected: routing already refuses `OPENAI_REJECT`.
4. **Not silent.** This is an interpretation of the registered text, so it needs CK's ruling and a §9
   row *before any result exists*: nothing has matured (first 5-day endpoint for 18 Nov is 25 Nov), so
   the choice is result-blind. Practical effect today: 18 Nov would have had 5 roster rows, below the
   10-row validity threshold either way.

**Codex's future-only status fix — not needed for EXPLORE-1; defer unless CK wants it for monitoring.**
If built: (a) `_reject_candidate`'s default maps *any* non-`passed` status to `INVENTED_CATALYST`
(`orchestrator.py` ≈3285, documented trap), so the fallback call site needs an explicit
`reason=OPENAI_REJECT` branch first; (b) it changes at least eight consumers — routing (outcome
unchanged), `tqs`, `watch_advisory`, `hold10_enrollment`/`hold10_policy_binding` (future fallbacks
would leave that registered study's population — correct, but CK should know), `origin_provenance`,
`canary.recovery_evidence`, the digest mix; (c) cover both exception paths (`:412`, `:461`) with one
value — I suggest `live_exception`, the labeller's existing word; (d) keep the flag, decision and
token inheritance unchanged.

## 5. Full suite

My clone at the exact SHA (`pytest tests/`, explicit exit code): **4,997 passed, 3 skipped, 0 failed,
EXITCODE=0** (34 min, run while a Codex suite was also running). Codex's run had the one failure
`test_last30days_partial_flow.py::test_timeout_with_no_returned_items_does_not_invent_partial_success`.
Five isolated runs each: base (`fc08f73`, code-identical to `3f1721c`) failed 2 of 5; tip 0 of 5. The
module imports none of the changed files and the diff touches nothing under `alphaos/research` or
`alphaos/ai`. Pre-existing timing flake, not caused by this change; it belongs with brief item 3.

## 6. Required before deployment

D1 (with CK's ruling on §4), D2, and CK's decision on O1. Then Codex hands the new exact SHA; I
re-audit the delta. Deployment must also wait for API credits to be restored, or the first enrollment
will carry only fallback rows.
