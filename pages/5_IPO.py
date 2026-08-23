"""
IPO & Recent Listings — stocks too young for an RS Rating.

Goes in /opt/breakoutscanner-lab/pages/5_IPO.py

STYLING
-------
The CSS below is copied VERBATIM from app.py's style block — same
.breakout-card gradients, .card-pill badges, .card-stat rows and
.summary-metric tiles. It is duplicated rather than imported because app.py is
a Streamlit script: importing it to reach the styles would execute the entire
main scanner page as a side effect. Streamlit injects CSS per page anyway, so
each page needs its own copy regardless.

If the palette in app.py changes, change it here too. That is the cost of not
coupling a page to the main script, and it is the cheaper of the two.

WHY THIS PAGE EXISTS
--------------------
O'Neil's RS Rating needs 252 trading days; Minervini's template needs a 200-day
average and a 52-week range. A company listed eight months ago has none of
them, so on the main screens it shows blank columns — and a blank column reads
as a bad number.

That is not hypothetical. When undefined RS was stored as 0, these stocks
looked like the weakest in the market, and the Daily Screen's `RS >= 80` rule
excluded every one of them: 676 of 3,044 rows, the youngest and fastest-moving
part of the market, invisible by construction.

Here the strength judgement uses measures that exist for young stocks:

    ret_1m_rank             needs 22 bars, not 252
    pct_of_listing_range    stands in for "% of 52-week high"
    approx_listing_days     how much history there actually is
    turnover_30d_cr         whether it can be traded at all
"""

import os
import sys

import pandas as pd
import streamlit as st

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

st.set_page_config(page_title="IPO & Recent Listings", page_icon="🆕",
                   layout="wide")

try:
    import advice
    import scan_engine as SE
    from build_snapshot import TABLE, connect, db_path
except Exception as e:                                  # pragma: no cover
    st.error(f"Could not import a required module: {e}")
    st.stop()


# ── styles, verbatim from app.py ───────────────────────────────────────────
st.markdown(
    """
<style>
.breakout-card {
    padding: 0.9rem 1rem;
    border-radius: 12px;
    margin-bottom: 0.75rem;
    border: 1px solid rgba(255,255,255,0.1);
    box-shadow: 0 4px 16px rgba(0,0,0,0.35);
    min-height: 130px;
}
.breakout-card.bullish {
    background: linear-gradient(145deg, #0d2818 0%, #1b4332 45%, #2d6a4f 100%);
    border-left: 5px solid #52b788;
}
.breakout-card.bearish {
    background: linear-gradient(145deg, #3b0a0a 0%, #6b1515 45%, #9b2226 100%);
    border-left: 5px solid #f4845f;
}
.card-top {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 0.5rem;
    margin-bottom: 0.55rem;
    flex-wrap: wrap;
}
.card-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem;
    justify-content: flex-end;
}
.card-symbol {
    font-size: 1.15rem;
    font-weight: 800;
    color: #ffffff;
    letter-spacing: 0.02em;
}
.card-badges span {
    display: inline-block;
    font-size: 0.72rem;
    font-weight: 700;
    padding: 0.18rem 0.5rem;
    border-radius: 999px;
    background: rgba(255,255,255,0.14);
    color: #f8fafc;
}
.card-pill {
    display: inline-block;
    font-size: 0.72rem;
    font-weight: 700;
    padding: 0.18rem 0.5rem;
    border-radius: 999px;
    background: rgba(255,255,255,0.14);
    color: #f8fafc;
    margin-left: 0.25rem;
}
.card-badges .high52,
.card-pill.high52 {
    background: rgba(251, 191, 36, 0.25);
    color: #fde68a;
}
.card-stat-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 0.75rem;
    margin-bottom: 0.45rem;
}
.card-stat {
    font-size: 0.82rem;
    color: #e2e8f0;
}
.card-stat b {
    color: #ffffff;
    font-weight: 700;
}
.card-foot {
    font-size: 0.74rem;
    color: #cbd5e1;
    opacity: 0.92;
}
.summary-metrics {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
}
.summary-metric {
    flex: 1 1 120px;
    min-width: 110px;
    padding: 0.5rem 0.65rem;
    border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.08);
}
.summary-metric .sm-label {
    font-size: 0.68rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 0.15rem;
    opacity: 0.9;
}
.summary-metric .sm-value {
    font-size: 0.95rem;
    font-weight: 700;
    line-height: 1.25;
    word-break: break-word;
}
.summary-metric.breakouts {
    background: linear-gradient(145deg, #1e1b4b 0%, #312e81 100%);
    border-left: 3px solid #a78bfa;
}
.summary-metric.breakouts .sm-label { color: #c4b5fd; }
.summary-metric.breakouts .sm-value { color: #ede9fe; }
.summary-metric.bullish {
    background: linear-gradient(145deg, #052e16 0%, #14532d 100%);
    border-left: 3px solid #4ade80;
}
.summary-metric.bullish .sm-label { color: #86efac; }
.summary-metric.bullish .sm-value { color: #dcfce7; }
.summary-metric.bearish {
    background: linear-gradient(145deg, #450a0a 0%, #7f1d1d 100%);
    border-left: 3px solid #f87171;
}
.summary-metric.bearish .sm-label { color: #fca5a5; }
.summary-metric.bearish .sm-value { color: #fee2e2; }
.summary-metric.symbols {
    background: linear-gradient(145deg, #0c4a6e 0%, #075985 100%);
    border-left: 3px solid #38bdf8;
}
.summary-metric.symbols .sm-label { color: #7dd3fc; }
.summary-metric.symbols .sm-value { color: #e0f2fe; }
.summary-metric.scanned {
    background: linear-gradient(145deg, #451a03 0%, #78350f 100%);
    border-left: 3px solid #fbbf24;
}
.summary-metric.scanned .sm-label { color: #fcd34d; }
.summary-metric.scanned .sm-value { color: #fef3c7; font-size: 0.82rem; font-weight: 600; }
</style>
""",
    unsafe_allow_html=True,
)


