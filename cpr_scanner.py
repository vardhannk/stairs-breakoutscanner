"""
CPR scanner — per-symbol and universe-wide.

Reconstructed to satisfy the import in `confluence_scanner.py`:

    from cpr_scanner import scan_symbol as scan_cpr_symbol
    ...
    cpr_daily = scan_cpr_symbol(sym, df, timeframe="Daily", use_cache=use_cache)
    cpr_daily.width_pct   # float, CPR width as % of pivot
    cpr_daily.status      # str, price position vs the CPR band
    cpr_daily.ltp         # float, last traded price

Mirrors the shape of `scanner.py` in the breakout project so the two can be
composed (see confluence_scanner.py).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

import pandas as pd

from cpr import CPRLevels, compute_cpr, cpr_trend, is_virgin_cpr

try:
    from config import NARROW_CPR_PCT
except ImportError:
    NARROW_CPR_PCT = 0.35

try:
    from data_loader import load_daily
except ImportError:  # standalone use
    load_daily = None


@dataclass
class CPRResult:
    """One symbol/timeframe CPR snapshot."""

    symbol: str
    timeframe: str          # "Daily" | "Weekly" | "Monthly"

    ltp: float              # last traded price
    pivot: float
    tc: float
    bc: float
    width: float
    width_pct: float        # <- the field confluence_scanner filters on

    status: str             # "Above TC" | "Inside CPR" | "Below BC"
    width_class: str        # "Narrow" | "Moderate" | "Wide"
    trend: str              # "Rising" | "Falling" | "Overlapping"
    virgin: bool

    r1: float
    r2: float
    s1: float
    s2: float

    bar_time: Optional[date] = None

    @property
    def is_narrow(self) -> bool:
        return self.width_pct <= NARROW_CPR_PCT

    @property
    def bias(self) -> str:
        """Coarse directional read combining position and width."""
        if self.width_class != "Narrow":
            return "Neutral"
        if self.status == "Above TC":
            return "Bullish"
        if self.status == "Below BC":
            return "Bearish"
        return "Breakout pending"


def result_to_row(result: CPRResult) -> dict:
    return {
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "ltp": round(result.ltp, 2),
        "pivot": round(result.pivot, 2),
        "tc": round(result.tc, 2),
        "bc": round(result.bc, 2),
        "width_pct": round(result.width_pct, 3),
        "width_class": result.width_class,
        "status": result.status,
        "trend": result.trend,
        "virgin": result.virgin,
        "bias": result.bias,
        "r1": round(result.r1, 2),
        "s1": round(result.s1, 2),
        "bar_time": result.bar_time,
    }


# --------------------------------------------------------------------------
# Single symbol
# --------------------------------------------------------------------------
def scan_symbol(
    symbol: str,
    df: Optional[pd.DataFrame] = None,
    *,
    timeframe: str = "Daily",
    use_cache: bool = True,
    days: int = 90,
    narrow_pct: float = NARROW_CPR_PCT,
    only_narrow: bool = False,
) -> Optional[CPRResult]:
    """
    Compute the CPR snapshot for one symbol.

    Args:
        symbol: NSE symbol, e.g. "RELIANCE".
        df: pre-loaded daily OHLCV. Fetched via data_loader.load_daily if None.
        timeframe: "Daily" | "Weekly" | "Monthly".
        use_cache: passed through to the loader.
        days: history to request when df is None. Weekly/Monthly need more.
        narrow_pct: width threshold for the "Narrow" classification.
        only_narrow: return None unless the CPR is narrow.

    Returns:
        CPRResult, or None if data is insufficient.
    """
    sym = symbol.upper().strip()
    tf = (timeframe or "Daily").strip().title()

    if df is None:
        if load_daily is None:
            return None
        need = {"Daily": days, "Weekly": max(days, 200), "Monthly": max(days, 800)}
        df = load_daily(sym, days=need.get(tf, days), use_cache=use_cache)

    if df is None or df.empty:
        return None

    frame = df.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    if "close" not in frame.columns:
        return None

    levels = compute_cpr(frame, timeframe=tf)
    if levels is None:
        return None

    ltp = float(frame["close"].dropna().iloc[-1])
    width_class = levels.classify_width(narrow_pct)

    if only_narrow and width_class != "Narrow":
        return None

    idx = frame.index[-1]
    bar_time = idx.date() if hasattr(idx, "date") else None

    return CPRResult(
        symbol=sym,
        timeframe=tf,
        ltp=ltp,
        pivot=levels.pivot,
        tc=levels.tc,
        bc=levels.bc,
        width=levels.width,
        width_pct=levels.width_pct,
        status=levels.position_of(ltp),
        width_class=width_class,
        trend=cpr_trend(frame),
        virgin=is_virgin_cpr(frame, levels),
        r1=levels.r1,
        r2=levels.r2,
        s1=levels.s1,
        s2=levels.s2,
        bar_time=bar_time,
    )


# --------------------------------------------------------------------------
# Universe
# --------------------------------------------------------------------------
def scan_universe(
    symbols: list[str],
    timeframes: list[str] | tuple[str, ...] = ("Daily",),
    *,
    use_cache: bool = True,
    narrow_pct: float = NARROW_CPR_PCT,
    only_narrow: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    max_workers: int = 8,
) -> pd.DataFrame:
    """Scan many symbols across one or more CPR timeframes, sorted by width."""
    symbols = [s.upper().strip() for s in symbols if s and str(s).strip()]
    rows: list[dict] = []
    total = len(symbols) * len(timeframes)
    done = 0

    def _work(sym: str, tf: str):
        return sym, tf, scan_symbol(
            sym,
            timeframe=tf,
            use_cache=use_cache,
            narrow_pct=narrow_pct,
            only_narrow=only_narrow,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_work, s, tf) for s in symbols for tf in timeframes]
        for fut in as_completed(futures):
            sym, tf, res = fut.result()
            done += 1
            if progress_callback:
                progress_callback(done, total, f"{sym} ({tf})")
            if res is not None:
                rows.append(result_to_row(res))

    if not rows:
        return pd.DataFrame()

    return (
        pd.DataFrame(rows)
        .sort_values(["timeframe", "width_pct"], ascending=[True, True])
        .reset_index(drop=True)
    )


def filter_results(
    df: pd.DataFrame,
    *,
    max_width_pct: Optional[float] = None,
    statuses: Optional[list[str]] = None,
    only_virgin: bool = False,
) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    if max_width_pct is not None:
        out = out[out["width_pct"] <= max_width_pct]
    if statuses:
        out = out[out["status"].isin(statuses)]
    if only_virgin and "virgin" in out.columns:
        out = out[out["virgin"]]
    return out.reset_index(drop=True)
