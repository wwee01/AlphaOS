"""PORT-1: sample-size discipline via correlated-observation clustering.

Ported from NightDesk DECISIONS.md #85/#81, adapted to AlphaOS's own data
model -- see docs/roadmap/ported/nightdesk-stats-contract.md Sec 5.

NightDesk's own clustering unit is one trading NIGHT (its decision-batch
unit): every candidate proposed the same night shares that night's regime
and conditions, so treating each as independent overstates the true sample
size. AlphaOS positions can span multiple holding days, so the adapted unit
is: dedup to one observation per (symbol, decision_date), then cluster
observations on the SAME symbol whose [decision_date, decision_date +
max_holding_days] windows overlap -- overlapping holding periods on the same
name share realized market moves during the overlap, the same
non-independence NightDesk's night-clustering defends against. This
adaptation was already correctly specified pre-port; this module is the
first concrete implementation of it.

ONE shared function -- every floor call site (reports AND, later, the PR13
promotion gate) must consume this SAME function, never a local
reimplementation (mirrors the one-replay-engine law applied to sample size
instead of replay).

Degrades gracefully when max_holding_days is absent from a row (not every
table this port draws from carries it -- e.g. attribution_records does not):
a missing/unparseable max_holding_days is treated as 0, i.e. a same-day-only
window for that observation. This still catches the dominant real-world
correlation case (multiple events firing off the same trading day's batch of
decisions); it just can't detect a multi-day holding-period overlap without
the real field. Never fabricated -- a caller that needs the finer adjustment
must supply real max_holding_days values.
"""

from __future__ import annotations

from datetime import date as _date
from datetime import timedelta
from typing import Any, Optional

# NightDesk's own "trustworthy" floor (>=20 independent clusters) -- below
# this, the answer is always "insufficient data," never a CI, however
# extreme the point estimate looks (contract doc Sec 5).
MIN_TRUSTWORTHY_CLUSTERS = 20


def _parse_date(v: Any) -> Optional[_date]:
    if v is None:
        return None
    try:
        return _date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _holding_days(v: Any) -> int:
    try:
        n = int(v)
        return n if n >= 0 else 0
    except (TypeError, ValueError):
        return 0


def effective_n(
    rows: list[dict],
    symbol_key: str = "symbol",
    date_key: str = "decision_date",
    holding_days_key: str = "max_holding_days",
) -> dict:
    """Cluster ``rows`` into independent units. Returns:
    ``{"effective_n", "n_raw", "n_deduped", "span_days", "trustworthy",
    "clusters"}`` -- ``clusters`` is a list of clusters, each a list of the
    original row dicts. Never raises; a row missing ``symbol_key`` or an
    unparseable ``date_key`` is silently excluded from every count
    (uncountable, not fabricated as either its own cluster or zero).
    """
    parsed: list[tuple[str, _date, int, dict]] = []
    for r in rows:
        symbol = r.get(symbol_key)
        d = _parse_date(r.get(date_key))
        if not symbol or d is None:
            continue
        parsed.append((str(symbol), d, _holding_days(r.get(holding_days_key)), r))
    n_raw = len(parsed)

    if n_raw == 0:
        return {
            "effective_n": 0, "n_raw": 0, "n_deduped": 0, "span_days": None,
            "trustworthy": False, "clusters": [],
        }

    # Dedup to one observation per (symbol, date) -- first-seen wins, same
    # role as NightDesk's own "highest-scoring matching setup" dedup, without
    # requiring a score field this port's callers don't uniformly have.
    deduped: dict[tuple[str, _date], tuple[str, _date, int, dict]] = {}
    for symbol, d, holding, r in parsed:
        key = (symbol, d)
        if key not in deduped:
            deduped[key] = (symbol, d, holding, r)
    obs = list(deduped.values())
    n_deduped = len(obs)

    all_dates = [d for _, d, _, _ in obs]
    span_days = float((max(all_dates) - min(all_dates)).days)

    by_symbol: dict[str, list[tuple[_date, _date, dict]]] = {}
    for symbol, d, holding, r in obs:
        by_symbol.setdefault(symbol, []).append((d, d + timedelta(days=holding), r))

    # Interval-overlap connected components, per symbol: sort by window
    # start, sweep forward, extend the current cluster while the next
    # window starts on/before the running max end. Standard sweep-line
    # merge -- correct for connected components of an interval graph, not
    # just pairwise-adjacent overlap.
    clusters: list[list[dict]] = []
    for symbol, intervals in by_symbol.items():
        intervals.sort(key=lambda t: t[0])
        current_cluster: list[dict] = []
        current_end: Optional[_date] = None
        for start, end, r in intervals:
            if current_end is not None and start <= current_end:
                current_cluster.append(r)
                current_end = max(current_end, end)
            else:
                if current_cluster:
                    clusters.append(current_cluster)
                current_cluster = [r]
                current_end = end
        if current_cluster:
            clusters.append(current_cluster)

    eff_n = len(clusters)
    return {
        "effective_n": eff_n,
        "n_raw": n_raw,
        "n_deduped": n_deduped,
        "span_days": span_days,
        "trustworthy": eff_n >= MIN_TRUSTWORTHY_CLUSTERS,
        "clusters": clusters,
    }
