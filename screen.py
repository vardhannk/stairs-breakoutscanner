"""
screen.py — stock-selection criteria as scan columns + filters.

Screening criteria implemented:
    price >= 100
    relative strength vs NIFTY over ~3 months
    10-day average volume >= 100k        (COMPLETED sessions only)
    not locked in upper/lower circuit
    close > 50 DMA
    (already in the app: new 20-day high = "last one month high")

Design notes
------------
RS window: 63 trading days ~= 92 calendar days ~= the 3 months measured on
the TradingView chart. Return between two timestamps is identical on hourly
or daily candles, so this uses the daily bars the scanner already loads
rather than downloading 1h data for 500 symbols.

Two RS numbers are produced because they answer different questions:

    rs_vs_nifty   stock 3m return minus NIFTY 3m return, in percentage points.
                  The literal "> Nifty" test. In a range-bound index (NIFTY
                  was +1.02% over the last quarter) this is a low bar.

    rs_rank       percentile 0-100 of that outperformance across the scanned
                  universe. This is what O'Neil / Minervini actually use to
                  find leaders. Only meaningful relative to what you scanned.

Volume: the surge test and the liquidity floor are deliberately different.
    surge  -> today's volume (extrapolated if the session is still open)
              vs the 20-day average. "Unusual for this stock."
    floor  -> mean of the last 10 COMPLETED sessions. "Tradeable at all."
              Never extrapolated: that would qualify a stock on a guess
              about a day that has not finished.
"""

from __future__ import annotations

from datetime import datetime, time as _time
from typing import Optional

import numpy as np
import pandas as pd

RS_WINDOW = 63          # trading days ~ 3 months
RET_1M_WINDOW = 21      # trading days ~ 1 month — "top monthly gainers"
VOL_FLOOR_WINDOW = 10   # completed sessions
DMA_WINDOW = 50

# NSE regular session, IST
SESSION_START = _time(9, 15)
SESSION_END = _time(15, 30)
SESSION_MINUTES = 375

# 2% bands exist but apply to very few stocks, and plenty of ordinary
# breakouts move ~2% and close at the high — including it produced false
# positives on normal names in testing. Left out deliberately.
CIRCUIT_BANDS = (0.05, 0.10, 0.20)
CIRCUIT_TOL = 0.001     # how close to a band counts as "at the band"
CIRCUIT_MAX_RANGE = 0.01  # a locked bar barely trades: range < 1% of price


# ---------------------------------------------------------------------------
# Volume extrapolation
# ---------------------------------------------------------------------------
def session_fraction_elapsed(now: Optional[datetime] = None) -> float:
    """
    How much of today's NSE session has completed, 0..1.

    Returns 1.0 outside market hours, so completed days are never scaled.
    """
    now = now or datetime.now()
    t = now.time()
    if now.weekday() >= 5 or t < SESSION_START or t >= SESSION_END:
        return 1.0
    elapsed = (now.hour * 60 + now.minute) - (SESSION_START.hour * 60 + SESSION_START.minute)
    return max(1e-6, min(1.0, elapsed / SESSION_MINUTES))


def extrapolate_last_volume(bars: pd.DataFrame, now: Optional[datetime] = None) -> float:
    """
    Scale a partial session's volume to a full-day estimate.

    Mirrors what resample_monthly() already does for partial months, and what
    discretionary traders do by eye: "20-day average is 1 lakh, stock has done 30,000
    in the first 15 minutes -> healthy".

    Only scales when the last bar is dated TODAY and the session is open.
    """
    if bars is None or bars.empty or "volume" not in bars.columns:
        return float("nan")
    vol = float(bars["volume"].iloc[-1])
    now = now or datetime.now()
    last_idx = bars.index[-1]
    last_date = last_idx.date() if hasattr(last_idx, "date") else None
    if last_date != now.date():
        return vol                      # completed bar, leave alone
    frac = session_fraction_elapsed(now)
    if frac >= 1.0:
        return vol
    return vol / frac