def _render(html: str) -> None:
    # Mirrors app.py's _render_card_html.
    if hasattr(st, "html"):
        st.html(html)
    else:
        st.markdown(html, unsafe_allow_html=True)


def _f(row, key):
    v = row.get(key)
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


DB = db_path()
if not os.path.isfile(DB):
    st.title("🆕 IPO & Recent Listings")
    st.warning("**No snapshot yet.** Build one first.")
    st.stop()


def _conn():
    # Not cached: a long-lived read connection blocks the nightly build from
    # taking a write lock, so an open browser tab would stall the pipeline.
    return connect(DB, read_only=True)


conn = _conn()
try:
    as_of = conn.execute(f"SELECT max(date) FROM {TABLE}").fetchone()[0]
except Exception as e:
    st.error(f"Snapshot unreadable ({e}).")
    st.stop()

st.markdown(
    """
<div style="background: linear-gradient(135deg, #312e81 0%, #7c3aed 50%, #db2777 100%);
padding: 1.1rem 1.3rem; border-radius: 12px; margin-bottom: 1rem;">
<div style="font-size:1.4rem;font-weight:800;color:#fff;">🆕 IPO &amp; Recent Listings</div>
<div style="color:#ede9fe;margin-top:0.35rem;">
Stocks with less than twelve months of history — too young for an RS Rating,
so they never appear on the main screens.</div>
</div>
""",
    unsafe_allow_html=True,
)

BREAKING = "★ Recent Listings — breaking out, no RS history yet"
ALL_NEW = "★ Recent Listings — all"

with st.sidebar:
    st.markdown("### Your numbers")
    risk = st.number_input("Risk per trade (₹)", 500.0, 1_000_000.0,
                           5_000.0, 500.0)
    capital = st.number_input("Total capital (₹)", 0.0, 100_000_000.0,
                              500_000.0, 10_000.0)
    st.divider()
    min_turnover = st.slider(
        "Minimum turnover (₹ crore/day)", 0.0, 50.0, 5.0, 0.5,
        help="Recent listings are often thin. Below about ₹5 crore your own "
             "order moves the price.")
    max_days = st.slider("Listed within (trading days)", 20, 252, 252, 10,
                         help="252 trading days is about a year — the point "
                              "at which RS Rating becomes computable and "
                              "these graduate to the main screens.")

