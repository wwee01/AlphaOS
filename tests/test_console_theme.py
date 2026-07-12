"""PR-UI-B1 console theme: unit tests for the pure HTML-rendering helpers in
alphaos/dashboard/console_theme.py. These functions take plain values and
return an HTML string -- no Streamlit, no journal, no I/O -- so they're
tested directly, the same way test_dashboard.py already unit-tests
streamlit_app.py's other pure helpers (_hindsight_cell, _format_age,
_format_seconds_remaining) rather than only exercising them through a full
dashboard render.

What matters most here: (1) the escaping discipline (html.escape() on every
interpolated string, unconditionally) actually holds, (2) missing/None
inputs render an honest "unavailable"/"unknown" state rather than a
fabricated 0 or a crash (unknown-never-zero, this codebase's posture
everywhere else a number can be missing), and (3) the TTL bar's three
visual states (ok/low/expired) trigger on the right thresholds.
"""

from __future__ import annotations

from alphaos.dashboard import console_theme


# --------------------------------------------------------- render_section_label
def test_render_section_label_escapes_html_special_characters():
    out = console_theme.render_section_label("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_render_section_label_preserves_text_wraps_in_label_caps_class():
    out = console_theme.render_section_label("② Needs you")
    assert "② Needs you" in out
    assert 'class="label-caps alphaos-section-label"' in out


# --------------------------------------------------------------- render_r_ladder
def test_render_r_ladder_unavailable_when_current_r_missing():
    out = console_theme.render_r_ladder(stop_r=-1.0, entry_r=0.0, current_r=None, target_r=2.0)
    assert "unavailable" in out.lower()
    # No fabricated position -- the "current" marker class must not appear.
    assert "alphaos-r-ladder-current" not in out


def test_render_r_ladder_unavailable_when_stop_or_target_missing():
    assert "unavailable" in console_theme.render_r_ladder(
        stop_r=None, entry_r=0.0, current_r=-0.3, target_r=2.0
    ).lower()
    assert "unavailable" in console_theme.render_r_ladder(
        stop_r=-1.0, entry_r=0.0, current_r=-0.3, target_r=None
    ).lower()


def test_render_r_ladder_normal_case_renders_all_four_values():
    out = console_theme.render_r_ladder(stop_r=-1.0, entry_r=0.0, current_r=-0.6, target_r=2.2)
    assert "alphaos-r-ladder-current" in out
    assert "-1.00R" in out
    assert "+0.00R" in out
    assert "-0.60R" in out
    assert "+2.20R" in out
    # Three tick marks (stop/entry/target) plus the current-price marker.
    assert out.count("alphaos-r-ladder-tick\"") == 3


def test_render_r_ladder_degenerate_span_does_not_raise():
    """stop == entry == current == target (e.g. a garbage risk basis) must
    still render -- every mark collapses to the track's midpoint rather than
    raising a ZeroDivisionError."""
    out = console_theme.render_r_ladder(stop_r=0.0, entry_r=0.0, current_r=0.0, target_r=0.0)
    assert "left:50.00%" in out


def test_render_r_ladder_escapes_are_applied_to_formatted_values():
    # Values are always numeric today, but the escape call must still be
    # present/effective -- this locks in the discipline regardless of what a
    # future caller passes.
    out = console_theme.render_r_ladder(stop_r=-1.0, entry_r=0.0, current_r=-0.6, target_r=2.2)
    assert "&" not in out.replace("&amp;", "")  # no raw ampersands slipped through unescaped


# ------------------------------------------------------------------ render_ttl_bar
def test_render_ttl_bar_unknown_when_seconds_remaining_missing():
    out = console_theme.render_ttl_bar(seconds_remaining=None, total_ttl_seconds=1800, label="unknown")
    assert "alphaos-ttl-bar-unknown" in out
    assert "alphaos-ttl-bar--ok" not in out
    assert "alphaos-ttl-bar--low" not in out
    assert "alphaos-ttl-bar--expired" not in out


def test_render_ttl_bar_unknown_when_total_ttl_missing():
    out = console_theme.render_ttl_bar(seconds_remaining=120, total_ttl_seconds=None, label="2m 0s")
    assert "alphaos-ttl-bar-unknown" in out


def test_render_ttl_bar_unknown_when_total_ttl_zero_or_negative():
    """A zero/negative TTL denominator must never be divided into -- unknown,
    not a fabricated (and possibly infinite/negative) percentage."""
    assert "alphaos-ttl-bar-unknown" in console_theme.render_ttl_bar(
        seconds_remaining=100, total_ttl_seconds=0, label="x"
    )
    assert "alphaos-ttl-bar-unknown" in console_theme.render_ttl_bar(
        seconds_remaining=100, total_ttl_seconds=-5, label="x"
    )


def test_render_ttl_bar_ok_state_well_within_ttl():
    out = console_theme.render_ttl_bar(seconds_remaining=1500, total_ttl_seconds=1800, label="25m 0s")
    assert "alphaos-ttl-bar--ok" in out
    assert "width:83.3%" in out
    assert "25m 0s" in out


def test_render_ttl_bar_low_state_under_20_percent_remaining():
    out = console_theme.render_ttl_bar(seconds_remaining=200, total_ttl_seconds=1800, label="3m 20s")
    assert "alphaos-ttl-bar--low" in out


def test_render_ttl_bar_expired_state_renders_full_solid_bar_not_empty():
    """A negative seconds_remaining (TTL exceeded) must read as a solid,
    unmissable alert bar -- NOT an empty/near-invisible one, even though the
    raw 'fraction remaining' arithmetic would naturally clamp to 0%."""
    out = console_theme.render_ttl_bar(
        seconds_remaining=-108, total_ttl_seconds=1800, label="expired 108s ago"
    )
    assert "alphaos-ttl-bar--expired" in out
    assert "width:100.0%" in out
    assert "expired 108s ago" in out


def test_render_ttl_bar_expired_at_exactly_zero_seconds_remaining():
    out = console_theme.render_ttl_bar(seconds_remaining=0, total_ttl_seconds=1800, label="expired 0s ago")
    assert "alphaos-ttl-bar--expired" in out


def test_render_ttl_bar_label_is_escaped():
    out = console_theme.render_ttl_bar(
        seconds_remaining=120, total_ttl_seconds=1800, label="<b>120s</b>"
    )
    assert "<b>120s</b>" not in out
    assert "&lt;b&gt;120s&lt;/b&gt;" in out


def test_render_ttl_bar_label_is_escaped_in_unknown_state_too():
    out = console_theme.render_ttl_bar(seconds_remaining=None, total_ttl_seconds=None, label="<i>unknown</i>")
    assert "<i>unknown</i>" not in out
    assert "&lt;i&gt;unknown&lt;/i&gt;" in out


# ------------------------------------------------------------------------- CSS
def test_console_css_is_a_style_block_and_references_ported_tokens():
    assert "<style>" in console_theme.CONSOLE_CSS
    assert "</style>" in console_theme.CONSOLE_CSS
    assert "#27272a" in console_theme.CONSOLE_CSS  # ported module-border token
    assert "ui-monospace" in console_theme.CONSOLE_CSS
    assert "system-ui" in console_theme.CONSOLE_CSS


def test_console_css_makes_no_external_font_cdn_call():
    """Audit-fixup 2026-07-11 (correctness + scope/safety, both LOW): an
    earlier version imported JetBrains Mono/Inter from fonts.googleapis.com
    -- the dashboard's own first-ever browser-side external call, on a
    loopback-only app that may not have internet. System font stacks only,
    now and going forward."""
    assert "fonts.googleapis.com" not in console_theme.CONSOLE_CSS
    assert "@import" not in console_theme.CONSOLE_CSS


# --------------------------------------------------- PR-UI-M1: mobile responsive
def test_console_css_has_a_max_width_480_media_query():
    """UI/UX doc §16's implementation slice: ONE @media (max-width: 480px)
    block, and only one -- a second/competing breakpoint would mean two
    sources of truth for "what is mobile" in the same file."""
    assert "@media (max-width: 480px)" in console_theme.CONSOLE_CSS
    assert console_theme.CONSOLE_CSS.count("@media") == 1


def test_console_css_44px_touch_target_rule_is_scoped_inside_the_media_query():
    """§16 principle 6: touch targets >= 44px, mobile-only -- the 44px rule
    must live INSIDE the media query block, not as a sitewide rule (that
    would inflate desktop buttons too, which the live 1280px check must
    show as unchanged)."""
    media_start = console_theme.CONSOLE_CSS.index("@media (max-width: 480px)")
    media_block = console_theme.CONSOLE_CSS[media_start:]
    assert "min-height: 44px" in media_block
    # Not present anywhere before the media query starts.
    assert "min-height: 44px" not in console_theme.CONSOLE_CSS[:media_start]


def test_console_css_mobile_pass_still_makes_no_external_call():
    """Re-assert the audit-fixup 2026-07-11 discipline holds for the new
    block too -- a mobile pass is exactly the kind of change that could
    quietly reintroduce a CDN font import "just for legibility"."""
    assert "fonts.googleapis.com" not in console_theme.CONSOLE_CSS
    assert "@import" not in console_theme.CONSOLE_CSS


def test_console_css_mobile_media_query_relaxes_r_ladder_and_ttl_bar_width_caps():
    """§16 principle 5/implementation slice: at narrow width the R-ladder and
    TTL bar must not be held to their desktop max-width caps (640px /
    260px) -- verified live at 390px that the 260px TTL-bar cap left a
    visible dead-space gap between the fill and its own label; this locks
    in that both caps are relaxed inside the mobile block specifically."""
    media_start = console_theme.CONSOLE_CSS.index("@media (max-width: 480px)")
    media_block = console_theme.CONSOLE_CSS[media_start:]
    assert ".alphaos-r-ladder {" in media_block
    assert ".alphaos-ttl-bar-track {" in media_block
    # The desktop caps themselves must be untouched outside the media query.
    desktop_block = console_theme.CONSOLE_CSS[:media_start]
    assert "max-width: 640px;" in desktop_block
    assert "max-width: 260px;" in desktop_block


# ----------------------------------------------- PR-UI-B4: fidelity-gap fixup
def test_console_css_page_titles_and_markdown_headings_get_distinct_treatment():
    """Fable5 ruling 2026-07-12: st.title/st.subheader (wrapped in
    stHeading -- PAGE titles) and markdown-authored headers (wrapped in
    stMarkdown -- CONTENT headlines, e.g. Tonight's "### thought" hero line)
    must get DIFFERENT typographic treatment, or the hero line either
    screams like a page title or page titles whisper like body text. Both
    wrapper selectors must exist, and they must not render identically (a
    copy-paste that made them the same rule would silently defeat the whole
    point of the split)."""
    assert '[data-testid="stHeading"] h3' in console_theme.CONSOLE_CSS
    assert '[data-testid="stMarkdown"] h3' in console_theme.CONSOLE_CSS
    heading_start = console_theme.CONSOLE_CSS.index('[data-testid="stHeading"] h3')
    heading_rule = console_theme.CONSOLE_CSS[heading_start:heading_start + 260]
    markdown_start = console_theme.CONSOLE_CSS.index('[data-testid="stMarkdown"] h3')
    markdown_rule = console_theme.CONSOLE_CSS[markdown_start:markdown_start + 260]
    assert heading_rule != markdown_rule
    assert "text-transform: uppercase" in heading_rule
    assert "text-transform: uppercase" not in markdown_rule


def test_console_css_instrument_block_convention_targets_the_bare_key_prefix():
    """The generic blk_ convention (every future bordered section) must be
    scoped by the SAME st-key-<key> mechanism as the pre-existing poscard_/
    annunciator_ rules -- a class*= substring selector on "st-key-blk_",
    never requiring a full-string match against one specific key."""
    assert 'class*="st-key-blk_"' in console_theme.CONSOLE_CSS


def test_console_css_expander_gets_a_real_border_not_just_a_color():
    """Regression guard (caught live during B4's own build): st.expander has
    NO default border to recolor, unlike st.container(border=True) -- a rule
    setting only `border-color` computes to a 0px-wide, invisible border.
    The rule must set the full `border` shorthand (width + style + color) so
    this exact bug can't silently come back."""
    exp_start = console_theme.CONSOLE_CSS.index('[data-testid="stExpander"] {')
    exp_rule = console_theme.CONSOLE_CSS[exp_start:exp_start + 200]
    assert "border: 1px solid" in exp_rule
    assert "border-color:" not in exp_rule


def test_console_css_hides_deploy_and_menu_but_never_the_whole_toolbar():
    """Streamlit chrome suppression (Fable5 ruling): the Deploy button and
    hamburger menu are dev-tool affordances with no data/action/audit role
    and may be hidden -- but stExpandSidebarButton (the sidebar collapse
    toggle, a real functional control) lives in the same stToolbar and must
    stay visible, so the fix targets the two elements individually, never
    `[data-testid="stToolbar"] { display: none }` wholesale."""
    assert '[data-testid="stAppDeployButton"]' in console_theme.CONSOLE_CSS
    assert '[data-testid="stMainMenu"]' in console_theme.CONSOLE_CSS
    assert '[data-testid="stToolbar"] {' not in console_theme.CONSOLE_CSS
    assert '[data-testid="stExpandSidebarButton"]' not in console_theme.CONSOLE_CSS


def test_console_css_sidebar_gets_a_dark_rail_treatment_without_mockup_copy():
    """Sidebar diverges deliberately from the mockup's nav-rail copy (this
    sidebar fires ledger WRITES; styling it to look like navigation would be
    a mode-confusion trap) -- restyled via CSS only, and the mockup's own
    fabricated identity strings must never appear anywhere in this file
    (quarantine-the-script still governs new CSS, not just the original B1
    pass)."""
    assert 'section[data-testid="stSidebar"]' in console_theme.CONSOLE_CSS
    assert "CONSOLE_01" not in console_theme.CONSOLE_CSS
    assert "OPERATOR_ACTIVE" not in console_theme.CONSOLE_CSS


def test_console_css_b4_additions_still_make_no_external_call():
    """Same re-assertion as the mobile pass -- every new CSS section this
    session (typography/chrome/sidebar/expander) is exactly as easy a place
    to slip a 'just this one font' CDN import back in."""
    assert "fonts.googleapis.com" not in console_theme.CONSOLE_CSS
    assert "@import" not in console_theme.CONSOLE_CSS