# ---------------------------------------------------------------------------
# Circuit detection
# ---------------------------------------------------------------------------
def is_circuit_locked(bars: pd.DataFrame) -> bool:
    """
    Heuristic — NSE does not publish per-stock bands in the OHLCV feed.

    Flags when either:
      * the bar has no range at all (high == low) — definitively frozen, or
      * ALL THREE of: the close is pinned to an extreme of the bar, the move
        sits within 0.1% of a standard band, AND the bar barely traded
        (range < 1% of price). A locked stock does not range.

    That third condition matters: without it, an ordinary breakout that
    happens to gain ~5% and close at its high gets flagged. Testing on
    normal names produced exactly that false positive.
    """
    if bars is None or len(bars) < 2:
        return False
    last = bars.iloc[-1]
    high, low, close = float(last["high"]), float(last["low"]), float(last["close"])
    prev_close = float(bars["close"].iloc[-2])
    if prev_close <= 0 or close <= 0:
        return False

    if high == low:
        return True

    move = abs(close / prev_close - 1.0)
    pinned = (close >= high - 1e-9) or (close <= low + 1e-9)
    if not pinned:
        return False
    if (high - low) / close > CIRCUIT_MAX_RANGE:
        return False
    return any(abs(move - b) <= CIRCUIT_TOL for b in CIRCUIT_BANDS)


# ---------------------------------------------------------------------------
# Per-symbol metrics
# ---------------------------------------------------------------------------
def _pct_return(series: pd.Series, window: int) -> float:
    s = series.dropna()
    if len(s) < window + 1:
        return float("nan")
    a, b = float(s.iloc[-window - 1]), float(s.iloc[-1])
    return (b / a - 1.0) if a > 0 else float("nan")


def nifty_return(nifty_bars: pd.DataFrame, window: int = RS_WINDOW) -> float:
    """Benchmark return over the RS window. ~0.0102 in the chart you sent."""
    if nifty_bars is None or nifty_bars.empty:
        return float("nan")
    return _pct_return(nifty_bars["close"].astype(float), window)


# ---------------------------------------------------------------------------
# Relative strength — three measures, all from the same daily bars
# ---------------------------------------------------------------------------
MANSFIELD_WINDOW = 200      # ~52 weeks of trading days (Weinstein used 52 weekly)
ONEIL_WINDOWS = (63, 126, 189, 252)
ONEIL_WEIGHTS = (2.0, 1.0, 1.0, 1.0)


def mansfield_rs(bars: pd.DataFrame, index_bars: pd.DataFrame,
                 window: int = MANSFIELD_WINDOW) -> float:
    """
    Stan Weinstein's Mansfield Relative Strength.

        ratio     = stock_close / index_close
        mansfield = (ratio / SMA(ratio, window) - 1) * 100

    ZERO is the meaningful line: above 0 the stock is outperforming its own
    recent relationship to the index — which catches stocks *turning* into
    outperformers, something a simple return comparison misses.

    Weinstein used weekly bars with a 52-week average; 200 daily bars is the
    same span. Dates are inner-joined so a missing session in either series
    cannot silently shift the alignment.
    """
    if bars is None or bars.empty or index_bars is None or index_bars.empty:
        return float("nan")
    s = bars["close"].astype(float).dropna()
    i = index_bars["close"].astype(float).dropna()
    joined = pd.concat([s.rename("s"), i.rename("i")], axis=1, join="inner").dropna()
    if len(joined) < window + 1:
        return float("nan")
    ratio = joined["s"] / joined["i"]
    sma = ratio.rolling(window).mean()
    if not np.isfinite(sma.iloc[-1]) or sma.iloc[-1] == 0:
        return float("nan")
    return float((ratio.iloc[-1] / sma.iloc[-1] - 1.0) * 100.0)


def oneil_score(bars: pd.DataFrame) -> float:
    """
    Raw O'Neil / IBD strength score — the input to the 1-99 RS Rating.

        2 x (3-month return) + (6-month) + (9-month) + (12-month)

    Absolute return, NOT measured against the index: the comparison happens
    later when this is percentile-ranked across the universe. Needs 252
    trading days, so recently listed stocks return NaN here while still
    getting the other two measures.
    """
    if bars is None or bars.empty:
        return float("nan")
    close = bars["close"].astype(float).dropna()
    total = 0.0
    for w, wt in zip(ONEIL_WINDOWS, ONEIL_WEIGHTS):
        r = _pct_return(close, w)
        if not np.isfinite(r):
            return float("nan")
        total += wt * r
    return float(total)


