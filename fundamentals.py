"""
fundamentals.py — fetch fundamentals for a list of symbols, once.

Goes in /opt/breakoutscanner/fundamentals.py.

Shared by the All NSE table view and the Quality page so the two cannot drift
into computing "EPS YoY" two different ways and disagreeing on the same stock.

THE CONSTRAINT THAT SHAPES EVERYTHING HERE
==========================================
Prices batch; fundamentals do not. yf.download() takes a list and returns
hundreds of symbols in one call, which is why the nightly build covers 3,046
stocks in minutes. There is no batch equivalent for .info — it is one HTTP
request per company, roughly a second each.

So this is never called automatically. Every caller puts it behind an
explicit action with the count and the expected wait shown first. A scan
returning 500 stocks is eight minutes of sequential requests, and that has to
be your decision, not a side effect of opening a tab.

Results are cached for six hours in the Streamlit process, so re-filtering,
switching tabs or re-sorting costs nothing after the first fetch.

QoQ AND YoY COME FROM THE STATEMENTS
====================================
Not from info["earningsQuarterlyGrowth"], which is YoY despite its name and
tells you nothing about whether profit outgrew sales. Revenue and PAT are
read from quarterly_income_stmt and the percentages are computed from the
rupee figures, so any of them can be checked by hand.

MISSING IS MISSING
==================
Absent fields come back as None and stay None. Nothing is filled with zero.
Yahoo's coverage of Indian smallcaps is thin — 403 symbols in the universe
have no price data at all, let alone fundamentals — and a zero in a P/E
column reads as "extremely cheap" rather than "unknown".
"""

from __future__ import annotations

import pandas as pd

try:
    import streamlit as st
except Exception:                                       # pragma: no cover
    st = None

try:
    import yfinance as yf
except Exception:                                       # pragma: no cover
    yf = None


INFO_FIELDS = {
    "marketCap": "mcap", "sector": "y_sector", "industry": "industry",
    "trailingPE": "pe", "priceToBook": "pb", "returnOnEquity": "roe",
    "profitMargins": "npm", "operatingMargins": "opm",
    "debtToEquity": "de", "currentRatio": "cr",
}

# The columns callers merge into a table, in display order.
NUMERIC_COLS = ["mcap_cr", "pe", "pb", "roe_%", "opm_%", "de",
                "rev_qoq_%", "rev_yoy_%", "pat_qoq_%", "pat_yoy_%"]

LABELS = {
    "mcap_cr": "Mkt Cap ₹cr", "pe": "P/E", "pb": "P/B", "roe_%": "ROE %",
    "opm_%": "OPM %", "de": "D/E",
    "rev_qoq_%": "Rev QoQ %", "rev_yoy_%": "Rev YoY %",
    "pat_qoq_%": "PAT QoQ %", "pat_yoy_%": "PAT YoY %",
}


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _row_of(df, *names):
    if df is None or getattr(df, "empty", True):
        return None
    idx = {str(i).strip().lower(): i for i in df.index}
    for want in names:
        k = want.strip().lower()
        if k in idx:
            return pd.to_numeric(df.loc[idx[k]], errors="coerce")
    return None


def _pct(cur, prev):
    if cur is None or prev is None:
        return None
    try:
        cur, prev = float(cur), float(prev)
    except (TypeError, ValueError):
        return None
    if prev == 0 or prev != prev or cur != cur:
        return None
    return (cur / prev - 1.0) * 100.0


def _fetch_one_uncached(sym: str) -> dict:
    """Everything wanted for one ticker. Never raises."""
    out = {"symbol": sym, "ok": False, "quarters": None}
    if yf is None:
        out["err"] = "yfinance not installed"
        return out
    try:
        t = yf.Ticker(f"{sym}.NS")
        info = t.info or {}
    except Exception as e:
        out["err"] = str(e)[:120]
        return out
    for k, short in INFO_FIELDS.items():
        v = info.get(k)
        out[short] = v if short in ("y_sector", "industry") else _num(v)
    out["ok"] = any(out.get(v) is not None for v in INFO_FIELDS.values())
    try:
        q = t.quarterly_income_stmt
        rev = _row_of(q, "Total Revenue", "Operating Revenue")
        pat = _row_of(q, "Net Income", "Net Income Common Stockholders")
        if rev is not None and pat is not None and len(rev) >= 2:
            f = pd.DataFrame({"rev": rev, "pat": pat}).dropna(how="all")
            out["quarters"] = f.sort_index()          # oldest -> newest
    except Exception:
        pass
    return out


if st is not None:
    fetch_one = st.cache_data(ttl=6 * 3600, show_spinner=False)(
        _fetch_one_uncached)
else:                                                   # pragma: no cover
    fetch_one = _fetch_one_uncached


def _growth(d, field, lag):
    q = d.get("quarters")
    if q is None or field not in q:
        return None
    s = q[field].dropna()
    if len(s) <= lag:
        return None
    return _pct(s.iloc[-1], s.iloc[-1 - lag])


def to_row(d: dict) -> dict:
    """One symbol's fetch result flattened to display columns."""
    return {
        "symbol": d.get("symbol"),
        "mcap_cr": (d["mcap"] / 1e7) if d.get("mcap") else None,
        "pe": d.get("pe"),
        "pb": d.get("pb"),
        "roe_%": (d["roe"] * 100) if d.get("roe") is not None else None,
        "opm_%": (d["opm"] * 100) if d.get("opm") is not None else None,
        "de": d.get("de"),
        "rev_qoq_%": _growth(d, "rev", 1),
        "rev_yoy_%": _growth(d, "rev", 4),
        "pat_qoq_%": _growth(d, "pat", 1),
        "pat_yoy_%": _growth(d, "pat", 4),
        "y_sector": d.get("y_sector"),
        "_ok": bool(d.get("ok")),
    }


def fetch_frame(symbols, progress=None) -> pd.DataFrame:
    """
    DataFrame indexed by symbol with the columns in NUMERIC_COLS.

    `progress` is called as progress(i, n, symbol) so the caller owns how the
    wait is displayed — a page with tabs wants a different widget from a page
    without.
    """
    syms = [str(s).upper().strip() for s in symbols if str(s).strip()]
    rows, n = [], len(syms)
    for i, s in enumerate(syms, 1):
        rows.append(to_row(fetch_one(s)))
        if progress is not None:
            try:
                progress(i, n, s)
            except Exception:
                pass
    if not rows:
        return pd.DataFrame(columns=["symbol"] + NUMERIC_COLS)
    return pd.DataFrame(rows).set_index("symbol")


def coverage(frame: pd.DataFrame) -> tuple[int, int]:
    """(found, total) — how many symbols returned anything at all."""
    if frame is None or frame.empty:
        return 0, 0
    if "_ok" in frame.columns:
        return int(frame["_ok"].sum()), len(frame)
    return int(frame[NUMERIC_COLS].notna().any(axis=1).sum()), len(frame)
