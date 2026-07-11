"""PR-UI-B1: console theme — dark "cockpit instrument" visual system for the
existing dashboard tabs. STYLING ONLY: this module renders no data of its
own, fetches nothing, writes nothing, and adds no new action/button/code
path. Every value passed into the helpers below is computed by the existing
call sites in ``streamlit_app.py`` from real settings/journal data — this
module never invents, hardcodes, or copies a value or label from the Stitch
mockup screens.

Governing ruling: Fable5, 2026-07-11, "adopt the skin, quarantine the
script" — the visual tokens (colors, typography, spacing, elevation-via-
borders, component patterns) are adopted from
``docs/roadmap/ported/stitch-design-tokens.md`` (ported from the Stitch
mockup's own DESIGN.md). No text content, copy, labels, or values are ever
copied from the mockup screens themselves (those had content bugs — wrong
autonomy level, a kill-switch description implying auto-liquidation, a
"LIVE" badge on a paper-only system, fabricated limits, an inverted ΔR sign
convention — none of which exists in this module or anywhere this module is
called from).

Three things live here:

1. ``CONSOLE_CSS`` — one CSS block, injected once via
   ``st.markdown(CONSOLE_CSS, unsafe_allow_html=True)`` near the top of
   ``main()``. Google Fonts import for JetBrains Mono (data/numerals) and
   Inter (labels/UI) WITH system fallbacks first in each font stack, so a
   blocked/offline font fetch (this app is loopback-only and may not always
   have internet — see streamlit_app.py's module docstring) degrades to a
   perfectly legible system font rather than breaking anything.

2. Two small, PURE, read-only HTML-rendering helpers for the two components
   Streamlit cannot render natively as widgets: ``render_r_ladder`` (a
   horizontal R-ladder for the Positions tab) and ``render_ttl_bar`` (a TTL
   countdown bar for the Approval Center / Tonight tabs). "Pure" means:
   given plain values in, an HTML string out. No ``st.*`` calls, no
   Streamlit import, no I/O, nothing to mock in tests — callers do
   ``st.markdown(render_r_ladder(...), unsafe_allow_html=True)`` themselves,
   using whichever ``st`` reference (real or test-mocked) is already in
   scope at the call site. Every piece of interpolated string content is
   passed through ``html.escape()`` before being placed in the returned
   markup — this codebase had no prior ``unsafe_allow_html`` call to match
   an existing pattern against (grepped: none), so this module establishes
   the discipline: escape everything interpolated, unconditionally, even
   today's callers only ever pass numeric/enum-like values.

3. ``render_section_label`` — a tiny third convenience helper (not one of
   the two mandated components) that wraps a section-header string in the
   ``.label-caps`` CSS utility class defined in ``CONSOLE_CSS``. Used by
   Tonight/Candidate Flow to upgrade their existing bold/``####`` section
   headers to the dense, uppercase-tracked "Instrument Block" label style
   (docs/roadmap/ported/stitch-design-tokens.md's ``label-caps`` typography
   token) — same header text, same position, different CSS treatment only.

The TTL bar deliberately takes a pre-formatted ``label`` string from the
caller rather than re-deriving one, so its displayed text is always
byte-identical to whatever ``streamlit_app.py``'s own
``_format_seconds_remaining()`` already produces elsewhere on the same
page — one source of truth for that formatting, not two that could drift.
"""

from __future__ import annotations

import html
from typing import Optional

# ---------------------------------------------------------------------------
# Theme tokens -- from docs/roadmap/ported/stitch-design-tokens.md. Named
# constants (not inlined magic strings) so a future token change is a
# one-line edit here, not a CSS hunt.
# ---------------------------------------------------------------------------
_BORDER = "#27272a"
_TEXT_DIM = "#869397"
_PRIMARY = "#4cd7f6"  # cyan -- interactive / "in progress" only, never decorative
_AMBER = "#ffb873"  # attention -- reserved for states that need eyes on them
_RED = "#ef4444"  # critical -- kill switch / expired / incident only

_MONO_STACK = (
    "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace"
)
_SANS_STACK = (
    "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif"
)