try:
    breaking = SE.run_scan(conn, SE.PREDEFINED[BREAKING], table=TABLE,
                           on_date=as_of, order_by="ret_1m_rank", limit=60)
    everything = SE.run_scan(conn, SE.PREDEFINED[ALL_NEW], table=TABLE,
                             on_date=as_of, order_by="ret_1m_rank", limit=500)
except Exception as e:
    st.error(f"Scan failed: {e}")
    st.stop()


def _keep(rows):
    out = []
    for r in rows:
        t, d = _f(r, "turnover_30d_cr"), _f(r, "approx_listing_days")
        if t is not None and t < min_turnover:
            continue
        if d is not None and d > max_days:
            continue
        out.append(r)
    return out


breaking, everything = _keep(breaking), _keep(everything)

liquid = sum(1 for r in everything if (_f(r, "turnover_30d_cr") or 0) >= 5)
days = [d for d in (_f(r, "approx_listing_days") for r in everything)
        if d is not None]
median_days = int(sorted(days)[len(days) // 2]) if days else 0

st.markdown(
    f"""
<div class="summary-metrics">
  <div class="summary-metric breakouts">
    <div class="sm-label">Recent Listings</div>
    <div class="sm-value">{len(everything)}</div></div>
  <div class="summary-metric bullish">
    <div class="sm-label">Breaking Out</div>
    <div class="sm-value">{len(breaking)}</div></div>
  <div class="summary-metric symbols">
    <div class="sm-label">Tradeable (≥₹5cr)</div>
    <div class="sm-value">{liquid}</div></div>
  <div class="summary-metric bearish">
    <div class="sm-label">Median Days Listed</div>
    <div class="sm-value">{median_days}</div></div>
  <div class="summary-metric scanned">
    <div class="sm-label">Snapshot</div>
    <div class="sm-value">{as_of}</div></div>
</div>
""",
    unsafe_allow_html=True,
)


def _ipo_card_html(row) -> str:
    """Same markup as app.py's _breakout_card_html, with the fields that mean
    something for a stock without twelve months of history."""
    direction = str(row.get("breakout_direction", "")).lower()
    cls = "bullish" if direction.startswith("bull") else "bearish"
    dir_label = "🟢 Bullish" if direction.startswith("bull") else "🔴 Bearish"
    tf = str(row.get("breakout_timeframes") or "—")

    badges = [f'<span class="card-pill">{tf}</span>',
              f'<span class="card-pill">{dir_label}</span>']
    d = _f(row, "approx_listing_days")
    if d is not None and d < 60:
        badges.append('<span class="card-pill high52">Just Listed</span>')
    # The defining property of this page, stated on every card so it is never
    # mistaken for a weak reading.
    badges.append('<span class="card-pill high52">No RS Yet</span>')

    bp = _f(row, "breakout_pct")
    sign = "+" if (bp or 0) >= 0 else ""
    vol = _f(row, "volume_ratio")
    r1 = _f(row, "ret_1m_rank")
    lr = _f(row, "pct_of_listing_range")
    to = _f(row, "turnover_30d_cr")
    lvl = _f(row, "breakout_level")
    close = _f(row, "close")

    row1 = [f'<span class="card-stat">Close <b>₹{close:,.2f}</b></span>'
            if close is not None else "",
            f'<span class="card-stat">Break <b>{sign}{bp:.2f}%</b></span>'
            if bp is not None else "",
            f'<span class="card-stat">Vol <b>{vol:.2f}×</b></span>'
            if vol is not None else "",
            f'<span class="card-stat">1m rank <b>{r1:.0f}</b></span>'
            if r1 is not None else ""]

    row2 = [f'<span class="card-stat">Level <b>₹{lvl:,.2f}</b></span>'
            if lvl is not None else "",
            f'<span class="card-stat">Listing range <b>{lr:.0f}%</b></span>'
            if lr is not None else "",
            f'<span class="card-stat">Turnover <b>₹{to:,.0f} cr</b></span>'
            if to is not None else ""]

    foot = []
    if d is not None:
        foot.append(f"{int(d)} trading days listed")
    lb = row.get("last_bar_date")
    if lb:
        foot.append(f"last bar {lb}")
    sec = row.get("sector")
    if sec:
        foot.append(str(sec))

    return (
        f'<div class="breakout-card {cls}">'
        f'<div class="card-top">'
        f'<span class="card-symbol">{row.get("symbol")}</span>'
        f'<span class="card-badges">{"".join(badges)}</span>'
        f"</div>"
        f'<div class="card-stat-row">{"".join(row1)}</div>'
        f'<div class="card-stat-row">{"".join(row2)}</div>'
        f'<span class="card-foot">{" · ".join(foot)}</span>'
        f"</div>"
    )


st.subheader(f"Breaking out today — {len(breaking)}")

if not breaking:
    st.info("**No recent listing broke out today.** Normal on most days — "
            "there are only a few hundred such stocks, and a breakout is an "
            "event rather than a state.")

cols = st.columns(2)
for i, row in enumerate(breaking):
    with cols[i % 2]:
        _render(_ipo_card_html(row))
        p = advice.plan(row, risk_rupees=risk, capital=capital)
        if p["shares"] > 0:
            st.caption(
                f"**{p['shares']:,} shares** · stop ₹{p['stop']:,.1f} "
                f"(−{p['stop_pct']:.1f}%) · cost ₹{p['notional']:,.0f} · "
                f"risking ₹{p['shares'] * p['risk_per_share']:,.0f}")
        for w in p["warnings"]:
            st.warning(w)

st.subheader(f"All recent listings — {len(everything)}")
st.caption("Ranked by one-month strength. No breakout required — this is what "
           "has listed recently and is behaving well.")

if everything:
    df = pd.DataFrame(everything)
    show_cols = [c for c in ("symbol", "sector", "close", "ret_1m_rank",
                             "pct_of_listing_range", "approx_listing_days",
                             "turnover_30d_cr", "adr_pct_5d", "is_breakout",
                             "breakout_timeframes", "last_bar_date")
                 if c in df.columns]
    st.dataframe(
        df[show_cols].rename(columns={
            "ret_1m_rank": "1m rank",
            "pct_of_listing_range": "% of listing range",
            "approx_listing_days": "days listed",
            "turnover_30d_cr": "turnover ₹cr",
            "adr_pct_5d": "ADR%",
            "is_breakout": "breakout",
            "breakout_timeframes": "timeframes",
            "last_bar_date": "last traded",
        }).round(2),
        hide_index=True, use_container_width=True)
else:
    st.info("No recent listings pass the current filters.")

with st.expander("Why these stocks need their own page"):
    st.markdown("""
RS Rating is a **percentile of a 12-month weighted return** across the scanned
universe. A stock listed eight months ago has no 12-month return, so the value
is genuinely undefined — not low.

That distinction has teeth. When undefined was stored as **0**, these stocks
looked like the weakest in the market, and `RS ≥ 80` on the Daily Screen
excluded every one of them. The youngest, fastest-moving segment of the market
was invisible, and nothing on screen said so.

Here the strength judgement uses the **1-month return percentile**, which needs
only 22 bars. The threshold is 80 — the same percentile O'Neil used for RS —
rather than a number chosen to make any particular stock appear.

**What this page cannot tell you.** These stocks have no long base to measure,
no 200-day trend to confirm, and often a lock-in expiry ahead that can add
supply without warning. A one-month percentile is thinner evidence than a
12-month one. Treat this as a smaller-position, higher-uncertainty list — not
the same setup with less history.
""")

st.caption("Research and education only. Not investment advice. Position "
           "sizing is arithmetic from the risk figure you entered.")
