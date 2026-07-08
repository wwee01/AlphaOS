# ALPHAOS SPECS — Regime Layer + Point-in-Time Text Archive + Universe Widening (drafted 2026-07-08)

Companion to the exit-review addendum. Same laws: spec → build → independent
review → **merge only on explicit human instruction**. Shadow-first. Additive
migrations only; bump `SCHEMA_VERSION`. §H.1 test discipline (date-seeded
mocks, direct construction). PR numbers are placeholders — the build session
reads HANDOVER.md first and slots into current numbering.

Build order and rationale:
**REG-1 → UNIV-1 → TEXT-0 → (months later, evidence-gated) REG-2.**
REG-1 lands first so every packet journaled from now on — including all future
text-card shadow evidence — is born regime-stamped instead of retrofitted.
UNIV-1 lands before TEXT-0 so the archiver's CIK map is built against the
widened universe from day one (archive gaps are unrecoverable). TEXT-0 then
starts immediately because its value compounds with calendar time. REG-2
changes live behavior and is gated on REG-1's shadow evidence. **Hard gate
carried through all of this: U3 (small caps) may never arm for execution
until cost-model calibration has shipped and been verified.**

---

## REG-1 — Regime Classifier + Packet Stamping (shadow/measurement only)

### Goal
Make regime a first-class dimension of all measurement: a frozen deterministic
classifier labels each trading day's market state; every candidate packet and
every shadow/replay row carries that label. No live decision changes.

### Non-goals
- **No arming, no gating, no allocation changes.** The existing regime filter
  (if any) is untouched. REG-1 measures; REG-2 (separate, later, evidence-gated)
  acts.
- Not a market-timing model. No optimization of regime boundaries against
  outcomes — the classifier is defined here, before any conditional results
  are examined (anti-data-mining law).
- No new data vendors. Inputs restricted to data v1 already has (Alpaca daily
  bars for SPY/market proxies).

### Design
1. **Classifier v1 — frozen, dumb, four states.** Computed once per trading
   day at scan time from EOD-available data only (no intraday peeking):
   - Inputs (all from daily bars): SPY close vs 50-day and 200-day SMA;
     20-day realized volatility of SPY as a percentile of its trailing
     1-year distribution.
   - Rules, evaluated top-down, first match wins:
     - `CRISIS`  : vol percentile ≥ 90
     - `CHOP`    : |SPY − SMA50| / SMA50 < 1.5% for ≥ 5 consecutive sessions
     - `TREND_UP`: SPY > SMA50 > SMA200
     - `TREND_DN`: SPY < SMA50 < SMA200
     - `CHOP`    : (fallback — anything unmatched)
   - All thresholds are literals in one config block, versioned as
     `regime_rules_v1`. Changing any threshold = `regime_rules_v2`, a new
     pre-registered version; v1 rows are never relabeled in place.
   - **Design rule (law for this subsystem): inputs describe the market,
     never the account.** No P&L, drawdown, or position fields may enter the
     classifier — enforced by test (function receives only a market-data
     struct with no account fields available to import).
2. **Storage (additive):**
   - table `regime_days` (date PK, regime, rules_version, input snapshot
     fields, computed ts) — one row per trading day, append-only; recompute
     under a new rules_version adds rows, never mutates.
   - new nullable column `regime` (+ `regime_rules_version`) on
     `candidate_packets`, stamped at packet creation from that day's
     `regime_days` row. Old packets stay NULL (see backfill below).
3. **Backfill (one-off, part of this PR):** compute `regime_days` from
   Alpaca daily history back to system inception and stamp existing packets
   by date-join. Deterministic from stored/vendor daily bars, so this is a
   derivation, not a mutation of evidence; journal one system_event
   `regime_backfill` with row counts and rules_version.
4. **Measurement plumbing:**
   - Replay/attribution outputs gain regime as a slice axis. Per the FDR law:
     regime-sliced statistics render with q-values once PORT-1 lands; until
     then the brief prints regime slices with the explicit caveat line
     "(descriptive only — no significance claimed)".
   - Effective-N note carried into PORT-1: regimes cluster in time; the
     `effective_n()` implementation must cluster same-regime consecutive
     days when evaluating regime-conditional claims.
5. **Daily Brief:** one header line — `Regime: TREND_UP (rules v1, day 14 of
   current state)` — plus regime column on card stat lines.
6. **Shadow arming-map scorer (the earn-its-existence instrument for REG-2):**
   a reporting job that computes, per card, replay R under two policies:
   `armed_always` vs `armed_per_map` for a **pre-registered candidate map**
   (v1 map: momentum cards → TREND_UP only; all cards stand down in CRISIS).
   Pure ledger math over existing shadow rows — nothing armed or disarmed in
   reality. Its output is the evidence REG-2 will later be judged on.
   Pre-registration block (paste into PR description): hypothesis (per-map
   arming improves card expectancy ≥ +0.1R over always-armed), metric
   (paired replay ΔR per card), floors (effective-N per regime per card,
   min 2 distinct regime episodes per state), analysis-not-before date.