CONSOLE_CSS = f"""
<style>
/* PR-UI-B1 console theme. Google Fonts import -- system fallbacks are listed
   FIRST in every font-family stack below, so if this fetch fails (offline /
   loopback-only host), text still renders in a legible system font; nothing
   depends on this import succeeding. */
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;600;700&display=swap');

/* ---- base numerals: st.metric values go monospaced/tabular ---- */
[data-testid="stMetricValue"] {{
    font-family: {_MONO_STACK};
    font-variant-numeric: tabular-nums;
}}
[data-testid="stMetricLabel"] {{
    font-family: {_SANS_STACK};
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-size: 11px;
    font-weight: 700;
}}

/* ---- label-caps utility: 11px/700/uppercase/0.08em, per the ported
   typography token of the same name. Used by render_section_label() and by
   the tick labels inside render_r_ladder()/render_ttl_bar() below. ---- */
.label-caps {{
    font-family: {_SANS_STACK};
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: {_TEXT_DIM};
}}
.alphaos-section-label {{
    margin: 0.35rem 0 0.15rem 0;
}}

/* ---- elevation via line-work, not shadows: bordered st.container() blocks
   get the 1px #27272a module border (DESIGN.md "Elevation & Depth" /
   "Borders: modules are separated by 1px borders instead of margins").
   Streamlit 1.58 emits the SAME data-testid ("stVerticalBlock") for bordered
   AND unbordered vertical blocks -- border=True is a runtime emotion/CSS-in-
   JS class with a content-hashed name, not a stable selector -- so a plain
   `[data-testid="stVerticalBlock"]` rule would put a border on every plain
   layout container in the app, not just the intentionally-bordered ones.
   Scoped instead via the same documented `st-key-<key>` mechanism used for
   the annunciator badges above: streamlit_app.py's Positions health cards
   pass key=f"poscard_{{position_id}}" to their existing
   st.container(border=True) call for exactly this hook (confirmed against
   the running app: the key class lands on the SAME element as
   data-testid="stVerticalBlock", not a wrapper, hence the compound selector
   with no descendant combinator). ---- */
div[data-testid="stVerticalBlock"][class*="st-key-poscard_"] {{
    border-color: {_BORDER} !important;
    border-radius: 4px;
}}

/* ---- tabs: Streamlit's own theme.primaryColor (config.toml) already
   colors baseweb's TabHighlight (the active-tab underline) cyan -- no CSS
   needed for that. This adds the matching label treatment so the active
   tab's TEXT also reads as active, not just its underline. ---- */
[data-testid="stTabs"] button[aria-selected="true"] p {{
    color: {_PRIMARY};
    font-weight: 700;
}}
[data-testid="stTabs"] button[aria-selected="false"] p {{
    color: {_TEXT_DIM};
}}

/* ---- annunciator badges (mode + kill-switch state), scoped via
   st.container(key=...)'s documented "st-key-<key>" CSS class -- deliberately
   NOT a global [data-testid="stAlert"] rule, so this never bleeds into
   System Health / Trade Packet / Scan Batches / Scheduler Runs / System
   Events, which this PR does not otherwise touch. Normal state renders as
   an outline; alert state renders as a solid fill -- both STATIC (no
   pulse/blink animation: the authoritative UX doc §13 bans flashing
   sitewide; see docs/roadmap/ported/stitch-design-tokens.md's "deviations"
   section for why DESIGN.md's literal "pulses" wording is not implemented). */
.st-key-annunciator_mode_badge [data-testid="stMetric"] {{
    border: 1px solid {_BORDER};
    border-radius: 4px;
    padding: 8px 12px;
}}
.st-key-annunciator_ks_badge [data-testid="stAlertContainer"] {{
    border-radius: 4px;
    border-width: 1px;
    border-style: solid;
}}
.st-key-annunciator_ks_badge [data-testid="stAlertContainer"]:has(
    [data-testid="stAlertContentSuccess"]
) {{
    background-color: transparent;
}}
.st-key-annunciator_ks_badge [data-testid="stAlertContainer"]:has(
    [data-testid="stAlertContentError"]
) {{
    background-color: rgba(239, 68, 68, 0.16);
    border-color: {_RED};
}}

/* ---- R-ladder (Positions tab) ---- */
.alphaos-r-ladder {{
    width: 100%;
    max-width: 640px;
    margin: 28px 0 34px 0;
}}
.alphaos-r-ladder-track {{
    position: relative;
    height: 4px;
    background: #3a3939;
    border: 1px solid {_BORDER};
    border-radius: 2px;
    margin: 0 6px;
}}
.alphaos-r-ladder-tick {{
    position: absolute;
    top: -9px;
    height: 22px;
    border-left: 2px solid;
    transform: translateX(-1px);
}}
.alphaos-r-ladder-tick-label {{
    position: absolute;
    top: -20px;
    left: 0;
    transform: translateX(-50%);
    white-space: nowrap;
}}
.alphaos-r-ladder-tick-value {{
    position: absolute;
    top: 16px;
    left: 0;
    transform: translateX(-50%);
    white-space: nowrap;
    font-family: {_MONO_STACK};
    font-size: 11px;
    color: {_TEXT_DIM};
}}
.alphaos-r-ladder-current {{
    position: absolute;
    top: -5px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: {_PRIMARY};
    transform: translateX(-6px);
    box-shadow: 0 0 0 2px #131313;
    z-index: 2;
}}
.alphaos-r-ladder-current-value {{
    position: absolute;
    top: -22px;
    left: 0;
    transform: translateX(-50%);
    white-space: nowrap;
    font-family: {_MONO_STACK};
    font-size: 12px;
    font-weight: 700;
    color: {_PRIMARY};
    z-index: 2;
}}
.alphaos-r-ladder--unavailable {{
    font-family: {_SANS_STACK};
    color: {_TEXT_DIM};
    font-size: 13px;
    margin: 6px 0;
}}

/* ---- TTL bar (Approval Center) ---- */
.alphaos-ttl-bar {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 4px 0 10px 0;
}}
.alphaos-ttl-bar-tag {{
    flex: 0 0 auto;
}}
.alphaos-ttl-bar-track {{
    position: relative;
    flex: 1 1 auto;
    height: 8px;
    background: #3a3939;
    border: 1px solid {_BORDER};
    border-radius: 2px;
    overflow: hidden;
    max-width: 260px;
}}
.alphaos-ttl-bar-fill {{
    position: absolute;
    top: 0;
    left: 0;
    bottom: 0;
}}
.alphaos-ttl-bar--ok .alphaos-ttl-bar-fill {{
    background: {_PRIMARY};
}}
.alphaos-ttl-bar--low .alphaos-ttl-bar-fill {{
    background: {_AMBER};
}}
.alphaos-ttl-bar--expired .alphaos-ttl-bar-fill {{
    background: {_RED};
}}
.alphaos-ttl-bar-label {{
    font-family: {_MONO_STACK};
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}}
.alphaos-ttl-bar-unknown {{
    font-family: {_SANS_STACK};
    font-size: 12px;
    color: {_TEXT_DIM};
}}
</style>
"""


