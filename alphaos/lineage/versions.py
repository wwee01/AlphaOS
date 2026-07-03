"""Static lineage-only version labels (PR4).

These are NOT real subsystem versioning -- alphaos/scanner, alphaos/strategy,
and the "feature engine" concept have no actual version history today (one
implementation, no v2 in flight). They exist purely so lineage_snapshots rows
carry a stable, auditable label for "what scanner/strategy code shape
produced this decision", ready to be hand-bumped the day real versioning is
introduced -- matching the existing PROMPT_TEMPLATE_VERSION / LABEL_VERSION_V1
/ POLARITY_PROMPT_VERSION convention elsewhere in this codebase. They must
never be read by, or influence, any scan/risk/strategy decision path
(measurement-only, per PR4's non-goals: no scanner v2, no strategy changes).
"""

from __future__ import annotations

SCANNER_VERSION = "v1"
SCANNER_RULE_VERSION = "v1"
PLAYBOOK_VERSION = "v1"
STRATEGY_VERSION = "v1"
FEATURE_ENGINE_VERSION = "v1"
