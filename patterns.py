"""
patterns.py — chart-pattern detectors, as pure functions over OHLCV.

Every detector takes a DataFrame indexed by date with columns
open/high/low/close/volume and returns plain scalars. No I/O, no globals, no
Streamlit, no database. That makes each one testable against a synthetic
series that exhibits the pattern by construction, which is the only honest way
to check a pattern detector — real charts are ambiguous and you end up
confirming what you already believed.

`compute_patterns()` returns one flat dict per symbol. That dict IS the row
written into the indicator snapshot table, so a scan becomes a WHERE clause
instead of a per-request computation.

DESIGN NOTE — why thresholds are module constants
-------------------------------------------------
Every number below is a convention with a citation in trading literature, not
a fitted parameter. None of them were optimised against returns, and none
should be until there is a backtest harness that can measure whether a change
helps out-of-sample. Naming them here at least makes them arguable.

A detector returning True means "this shape is present", never "this will go
up". The two get conflated constantly and it is the main way pattern screeners
mislead people.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ── conventions ────────────────────────────────────────────────────────────
SWING_WINDOW = 5          # bars either side that define a pivot
LEVEL_TOLERANCE = 0.015   # 1.5% — how close two highs must be to be one level
LEVEL_MIN_TOUCHES = 3     # a level nobody respected 3x is not a level
LEVEL_LOOKBACK = 250      # ~1 year of daily bars

VCP_MIN_CONTRACTIONS = 2
VCP_MAX_CONTRACTIONS = 6
VCP_TIGHTENING = 0.75     # each pullback ≤75% of the previous one
VCP_MAX_DEPTH = 0.35      # a 50% drawdown is a downtrend, not a base
VCP_LOOKBACK = 120
# The FINAL contraction is what makes it a VCP. Minervini's last pullback is
# typically 3-10%; allowing 35% turns "two pullbacks, the second smaller" into
# the whole test, which fires on roughly a quarter of any random universe.
VCP_MAX_FINAL_DEPTH = 0.15
# Supply drying up is half the pattern's meaning, not a decoration. It was
# computed and then ignored — requiring it is what separates a real
# contraction from price simply wandering less.
VCP_REQUIRE_VOL_DRYUP = True

TIGHT_RANGE_PCT = 0.03    # bar range under 3% of price
TIGHT_RANGE_BARS = 5      # sustained over this many bars

FLAG_POLE_MIN = 0.15      # 15% advance
FLAG_POLE_BARS = 15
FLAG_MAX_BARS = 25
FLAG_MAX_RETRACE = 0.50   # deeper than half the pole is not a flag
# The defining geometry, and the condition an earlier version was missing:
# a flag is TIGHT RELATIVE TO ITS POLE. Without this the detector scans every
# consolidation length from 4 to 25 bars, takes the best, and fires on a
# quarter of a random-walk universe — twenty-two chances to find a shape.
FLAG_MAX_RANGE_OF_POLE = 0.40

SHAKEOUT_VOL_MULT = 1.2   # shakeouts happen on conviction, not on quiet days
GAP_MIN_PCT = 0.02        # below 2% is noise, not a gap
RS_HIGH_WINDOW = 63       # ~3 months


# ── helpers ────────────────────────────────────────────────────────────────
def _ok(df: pd.DataFrame, need: int = 30) -> bool:
    return (df is not None and not df.empty and len(df) >= need
            and {"open", "high", "low", "close"}.issubset(df.columns))


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def swing_points(df: pd.DataFrame, window: int = SWING_WINDOW):
    """
    Pivot highs and lows: a bar whose high is the max of the surrounding
    2*window+1 bars. Returns (high_idx, low_idx) as positional arrays.

    Deliberately NOT causal-safe at the right edge — the last `window` bars
    cannot be confirmed as pivots yet. Callers doing historical backtests must
    account for that or they will look ahead. Live scanning is unaffected
    because an unconfirmed pivot simply is not reported.
    """
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    hi, lo = [], []
    for i in range(window, n - window):
        seg_h = h[i - window:i + window + 1]
        seg_l = l[i - window:i + window + 1]
        if h[i] == seg_h.max() and (seg_h.argmax() == window):
            hi.append(i)
        if l[i] == seg_l.min() and (seg_l.argmin() == window):
            lo.append(i)
    return np.array(hi, dtype=int), np.array(lo, dtype=int)


# ── horizontal resistance ──────────────────────────────────────────────────
def horizontal_levels(df: pd.DataFrame, lookback: int = LEVEL_LOOKBACK,
                      tol: float = LEVEL_TOLERANCE,
                      min_touches: int = LEVEL_MIN_TOUCHES) -> dict:
    """
    Cluster swing highs into price levels; report the nearest one overhead.

    Clustering by relative distance rather than absolute rupees, because a
    ₹20 gap means something entirely different on a ₹150 stock than on a
    ₹4,000 one.
    """
    out = {"resistance": np.nan, "resistance_touches": 0,
           "pct_to_resistance": np.nan, "n_levels": 0}
    if not _ok(df, 40):
        return out

    d = df.iloc[-lookback:]
    hi_idx, _ = swing_points(d)
    if len(hi_idx) < min_touches:
        return out

    highs = np.sort(d["high"].to_numpy()[hi_idx])
    clusters, cur = [], [highs[0]]
    for p in highs[1:]:
        if abs(p - np.mean(cur)) / np.mean(cur) <= tol:
            cur.append(p)
        else:
            clusters.append(cur)
            cur = [p]
    clusters.append(cur)

    levels = [(float(np.mean(c)), len(c)) for c in clusters if len(c) >= min_touches]
    out["n_levels"] = len(levels)
    if not levels:
        return out

    close = float(d["close"].iloc[-1])
    above = [(lv, t) for lv, t in levels if lv > close]
    if not above:
        return out
    lv, touches = min(above, key=lambda x: x[0] - close)
    out.update({"resistance": lv, "resistance_touches": touches,
                "pct_to_resistance": (lv / close - 1.0) * 100.0})
    return out


# ── volatility contraction ─────────────────────────────────────────────────
def detect_vcp(df: pd.DataFrame, lookback: int = VCP_LOOKBACK) -> dict:
    """
    Successive pullbacks, each shallower than the last, on falling volume.

    Measured peak-to-trough between confirmed pivots rather than by fitting a
    wedge: the defining property is that supply is drying up, which shows in
    the DEPTH sequence and in volume, not in trendline geometry.
    """
    out = {"is_vcp": 0, "vcp_contractions": 0, "vcp_last_depth": np.nan,
           "vcp_vol_dryup": 0}
    if not _ok(df, 60):
        return out

    d = df.iloc[-lookback:]
    hi_idx, lo_idx = swing_points(d)
    if len(hi_idx) < 2 or len(lo_idx) < 2:
        return out

    piv = sorted([(i, "H") for i in hi_idx] + [(i, "L") for i in lo_idx])
    depths, seq = [], []
    for (i, k), (j, k2) in zip(piv, piv[1:]):
        if k == "H" and k2 == "L":
            top, bot = float(d["high"].iloc[i]), float(d["low"].iloc[j])
            if top > 0:
                depths.append((top - bot) / top)
                seq.append((i, j))
    if len(depths) < VCP_MIN_CONTRACTIONS:
        return out

    # walk backwards: keep the run of progressively tightening pullbacks
    run = [depths[-1]]
    for prev in reversed(depths[:-1]):
        if run[-1] <= prev * VCP_TIGHTENING and prev <= VCP_MAX_DEPTH:
            run.append(prev)
        else:
            break
    n = len(run)
    out["vcp_contractions"] = min(n, VCP_MAX_CONTRACTIONS)
    out["vcp_last_depth"] = float(depths[-1] * 100.0)

    if "volume" in d.columns and len(seq) >= 2:
        a = float(d["volume"].iloc[seq[-1][0]:seq[-1][1] + 1].mean())
        b = float(d["volume"].iloc[seq[-2][0]:seq[-2][1] + 1].mean())
        out["vcp_vol_dryup"] = int(np.isfinite(a) and np.isfinite(b) and a < b)

    out["is_vcp"] = int(
        n >= VCP_MIN_CONTRACTIONS
        and depths[-1] <= VCP_MAX_FINAL_DEPTH
        and (out["vcp_vol_dryup"] == 1 or not VCP_REQUIRE_VOL_DRYUP))
    return out


# ── inside bars ────────────────────────────────────────────────────────────
def inside_bar(df: pd.DataFrame) -> dict:
    """Last bar's range wholly inside the prior bar's. Weekly = resampled."""
    out = {"is_inside_bar_d": 0, "is_inside_bar_w": 0}
    if not _ok(df, 3):
        return out
    h, l = df["high"], df["low"]
    out["is_inside_bar_d"] = int(h.iloc[-1] <= h.iloc[-2] and l.iloc[-1] >= l.iloc[-2])

    try:
        w = df.resample("W-FRI").agg({"open": "first", "high": "max",
                                      "low": "min", "close": "last"}).dropna()
        if len(w) >= 2:
            out["is_inside_bar_w"] = int(w["high"].iloc[-1] <= w["high"].iloc[-2]
                                         and w["low"].iloc[-1] >= w["low"].iloc[-2])
    except (TypeError, ValueError):
        pass                       # non-datetime index; daily result still valid
    return out


# ── shakeouts ──────────────────────────────────────────────────────────────
def shakeouts(df: pd.DataFrame) -> dict:
    """
    Price pierced a moving average intraday and closed back above it.

    The volume condition matters: without it this fires on every quiet drift
    across a flat average, which is not a shakeout — it is noise. A shakeout
    is supply being flushed, and that leaves a volume signature.
    """
    out = {f"shakeout_{p}ema": 0 for p in (10, 21, 50, 200)}
    if not _ok(df, 30):
        return out
    close, low, vol = df["close"], df["low"], df.get("volume")
    for p in (10, 21, 50, 200):
        if len(df) < p + 5:
            continue
        e = _ema(close, p)
        pierced = low.iloc[-1] < e.iloc[-1]
        recovered = close.iloc[-1] > e.iloc[-1]
        was_above = close.iloc[-2] > e.iloc[-2]
        conviction = True
        if vol is not None and len(vol) > 20:
            avg = float(vol.iloc[-21:-1].mean())
            conviction = avg > 0 and float(vol.iloc[-1]) >= avg * SHAKEOUT_VOL_MULT
        out[f"shakeout_{p}ema"] = int(pierced and recovered and was_above and conviction)
    return out


# ── flags and pennants ─────────────────────────────────────────────────────
def flag_pennant(df: pd.DataFrame) -> dict:
    """
    A sharp pole, then a shallow drift whose range narrows (pennant) or
    holds roughly parallel (flag).

    Separated by whether the consolidation's range is CONTRACTING, which is
    the only property that reliably distinguishes the two on real data.
    """
    out = {"is_flag": 0, "is_pennant": 0, "pole_pct": np.nan,
           "flag_bars": 0, "flag_retrace": np.nan}
    if not _ok(df, FLAG_POLE_BARS + 8):
        return out

    close, high, low = df["close"], df["high"], df["low"]
    best = None
    for cons in range(4, min(FLAG_MAX_BARS, len(df) - FLAG_POLE_BARS - 1)):
        cons_slice = slice(len(df) - cons, len(df))
        pole_end = len(df) - cons
        pole_start = max(0, pole_end - FLAG_POLE_BARS)
        p0 = float(close.iloc[pole_start])
        p1 = float(close.iloc[pole_end - 1])
        if p0 <= 0:
            continue
        pole = (p1 - p0) / p0
        if pole < FLAG_POLE_MIN:
            continue
        hi = float(high.iloc[cons_slice].max())
        lo = float(low.iloc[cons_slice].min())
        retrace = (p1 - lo) / (p1 - p0) if p1 > p0 else 1.0
        if retrace > FLAG_MAX_RETRACE or retrace < 0:
            continue
        # tight relative to the pole, or it is just a drift after a rise
        if (hi - lo) > (p1 - p0) * FLAG_MAX_RANGE_OF_POLE:
            continue
        first = float(high.iloc[cons_slice][:cons // 2].max()
                      - low.iloc[cons_slice][:cons // 2].min())
        second = float(high.iloc[cons_slice][cons // 2:].max()
                       - low.iloc[cons_slice][cons // 2:].min())
        contracting = second < first * 0.75
        cand = {"pole_pct": pole * 100.0, "flag_bars": cons,
                "flag_retrace": retrace * 100.0,
                "is_pennant": int(contracting), "is_flag": int(not contracting)}
        if best is None or cand["pole_pct"] > best["pole_pct"]:
            best = cand
    if best:
        out.update(best)
    return out


# ── gaps ───────────────────────────────────────────────────────────────────
def gaps(df: pd.DataFrame, min_pct: float = GAP_MIN_PCT) -> dict:
    """
    Most recent significant gap, and whether price has since traded back
    through it. An unfilled gap overhead is resistance; below, support.
    """
    out = {"gap_open_pct": np.nan, "gap_unfilled": 0, "gap_age_bars": 0,
           "gap_direction": ""}
    if not _ok(df, 5):
        return out
    o, c, h, l = df["open"], df["close"], df["high"], df["low"]
    for k in range(len(df) - 1, 0, -1):
        up = (o.iloc[k] - c.iloc[k - 1]) / c.iloc[k - 1]
        if abs(up) < min_pct:
            continue
        after = df.iloc[k + 1:]
        if up > 0:
            filled = bool((after["low"] <= c.iloc[k - 1]).any()) if len(after) else False
        else:
            filled = bool((after["high"] >= c.iloc[k - 1]).any()) if len(after) else False
        out.update({"gap_open_pct": float(up * 100.0),
                    "gap_unfilled": int(not filled),
                    "gap_age_bars": int(len(df) - 1 - k),
                    "gap_direction": "up" if up > 0 else "down"})
        break
    return out


# ── tight range ────────────────────────────────────────────────────────────
def tight_range(df: pd.DataFrame) -> dict:
    out = {"tight_range_d": 0, "range_pct_5d": np.nan}
    if not _ok(df, TIGHT_RANGE_BARS + 1):
        return out
    d = df.iloc[-TIGHT_RANGE_BARS:]
    close = float(d["close"].iloc[-1])
    if close <= 0:
        return out
    rng = (float(d["high"].max()) - float(d["low"].min())) / close
    out["range_pct_5d"] = rng * 100.0
    out["tight_range_d"] = int(rng <= TIGHT_RANGE_PCT)
    return out


# ── RS line new high before price ──────────────────────────────────────────
def rs_line_new_high(df: pd.DataFrame, index_df: pd.DataFrame,
                     window: int = RS_HIGH_WINDOW) -> dict:
    """
    The relative-strength line makes a new high while price has not.

    RS line = close / index_close. When it leads price to a new high, the
    stock is outperforming during a market pullback — which is a different
    and earlier signal than price strength alone. Cheap to compute and
    genuinely differentiated; most screeners do not carry it.
    """
    out = {"rs_line_new_high": 0, "price_new_high": 0, "rs_leads_price": 0}
    if not _ok(df, window + 5) or index_df is None or index_df.empty:
        return out
    idx = index_df["close"].reindex(df.index).ffill()
    rs = (df["close"] / idx).dropna()
    if len(rs) < window + 1:
        return out
    w = min(window, len(rs) - 1)
    out["rs_line_new_high"] = int(rs.iloc[-1] >= rs.iloc[-w - 1:].max())
    p = df["close"].iloc[-w - 1:]
    out["price_new_high"] = int(df["close"].iloc[-1] >= p.max())
    out["rs_leads_price"] = int(out["rs_line_new_high"] and not out["price_new_high"])
    return out


# ── one row per symbol ─────────────────────────────────────────────────────
def compute_patterns(df: pd.DataFrame,
                     index_df: pd.DataFrame | None = None) -> dict:
    """
    Every detector, flattened into the dict that becomes one snapshot row.

    Each detector is wrapped: a failure on one symbol's odd data must not
    lose the other twenty columns. A NaN is a missing measurement; a crashed
    nightly job is a missing day for every symbol.
    """
    row: dict = {}
    for fn, args in ((horizontal_levels, (df,)), (detect_vcp, (df,)),
                     (inside_bar, (df,)), (shakeouts, (df,)),
                     (flag_pennant, (df,)), (gaps, (df,)), (tight_range, (df,))):
        try:
            row.update(fn(*args))
        except Exception:
            pass
    try:
        row.update(rs_line_new_high(df, index_df))
    except Exception:
        pass
    return row


PATTERN_COLUMNS = [
    "resistance", "resistance_touches", "pct_to_resistance", "n_levels",
    "is_vcp", "vcp_contractions", "vcp_last_depth", "vcp_vol_dryup",
    "is_inside_bar_d", "is_inside_bar_w",
    "shakeout_10ema", "shakeout_21ema", "shakeout_50ema", "shakeout_200ema",
    "is_flag", "is_pennant", "pole_pct", "flag_bars", "flag_retrace",
    "gap_open_pct", "gap_unfilled", "gap_age_bars", "gap_direction",
    "tight_range_d", "range_pct_5d",
    "rs_line_new_high", "price_new_high", "rs_leads_price",
]
