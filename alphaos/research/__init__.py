"""Roadmap 2.5: last30days research / narrative-context enrichment.

A SEPARATE social/research layer from official news (Roadmap 2.4's ``news``
package). It calls a globally-installed ``last30days`` skill (no vendored code)
to gather recent community narrative, and folds it into the candidate packet as
CONTEXT ONLY — never execution authority. Disabled by default.
"""