### Tests
- Classifier: fixture bar histories → each of the four states reachable and
  deterministic across runs/hash seeds; boundary values (vol pct = 90 exactly,
  |dev| = 1.5% exactly) pinned by test so future edits scream.
- Precedence: CRISIS wins over TREND_UP when both match.
- No-account-inputs test (import/AST or interface-level).
- Packet stamping: packet created on date D carries regime(D); missing
  `regime_days` row → packet still journals with regime NULL + loud alert
  (never block the scan on the classifier).
- Backfill idempotent; re-run produces zero new rows under same rules_version.
- Brief renders caveat line pre-PORT-1 (regression on brief queries).

### Acceptance
- `regime_days` populated from inception to today; spot-check 5 known dates
  by hand against SPY charts, logged in `docs/incidents/` as a mini-drill.
- One week of new packets auto-stamped; shadow arming-map scorer produces its
  first (caveated) report in the brief.

---

## REG-2 — Regime as Allocator (STUB — do not build yet)

Registered now so intent and gate are on the record; spec to be written only
when the gate opens.

- **Gate:** REG-1's shadow arming-map scorer reaches its pre-registered floors
  AND verdict confirms per-map arming adds expectancy (q-corrected once
  PORT-1 is live).
- **Scope when built:** nightly arming map (which cards may propose tonight,
  per regime), map itself frozen + versioned like a card; the **one
  hard-coded exception** — `CRISIS` ⇒ all cards stand down — ships as a risk
  rule that does not wait for statistical proof, since its job is protecting
  the account, not earning R. (If the operator wants the CRISIS stand-down
  earlier than full REG-2, it can ship as a one-line addition to the existing
  deterministic gates in its own micro-PR.)
- Changes live behavior ⇒ full T-process, CRO worst-case re-check, drill.

---

## TEXT-0 — Point-in-Time Text Archive (collect only; no strategy, no AI, no trades)

### Goal
Begin accumulating the moat: an immutable, **seen-at-stamped** archive of
public company text (EDGAR filings + press releases) for the tradable
universe, so that future event/text strategies can be backtested and
shadow-tested against an honest timeline. Value compounds strictly with
calendar time; that is the entire reason this ships before any text strategy
exists.

### Non-goals
- **No trading logic, no scanner, no AI calls, no scoring.** TEXT-0 produces
  zero candidates and zero brief signals beyond a health line.
- No paid data sources. EDGAR (free, official) only in v1; the deferred news
  API remains gated behind the paired-comparison law in the decision log.
- No full-text search UI, no NLP preprocessing beyond storage hygiene.
  Readers come later; the archive's only job is to be *honest and complete*.

### Design
1. **Fetcher job:** new scheduler job `text_archive_pull`, nightly after US
   close (suggest 07:00 SGT), standard fuse + failure paging.
   - Source v1: SEC EDGAR daily-index/submissions endpoints for the current
     universe's CIKs. **Form catalog v1** (versioned in config as
     `edgar_forms_v1`; grouped by why they matter):
     - *Core reporting:* 8-K (+ all 99.x press-release exhibits), 10-K, 10-Q,
       6-K, 20-F
     - *Amendments/restatements:* 8-K/A, 10-K/A, 10-Q/A (restatement risk)
     - *Late-filing notices:* NT 10-K, NT 10-Q (potent negative signal in
       small caps)
     - *Insider activity:* Forms 3, 4, 5 (tiny XML docs, high volume,
       high value in no-coverage names)
     - *Ownership/activism:* SC 13D, SC 13G (+ /A amendments)
     - *Dilution/capital raises:* S-1, S-3, 424B*, S-8, Form D (private
       placements)
     - *Governance/comp:* DEF 14A, DEFA14A
     - *Corporate events:* SC TO-*, SC 14D9, DEFM14A (tenders/mergers),
       Form 25, Form 15 (delisting/deregistration)
     - *Institutional holdings:* 13F-HR (quarterly, lagged — archived for
       completeness; any strategy use must respect the lag via seen_at)
     Adding/removing forms later = `edgar_forms_v2`; coverage windows per
     form version are derivable from `fetch_run` history, so gaps are always
     attributable.
   - Universe→CIK mapping table maintained by the job (additive table
     `cik_map`), refreshed weekly — built from the **union of all UNIV-1
     tiers** (see UNIV-1 §7: once archived-for, always archived-for).
   - **Politeness/compliance:** honor SEC rate guidance (≤10 req/s hard-coded
     lower in config, proper User-Agent with contact email from `.env`),
     exponential backoff, resume tokens — the job must be a good citizen or
     the moat gets IP-banned.
