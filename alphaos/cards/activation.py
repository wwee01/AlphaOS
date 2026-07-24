"""S1c: the ONE per-scan-batch gate that decides whether ``select_card()``
may run at all this scan, and if so with what frozen context.

Layered ABOVE ``alphaos.cards.selector`` (S1a, pure) and
``alphaos.cards.per_evidence``'s ``s1c_activation_preflight()`` (S1b-
integrity follow-up, checks the CORRECTED H-PER-1P-v2/H-PER-1N-v2
preregistration pair is intact) -- this is the first module in the whole
SETUP-1 lineage that is actually imported by production code
(``alphaos/orchestrator.py``, ``alphaos/scanner/candidate_scanner.py``).
Nothing below it (``cards.selector``, ``cards.per_evidence``) changes
semantics because of this module; it only consumes their existing,
already-audited contracts.

WHAT THIS GATES: whether a NEW candidate MAY be tagged
``post_earnings_reaction`` this scan. It does NOT gate eligibility,
evaluation, TQS, direction, proposal creation, entry/stop/target,
sizing, risk/approval, broker routing, or execution -- none of those
read this module or anything downstream of it.

FAIL-CLOSED: any preflight failure (corrected pair missing/evaluated/
mismatched identity/mismatched analysis_not_before/live identity
unavailable) makes ``select_card()`` not run AT ALL for the whole scan
batch -- every candidate that batch produces falls back to the existing
default card, tagged with a diagnosable, non-``'ok'``
``card_assignment_status`` (``PREFLIGHT_FAILED_STATUS``) that is
DISTINCT from every value ``alphaos.cards.selector.CacheHealth`` can
produce, so a downstream query grouping by ``card_assignment_status``
can always tell a preregistration-readiness failure apart from a cache-
health failure. This can never crash a scan: ``build_scan_card_activation()``
itself wraps its own body in a broad ``except`` (audit finding -- prior to
this, a hand-corrupted ``params_json`` on a preregistration row would have
raised ``json.JSONDecodeError`` out of ``s1c_activation_preflight()``,
which makes no actual never-raises promise despite this module's own
earlier docstring claiming otherwise), degrading to ``active=False`` with
reason ``ACTIVATION_ERROR_STATUS`` exactly like every other refusal path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from alphaos.cards.per_evidence import s1c_activation_preflight
from alphaos.cards.selector import SelectorContext, build_selector_context

# Deliberately distinct from every alphaos.cards.selector.CacheHealth value
# (ok/refresh_failed_recent/stale/cache_empty/unknown) -- this is a
# PREREGISTRATION-readiness failure, one layer above cache health, and the
# two must never be conflated by a downstream report or query.
PREFLIGHT_FAILED_STATUS = "preregistration_unready"

# A genuinely unexpected failure while computing the activation decision
# itself (e.g. corrupted params_json on a preregistration row) -- distinct
# from PREFLIGHT_FAILED_STATUS (a normal, anticipated "not ready yet"
# outcome) so a downstream report can tell "the pair isn't ready" apart
# from "something is actually broken and needs operator attention."
ACTIVATION_ERROR_STATUS = "activation_error"


@dataclass(frozen=True)
class ScanCardActivation:
    """One instance per scan batch, built ONCE and reused for every
    candidate (core AND shadow) that batch produces -- see
    ``build_scan_card_activation()``'s own docstring for why."""

    active: bool
    context: Optional[SelectorContext]
    reason: Optional[str]  # non-None only when active is False


def build_scan_card_activation(
    journal, assignment_as_of_utc: str, universe_symbols: Iterable[str],
) -> ScanCardActivation:
    """Called ONCE per scan batch -- before either ``CandidateScanner.scan()``
    or ``.scan_shadow_tier()`` runs -- so every candidate in the batch, core
    or shadow, sees the IDENTICAL activation verdict and (if active) the
    identical frozen ``SelectorContext``. A preflight failure never raises
    here; it degrades to ``active=False`` carrying the preflight's own
    reason string, which the caller (``Orchestrator.run_scan_once``) logs
    as ONE system event per scan, never per-candidate.

    Audit finding: wraps the WHOLE body in a broad except -- neither
    ``s1c_activation_preflight()`` nor ``build_selector_context()`` actually
    promises never to raise (a hand-corrupted ``params_json`` on a
    preregistration row, for example, would previously have propagated a
    bare ``json.JSONDecodeError`` out of here and crashed the entire scan).
    This function's own contract is the one that matters to its caller, so
    it is the one place that must actually hold that line."""
    try:
        preflight = s1c_activation_preflight(journal)
        if not preflight["ready"]:
            return ScanCardActivation(active=False, context=None, reason=preflight["reason"])
        context = build_selector_context(journal, assignment_as_of_utc, universe_symbols)
        return ScanCardActivation(active=True, context=context, reason=None)
    except Exception:  # noqa: BLE001 -- an activation decision must never crash a scan
        return ScanCardActivation(active=False, context=None, reason=ACTIVATION_ERROR_STATUS)
