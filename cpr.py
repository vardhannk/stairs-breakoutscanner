"""
Central Pivot Range (CPR) computation.

Reconstructed to satisfy the imports in `confluence_scanner.py`:

    from cpr import compute_cpr, levels_for_chart

CPR (Frank Ochoa / "PivotBoss") is a three-line floor-trader pivot construct.
The levels for period N are derived entirely from period N-1's High/Low/Close,
which is what makes them usable as *forward-looking* support/resistance: you
know tomorrow's CPR before tomorrow opens.

    Pivot (P)          = (H + L + C) / 3
    Bottom Central (BC)= (H + L) / 2
    Top Central (TC)   = 2P - BC          [ = P + (P - BC) ]

TC and BC are then ordered so TC >= BC (the raw formula inverts them whenever
the close sits below the bar's midpoint).

Width is the headline number:

    width_pct = (TC - BC) / P * 100

A *narrow* CPR (width below ~0.35%) means the prior session had little
value-area development -> the market is coiled -> a trending/breakout day is
more likely. A *wide* CPR implies balance and favours rangebound behaviour.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

import pandas as pd

try:
    from config import NARROW_CPR_PCT
except ImportError:  # standalone use
    NARROW_CPR_PCT = 0.35


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CPRLevels:
    """CPR + classic floor-trader pivots for one forward period."""

    pivot: float
    tc: float
    bc: float
    width: float
    width_pct: float

    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float

    # Source bar the levels were derived from
    src_high: float
    src_low: float
    src_close: float
    src_time: Optional[pd.Timestamp] = None

    @property
    def is_narrow(self) -> bool:
        return self.width_pct <= NARROW_CPR_PCT

    def classify_width(self, narrow_pct: float = NARROW_CPR_PCT) -> str:
        """'Narrow' (trend day likely) / 'Moderate' / 'Wide' (rangebound)."""
        if self.width_pct <= narrow_pct:
            return "Narrow"
        if self.width_pct <= narrow_pct * 2.0:
            return "Moderate"
        return "Wide"

    def position_of(self, price: float) -> str:
        """Where a price sits relative to the central range."""
        if price > self.tc:
            return "Above TC"
        if price < self.bc:
            return "Below BC"
        return "Inside CPR"

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Core computation
# --------------------------------------------------------------------------
_OHLCV_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}


def _resample(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate daily bars up to a higher timeframe (W-FRI, ME, ...)."""
    agg = {k: v for k, v in _OHLCV_AGG.items() if k in frame.columns}
    return frame.resample(rule).agg(agg).dropna(subset=["close"])


def _cpr_from_hlc(high: float, low: float, close: float) -> tuple[float, float, float]:
    """Return (pivot, tc, bc) from a single bar's H/L/C, TC always >= BC."""
    pivot = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc = 2.0 * pivot - bc
    if tc < bc:  # raw formula inverts when close < bar midpoint
        tc, bc = bc, tc
    return pivot, tc, bc


def compute_cpr(
    df: pd.DataFrame,
    *,
    timeframe: str = "Daily",
    resample: bool = True,
) -> Optional[CPRLevels]:
    """
    Compute the CPR for the *next* period from `df`'s last completed bar.

    Args:
        df: OHLC(V) frame, DatetimeIndex, lowercase columns
            (open/high/low/close/volume). Daily bars expected.
        timeframe: "Daily" | "Weekly" | "Monthly". Anything other than Daily
            resamples the daily frame up before taking the last bar.
        resample: set False if `df` is already at the target timeframe.

    Returns:
        CPRLevels, or None if there is not enough data.
    """
    if df is None or df.empty:
        return None

    frame = df.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    required = {"high", "low", "close"}
    if not required.issubset(frame.columns):
        return None

    frame = frame.dropna(subset=["high", "low", "close"]).sort_index()
    if frame.empty:
        return None

    tf = (timeframe or "Daily").strip().lower()
    if resample and tf in ("weekly", "1w", "w"):
        frame = _resample(frame, "W-FRI")
    elif resample and tf in ("monthly", "1m", "m"):
        frame = _resample(frame, "ME")

    if frame.empty:
        return None

    # Use the last COMPLETED bar. The in-progress bar's H/L/C keep changing,
    # so deriving levels from it would repaint them intraday.
    src = frame.iloc[-1]

    high = float(src["high"])
    low = float(src["low"])
    close = float(src["close"])

    pivot, tc, bc = _cpr_from_hlc(high, low, close)
    if pivot <= 0:
        return None

    rng = high - low
    width = tc - bc

    return CPRLevels(
        pivot=pivot,
        tc=tc,
        bc=bc,
        width=width,
        width_pct=width / pivot * 100.0,
        r1=2.0 * pivot - low,
        r2=pivot + rng,
        r3=high + 2.0 * (pivot - low),
        s1=2.0 * pivot - high,
        s2=pivot - rng,
        s3=low - 2.0 * (high - pivot),
        src_high=high,
        src_low=low,
        src_close=close,
        src_time=frame.index[-1] if len(frame.index) else None,
    )