2. **The law of this subsystem — seen-at stamping:** every document row
   records BOTH `published_at` (source's own timestamp) and `seen_at` (wall
   clock when AlphaOS fetched it). **All future backtests and shadow tests
   may only condition on `seen_at`.** Write this sentence into the table's
   schema comment and HANDOVER — it is the single fact that makes the
   archive worth anything (the PEAD-audit lesson, inverted).
3. **Storage (additive):**
   - table `text_documents` (id, cik, ticker-at-time, form type, accession
     no UNIQUE, published_at, seen_at, source_url, sha256, byte size,
     storage path, fetch_run id) — append-only; re-fetch of same accession
     is a no-op by unique constraint.
   - raw bodies on disk `data/text_archive/YYYY/MM/<accession>.gz`
     (gzipped as-received bytes — no cleaning, no parsing; parsed derivatives
     are future, separate, versioned tables). SQLite holds metadata only, so
     the ledger DB stays small.
   - `MANIFEST` semantics: sha256 verified on write and on backup.
4. **Backup integration:** archive directory joins the OPS-B nightly
   manifest; monthly off-ecosystem copy includes it. Growth estimate at
   ~2,000 small/mid caps with the full form catalog: dominated by Form 4
   volume (many, tiny) and 13F/proxy bulk — order of 4–8 GB/year gzipped —
   flag in brief if monthly growth exceeds 2× trailing average (universe bug
   or form-type explosion).
5. **Health surface:** one Daily Brief line —
   `Text archive: +214 docs last night · 18,340 total · 0 fetch errors ·
   oldest gap: none` — plus paging on job failure or on a
   zero-documents-fetched trading day (EDGAR is never truly quiet; silence
   means the fetcher is broken: the T1 lesson applied here).
6. **TEXT-0.1 (follow-on micro-PR, free official non-EDGAR sources):**
   scoped separately because these are different APIs with different failure
   modes; each ships as a config-gated fetcher module behind the same
   `text_documents` schema (add `source` column, additive):
   - FDA press announcements + advisory-committee calendar (free; only
     armed if the universe contains biotech/pharma names — config flag)
   - ClinicalTrials.gov API v2 study-record changes for universe-linked
     sponsors (same flag)
   - SEC litigation releases / trading suspensions (free, low volume,
     strong negative events)
   Rule for admitting any future source into v-free: **official primary
   publisher, free, stable API/ToS-clean** — no scraping of exchanges or
   news sites, no RSS of company IR pages (fragile, per-company, silent-gap
   prone; silent gaps are the one unforgivable defect in a point-in-time
   archive).
