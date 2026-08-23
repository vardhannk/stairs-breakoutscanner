"""Load NSE F&O equity symbol list."""

from __future__ import annotations

import csv
import gzip
import io
import ssl
import urllib.request
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from config import FNO_CACHE, FNO_CACHE_SIBLING, ensure_dirs

NSE_FO_CONTRACT_URL = "https://nsearchives.nseindia.com/content/fo/NSE_FO_contract_{date_tag}.csv.gz"

FNO_INDEX_SYMBOLS = frozenset({
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
    "SENSEX",
    "BANKEX",
    "NIFTYIT",
})

FNO_FALLBACK_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "ITC", "SBIN", "BHARTIARTL",
    "KOTAKBANK", "LT", "AXISBANK", "BAJFINANCE", "MARUTI", "TITAN", "HINDUNILVR",
    "ASIANPAINT", "SUNPHARMA", "WIPRO", "ULTRACEMCO", "NTPC", "POWERGRID", "ONGC",
    "COALINDIA", "TATASTEEL", "ADANIENT", "ADANIPORTS", "JSWSTEEL", "M&M", "BAJAJ-AUTO",
    "HCLTECH", "TECHM", "NESTLEIND", "GRASIM", "DIVISLAB", "DRREDDY", "CIPLA", "EICHERMOT",
    "HEROMOTOCO", "BRITANNIA", "APOLLOHOSP", "INDUSINDBK", "TMPV",
]


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch_fno_from_nse(as_of: Optional[date] = None) -> list[str]:
    as_of = as_of or date.today()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://www.nseindia.com/",
        "Accept": "*/*",
    }
    ctx = _ssl_context()

    for offset in range(6):
        day = as_of - timedelta(days=offset)
        tag = day.strftime("%d%m%Y")
        url = NSE_FO_CONTRACT_URL.format(date_tag=tag)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                text = gzip.decompress(resp.read()).decode("utf-8", errors="replace")
            rows = list(csv.DictReader(io.StringIO(text)))
            symbols: set[str] = set()
            for row in rows:
                sym = (row.get("TckrSymb") or "").strip().upper()
                kind = (row.get("FinInstrmNm") or "").strip().upper()
                if not sym or sym in FNO_INDEX_SYMBOLS:
                    continue
                if "NSETEST" in sym:
                    continue
                if kind not in ("FUTSTK", "OPTSTK"):
                    continue
                symbols.add(sym)
            if symbols:
                return sorted(symbols)
        except Exception:
            continue
    return []


def _save_cache(symbols: list[str]) -> None:
    ensure_dirs()
    pd.DataFrame({"symbol": symbols, "updated": date.today().isoformat()}).to_csv(
        FNO_CACHE, index=False
    )


def _load_cache(path) -> list[str]:
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path)
        if "symbol" not in df.columns or df.empty:
            col = "Symbol" if "Symbol" in df.columns else df.columns[0]
            if col not in df.columns:
                return []
            return sorted(df[col].astype(str).str.strip().str.upper().unique().tolist())
        return sorted(df["symbol"].astype(str).str.strip().str.upper().unique().tolist())
    except Exception:
        return []


def load_fno_symbols(refresh: bool = False) -> list[str]:
    """Return sorted F&O equity symbols; cache locally after NSE fetch."""
    if not refresh:
        for path in (FNO_CACHE, FNO_CACHE_SIBLING):
            cached = _load_cache(path)
            if cached:
                if path != FNO_CACHE:
                    _save_cache(cached)
                return cached

    fetched = _fetch_fno_from_nse()
    if fetched:
        _save_cache(fetched)
        return fetched

    cached = _load_cache(FNO_CACHE) or _load_cache(FNO_CACHE_SIBLING)
    if cached:
        return cached

    return sorted(set(FNO_FALLBACK_SYMBOLS))


def fno_symbol_set(refresh: bool = False) -> frozenset[str]:
    return frozenset(load_fno_symbols(refresh=refresh))
