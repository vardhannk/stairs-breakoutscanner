"""
ui_theme.py — one stylesheet for every page, and the mobile fixes.

Import and call `apply()` as the first thing after st.set_page_config().
Everything here is CSS injected into the page; no Streamlit behaviour is
patched and no other module is touched, so deleting this file returns the
app to stock appearance.

What the mobile rules actually fix
----------------------------------
Streamlit's default layout assumes a wide viewport. On a phone:

  * the sidebar overlays the whole screen and has to be dismissed manually
  * st.columns() keeps its horizontal split at 375px, so a 4-column metric
    row becomes four unreadable slivers
  * dataframes render at full table width and clip rather than scroll
  * the default 14px base font with tight padding gives sub-40px tap targets

The @media block below collapses columns to full width under 640px, turns
tables into horizontally scrollable panes, and raises control heights to
44px, which is the smallest reliably tappable target on iOS.
"""

from __future__ import annotations

# Palette. Deliberately small — three accents and a neutral ramp. Values are
# picked to stay legible on both Streamlit themes, so nothing here assumes a
# light background.
INK = "#0f172a"
MUTED = "#64748b"
LINE = "#e2e8f0"
UP = "#059669"
DOWN = "#dc2626"
ACCENT = "#2563eb"
WARM = "#d97706"

GRADE_COLORS = {"A": UP, "B": ACCENT, "C": WARM, "D": MUTED}

_CSS = """
<style>
/* ── layout ─────────────────────────────────────────────────────────── */
.block-container {padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1280px;}
h1 {font-size: 1.65rem !important; font-weight: 650 !important;
    letter-spacing: -0.02em; margin-bottom: .15rem !important;}
h2 {font-size: 1.15rem !important; font-weight: 620 !important;
    letter-spacing: -0.01em; margin-top: 1.6rem !important;}
h3 {font-size: .95rem !important; font-weight: 600 !important;}

/* Streamlit draws a heavy divider; a hairline reads better at this density */
hr {margin: 1.6rem 0 !important; border-color: rgba(148,163,184,.28) !important;}

/* ── the funnel strip ───────────────────────────────────────────────── */
.funnel {display:flex; gap:.5rem; flex-wrap:wrap; margin:.4rem 0 1.2rem;}
.funnel-step {flex:1 1 130px; min-width:120px; border:1px solid rgba(148,163,184,.3);
    border-radius:12px; padding:.7rem .85rem; background:rgba(148,163,184,.06);
    position:relative;}
.funnel-step.is-last {border-color:rgba(37,99,235,.5); background:rgba(37,99,235,.09);}
.funnel-n {font-size:1.5rem; font-weight:680; line-height:1.1; letter-spacing:-.02em;}
.funnel-l {font-size:.7rem; text-transform:uppercase; letter-spacing:.07em;
    opacity:.62; margin-top:.15rem;}
.funnel-d {font-size:.72rem; opacity:.55; margin-top:.3rem;}

/* ── result cards ───────────────────────────────────────────────────── */
.card {border:1px solid rgba(148,163,184,.28); border-radius:14px;
    padding:.85rem 1rem; margin-bottom:.6rem; background:rgba(148,163,184,.04);}
.card-top {display:flex; align-items:baseline; gap:.6rem; flex-wrap:wrap;}
.card-sym {font-size:1.05rem; font-weight:660; letter-spacing:-.01em;}
.card-sec {font-size:.78rem; opacity:.6;}
.card-row {display:flex; gap:1.4rem; flex-wrap:wrap; margin-top:.55rem;}
.card-kv {display:flex; flex-direction:column;}
.card-k {font-size:.66rem; text-transform:uppercase; letter-spacing:.06em; opacity:.5;}
.card-v {font-size:.92rem; font-weight:560; font-variant-numeric:tabular-nums;}
.card-miss {font-size:.74rem; opacity:.6; margin-top:.5rem;}

/* ── badges ─────────────────────────────────────────────────────────── */
.badge {display:inline-block; padding:.13rem .5rem; border-radius:999px;
    font-size:.72rem; font-weight:620; letter-spacing:.02em;
    border:1px solid currentColor;}
.flags {font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    font-size:.9rem; letter-spacing:.16em;}

/* ── gate legend ────────────────────────────────────────────────────── */
.legend {display:flex; gap:.4rem; flex-wrap:wrap; margin:.2rem 0 .9rem;}
.legend-i {font-size:.72rem; padding:.2rem .55rem; border-radius:8px;
    background:rgba(148,163,184,.14); opacity:.85;}

/* ── tables ─────────────────────────────────────────────────────────── */
[data-testid="stDataFrame"] {border-radius:10px; overflow:hidden;}

/* ── mobile ─────────────────────────────────────────────────────────── */
@media (max-width: 640px) {
    .block-container {padding:1.1rem .7rem 2.5rem !important;}
    h1 {font-size: 1.3rem !important;}
    h2 {font-size: 1.02rem !important;}

    /* Streamlit keeps columns side-by-side at any width; stack them */
    [data-testid="stHorizontalBlock"] {flex-direction:column !important; gap:.5rem !important;}
    [data-testid="stHorizontalBlock"] > div {width:100% !important; flex:1 1 100% !important;}
    [data-testid="column"] {width:100% !important; flex:1 1 100% !important;
        min-width:100% !important;}

    /* 44px is the smallest tap target that works reliably on iOS */
    .stButton button, .stDownloadButton button {width:100% !important;
        min-height:44px !important;}
    [data-baseweb="select"] > div {min-height:44px !important;}
    .stCheckbox, .stRadio {min-height:36px;}

    /* scroll wide tables instead of clipping them */
    [data-testid="stDataFrame"] {overflow-x:auto !important;}

    /* the sidebar overlays the page on a phone — make dismissal obvious */
    [data-testid="stSidebar"] {width:86vw !important; min-width:0 !important;}

    .funnel-step {flex:1 1 45%; min-width:0; padding:.55rem .6rem;}
    .funnel-n {font-size:1.2rem;}
    .card-row {gap:.9rem;}
    /* iOS zooms any input under 16px on focus; this prevents that jump */
    input, select, textarea {font-size:16px !important;}
}
</style>
"""