7. **Deliberate deferrals (record in Decision Log):** earnings-call
   transcripts and news wires (paid — gated on a text card earning evidence
   first, per the decision log's existing law); historical backfill of
   pre-inception filings (possible later via EDGAR archives but such rows
   must carry `seen_at = backfill` and are **never** valid for point-in-time
   tests — store them flagged or not at all).

### Tests
- Fetch pipeline against fixture EDGAR index/doc responses (no live network
  in tests): rows created, shas match, gzip round-trips byte-identical.
- Idempotency: same accession fetched twice → one row, one file.
- seen_at ≥ fetch start ts; published_at parsed from source, never from clock.
- Rate limiter honors configured ceiling (mock clock).
- Form catalog: fixture index containing one of each v1 form type → all
  captured; a non-catalog form (e.g. 11-K) → skipped and counted in a
  `skipped_forms` tally (visible, so catalog gaps are observable, not silent).
- Source column populated; TEXT-0.1 modules disabled by default in config.
- Zero-doc trading day → alert emitted; weekend/holiday → no false alert
  (market-calendar aware, date-seeded fixtures).
- Backup manifest includes archive shas.

### Acceptance
- Seven consecutive nights of real pulls; operator hand-opens two random
  stored documents against their EDGAR originals (sha + content spot check),
  logged in `docs/incidents/`.
- HANDOVER updated with the seen-at law and the deferral decisions.

---

## UNIV-1 — Tiered Universe Widening (measure everything, execute nothing new)

### Goal
Widen the scanned/measured universe downward in market cap — where coverage
thins and edges plausibly survive — via frozen, formula-defined tiers, while
changing **nothing** about what may execute. Every widened name flows through
scanner → gates → shadow paths → replay; execution eligibility remains exactly
today's book until separately and explicitly changed.

### Non-goals
- **No execution changes.** Proposals eligible for real (paper) orders remain
  restricted to the current book (U1). U2/U3 candidates are measurement-only:
  they may be journaled, baseline-scored, regime-stamped, replayed, and (within
  cost cap) AI-labelled, but the proposal constructor must refuse to emit an
  executable proposal for a non-U1 name. (When the operator later wants U2
  executable, that is a one-line eligibility change shipped as its own PR with
  its own pre-registration.)
- No new data vendors. Membership computed from Alpaca daily bars + reference
  data only.
- No micro caps, ever, in v1 (see floor).

### Design
1. **Tier definitions — frozen formulas, not lists** (config block
   `universe_rules_v1`; all thresholds literals; any change = v2):
   - `U1` — current book, unchanged (control group; the only
     execution-eligible tier).
   - `U2` — mid caps: market cap $2B–$10B, median 20-day dollar volume
     ≥ $20M, price ≥ $5.
   - `U3` — small caps: market cap $300M–$2B, median 20-day dollar volume
     ≥ $5M, price ≥ $3. **Shadow-only permanently until the cost-calibration
     gate opens (below).**
   - **Floor (excluded from everything):** cap < $300M, dollar volume below
     U3 threshold, price < $3, leveraged/inverse ETFs, pre-deal SPACs.
     Rationale journaled in Decision Log: below the floor, spreads and
     manipulation make even shadow R fiction.
   - **Flags, not exclusions:** `recent_ipo` (< 12 months listed) and
     `biotech_pharma` (sector code) are stamped as boolean columns — these
     names are event-rich and wanted, but must be sliceable later.
2. **Monthly rebalance job:** membership recomputed on the first trading day
   of each month from trailing data; intra-month membership is frozen (no
   daily churn — churn creates untrackable ledger identity). A name crossing
   a boundary mid-month keeps its tier until rebalance.
3. **Membership journaling — the survivorship law (non-negotiable):**
   additive table `universe_days` (date, ticker, tier, flags, rules_version)
   — one row per name per trading day, append-only, written by the nightly
   scan regardless of whether the name produced a candidate. This is the
   system's own point-in-time record of *what it could see*; without it, all
   future backtests inherit survivor bias as delistings silently shrink the
   universe. Delisted names simply stop appearing — their history remains.
   (Storage: ~2,500 rows/day ≈ 600k rows/yr; trivial.)
4. **Scanner + shadow integration:**
   - Deterministic paths (scanner scoring, gates, baseline rule, regime
     stamp, replay) run on **all** tiers — they are free.
   - AI labelling is metered: only the top-N ranked candidates per night
     receive an LLM label, N from the existing cost guard config. Ranking is
     tier-blind (the best candidates get labelled wherever they live), but
     tier is stamped so attribution can slice AI value by tier.
   - `candidate_packets` gains additive columns: `tier`, `universe_rules_version`,
     flag booleans.
5. **Cost-calibration gate for U3 (pre-registered here):** U3 may become
   execution-eligible only after (a) the cost-model calibration task (existing
   punch list) has shipped, (b) calibration includes spread/slippage measured
   on U3-liquidity names specifically, and (c) shadow evidence for at least
   one card shows positive expectancy in U3 **net of the calibrated costs** at
   effective-N floor. All three, in writing, before the eligibility PR is
   even specced.
6. **Daily Brief:** universe line — `Universe: U1 512 · U2 634 · U3 887
   (rules v1, rebalanced 2026-08-03) · delisted since inception: 14` — and
   candidate/stat lines gain a tier column.
7. **TEXT-0 dependency:** the archiver's `cik_map` is built from the union of
   all tiers (including floor-excluded names *previously* in a tier — once
   archived-for, always archived-for, so delistings don't create text gaps).

### Tests
- Tier formulas: constructed fixtures at each boundary (cap exactly $2B,
  ADV exactly $20M, price exactly $5) → pinned assignments; leveraged-ETF
  and SPAC fixtures → floor.
- Determinism across runs/hash seeds; membership for a fixture month
  reproducible from stored bars.
- Mid-month boundary crossing → tier unchanged until rebalance.
- `universe_days` written for every member nightly, including names with no
  candidates; append-only enforced (update attempt fails in test).
- Proposal constructor: U2/U3 candidate → refuses executable proposal,
  journals `skip_reason=tier_not_executable`; U1 unchanged (regression).
- AI labelling: fixture night with 40 candidates, cap N=8 → exactly 8
  labelled, ranking order respected, tiers recorded.
- Brief renders universe line; tier column present in card stats.

### Acceptance
- First monthly rebalance produces plausible counts (operator sanity-checks
  ~10 names per tier against public data, logged in `docs/incidents/`).
- One week of `universe_days` accumulation verified complete vs member count.
- A U3 candidate observed flowing scanner → shadow → replay with zero
  proposals emitted (journal inspection), logged as a mini-drill.

---

*REG-1, UNIV-1, and TEXT-0 are measurement/collection only — none touches the
chokepoint, execution eligibility, arming, or the Never-List. REG-2 is a stub
with an explicit evidence gate and is not authorized for build by this
document. U3 execution eligibility is pre-gated as specified in UNIV-1 §5.*
