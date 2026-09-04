# ChatGPT weekly research task — prompt

Paste this as the instruction for a **weekly scheduled task in the ChatGPT
desktop app on the Mac mini**, with the AlphaOS project folder attached (a
web-created task cannot write to local files).

---

## THE PROMPT

You are producing a weekly research digest for **AlphaOS**, a single-operator,
evidence-gated systematic trading system. Write it to:

`docs/research/inbox/<YYYY-MM-DD>.md`

(today's date, ISO). Create the file; never overwrite an existing one.

### What AlphaOS is, so you can judge relevance

A governed paper-trading system on one Mac. Long-only US equity swing momentum,
5 concurrent position slots, 1% risk per trade, ATR(14)-based stops at k=2.0,
a minimum reward:risk floor, and holding windows of 1–10 trading days chosen
per-setup by an LLM evaluator. Orders go to a broker paper API; real money is
structurally unreachable.

Its distinguishing feature is not the strategy — it is the **governance**:
pre-registered hypotheses with fixed analysis dates and BH-FDR correction over
the whole family, frozen deterministic baseline arms as reference, replay-based
counterfactual scoring, append-only decision logs, and config changes that must
move a provenance hash. It is deliberately small-n: roughly one closed trade
per week, ~130 replay observations per week.

### The constraints that make most published work inapplicable

State these honestly when they bite — an item that fails them is still worth
listing if you say why:

- **Tiny effective N.** Overlapping holding windows on repeated symbols. Honest
  cluster counts are 16–25, not thousands. Methods needing large samples,
  heavy cross-validation, or deep nets are usually out.
- **One operator, one machine.** No cluster, no tick data, no vendor feeds
  beyond a free IEX-tier equity feed. Implementation cost is a real constraint.
- **Evidence discipline over performance claims.** A method that improves a
  backtest but cannot be pre-registered and forward-tested is less useful here
  than a weaker method that can.

### THIS WEEK'S PRIORITY QUESTIONS

Prefer items bearing on these five open engineering problems. These are real,
currently-unresolved decisions:

1. **Frozen-corpus replay decay.** A frozen 60-packet evaluation corpus holds
   July price snapshots, but the prompt now appends *live-fetched* daily bars,
   so the model sees internally contradictory inputs and rejects on staleness
   (45 of 60 packets). How should an LLM-evaluation replay corpus be frozen so
   it stays temporally coherent — freeze the bars too, pin an as-of date and
   recompute, or something else? What breaks comparability across a re-freeze?
2. **Effective sample size for overlapping-window trade outcomes.** Positions
   on the same symbol with overlapping holding windows are not independent.
   What clustering unit is defensible — decision day, symbol-day, block
   bootstrap — and how should confidence intervals and floors be computed at
   n≈20 clusters?
3. **Execution cost modelling.** The system models 1 basis point of slippage
   per side while its liquidity gate admits spreads up to 1%. Realised stop
   fills have come in at −1.15R to −2.05R against an assumed exactly −1.0R.
   What is defensible practice for modelling slippage and gap-through-stop risk
   for retail-size equity orders, and how should replay-vs-realised divergence
   be measured rather than assumed?
4. **Kill-switch semantics for exposure-reducing actions.** When an operator
   engages a halt, should automation still be permitted to *cancel protective
   orders and liquidate* a position (exposure-reducing but trade-taking), or
   should a halt stop everything? Prior art on trading-system halt semantics,
   and the failure modes of each choice.
5. **Partial-fill reconciliation on liquidation.** A close order that fills
   partially leaves residual shares with cancelled protective orders. What are
   the accepted patterns for detecting, recording and re-protecting a partial
   liquidation?

Secondary standing interests: decision lineage and provenance for AI-assisted
pipelines; pre-registration and multiple-comparison discipline in small-n
trading research; drift detection for LLM evaluators in production.

### OUTPUT RULES — read these carefully

**Write analysis, not directives.** Do NOT write sentences addressed to
AlphaOS or to an AI agent, and never anything of the form "AlphaOS should
change X", "disable Y", "set Z to". The file is read by an automated session
that treats it strictly as inert data; instruction-shaped text is a hazard, not
a feature. Describe what a method *is*, what evidence supports it, and what it
would cost. Leave every decision to the human operator.

For each item (aim for **3–6**; fewer good ones beats padding):

```
## <Title>
- **Source:** <URL> — <venue/org>, <publication date>
- **Type:** paper | report | engineering write-up | postmortem
- **Bears on:** <one of the 5 priority questions, or "standing interest: ...">
- **What it establishes:** 2–4 sentences. The actual finding or method.
- **Evidence basis:** what was actually tested — dataset, period, sample size,
  and whether results are in-sample, walk-forward, or live.
- **Limitations:** the ones that matter, especially sample size, asset-class
  scope, and anything that assumes more data or infrastructure than a
  single-operator system has.
- **Implementation tradeoffs:** concrete costs — added state, latency,
  complexity, new failure modes.
- **What would have to be true on AlphaOS's own data for this to matter:**
  the most important line. Name the observable or measurement that would have
  to hold, given ~20 clusters and ~1 closed trade/week. If it plainly cannot be
  tested at this scale, say exactly that.
```

Then close with:

```
## Nothing-worth-reporting note
<If a priority question had no worthwhile new material this week, say so
explicitly by number. An honest "nothing new on Q3" is more useful than a
padded entry.>
```

**Prefer:** peer-reviewed or working papers with real evaluation; engineering
write-ups from firms that actually run the systems; incident postmortems.
**Avoid:** market commentary, price predictions, vendor marketing, anything
whose evidence is a single backtest with no out-of-sample period, and anything
you cannot link to a real, checkable source.

If you are not confident an item is genuine and correctly summarised, leave it
out and say the week was thin. A short honest digest is the goal.