# Streamlit builds every selectbox and multiselect on BaseWeb's Select, which
# is a real <input> so you can type to filter. On a phone that means tapping
# "Symbol universe" opens the keyboard over the very list you are trying to
# read, and you have to dismiss it before you can pick anything.
#
# Marking the input readonly stops the virtual keyboard on both iOS and
# Android while leaving it focusable, so the dropdown still opens normally.
# The cost is that you lose type-to-filter ON PHONES ONLY — with 5 universes
# and 26 sectors, scrolling is faster than typing anyway. Desktop is untouched.
#
# This has to run as real JavaScript. Streamlit does not execute <script> tags
# inside st.markdown, so it goes through components.v1.html, which renders a
# same-origin iframe that can reach the parent document. That is a workaround,
# not an API, and a future Streamlit could break it — hence the try/except and
# the fact that nothing else depends on it.
_KEYBOARD_JS = """
<script>
(function () {
  try {
    var doc = window.parent.document;
    var isPhone = function () { return window.parent.innerWidth <= 640; };
    var apply = function () {
      var on = isPhone();
      doc.querySelectorAll('[data-baseweb="select"] input').forEach(function (el) {
        if (on) {
          el.setAttribute('readonly', 'readonly');
          el.setAttribute('inputmode', 'none');
        } else {
          el.removeAttribute('readonly');
          el.removeAttribute('inputmode');
        }
      });
    };
    apply();
    // Streamlit rebuilds widgets on every rerun, so a one-shot pass is not
    // enough — the observer re-applies it to whatever gets mounted later.
    new MutationObserver(apply).observe(doc.body, {childList: true, subtree: true});
    window.parent.addEventListener('resize', apply);
  } catch (e) { /* never let styling break the page */ }
})();
</script>
"""


def apply(stop_mobile_keyboard: bool = True) -> None:
    """
    Inject the stylesheet and the mobile fixes. Safe to call once per page.

    The sidebar is deliberately left alone. app.py opens it on load and on a
    phone it covers the page, which is annoying — but it is also the only way
    to reach the scan controls and the other pages, and every CSS trick for
    auto-hiding it relies on :hover, which does not exist on a touchscreen.
    Trading a mild annoyance for unreachable navigation is a bad trade. Tap
    the X to close it; the base stylesheet already sizes it as a drawer.
    """
    import streamlit as st
    st.markdown(_CSS, unsafe_allow_html=True)

    if stop_mobile_keyboard:
        try:
            import streamlit.components.v1 as components
            components.html(_KEYBOARD_JS, height=0, width=0)
        except Exception:
            pass


def pct(v, digits: int = 2, sign: bool = True) -> str:
    """Format a percentage, or an em dash when there is nothing to format."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f != f:
        return "—"
    return f"{f:+.{digits}f}%" if sign else f"{f:.{digits}f}%"


def num(v, digits: int = 0) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f != f:
        return "—"
    return f"{f:,.{digits}f}"


def color_for(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return MUTED
    if f != f:
        return MUTED
    return UP if f > 0 else DOWN if f < 0 else MUTED


def funnel(steps) -> str:
    """
    steps: [(count, label, detail), ...] — returns HTML for the strip.

    Showing the count at every stage is the point. A shortlist of 4 means
    something different when it came from 62 than when it came from 9.
    """
    out = ['<div class="funnel">']
    for i, (n, label, detail) in enumerate(steps):
        last = " is-last" if i == len(steps) - 1 else ""
        out.append(
            f'<div class="funnel-step{last}"><div class="funnel-n">{n}</div>'
            f'<div class="funnel-l">{label}</div>'
            f'<div class="funnel-d">{detail}</div></div>')
    out.append("</div>")
    return "".join(out)


def badge(text: str, color: str) -> str:
    return f'<span class="badge" style="color:{color}">{text}</span>'