W52 = 252          # trading days in 52 weeks
MINERVINI_HIGH_PCT = 75.0   # within 25% of the 52-week high
MINERVINI_LOW_PCT = 30.0    # at least 30% above the 52-week low
MINERVINI_RS_MIN = 70.0     # RS Rating floor


def trend_template(bars: pd.DataFrame, rs_rating: float = float("nan")) -> dict:
    """
    Mark Minervini's Trend Template — all eight criteria.

    (Criterion 7 is the "near 52-week high" test.)

        1  price > 150 DMA and > 200 DMA
        2  150 DMA > 200 DMA
        3  200 DMA trending up for at least a month
        4  50 DMA > 150 DMA and > 200 DMA
        5  price > 50 DMA
        6  price >= 30% above the 52-week low
        7  price within 25% of the 52-week high   (>= 75%)
        8  RS Rating >= 70

    Returns each criterion plus `minervini_score` (0-8) and `minervini_pass`.
    Partial scores are useful on their own — 7/8 with only the RS leg failing
    is a very different stock from 3/8.
    """
    out = {f"mv{i}": False for i in range(1, 9)}
    out.update({"minervini_score": 0, "minervini_pass": False,
                "pct_of_52w_high": np.nan, "pct_of_52w_low": np.nan,
                "dma150": np.nan, "dma200": np.nan, "dma200_rising": False})
    if bars is None or bars.empty:
        return out

    b = bars.dropna(subset=["close"])
    close = b["close"].astype(float)
    if len(close) < 200:
        return out

    price = float(close.iloc[-1])
    dma50 = float(close.iloc[-50:].mean())
    dma150 = float(close.iloc[-150:].mean())
    dma200 = float(close.iloc[-200:].mean())
    out["dma150"], out["dma200"] = dma150, dma200

    # 200 DMA a month ago, to test slope
    if len(close) >= 221:
        dma200_prev = float(close.iloc[-221:-21].mean())
        out["dma200_rising"] = dma200 > dma200_prev

    # Require the FULL 52-week window. Using whatever history exists would
    # label a 220-day high as a "52-week high" — a different number wearing
    # the same name. NaN is the honest answer for short histories.
    if len(b) >= W52:
        hi52 = float(b["high"].astype(float).iloc[-W52:].max())
        lo52 = float(b["low"].astype(float).iloc[-W52:].min())
        out["pct_of_52w_high"] = price / hi52 * 100.0 if hi52 > 0 else np.nan
        out["pct_of_52w_low"] = (price / lo52 - 1.0) * 100.0 if lo52 > 0 else np.nan

    out["mv1"] = price > dma150 and price > dma200
    out["mv2"] = dma150 > dma200
    out["mv3"] = bool(out["dma200_rising"])
    out["mv4"] = dma50 > dma150 and dma50 > dma200
    out["mv5"] = price > dma50
    out["mv6"] = bool(out["pct_of_52w_low"] >= MINERVINI_LOW_PCT) \
        if np.isfinite(out["pct_of_52w_low"]) else False
    out["mv7"] = bool(out["pct_of_52w_high"] >= MINERVINI_HIGH_PCT) \
        if np.isfinite(out["pct_of_52w_high"]) else False
    out["mv8"] = bool(rs_rating >= MINERVINI_RS_MIN) if np.isfinite(rs_rating) else False

    out["minervini_score"] = int(sum(out[f"mv{i}"] for i in range(1, 9)))
    out["minervini_pass"] = out["minervini_score"] == 8
    return out


def add_trend_template(df: pd.DataFrame, bars_for) -> pd.DataFrame:
    """
    Apply trend_template row-wise. Runs AFTER add_rs_rating() because
    criterion 8 needs the RS Rating, which is universe-relative.
    """
    if df is None or df.empty:
        return df
    rows = []
    for _, r in df.iterrows():
        try:
            rows.append(trend_template(bars_for(r["symbol"]),
                                       float(r.get("rs_rating", np.nan))))
        except Exception:
            rows.append({})
    tdf = pd.DataFrame(rows, index=df.index)
    out = df.copy()
    for c in tdf.columns:
        out[c] = tdf[c]
    return out


