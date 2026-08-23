"""Configuration for NIFTY 500 multi-timeframe breakout scanner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data_cache"
CACHE_DAILY = DATA_DIR / "prices_daily"
CACHE_HOURLY = DATA_DIR / "prices_hourly"

# Optional sibling caches (105-stockdna)
STOCKDNA_UNIVERSE = ROOT_DIR.parent / "105-stockdna" / "data_cache" / "nifty500_symbols.csv"

YFINANCE_SUFFIX = ".NS"
NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
NIFTY50_URL = "https://archives.nseindia.com/content/indices/ind_nifty50list.csv"
UNIVERSE_CACHE = DATA_DIR / "nifty500_symbols.csv"
NIFTY50_CACHE = DATA_DIR / "nifty50_symbols.csv"
FNO_CACHE = DATA_DIR / "fno_symbols.csv"
FNO_CACHE_SIBLING = ROOT_DIR.parent / "107-CPRScanner" / "data_cache" / "fno_symbols.csv"
NIFTY50_CACHE_SIBLING = ROOT_DIR.parent / "015-NIFTY" / "nifty50_stocks_latest.csv"

UNIVERSE_NIFTY10 = "NIFTY 10"
UNIVERSE_NIFTY50 = "NIFTY 50"
UNIVERSE_FNO = "F&O stocks"
UNIVERSE_NIFTY500 = "NIFTY 500"
UNIVERSE_CHOICES: tuple[str, ...] = (UNIVERSE_NIFTY10, UNIVERSE_NIFTY50, UNIVERSE_FNO, UNIVERSE_NIFTY500)
SCAN_RESULTS_CSV = DATA_DIR / "scan_results.csv"
SCAN_INFO_CSV = DATA_DIR / "scan_info.csv"
SCAN_META_JSON = DATA_DIR / "scan_meta.json"

LOOKBACK_DAYS = 400
# Monthly bars need much deeper daily history (60-bar Donchian max + ATR warmup)
MONTHLY_LOOKBACK_DAYS = 1500
HOURLY_PERIOD = "60d"
BATCH_SIZE = 25

YAHOO_TICKER_MAP = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "VIX": "^INDIAVIX",
    "INDIAVIX": "^INDIAVIX",
}

DEFAULT_WATCHLIST = [
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "SBIN",
    "TCS",
    "INFY",
    "BHARTIARTL",
    "ITC",
    "KOTAKBANK",
    "LT",
    "AXISBANK",
    "BAJFINANCE",
    "MARUTI",
    "TITAN",
    "HINDUNILVR",
    "ASIANPAINT",
    "SUNPHARMA",
    "WIPRO",
    "ULTRACEMCO",
    "NTPC",
]


@dataclass(frozen=True)
class TimeframeConfig:
    label: str
    lookback: int
    vol_lookback: int
    min_bars: int
    vol_mult: float = 1.25
    strong_close_pct: float = 0.60
    atr_period: int = 14
    atr_mult: float = 1.2


# Strict mode defaults (Donchian + 1.5× vol + TR > ATR expansion + strong close)
STRICT_VOL_MULT = 1.5
STRICT_ATR_MULT = 1.2
STRICT_ATR_PERIOD = 14

TIMEFRAMES: dict[str, TimeframeConfig] = {
    "1H": TimeframeConfig(
        label="1 Hour",
        lookback=20,
        vol_lookback=20,
        min_bars=80,
        vol_mult=1.20,
        strong_close_pct=0.55,
        atr_period=14,
        atr_mult=1.0,
    ),
    "1D": TimeframeConfig(
        label="1 Day",
        lookback=20,
        vol_lookback=20,
        min_bars=60,
        vol_mult=1.25,
        strong_close_pct=0.60,
        atr_period=14,
        atr_mult=1.2,
    ),
    "1W": TimeframeConfig(
        label="1 Week",
        lookback=10,
        vol_lookback=10,
        min_bars=30,
        vol_mult=1.15,
        strong_close_pct=0.55,
        atr_period=14,
        atr_mult=1.2,
    ),
    "1M": TimeframeConfig(
        label="1 Month",
        lookback=6,
        vol_lookback=6,
        min_bars=24,
        vol_mult=1.10,
        strong_close_pct=0.55,
        atr_period=12,
        atr_mult=1.2,
    ),
}


# Canonical display / scan order
TIMEFRAME_ORDER: tuple[str, ...] = ("1H", "1D", "1W", "1M")


def sort_timeframes(timeframes: list[str] | tuple[str, ...]) -> list[str]:
    rank = {tf: i for i, tf in enumerate(TIMEFRAME_ORDER)}
    return sorted(
        [tf.upper() for tf in timeframes if tf.upper() in TIMEFRAMES],
        key=lambda tf: rank.get(tf, 99),
    )


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DAILY.mkdir(parents=True, exist_ok=True)
    CACHE_HOURLY.mkdir(parents=True, exist_ok=True)

# CPR: narrow central-pivot-range threshold (width as % of pivot)
NARROW_CPR_PCT = 0.35


# ── NSE index segments (added by patch_universes.py) ───────────────────
# Non-overlapping bands so a 750-stock universe can be scanned 250 at a
# time instead of all at once. Names must match universes.INDEX_SLUGS.
UNIVERSE_NIFTY100 = "NIFTY 100"
UNIVERSE_MIDCAP150 = "NIFTY Midcap 150"
UNIVERSE_SMALLCAP250 = "NIFTY Smallcap 250"
UNIVERSE_MICROCAP250 = "NIFTY Microcap 250"
UNIVERSE_TOTALMARKET = "NIFTY Total Market"

UNIVERSE_CHOICES = UNIVERSE_CHOICES + (
    UNIVERSE_NIFTY100,
    UNIVERSE_MIDCAP150,
    UNIVERSE_SMALLCAP250,
    UNIVERSE_MICROCAP250,
    UNIVERSE_TOTALMARKET,
)
# ───────────────────────────────────────────────────────────────────────


# ── every listed NSE equity ────────────────────────────────────────────────
# Added by patch_all_nse.py. Appended rather than merged into the tuple above
# so that nothing already working can be disturbed by the edit.
#
# resolve_universe_symbols() already resolves any name in
# universes.INDEX_REGISTRY, where "NSE All Equity" is registered, so this is
# the only thing that was missing: the sidebar is built from UNIVERSE_CHOICES
# and the name was not in it.
UNIVERSE_ALL_NSE = "NSE All Equity"
# Moved to the lab app (/opt/breakoutscanner-lab) so this picker stays as
# it was. The constant is kept for reference.
# UNIVERSE_CHOICES = UNIVERSE_CHOICES + (UNIVERSE_ALL_NSE,)
