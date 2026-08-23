"""
universes.py — NSE index constituent lists as scan universes.

Adds five segments so you can scan one band at a time instead of always
loading NIFTY 500:

    NIFTY 100        ranks    1-100   large cap
    NIFTY Midcap 150 ranks  101-250   mid cap
    NIFTY Smallcap 250 ranks 251-500  small cap
    NIFTY Microcap 250 ranks 501-750  micro cap   <- new coverage
    NIFTY Total Market       1-750    all of the above

They are non-overlapping (except Total Market, which is the union), so four
scans of <=250 names cover 750 stocks while never holding more than 250
DataFrames in memory. That matters on a 1 vCPU / 1 GB box where 750 at once
does not fit.

Deliberately NOT included: NEXT 50/100, NIFTY 200, Smallcap 50/100, Midcap
50/100, LargeMidcap 250, MidSmallcap 400 — all subsets or unions of the
above. Nor the weighted variants (MULTICAP 50:25:25, EQUAL-CAP WEIGHTED,
MIDSMALLCAP400 50:50): weighting changes the index value, not the
constituent list, so to a scanner they are exact duplicates of their parent.

NSE moves these paths from time to time, so each index carries several
candidate URLs and a local cache. A failed download degrades to "universe
unavailable" rather than breaking a scan.

The Industry column is preserved — NSE ships it in every one of these files
and it is what sector/theme filtering needs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

try:
    from config import DATA_DIR
except ImportError:                                    # standalone use
    DATA_DIR = Path(__file__).resolve().parent / "data_cache"

ARCHIVE_HOSTS = (
    "https://nsearchives.nseindia.com/content/indices",
    "https://archives.nseindia.com/content/indices",
)

# display name -> NSE file slug
INDEX_SLUGS = {
    "NSE All Equity": "nse_all_equity",  # every listed NSE equity; cache written by build_nse_universe.py, not downloaded from NSE
    "NIFTY 100": "nifty100",
    "NIFTY Midcap 150": "niftymidcap150",
    "NIFTY Smallcap 250": "niftysmallcap250",
    "NIFTY Microcap 250": "niftymicrocap250",
    "NIFTY Total Market": "niftytotalmarket",
}

INDEX_REGISTRY = tuple(INDEX_SLUGS)

# Rough constituent counts, used only to warn when a download looks wrong
EXPECTED_COUNTS = {
    "NIFTY 100": 100,
    "NIFTY Midcap 150": 150,
    "NIFTY Smallcap 250": 250,
    "NIFTY Microcap 250": 250,
    "NIFTY Total Market": 750,
}


FETCH_TIMEOUT = 12          # seconds per URL — pd.read_csv(url) has NO timeout
                            # and will hang forever if NSE does not answer

# NSE rejects the default python-urllib agent. Same headers fno_loader.py
# already uses successfully against the same archive hosts.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/csv,application/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _ssl_context():
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch_csv(url: str, timeout: int = FETCH_TIMEOUT) -> pd.DataFrame:
    """
    Download a CSV with a hard timeout and browser headers.

    Do NOT use pd.read_csv(url) here: it goes through urllib with no timeout
    and the default agent, so a slow or blocking NSE hangs the process
    indefinitely with no way out but Ctrl-C.
    """
    import io
    import urllib.request

    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, context=_ssl_context(), timeout=timeout) as resp:
        raw = resp.read()
    return pd.read_csv(io.BytesIO(raw))


def _candidate_urls(slug: str) -> list[str]:
    """
    NSE is inconsistent about the separator before 'list' and about which
    archive host serves a given file, so try the combinations.
    """
    names = [f"ind_{slug}list.csv", f"ind_{slug}_list.csv", f"ind_{slug}List.csv"]
    return [f"{host}/{n}" for host in ARCHIVE_HOSTS for n in names]


def cache_path(name: str) -> Path:
    return DATA_DIR / f"{INDEX_SLUGS[name]}_symbols.csv"


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Pull Symbol + Industry out of an NSE constituents CSV."""
    cols = {c.strip().lower(): c for c in df.columns}
    sym_col = cols.get("symbol")
    if sym_col is None:
        raise ValueError(f"no Symbol column; got {list(df.columns)}")
    out = pd.DataFrame()
    out["symbol"] = df[sym_col].dropna().astype(str).str.upper().str.strip()
    ind_col = cols.get("industry")
    # keep Industry — the sector/theme work needs it, and load_universe_symbols
    # in the stock app throws it away
    out["industry"] = (df[ind_col].astype(str).str.strip()
                       if ind_col else "")
    name_col = cols.get("company name")
    if name_col:
        out["company"] = df[name_col].astype(str).str.strip()
    return out[out["symbol"] != ""].drop_duplicates("symbol").reset_index(drop=True)