# ---------------------------------------------------------------------------
# All-time high
# ---------------------------------------------------------------------------
def add_ath(df: pd.DataFrame, deep_bars_for) -> pd.DataFrame:
    """
    Distance from the all-time high.

    True ATH needs the full listing history, which the scanner does not fetch
    (load_daily covers ~440 trading days). Fetching period="max" for 500
    symbols would be prohibitive — but this runs AFTER the scan, on the
    handful of rows that produced a breakout, so it is ~20 fetches, not 500.

    `deep_bars_for(symbol)` should return the deepest history available, or
    None. Where it returns None the columns are NaN rather than silently
    falling back to a shallow window and calling it an all-time high.
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    ath, pct, is_ath, span = [], [], [], []
    for _, r in out.iterrows():
        try:
            d = deep_bars_for(r["symbol"])
        except Exception:
            d = None
        if d is None or d.empty or "high" not in d.columns:
            ath.append(np.nan); pct.append(np.nan); is_ath.append(False); span.append(np.nan)
            continue
        h = float(d["high"].astype(float).max())
        p = float(d["close"].astype(float).iloc[-1])
        ath.append(h)
        pct.append(p / h * 100.0 if h > 0 else np.nan)
        is_ath.append(bool(h > 0 and p >= h * 0.999))   # within 0.1% counts
        span.append(int(len(d)))
    out["ath"] = ath
    out["pct_of_ath"] = pct
    out["at_ath"] = is_ath
    out["ath_history_bars"] = span
    return out


# ---------------------------------------------------------------------------
# Listing age / IPO
# ---------------------------------------------------------------------------
IPO_MIN_BARS = 60      # detect_breakout cannot fire below this anyway
IPO_MAX_BARS = W52     # "listed within the last year"
LISTING_RANGE_BARS = 5  # the first week of trading defines the listing range


def listing_range_position(bars: pd.DataFrame, only_if_recent: bool = True) -> float:
    """
    Where price sits relative to the FIRST WEEK of trading, as a percentage.

        100 = at the top of the listing range
          0 = at the bottom
        >100 = has broken above its entire listing range
         <0 = has broken below it

    Only computed for recent listings. For an established stock the first
    five rows of a 440-bar fetch are simply 440 days ago, not its listing
    week, so the number would be meaningless — NaN instead.
    """
    if bars is None or bars.empty:
        return float("nan")
    b = bars.dropna(subset=["close", "high", "low"])
    n = len(b)
    if n < LISTING_RANGE_BARS + 1:
        return float("nan")
    if only_if_recent and not (IPO_MIN_BARS <= n < IPO_MAX_BARS):
        return float("nan")
    hi = float(b["high"].astype(float).iloc[:LISTING_RANGE_BARS].max())
    lo = float(b["low"].astype(float).iloc[:LISTING_RANGE_BARS].min())
    if hi <= lo:
        return float("nan")
    return (float(b["close"].iloc[-1]) - lo) / (hi - lo) * 100.0


def listing_age(bars: pd.DataFrame) -> dict:
    """
    Infer listing age from how much history exists — no listings feed needed.

    Ask for ~440 sessions and get 150 back, and the stock listed ~150
    sessions ago. Note the hard floor: below 60 daily bars detect_breakout
    returns None, so a stock listed last month is invisible to this scanner
    regardless of any filter.
    """
    n = 0 if bars is None or bars.empty else int(len(bars.dropna(subset=["close"])))
    return {
        "bars_available": n,
        "approx_listing_days": n,
        "is_recent_listing": bool(IPO_MIN_BARS <= n < IPO_MAX_BARS),
    }


def add_rs_rating(df: pd.DataFrame) -> pd.DataFrame:
    """
    Percentile-rank oneil_score into a 1-99 RS Rating across the scanned
    universe. O'Neil looked for 80+. Only meaningful relative to what you
    scanned — rank within NIFTY 50 means something different from NIFTY 500.
    """
    if df is None or df.empty or "oneil_score" not in df.columns:
        return df
    out = df.copy()
    valid = out["oneil_score"].notna()
    out["rs_rating"] = np.nan
    if valid.sum() >= 2:
        pct = out.loc[valid, "oneil_score"].rank(pct=True)
        out.loc[valid, "rs_rating"] = (pct * 98 + 1).round(0)
    return out


def compute_metrics(bars: pd.DataFrame, bench_return: float,
                    now: Optional[datetime] = None,
                    index_bars: Optional[pd.DataFrame] = None,
                    bench_return_1m: float = float("nan")) -> dict:
    """All screening criteria for one symbol. NaN where there isn't enough history."""
    out = {
        "price": np.nan, "ret_3m": np.nan, "rs_vs_nifty": np.nan,
        "ret_1m": np.nan, "rs_1m_vs_nifty": np.nan,
        "mansfield_rs": np.nan, "oneil_score": np.nan,
        "avg_vol_10d": np.nan, "turnover_10d": np.nan,
        "vol_today_extrapolated": np.nan,
        "dma50": np.nan, "above_50dma": False, "circuit_suspect": False,
        "bars_available": 0, "approx_listing_days": 0, "is_recent_listing": False,
        "pct_of_listing_range": np.nan,
    }
    if bars is None or bars.empty:
        return out

    b = bars.dropna(subset=["close"])
    if b.empty:
        return out

    close = b["close"].astype(float)
    price = float(close.iloc[-1])
    out["price"] = price

    r = _pct_return(close, RS_WINDOW)
    out["ret_3m"] = r
    if np.isfinite(r) and np.isfinite(bench_return):
        # 1. simple outperformance — percentage POINTS, not a ratio
        out["rs_vs_nifty"] = (r - bench_return) * 100.0

    # 1-month: "top monthly gainers". Needs only 22 bars, so it
    # works on recent listings where rs_rating and Minervini cannot.
    r1 = _pct_return(close, RET_1M_WINDOW)
    out["ret_1m"] = r1
    if np.isfinite(r1) and np.isfinite(bench_return_1m):
        out["rs_1m_vs_nifty"] = (r1 - bench_return_1m) * 100.0

    # 2. Mansfield (Weinstein) — needs the index series, not just its return
    if index_bars is not None:
        out["mansfield_rs"] = mansfield_rs(b, index_bars)

    # 3. O'Neil raw score — percentile-ranked later by add_rs_rating()
    out["oneil_score"] = oneil_score(b)

    if "volume" in b.columns and len(b) >= VOL_FLOOR_WINDOW + 1:
        # completed sessions only — exclude the (possibly partial) last bar
        avg10 = float(b["volume"].astype(float).iloc[-VOL_FLOOR_WINDOW - 1:-1].mean())
        out["avg_vol_10d"] = avg10
        out["turnover_10d"] = avg10 * price
        out["vol_today_extrapolated"] = extrapolate_last_volume(b, now)

    if len(close) >= DMA_WINDOW:
        dma = float(close.iloc[-DMA_WINDOW:].mean())
        out["dma50"] = dma
        out["above_50dma"] = price > dma

    out["circuit_suspect"] = is_circuit_locked(b)
    out.update(listing_age(b))
    out["pct_of_listing_range"] = listing_range_position(b)
    return out


