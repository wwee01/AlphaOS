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
   ``main()``. System-only font stacks (no Google Fonts/any external CDN
   import) for data/numerals and labels/UI — this app is loopback-only and
   may not always have internet (see streamlit_app.py's module docstring),
   and a webfont fetch would also be the dashboard's own first-ever
   browser-side call to an external host (the backend already talks to
   Alpaca/SEC/etc., but the operator's BROWSER never has, until now).
   Audit-fixup 2026-07-11: an earlier version imported JetBrains Mono/Inter
   from fonts.googleapis.com with these same system stacks as a fallback;
   dropped the import rather than keep an egress that only ever existed to
   be degraded away, on a console whose whole point is a calm, minimal-
   dependency operating surface. System stacks alone (ui-monospace/SF Mono/
   Menlo for data, -apple-system/Segoe UI/system-ui for labels) already
   render cleanly on every real platform this app runs on.

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

_MONO_STACK = "ui-monospace, 'SF Mono', Menlo, Consolas, monospace"
_SANS_STACK = "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif"

CONSOLE_CSS = f"""
<style>
/* PR-UI-B1 console theme. System-only font stacks -- no external font CDN
   import (audit-fixup 2026-07-11: an earlier version imported JetBrains
   Mono/Inter from Google Fonts; dropped so this loopback-only, may-not-
   have-internet app's browser side makes zero external calls, same as it
   always has). */

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

/* ---- PR-UI-M1: mobile responsive pass (UI/UX doc §16). ONE media query,
   scoped to <=480px -- every rule below is inert above that width, so the
   desktop layout this whole file already renders is provably unchanged
   (verified live at 1280px: no rule in this block matches). Same
   selectors as the desktop rules above; narrower/denser VALUES only,
   never new elements, never JS, never a viewport-meta hack. Streamlit
   1.58's own st.columns() already stacks to a single column below its
   internal breakpoint with no CSS help (verified live at 390px against
   the annunciator's col_mode/col_ks columns before writing this block) --
   so nothing here forces stacking, only condenses what stacking already
   produced. ---- */
@media (max-width: 480px) {{
    /* Annunciator (§16 principle 4, "condenses, never disappears"): the
       mode badge and kill-switch badge already stack full-width (see
       above); shrink their padding/type so mode + kill state read as a
       tight strip rather than two tall desktop-sized cards eating the
       first screen. Same st.metric/st.alert content, denser box only. */
    .st-key-annunciator_mode_badge [data-testid="stMetric"] {{
        padding: 4px 10px;
    }}
    .st-key-annunciator_mode_badge [data-testid="stMetricValue"] {{
        font-size: 1.15rem;
    }}
    .st-key-annunciator_mode_badge [data-testid="stMetricLabel"] {{
        font-size: 10px;
    }}
    .st-key-annunciator_ks_badge [data-testid="stAlertContainer"] {{
        padding: 6px 10px;
    }}
    .st-key-annunciator_ks_badge [data-testid="stAlertContainer"] p {{
        font-size: 13px;
        margin: 0;
    }}

    /* Touch targets >= 44px (§16 principle 6): controls grow, data density
       is untouched (stDataFrame/st.dataframe rows, R-ladder tick text,
       etc. are not targeted by this rule). stBaseButton covers every
       st.button variant (primary/secondary/tertiary all share the
       "stBaseButton-*" data-testid prefix in 1.58); stTab covers the tab
       strip labels named in §16's touch-target line. */
    [data-testid^="stBaseButton"] {{
        min-height: 44px;
    }}
    [data-testid="stTab"] {{
        min-height: 44px;
        display: flex;
        align-items: center;
    }}

    /* R-ladder + TTL bar at narrow width (§16 principle 5: "R-ladders stay
       horizontal ... TTL bars keep their label text"). The r-ladder's
       640px desktop cap never actually binds below 640px of available
       width, so it already renders full-bleed on a 390px screen (verified
       live) -- this rule just makes that explicit/future-proof rather
       than relying on the accident of "480 < 640". The TTL bar's 260px
       track cap DOES bind on mobile (verified live: a long gap of empty
       space sat between the fill and its own label at 390px) -- relaxed
       here so the bar uses the full available row width instead. */
    .alphaos-r-ladder {{
        max-width: 100%;
    }}
    .alphaos-ttl-bar-track {{
        max-width: none;
    }}
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