def download_index(name: str) -> Optional[pd.DataFrame]:
    """Fetch constituents from NSE. Returns None if every candidate fails."""
    if name not in INDEX_SLUGS:
        return None
    last_err = None
    for url in _candidate_urls(INDEX_SLUGS[name]):
        try:
            raw = _fetch_csv(url)        # timeout + browser headers
            df = _normalise(raw)
            if df.empty:
                continue
            exp = EXPECTED_COUNTS.get(name)
            if exp and abs(len(df) - exp) > exp * 0.25:
                log.warning("%s: got %d symbols, expected ~%d (%s)",
                            name, len(df), exp, url)
            log.info("  %s: ok from %s", name, url)
            return df
        except Exception as e:                          # 404, timeout, parse error
            last_err = e
            log.debug("  %s: %s -> %s", name, url, e)
            continue
    log.warning("%s: all %d candidate URLs failed (last: %s)",
                name, len(_candidate_urls(INDEX_SLUGS[name])), last_err)
    return None


def load_index(name: str, *, refresh: bool = False) -> Optional[pd.DataFrame]:
    """
    Constituents as a DataFrame with symbol / industry / company.

    Cache first unless refresh=True. On a failed refresh the existing cache
    is kept rather than being replaced with nothing.
    """
    if name not in INDEX_SLUGS:
        return None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path(name)

    if not refresh and path.is_file():
        try:
            df = pd.read_csv(path)
            if not df.empty and "symbol" in df.columns:
                return df
        except Exception:
            pass

    df = download_index(name)
    if df is None or df.empty:
        if path.is_file():                              # keep what we had
            try:
                return pd.read_csv(path)
            except Exception:
                return None
        return None

    try:
        df.to_csv(path, index=False)
    except Exception as e:
        log.warning("could not cache %s: %s", name, e)
    return df


def load_index_symbols(name: str, *, refresh: bool = False) -> list[str]:
    """Symbols only. Empty list if the index is unavailable."""
    df = load_index(name, refresh=refresh)
    if df is None or df.empty or "symbol" not in df.columns:
        return []
    return sorted(df["symbol"].dropna().astype(str).str.upper().str.strip().unique())


def industry_map(names: Optional[list[str]] = None) -> dict:
    """
    symbol -> industry, merged across cached indices.

    Feeds the top-down half of the mentor's method: rank industries by the
    strength of their constituents, then take setups from leading sectors.
    """
    out: dict = {}
    for name in (names or INDEX_REGISTRY):
        df = load_index(name)
        if df is None or df.empty or "industry" not in df.columns:
            continue
        for sym, ind in zip(df["symbol"], df["industry"]):
            if isinstance(ind, str) and ind.strip():
                out.setdefault(str(sym).upper().strip(), ind.strip())
    return out


def refresh_all() -> dict:
    """Re-download every index. Returns name -> count (0 = failed)."""
    out = {}
    for n in INDEX_REGISTRY:
        print(f"fetching {n} ...", flush=True)      # so a slow run is visible
        out[n] = len(load_index_symbols(n, refresh=True))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for n, c in refresh_all().items():
        exp = EXPECTED_COUNTS.get(n, "?")
        print(f"{n:<22} {c:>4} symbols (expected ~{exp})"
              f"{'   <- FAILED' if c == 0 else ''}")
    im = industry_map()
    print(f"\nindustry map: {len(im)} symbols tagged")
    if im:
        from collections import Counter
        for ind, k in Counter(im.values()).most_common(8):
            print(f"  {ind:<38} {k}")
