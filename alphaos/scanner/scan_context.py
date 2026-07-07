"""ScanContext: the typed replacement for the ``cand["_*"]`` side-channel.

Structural fix for the exit review's T5 finding: the scanner/orchestrator
used to stash transient, non-persisted working objects directly on the
candidate dict using an underscore-prefix convention (``_snapshot``,
``_interest``, ``_catalyst``, ``_last30``, ``_polarity``, ``_earnings``,
``_packet_id``, ``_arming_classification``, ``_narrative_warning``). That
convention caused a real bug: PR9.1 found ``build_no_news_user_prompt`` did
``json.dumps(candidate)`` and leaked catalyst/narrative text into the "no
news" eval prompt while telling the model no news existed. The fix at the
time (``prompt_templates._public()``, a prefix-based filter applied at the
3 prompt-building call sites) stopped the active leak but left the
underlying seam in place — "every next PR presses on this exact seam"
(exit review). This module removes the seam itself.

``row`` is the real, persisted ``candidates``-table-shaped dict: exactly the
fields the scanner writes via ``journal.insert("candidates", ...)``, plus
harmless in-process convenience scalars (e.g. ``last_price``) that were
never DB columns to begin with and never leaked anything (not underscore-
prefixed, never contain narrative/free text). ``row`` is always safe to
``json.dumps``, hand to a report builder, or pass to ``JournalStore.insert``
as-is, because nothing else may ever be stashed inside it — see
``__setitem__``. Every transient enrichment object gets its own typed
attribute instead.

Dict-like delegation (``ctx["symbol"]``, ``ctx.get("direction")``,
``"card_id" in ctx``, ``ctx["status"] = CandidateStatus.WATCH.value``) means
every EXISTING call site that only touches real columns keeps working
completely unchanged — only the 9 former private-key call sites needed to
become typed attribute access (``ctx.snapshot`` instead of
``cand["_snapshot"]``, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from alphaos.ai.last30days_polarity import PolarityResult
    from alphaos.earnings.earnings_enricher import EarningsProximityContext
    from alphaos.news.catalyst_enricher import CatalystContext
    from alphaos.research.last30days_enricher import Last30DaysContext
    from alphaos.scanner.interest_scanner import InterestSignals


@dataclass
class ScanContext:
    row: dict[str, Any]
    snapshot: Optional[dict] = None
    interest: "Optional[InterestSignals]" = None
    catalyst: "Optional[CatalystContext]" = None
    last30: "Optional[Last30DaysContext]" = None
    polarity: "Optional[PolarityResult]" = None
    earnings: "Optional[EarningsProximityContext]" = None
    packet_id: Optional[str] = None
    arming_classification: Optional[str] = None
    narrative_warning: Optional[str] = None

    # ------------------------------------------------------ dict delegation
    def __getitem__(self, key: str) -> Any:
        return self.row[key]

    def __setitem__(self, key: str, value: Any) -> None:
        if isinstance(key, str) and key.startswith("_"):
            raise ValueError(
                f"ScanContext.row must never hold a private key ({key!r}) -- "
                "add a typed field to ScanContext instead of stashing it in row. "
                "This guard exists BECAUSE a private dict key once leaked into "
                "an LLM prompt (see this module's docstring)."
            )
        self.row[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self.row

    def get(self, key: str, default: Any = None) -> Any:
        return self.row.get(key, default)

    def items(self):
        return self.row.items()
