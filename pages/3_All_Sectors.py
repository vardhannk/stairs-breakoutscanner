"""
All Sectors — every sectoral index, ranked, advancing and declining.

Goes in /opt/breakoutscanner/pages/3_All_Sectors.py

This is the whole-market view. The Sector Performance page hides declining
sectors because it feeds a long-only screen; that is the right default there
and the wrong one here. Rotation is a comparison, and you cannot see money
leaving a sector if the leaving sectors are not drawn.

Adds two things a single index number hides:

  BREADTH     how many of the sector's constituents are actually up. A sector
              can print +1.8% on two heavyweights while most of its names
              fall. "Nifty Auto +1.8%, 6 of 15 advancing" is a different
              sector from "+1.8%, 13 of 15 advancing".

  EXTRAS      eleven further NSE thematic indices beyond the 26 used for
              tagging — railways, digital, mobility, EV, housing, logistics
              and the MidSmall cuts. Their URLs are inferred rather than
              verified, so any that NSE names differently simply do not
              appear.

Returns are equal-weighted means of each index's constituents, so the levels
will not match NSE's published free-float cap-weighted figures. The ranking is
what a rotation view needs.

app.py is not modified.
"""

import os
import sys

import pandas as pd
import streamlit as st

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

st.set_page_config(page_title="All Sectors", page_icon="🌐", layout="wide")

try:
    import sectors
    import ui_theme as T
    from data_loader import load_daily
except Exception as e:                                  # pragma: no cover
    st.error(f"Could not import a required module: {e}")
    st.stop()

T.apply()

st.title("🌐 All Sectors")
st.caption("Every sectoral index, ranked. Declining sectors included — "
           "rotation is a comparison.")

# ── timeframe selector ─────────────────────────────────────────────────────
LABELS = {w: f"{w} Chg%" for w in sectors.WINDOW_ORDER}
INV = {v: k for k, v in LABELS.items()}

if hasattr(st, "segmented_control"):
    picked = st.segmented_control(
        "Timeframe", list(LABELS.values()), default=LABELS["1D"],
        label_visibility="collapsed")
    window = INV.get(picked or LABELS["1D"], "1D")
else:                                    # Streamlit < 1.40
    window = INV[st.radio("Timeframe", list(LABELS.values()), horizontal=True,
                          label_visibility="collapsed")]


@st.cache_data(ttl=1800, show_spinner=False)
def _load() -> pd.DataFrame:
    return sectors.all_sector_performance(
        loader=lambda s: load_daily(s, use_cache=True))


c1, c2 = st.columns([3, 1])
with c2:
    if st.button("🔄 Refresh", use_container_width=True):
        _load.clear()
        st.rerun()

with st.spinner("Reading constituents for every sectoral index…"):
    perf = _load()

if perf is None or perf.empty:
    st.error(
        "No sector data. This page reads the cached constituent lists and "
        "daily bars — run `universes.py` once, then a scan, so the bars exist."
    )
    st.stop()

bench = perf.attrs.get("benchmark", {})
bench_name = perf.attrs.get("benchmark_name", "Nifty 50")
bv = bench.get(window)
has_bench = bv is not None and bv == bv

ranked = perf.dropna(subset=[window]).sort_values(window, ascending=False)
adv_col, tot_col = f"{window} adv", f"{window} tot"

m1, m2, m3 = st.columns(3)
with m1:
    st.metric(f"{bench_name} — {window}", f"{bv:+.2f}%" if has_bench else "—")
with m2:
    up = int((ranked[window] > 0).sum())
    st.metric("Sectors advancing", f"{up} of {len(ranked)}")
with m3:
    if adv_col in ranked.columns:
        a, t = int(ranked[adv_col].sum()), int(ranked[tot_col].sum())
        st.metric("Stocks advancing", f"{a:,} of {t:,}",
                  help="Across every constituent of every index. Overlaps are "
                       "counted more than once — a stock in three indices "
                       "appears three times.")