# ---------------------------------------------------------------------------
# Universe-level
# ---------------------------------------------------------------------------
def add_ret_1m_rank(df: pd.DataFrame) -> pd.DataFrame:
    """
    Percentile-rank the 1-month return — "top monthly gainers".

    Unlike rs_rating this needs only 22 bars, so recent listings get a real
    number instead of NaN. That makes it the RS measure that survives an
    IPO scan.
    """
    if df is None or df.empty or "ret_1m" not in df.columns:
        return df
    out = df.copy()
    out["ret_1m_rank"] = out["ret_1m"].rank(pct=True) * 100.0
    return out


def add_rs_rank(df: pd.DataFrame) -> pd.DataFrame:
    """Percentile rank 0-100 of rs_vs_nifty across the scanned universe."""
    if df is None or df.empty or "rs_vs_nifty" not in df.columns:
        return df
    out = df.copy()
    out["rs_rank"] = out["rs_vs_nifty"].rank(pct=True) * 100.0
    return out


def apply_filters(df: pd.DataFrame, *, min_price: float = 100.0,
                  min_avg_vol_10d: float = 100_000.0,
                  require_rs_positive: bool = True,
                  min_rs_rank: Optional[float] = None,
                  min_rs_rating: Optional[float] = None,
                  min_ret_1m_rank: Optional[float] = None,
                  min_mansfield_rs: Optional[float] = None,
                  require_rs_1m_positive: bool = False,
                  require_above_50dma: bool = True,
                  exclude_circuit: bool = True,
                  min_pct_of_52w_high: Optional[float] = None,
                  min_minervini_score: Optional[int] = None,
                  recent_listing: Optional[bool] = None) -> pd.DataFrame:
    """Bottom-up screen. Defaults are the standard momentum thresholds."""
    if df is None or df.empty:
        return df
    out = df.copy()
    if min_price is not None and "price" in out:
        out = out[out["price"].fillna(0) >= min_price]
    if min_avg_vol_10d is not None and "avg_vol_10d" in out:
        out = out[out["avg_vol_10d"].fillna(0) >= min_avg_vol_10d]
    if require_rs_positive and "rs_vs_nifty" in out:
        out = out[out["rs_vs_nifty"].fillna(-1e9) > 0]
    if min_rs_rank is not None and "rs_rank" in out:
        out = out[out["rs_rank"].fillna(0) >= min_rs_rank]
    if require_above_50dma and "above_50dma" in out:
        out = out[out["above_50dma"].fillna(False)]
    if exclude_circuit and "circuit_suspect" in out:
        out = out[~out["circuit_suspect"].fillna(False)]
    if min_rs_rating is not None and "rs_rating" in out:
        out = out[out["rs_rating"].fillna(0) >= min_rs_rating]
    if min_ret_1m_rank is not None and "ret_1m_rank" in out:
        out = out[out["ret_1m_rank"].fillna(0) >= min_ret_1m_rank]
    if min_mansfield_rs is not None and "mansfield_rs" in out:
        out = out[out["mansfield_rs"].fillna(-1e9) >= min_mansfield_rs]
    if require_rs_1m_positive and "rs_1m_vs_nifty" in out:
        out = out[out["rs_1m_vs_nifty"].fillna(-1e9) > 0]
    if min_pct_of_52w_high is not None and "pct_of_52w_high" in out:
        out = out[out["pct_of_52w_high"].fillna(0) >= min_pct_of_52w_high]
    if min_minervini_score is not None and "minervini_score" in out:
        out = out[out["minervini_score"].fillna(0) >= min_minervini_score]
    if recent_listing is not None and "is_recent_listing" in out:
        # separate selection: True = IPO scan only, False = exclude IPOs
        out = out[out["is_recent_listing"].fillna(False) == bool(recent_listing)]
    return out.reset_index(drop=True)