def render_section_label(text: str) -> str:
    """Wrap an existing section-header string in the ``.label-caps`` utility
    class. Pure: same text, same meaning, different CSS treatment only --
    the caller passes the exact string it already renders today (e.g.
    ``"② Needs you"``), this does not add, remove, or alter any word of it."""
    return f'<div class="label-caps alphaos-section-label">{html.escape(text)}</div>'


def render_r_ladder(
    *,
    stop_r: Optional[float],
    entry_r: float,
    current_r: Optional[float],
    target_r: Optional[float],
) -> str:
    """Horizontal bordered bar + tick marks: stop -> entry -> current ->
    target, all in R-multiples (UI/UX doc §8: "everything positions-related
    speaks in R, not $"). Pure -- returns an HTML string, makes no st.*
    calls. Defensive about partial data even though today's only caller
    (tab_positions_health) already gates on all three of stop_r/current_r/
    target_r being non-None before calling this: a missing value here still
    renders an honest "unavailable" state rather than crashing or drawing a
    ladder with a fabricated position (unknown-never-zero, matching this
    codebase's existing posture everywhere else numbers can be missing)."""
    if current_r is None or stop_r is None or target_r is None:
        return (
            '<div class="alphaos-r-ladder alphaos-r-ladder--unavailable">'
            "R-ladder unavailable (missing stop, target, or current price)"
            "</div>"
        )

    marks = {"stop": stop_r, "entry": entry_r, "target": target_r}
    lo = min(*marks.values(), current_r)
    hi = max(*marks.values(), current_r)
    span = hi - lo

    def _pct(v: float) -> float:
        # A degenerate/zero span (every mark coincides -- e.g. a garbage
        # risk basis) still renders, at the track's midpoint, rather than
        # dividing by zero.
        return 50.0 if span <= 1e-9 else round((v - lo) / span * 100.0, 2)

    def _fmt(v: float) -> str:
        return html.escape(f"{v:+.2f}R")

    tick_colors = {"stop": _RED, "entry": _TEXT_DIM, "target": _PRIMARY}
    tick_html = "".join(
        f'<div class="alphaos-r-ladder-tick" style="left:{_pct(v):.2f}%;'
        f'border-left-color:{tick_colors[name]};" '
        f'title="{html.escape(name)} {_fmt(v)}">'
        f'<span class="alphaos-r-ladder-tick-label label-caps">{html.escape(name)}</span>'
        f'<span class="alphaos-r-ladder-tick-value">{_fmt(v)}</span>'
        "</div>"
        for name, v in marks.items()
    )
    current_pct = _pct(current_r)
    current_html = (
        f'<div class="alphaos-r-ladder-current" style="left:{current_pct:.2f}%;" '
        f'title="{html.escape("now")} {_fmt(current_r)}">'
        f'<span class="alphaos-r-ladder-current-value">{_fmt(current_r)}</span>'
        "</div>"
    )
    return (
        '<div class="alphaos-r-ladder">'
        f'<div class="alphaos-r-ladder-track">{current_html}{tick_html}</div>'
        "</div>"
    )