# ── chart ──────────────────────────────────────────────────────────────────
try:
    import plotly.graph_objects as go
    hover = [
        f"<b>{s}</b><br>{window}: {v:+.2f}%<br>advancing: {int(a)} of {int(t)}"
        for s, v, a, t in zip(ranked["sector"], ranked[window],
                              ranked.get(adv_col, pd.Series([0] * len(ranked))),
                              ranked.get(tot_col, pd.Series([0] * len(ranked))))
    ]
    fig = go.Figure(go.Bar(
        x=ranked["sector"], y=ranked[window],
        marker=dict(color="#6b7cf5"),
        hovertext=hover, hoverinfo="text",
    ))
    if has_bench:
        fig.add_hline(y=bv, line_dash="dot", line_color="#94a3b8",
                      annotation_text=f"{bench_name} {bv:+.2f}%",
                      annotation_position="top left")
    fig.update_layout(
        height=560, margin=dict(l=8, r=8, t=20, b=170),
        yaxis_title=None, xaxis_title=None, xaxis_tickangle=-45,
        showlegend=False, bargap=0.28,
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        font=dict(size=12))
    fig.update_yaxes(ticksuffix="%", gridcolor="rgba(148,163,184,.25)",
                     zeroline=True, zerolinecolor="rgba(100,116,139,.7)",
                     zerolinewidth=1)
    fig.update_xaxes(showgrid=False)
    st.plotly_chart(fig, use_container_width=True)
except Exception as e:
    st.warning(f"Chart unavailable ({e}); table below.")

# ── leaders and laggards ───────────────────────────────────────────────────
def _chips(frame: pd.DataFrame) -> str:
    """
    Build the pill row by zipping columns, NOT via itertuples().

    itertuples() renames any column that is not a valid Python identifier,
    and every window here starts with a digit — "1D" becomes "_1". Attribute
    access then raises AttributeError for a column that is plainly present.
    Zipping the Series sidesteps the rename entirely.
    """
    return ('<div class="legend">'
            + "".join(f'<span class="legend-i">{s} <b>{v:+.2f}%</b></span>'
                      for s, v in zip(frame["sector"], frame[window]))
            + "</div>")


lead = ranked.head(5)
lag = ranked.tail(5).iloc[::-1]

L, R = st.columns(2)
with L:
    st.markdown(f"**Leading — {window}**")
    st.markdown(_chips(lead), unsafe_allow_html=True)
with R:
    st.markdown(f"**Lagging — {window}**")
    st.markdown(_chips(lag), unsafe_allow_html=True)

# ── table ──────────────────────────────────────────────────────────────────
st.subheader("Every sector, every window")

show = ranked.copy()
if adv_col in show.columns:
    show["breadth"] = [
        f"{int(a)} of {int(t)}" if t else "—"
        for a, t in zip(show[adv_col], show[tot_col])
    ]
    show["breadth %"] = [
        round(100.0 * a / t, 1) if t else float("nan")
        for a, t in zip(show[adv_col], show[tot_col])
    ]

cols = ["sector", "breadth", "breadth %", f"{window} vs Nifty"] \
    + sectors.WINDOW_ORDER + ["members", "with_data"]
st.dataframe(show[[c for c in dict.fromkeys(cols) if c in show.columns]].round(2),
             hide_index=True, use_container_width=True)

st.caption(
    f"{len(perf)} indices with enough cached data. Returns are equal-weighted "
    "means of each index's constituents, so they will not match NSE's "
    "published free-float cap-weighted figures — the ranking is the point. "
    "`breadth` counts constituents up over the selected window; a strong "
    "index number on weak breadth is two large stocks, not a sector move. "
    "Indices missing entirely have no cached constituent file — run "
    "`universes.py`, and note the newer thematic slugs are inferred, so a few "
    "may not resolve at all."
)