def compute_cpr_series(df: pd.DataFrame) -> pd.DataFrame:
    """
    Vectorised CPR for every bar, shifted forward one period.

    Row i holds the CPR that applies *during* bar i, derived from bar i-1.
    Useful for backtesting and for plotting historical CPR bands.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    frame = df.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    if not {"high", "low", "close"}.issubset(frame.columns):
        return pd.DataFrame()

    high = frame["high"].astype(float).shift(1)
    low = frame["low"].astype(float).shift(1)
    close = frame["close"].astype(float).shift(1)

    pivot = (high + low + close) / 3.0
    bc_raw = (high + low) / 2.0
    tc_raw = 2.0 * pivot - bc_raw

    tc = tc_raw.combine(bc_raw, max)
    bc = tc_raw.combine(bc_raw, min)
    rng = high - low

    out = pd.DataFrame(
        {
            "pivot": pivot,
            "tc": tc,
            "bc": bc,
            "width": tc - bc,
            "width_pct": (tc - bc) / pivot * 100.0,
            "r1": 2.0 * pivot - low,
            "r2": pivot + rng,
            "r3": high + 2.0 * (pivot - low),
            "s1": 2.0 * pivot - high,
            "s2": pivot - rng,
            "s3": low - 2.0 * (high - pivot),
        },
        index=frame.index,
    )
    return out.dropna(subset=["pivot"])


# --------------------------------------------------------------------------
# Derived signals
# --------------------------------------------------------------------------
def cpr_trend(df: pd.DataFrame, lookback: int = 2) -> str:
    """
    Compare consecutive CPRs: 'Rising' / 'Falling' / 'Overlapping'.

    Higher-value CPRs stacked upward = bullish continuation; overlapping CPRs
    = sideways / no directional edge.
    """
    series = compute_cpr_series(df)
    if len(series) < lookback + 1:
        return "Unknown"

    cur = series.iloc[-1]
    prev = series.iloc[-1 - lookback]

    if cur["bc"] > prev["tc"]:
        return "Rising"
    if cur["tc"] < prev["bc"]:
        return "Falling"
    return "Overlapping"


def is_virgin_cpr(df: pd.DataFrame, levels: CPRLevels) -> bool:
    """
    True if price never traded into the CPR band during the current period.

    An untouched ("virgin") CPR acts as a strong magnet/reversal zone the next
    time price reaches it.
    """
    if df is None or df.empty or levels is None:
        return False
    frame = df.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    if not {"high", "low"}.issubset(frame.columns):
        return False
    last = frame.iloc[-1]
    return not (float(last["low"]) <= levels.tc and float(last["high"]) >= levels.bc)


# --------------------------------------------------------------------------
# Charting helper
# --------------------------------------------------------------------------
def levels_for_chart(
    levels: CPRLevels,
    *,
    include_pivots: bool = True,
) -> list[dict]:
    """
    Flatten CPRLevels into horizontal-line specs for a plotting layer.

    Returns a list of dicts: {"label", "value", "color", "dash", "group"} —
    ready to feed Plotly `add_hline` / mplfinance `hlines`.
    """
    if levels is None:
        return []

    lines = [
        {"label": "TC", "value": levels.tc, "color": "#f59e0b", "dash": "solid", "group": "cpr"},
        {"label": "Pivot", "value": levels.pivot, "color": "#3b82f6", "dash": "solid", "group": "cpr"},
        {"label": "BC", "value": levels.bc, "color": "#f59e0b", "dash": "solid", "group": "cpr"},
    ]

    if include_pivots:
        lines += [
            {"label": "R3", "value": levels.r3, "color": "#ef4444", "dash": "dot", "group": "resistance"},
            {"label": "R2", "value": levels.r2, "color": "#ef4444", "dash": "dash", "group": "resistance"},
            {"label": "R1", "value": levels.r1, "color": "#ef4444", "dash": "dash", "group": "resistance"},
            {"label": "S1", "value": levels.s1, "color": "#22c55e", "dash": "dash", "group": "support"},
            {"label": "S2", "value": levels.s2, "color": "#22c55e", "dash": "dash", "group": "support"},
            {"label": "S3", "value": levels.s3, "color": "#22c55e", "dash": "dot", "group": "support"},
        ]

    return sorted(lines, key=lambda d: d["value"], reverse=True)
