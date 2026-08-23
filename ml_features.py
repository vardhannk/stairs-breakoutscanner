"""
ml_features.py — single source of truth for breakout ML features.

WHY THIS EXISTS
---------------
Upstream `ml_engine.py` computes one feature differently in training vs
inference:

    training  (extract_historical_breakouts):
        tr = max(H-L, |H-prev_close|, |L-prev_close|)      # true range

    inference (predict_confidence):
        tr_val = close.diff().abs()                        # NOT true range
        #        ^ the upstream comment even calls it "an approximation"

A model trained on one distribution and scored on another produces numbers
that look like probabilities but aren't. Both paths now import
`compute_features` from here, so they cannot drift apart again.

FEATURES (order is significant — the model is fitted on this exact order)
    rsi            Wilder-style RSI(14), 0-100
    volume_ratio   volume / SMA(volume, 20)
    ema_dist       (close - EMA20) / EMA20
    tr_atr_ratio   true_range / ATR(14)
    return_10d     10-bar percentage change
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

FEATURE_COLS = ["rsi", "volume_ratio", "ema_dist", "tr_atr_ratio", "return_10d"]

RSI_PERIOD = 14
ATR_PERIOD = 14
EMA_SPAN = 20
VOL_WINDOW = 20
RET_WINDOW = 10

# Minimum bars before features are trustworthy (longest warmup + margin)
MIN_BARS = 40


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase columns, flatten a yfinance MultiIndex, coerce numerics."""
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)
    out.columns = [str(c).lower() for c in out.columns]
    out = out.loc[:, ~out.columns.duplicated()]
    for c in ("open", "high", "low", "close", "volume"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    """Real true range, including gaps. Used by BOTH train and serve."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    return true_range(df).rolling(period).mean()


def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """
    Wilder RSI. Note the upstream version fills warmup NaN with 50 and also
    returns 50 when there are no losses (rs = inf) — which should be 100.
    Handled correctly here; warmup stays NaN so callers can drop it.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    out[avg_loss == 0] = 100.0     # no losses -> maximally overbought
    out[avg_gain == 0] = 0.0       # no gains  -> maximally oversold
    return out


def ema(series: pd.Series, span: int = EMA_SPAN) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


# ---------------------------------------------------------------------------
# Feature frame
# ---------------------------------------------------------------------------
def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return a DataFrame indexed like `df` with exactly FEATURE_COLS.

    Warmup rows are left as NaN rather than filled — filling them with 0
    (as upstream does) makes an untrained MACD/RSI look like a real extreme
    reading. Callers drop or slice as appropriate.
    """
    d = normalise(df)
    if not {"high", "low", "close"}.issubset(d.columns):
        return pd.DataFrame(columns=FEATURE_COLS)

    close = d["close"].astype(float)
    volume = (
        d["volume"].astype(float)
        if "volume" in d.columns
        else pd.Series(np.nan, index=d.index)
    )

    atr_s = atr(d, ATR_PERIOD)
    ema_s = ema(close, EMA_SPAN)
    avg_vol = volume.rolling(VOL_WINDOW).mean()

    feats = pd.DataFrame(index=d.index)
    feats["rsi"] = rsi(close, RSI_PERIOD)
    feats["volume_ratio"] = volume / avg_vol.replace(0, np.nan)
    feats["ema_dist"] = (close - ema_s) / ema_s.replace(0, np.nan)
    feats["tr_atr_ratio"] = true_range(d) / atr_s.replace(0, np.nan)
    feats["return_10d"] = close.pct_change(RET_WINDOW)

    return feats[FEATURE_COLS].replace([np.inf, -np.inf], np.nan)


def latest_features(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """
    One-row frame for the most recent bar — what inference should use.

    Returns None if there is not enough history or the row is incomplete,
    so callers can decline to predict rather than guess.
    """
    d = normalise(df)
    if d is None or d.empty or len(d) < MIN_BARS:
        return None
    feats = compute_features(d)
    if feats.empty:
        return None
    row = feats.iloc[[-1]]
    if row.isna().any(axis=1).iloc[0]:
        return None
    return row


# ---------------------------------------------------------------------------
# Labelling
# ---------------------------------------------------------------------------
def label_outcome(
    close: pd.Series,
    idx: int,
    direction: str,
    horizon: int = 10,
    target: float = 0.03,
    stop: float = 0.025,
) -> Optional[int]:
    """
    1 if the trade reached `target` before `stop` within `horizon` bars, else 0.
    None if there is not enough forward data to decide — such rows must be
    dropped, never defaulted to 0.
    """
    fut = close.iloc[idx + 1: idx + 1 + horizon]
    if len(fut) < horizon:
        return None
    entry = float(close.iloc[idx])
    if direction == "bullish":
        up, dn = entry * (1 + target), entry * (1 - stop)
        for p in fut:
            if p >= up:
                return 1
            if p <= dn:
                return 0
    else:
        dn, up = entry * (1 - target), entry * (1 + stop)
        for p in fut:
            if p <= dn:
                return 1
            if p >= up:
                return 0
    return 0  # neither level hit within the horizon = not a win
