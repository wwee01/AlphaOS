"""PR14: Red-Team Debate v0 -- shadow-only, bear-only adversarial voting.

Mirrors ``alphaos.tqs``'s own package law: a measurement-only signal,
computed strictly AFTER a scan batch's decisions are already committed, that
NO decision path may import or read from. See ``alphaos.tqs``'s module
docstring for the general shape of this guarantee and
``alphaos.debate.batch``'s own docstring for this package's specific call
site and cost-budget layering.

NO DECISION PATH MAY IMPORT OR READ FROM THIS PACKAGE. If you are adding a
call to anything in ``alphaos.debate`` from ``alphaos/risk/``,
``alphaos/approval.py``, or any orchestrator decide/approve/execute method,
stop: that is out of scope for this package's entire reason for existing.
"""

from __future__ import annotations

from alphaos.debate.batch import score_debate_batch, vote_on_proposal

__all__ = ["score_debate_batch", "vote_on_proposal"]
