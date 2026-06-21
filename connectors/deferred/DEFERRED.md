# Deferred connectors

These connectors are **intentionally not part of the active AlphaOS v1 runtime**.
They are kept as labelled seams so they are cheap to wire in later. Nothing in
the active runtime imports or calls them; an import-graph test
(`tests/test_import_graph.py`) enforces that, and every entry point raises
`NotImplementedError("deferred in v1")` rather than hitting the network or
returning fabricated data.

> "Deferred" must not drift into "default" or "never." Each connector below has
> an explicit trigger that would justify activating it.

---

## `massive.py` — Massive market data

**Status:** deferred. Alpaca/IEX is the sole active market-data provider in v1.

**Activate when ANY of the following is true:**
- The freshness guard begins blocking real trades because IEX data is too
  sparse/stale for symbols in the active universe.
- The universe is broadened beyond liquid large-caps/ETFs.
- AlphaOS moves from early MVP learning into serious forward testing where
  full-market data is required.
- Cross-provider sanity checks become necessary (the freshness guard's
  `cross_provider_consistent` hook is reserved for this).

Until then, Massive remains deferred. Alternatives to weigh at that point:
Polygon, Alpaca SIP.

---

## `benzinga.py` — Benzinga news

**Status:** deferred. v1 runs in no-news mode (momentum continuation baseline).

**Activate when:**
- The no-news momentum loop is proven and measured, and the goal becomes testing
  whether news *improves* the established baseline.

Notes:
- `last30days` is the separately planned post-MVP research/catalyst/narrative
  source. Whether Benzinga is additive or redundant to `last30days` is an open
  decision to settle later — not now.
- Benzinga and `last30days` may surface materially different data; neither is
  active in v1.

---

## `web_news.py` — isolated web-news scraper

**Status:** deferred. Disabled in v1 alongside Benzinga.

**Activate when:** the news layer is reintroduced (same trigger as Benzinga), and
only as a polite, isolated fallback with full source logging — never bypassing
paywalls, auth, robots, or anti-bot protections.