def render_ttl_bar(
    *,
    seconds_remaining: Optional[float],
    total_ttl_seconds: Optional[float],
    label: str,
) -> str:
    """Horizontal TTL countdown bar. Pure -- HTML/CSS only, no client-side JS
    timer: the fill percentage is computed once, server-side, at render
    time, matching the dashboard's existing manual-refresh-plus-heartbeat
    cadence (UI/UX doc §13, "no autorefresh anxiety" -- a JS countdown timer
    would visually tick down between manual refreshes even though nothing
    has actually been re-checked, which is exactly the false-precision this
    rule bans).

    ``label`` is a pre-formatted string from the caller's own
    ``_format_seconds_remaining()`` (streamlit_app.py) so the text here is
    always byte-identical to what the rest of the page already shows for
    the same value -- this function does not re-derive or duplicate that
    formatting.

    None seconds_remaining/total_ttl_seconds (unknown/unparseable expiry)
    renders an explicit "unknown" state rather than a fabricated 0% or 100%
    bar -- unknown-never-zero, the same posture ``_format_seconds_remaining``
    itself already takes."""
    safe_label = html.escape(label)
    if seconds_remaining is None or total_ttl_seconds is None or total_ttl_seconds <= 0:
        return (
            '<div class="alphaos-ttl-bar">'
            '<span class="label-caps alphaos-ttl-bar-tag">TTL</span>'
            f'<span class="alphaos-ttl-bar-unknown">{safe_label}</span>'
            "</div>"
        )

    is_expired = seconds_remaining <= 0
    # A "remaining time" bar naturally empties to 0% as the deadline nears --
    # but an EXPIRED proposal should read as a clear, solid alert rather than
    # an empty (near-invisible) track, so this is the one case where the bar
    # is drawn FULL: it's still "the same fill logic," just visually
    # committing to "TTL exceeded" as a filled alert state rather than an
    # absence of fill (DESIGN.md's Annunciator Badges: alert states render
    # as a solid fill, not an outline/empty one).
    pct = 100.0 if is_expired else max(0.0, min(100.0, (seconds_remaining / total_ttl_seconds) * 100.0))
    is_low = (not is_expired) and pct < 20.0
    state = "expired" if is_expired else ("low" if is_low else "ok")
    return (
        f'<div class="alphaos-ttl-bar alphaos-ttl-bar--{state}">'
        '<span class="label-caps alphaos-ttl-bar-tag">TTL</span>'
        '<div class="alphaos-ttl-bar-track">'
        f'<div class="alphaos-ttl-bar-fill" style="width:{pct:.1f}%;"></div>'
        "</div>"
        f'<span class="alphaos-ttl-bar-label">{safe_label}</span>'
        "</div>"
    )