RANKABLE = {
    "ret_1m": "ret_1m_rank",
    "rs_vs_nifty": "rs_rank",
    "oneil_score": "rs_rating",
}


def rerank(df: pd.DataFrame) -> pd.DataFrame:
    """
    Recompute the universe-relative ranks across whatever rows are present.

    Ranks are computed during the scan across the WHOLE universe. Filter to
    IPOs afterwards and a rank of 90 still means "top 10% of 500 mature
    stocks" — comparing a stock in price discovery against one with years of
    base-building, which is not a comparison at all.

    Call this after filtering so the ranks describe the surviving set.
    Columns with fewer than 2 valid values are left as NaN: a percentile over
    one row is meaningless.
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    for src, dst in RANKABLE.items():
        if src not in out.columns:
            continue
        valid = out[src].notna()
        if valid.sum() < 2:
            out[dst] = np.nan
            continue
        pct = out.loc[valid, src].rank(pct=True)
        out[dst] = np.nan
        out.loc[valid, dst] = (pct * 98 + 1).round(0) if dst == "rs_rating" else pct * 100.0
    return out


def filter_report(before: pd.DataFrame, **kw) -> pd.DataFrame:
    """
    Show what each criterion removes, one at a time, so nothing is applied
    silently. Returns a small summary frame.
    """
    if before is None or before.empty:
        return pd.DataFrame()
    steps, cur = [], before.copy()
    order = [
        ("price >= %s" % kw.get("min_price", 100), dict(min_price=kw.get("min_price", 100),
         min_avg_vol_10d=None, require_rs_positive=False, require_above_50dma=False, exclude_circuit=False)),
        ("avg_vol_10d >= %s" % kw.get("min_avg_vol_10d", 100000), dict(min_price=None,
         min_avg_vol_10d=kw.get("min_avg_vol_10d", 100000), require_rs_positive=False,
         require_above_50dma=False, exclude_circuit=False)),
        ("rs_vs_nifty > 0", dict(min_price=None, min_avg_vol_10d=None,
         require_rs_positive=True, require_above_50dma=False, exclude_circuit=False)),
        ("above 50 DMA", dict(min_price=None, min_avg_vol_10d=None,
         require_rs_positive=False, require_above_50dma=True, exclude_circuit=False)),
        ("not circuit-locked", dict(min_price=None, min_avg_vol_10d=None,
         require_rs_positive=False, require_above_50dma=False, exclude_circuit=True)),
    ]
    for label, params in order:
        n_before = len(cur)
        cur = apply_filters(cur, **params)
        steps.append({"filter": label, "before": n_before,
                      "after": len(cur), "removed": n_before - len(cur)})
    return pd.DataFrame(steps)


# ---------------------------------------------------------------------------
# The sequence, as one column
# ---------------------------------------------------------------------------
# Five questions asked of every surviving row, in the order a human would ask
# them. Each is a plain yes/no; the score is how many were yes.
#
# This is deliberately NOT a model. It is a checklist with a counter — the
# thresholds below are conventions, not fitted parameters, and no attempt is
# made to weight them. A 5/5 means "nothing obvious is wrong with it", which
# is a much weaker and much more honest claim than a probability.
#
# Order matters only for reading `setup_missing`. Gate 1 is first because it
# is the one that removes the most rows for the least thought.

SETUP_MIN_PRICE = 100.0            # gate 1
SETUP_MIN_VOL = 100_000.0          # gate 1
SETUP_LEADER_RET1M_RANK = 90.0     # gate 3, either measure clears it
SETUP_LEADER_RS_RATING = 80.0      # gate 3
SETUP_NEAR_HIGH_PCT = 75.0         # gate 4
SETUP_MAX_BREAKOUT_PCT = 3.0       # gate 5

SETUP_GATES = ("Liquid", "Sector", "Leader", "Near high", "Not extended")

SETUP_GATE_HELP = {
    "Liquid":       f"price ≥ ₹{SETUP_MIN_PRICE:,.0f} and 10-day average volume "
                    f"≥ {SETUP_MIN_VOL:,.0f} shares",
    "Sector":       "primary sector is one of the leading sectors for the "
                    "window chosen on the Sector Performance page",
    "Leader":       f"1-month rank ≥ {SETUP_LEADER_RET1M_RANK:.0f} OR "
                    f"RS Rating ≥ {SETUP_LEADER_RS_RATING:.0f}",
    "Near high":    f"trading at ≥ {SETUP_NEAR_HIGH_PCT:.0f}% of the 52-week high",
    "Not extended": f"price is ≤ {SETUP_MAX_BREAKOUT_PCT:.0f}% above the level "
                    f"it broke",
}


def _norm_sector(name: str) -> str:
    """`Nifty Auto (basket)` and `Nifty Auto` are the same sector."""
    return str(name or "").replace(" (basket)", "").strip().lower()


def setup_gates(row, leading: set) -> dict:
    """
    Evaluate the five gates for one row.

    Returns {gate: True | False | None}. None means "cannot be judged" — the
    input is missing, typically a sub-year listing that has no 52-week high.
    That is kept distinct from False on purpose: not knowing is not the same
    as failing, and collapsing the two is how a screen starts lying to you.
    """
    def num(col):
        v = row.get(col, np.nan)
        try:
            v = float(v)
        except (TypeError, ValueError):
            return np.nan
        return v

    g = {}

    # 1. Liquid — can this actually be traded?
    price = num("price")
    if not np.isfinite(price):
        price = num("close")
    vol = num("avg_vol_10d")
    if not np.isfinite(price) or not np.isfinite(vol):
        g["Liquid"] = None
    else:
        g["Liquid"] = bool(price >= SETUP_MIN_PRICE and vol >= SETUP_MIN_VOL)

    # 2. Sector — is the industry working?
    sec = _norm_sector(row.get("sector", ""))
    if not leading:
        g["Sector"] = None          # no sector data loaded; don't pretend
    elif not sec:
        g["Sector"] = None          # symbol outside the cached constituent lists
    else:
        g["Sector"] = sec in leading

    # 3. Leader — strong relative to everything else, on either horizon
    r1 = num("ret_1m_rank")
    rr = num("rs_rating")
    if not np.isfinite(r1) and not np.isfinite(rr):
        g["Leader"] = None
    else:
        g["Leader"] = bool(
            (np.isfinite(r1) and r1 >= SETUP_LEADER_RET1M_RANK) or
            (np.isfinite(rr) and rr >= SETUP_LEADER_RS_RATING))

    # 4. Near high — leader resuming, or bounce inside a downtrend?
    h = num("pct_of_52w_high")
    g["Near high"] = None if not np.isfinite(h) else bool(h >= SETUP_NEAR_HIGH_PCT)

    # 5. Not extended — is the move still available?
    b = num("breakout_pct")
    g["Not extended"] = None if not np.isfinite(b) else bool(
        abs(b) <= SETUP_MAX_BREAKOUT_PCT)

    return g


def add_setup(df: pd.DataFrame, leading_sectors=None) -> pd.DataFrame:
    """
    Add the sequence columns.

      setup_score    0-5, how many gates passed
      setup          "4/5" — score with the number judgeable, so a row where
                     one gate could not be evaluated reads "4/4", not "4/5"
      setup_grade    A (all pass) / B (one off) / C (two off) / D
      setup_missing  the gates that did not pass, with "(no data)" where the
                     input was absent rather than bad
      setup_flags    compact ✓/✗/· strip in gate order, for scanning a table

    `leading_sectors` is the list from sectors.leading_sectors(). Omit it and
    the Sector gate is reported as unjudgeable rather than silently passing.
    """
    if df is None or df.empty:
        return df

    leading = {_norm_sector(s) for s in (leading_sectors or []) if s}
    out = df.copy()

    scores, labels, grades, missing, flags, fails, judgeds = [], [], [], [], [], [], []
    for _, row in out.iterrows():
        g = setup_gates(row, leading)
        judged = [k for k in SETUP_GATES if g[k] is not None]
        passed = [k for k in judged if g[k]]
        failed = [k for k in judged if not g[k]]
        unknown = [k for k in SETUP_GATES if g[k] is None]

        scores.append(len(passed))
        judgeds.append(len(judged))
        fails.append(len(failed))
        labels.append(f"{len(passed)}/{len(judged)}" if judged else "—")

        # An A must mean all five were checked and all five passed. A row with
        # two unknown gates that passed the other three is not the same claim,
        # and grading it alongside a genuine 5/5 is how a checklist starts
        # flattering the rows it knows least about.
        if not judged:
            grades.append("D")
        elif len(failed) == 0 and not unknown:
            grades.append("A")
        elif len(failed) == 0 or (len(failed) == 1 and not unknown):
            grades.append("B")
        elif len(failed) <= 2:
            grades.append("C")
        else:
            grades.append("D")

        missing.append(", ".join(failed + [f"{u} (no data)" for u in unknown]))
        flags.append("".join("✓" if g[k] else "·" if g[k] is None else "✗"
                             for k in SETUP_GATES))

    out["setup_score"] = scores
    out["setup"] = labels
    out["setup_grade"] = grades
    out["setup_fails"] = fails
    out["setup_judged"] = judgeds
    out["setup_missing"] = missing
    out["setup_flags"] = flags
    return out


def shortlist_mask(df: pd.DataFrame, min_judged: int = 3) -> pd.Series:
    """
    Rows where nothing checkable failed and enough was checkable to mean it.

    Using `setup_score >= 4` instead would quietly exclude every recent
    listing, since two of the five gates need history an IPO does not have.
    Requiring zero failures with at least `min_judged` gates evaluated keeps
    those rows eligible without pretending the missing checks passed.
    """
    if df is None or df.empty or "setup_fails" not in df.columns:
        return pd.Series(dtype=bool)
    return (df["setup_fails"] == 0) & (df["setup_judged"] >= min_judged)


def thin_volume_artifacts(df: pd.DataFrame,
                          min_ratio: float = 3.0,
                          max_avg_vol: float = SETUP_MIN_VOL) -> pd.DataFrame:
    """
    Rows whose volume spike is large but whose normal volume is not.

    A stock averaging 2,000 shares that trades 100,000 on one order posts a
    50x volume ratio and sorts to the top of an unfiltered scan. Counting
    these per scan turns an anecdote into a measurement — if the count is
    usually zero, the liquidity gate is not earning its place.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    if "volume_ratio" not in df.columns or "avg_vol_10d" not in df.columns:
        return pd.DataFrame()
    m = (df["volume_ratio"].fillna(0) >= min_ratio) & \
        (df["avg_vol_10d"].fillna(0) < max_avg_vol)
    return df[m]
