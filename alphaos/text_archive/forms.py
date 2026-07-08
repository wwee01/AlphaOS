"""TEXT-0: the EDGAR form catalog (v1), versioned as ``edgar_forms_v1``.

Grouped by why each family matters (per the spec) -- collection only, no
scoring/weighting here; that's for a future text-strategy PR to define, once
this archive has actual history to backtest against.

Adding/removing forms later is a NEW ``edgar_forms_v2`` version (a new
constant, never an in-place edit of this one) -- coverage windows per form
version are derivable from ``fetch_run`` history, so a catalog gap is always
attributable to "we weren't collecting this form yet," never silently
indistinguishable from "this company never filed it."

Some families are genuinely prefix-shaped in SEC's own form-type strings
(e.g. every 424B variant: 424B1, 424B2, 424B3, 424B4, 424B5; every SC TO-*
tender-offer sub-form; multiple Form 15/25 deregistration sub-forms) -- those
are matched by prefix, everything else by exact string. Best-effort against
SEC's real published form-type strings, not a from-first-principles parse of
the form-type grammar.
"""

from __future__ import annotations

EDGAR_FORMS_V1 = "edgar_forms_v1"

# Exact-match form-type strings (as SEC's submissions API actually emits
# them in its "form" array).
_EXACT_FORMS_V1 = frozenset({
    # Core reporting
    "8-K", "10-K", "10-Q", "6-K", "20-F",
    # Amendments/restatements
    "8-K/A", "10-K/A", "10-Q/A",
    # Late-filing notices (potent negative signal in small caps)
    "NT 10-K", "NT 10-Q",
    # Insider activity
    "3", "4", "5", "3/A", "4/A", "5/A",
    # Ownership/activism
    "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A",
    # Dilution/capital raises
    "S-1", "S-1/A", "S-3", "S-3/A", "S-8", "D", "D/A",
    # Governance/comp
    "DEF 14A", "DEFA14A",
    # Corporate events (tenders/mergers)
    "SC 14D9", "SC 14D9/A", "DEFM14A",
    # Institutional holdings (quarterly, lagged -- archived for completeness;
    # any strategy use must respect the lag via seen_at, per the spec's own
    # seen-at law)
    "13F-HR", "13F-HR/A",
})

# Prefix families -- SEC emits multiple concrete sub-forms per family.
_PREFIX_FORMS_V1 = (
    "424B",   # prospectus supplements: 424B1..424B5
    "SC TO-",  # tender offers: SC TO-T, SC TO-I, SC TO-T/A, ...
    "15-",    # Form 15 deregistration sub-forms: 15-12B, 15-12G, 15-15D, ...
    "25-",    # Form 25 delisting sub-forms: 25-NSE, ...
)


def is_catalog_form(form: str) -> bool:
    """True if ``form`` (SEC's own form-type string, e.g. "8-K", "424B3")
    is in the v1 catalog. Unknown/uncataloged forms return False -- callers
    must count these in a ``skipped_forms`` tally (visible, never silent) per
    the spec's own test requirement, not just drop them unaccounted-for."""
    if not form:
        return False
    if form in _EXACT_FORMS_V1:
        return True
    return any(form.startswith(prefix) for prefix in _PREFIX_FORMS_V1)
