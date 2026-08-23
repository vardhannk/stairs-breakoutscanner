"""
sectors.py — sectoral index performance across timeframes.

Answers "which sectors are leading?" so scan results can be filtered to the
strong ones (top-down), rather than treating all 500 stocks as equals.

Two data paths, tried in order per sector:

  INDEX     the actual NSE sectoral index from Yahoo (^CNXAUTO, ^CNXIT, ...).
            Matches published numbers because it is the published series.

  BASKET    equal-weighted mean return of that sector's constituents, taken
            from the Industry column already cached by universes.py.
            Used when Yahoo has no ticker or the fetch fails.

The method used is reported per row. That matters: a BASKET return is
equal-weighted while NSE's indices are free-float market-cap weighted, so
the numbers will differ from a published figure even when the RANKING agrees.
Ranking is what sector rotation needs; exact levels are not.

Windows match a typical sector dashboard: 1D / 1W / 1M / 3M / 6M / 1Y.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# trading days per window
WINDOWS = {"1D": 1, "1W": 5, "1M": 21, "3M": 63, "6M": 126, "1Y": 252}
WINDOW_ORDER = list(WINDOWS)

# NSE sectoral / thematic index -> Yahoo ticker.
# Yahoo's coverage of Indian sector indices is patchy and it renames them
# occasionally, so every one of these can fail; BASKET picks up the slack.
SECTOR_TICKERS = {
    "Nifty Auto": "^CNXAUTO",
    "Nifty IT": "^CNXIT",
    "Nifty Bank": "^NSEBANK",
    "Nifty Pharma": "^CNXPHARMA",
    "Nifty FMCG": "^CNXFMCG",
    "Nifty Metal": "^CNXMETAL",
    "Nifty Realty": "^CNXREALTY",
    "Nifty Energy": "^CNXENERGY",
    "Nifty Infrastructure": "^CNXINFRA",
    "Nifty PSU Bank": "^CNXPSUBANK",
    "Nifty Media": "^CNXMEDIA",
    "Nifty India Consumption": "^CNXCONSUM",
    "Nifty Commodities": "^CNXCMDT",
    "Nifty Services Sector": "^CNXSERVICE",
    "Nifty MNC": "^CNXMNC",
    "Nifty PSE": "^CNXPSE",
    "Nifty Financial Services": "NIFTY_FIN_SERVICE.NS",
}

# NSE "Industry" values (from the constituent CSVs) -> a sector bucket, for
# the BASKET path. These are NSE's 20 macro sectors, which are close to but
# not identical with the thematic indices above.
INDUSTRY_BUCKETS = {
    "Automobile and Auto Components": "Auto (basket)",
    "Information Technology": "IT (basket)",
    "Financial Services": "Financial Services (basket)",
    "Healthcare": "Healthcare (basket)",
    "Fast Moving Consumer Goods": "FMCG (basket)",
    "Metals & Mining": "Metals (basket)",
    "Realty": "Realty (basket)",
    "Oil Gas & Consumable Fuels": "Oil & Gas (basket)",
    "Capital Goods": "Capital Goods (basket)",
    "Chemicals": "Chemicals (basket)",
    "Consumer Durables": "Consumer Durables (basket)",
    "Consumer Services": "Consumer Services (basket)",
    "Construction": "Construction (basket)",
    "Construction Materials": "Construction Materials (basket)",
    "Power": "Power (basket)",
    "Telecommunication": "Telecom (basket)",
    "Services": "Services (basket)",
    "Textiles": "Textiles (basket)",
    "Media Entertainment & Publication": "Media (basket)",
    "Forest Materials": "Forest Materials (basket)",
    "Diversified": "Diversified (basket)",
}

BENCHMARK = ("Nifty 50", "^NSEI")

# ---------------------------------------------------------------------------
# Sector membership from NSE constituent CSVs
# ---------------------------------------------------------------------------
# Yahoo's coverage of Indian sector indices is thin and it renames tickers, so
# membership comes from NSE's own constituent files instead — the same
# ind_<slug>list.csv pattern universes.py already fetches. This gives exact
# membership for every sectoral index, including newer ones (Defence,
# Tourism, Capital Markets) that Yahoo has no ticker for at all.
#
# Order matters: a stock can belong to several indices (RELIANCE is in Energy,
# Oil & Gas and Commodities), so the FIRST match in this order becomes its
# primary `sector`. Narrow, specific indices come before broad ones.
SECTOR_INDEX_SLUGS = {
    # narrow / thematic first
    "Nifty India Defence": "niftyindiadefence",
    "Nifty India Tourism": "niftyindiatourism",
    "Nifty Capital Markets": "niftycapitalmarkets",
    "Nifty Private Bank": "niftyprivatebank",
    "Nifty PSU Bank": "niftypsubank",
    "Nifty Healthcare": "niftyhealthcare",
    "Nifty Auto": "niftyauto",
    "Nifty IT": "niftyit",
    "Nifty Pharma": "niftypharma",
    "Nifty FMCG": "niftyfmcg",
    "Nifty Metal": "niftymetal",
    "Nifty Realty": "niftyrealty",
    "Nifty Media": "niftymedia",
    "Nifty Oil & Gas": "niftyoilgas",
    "Nifty Consumer Durables": "niftyconsumerdurables",
    "Nifty Bank": "niftybank",
    "Nifty Financial Services": "niftyfinancialservices",
    "Nifty Energy": "niftyenergy",
    "Nifty India Manufacturing": "niftyindiamanufacturing",
    "Nifty Infrastructure": "niftyinfrastructure",
    "Nifty CPSE": "niftycpse",
    "Nifty PSE": "niftypse",
    "Nifty MNC": "niftymnc",
    # broadest last
    "Nifty Commodities": "niftycommodities",
    "Nifty India Consumption": "niftyconsumption",
    "Nifty Services Sector": "niftyservicessector",
}


# NSE's file naming is inconsistent — some indices abbreviate ("infra" for
# Infrastructure), some singularise ("service"), some drop words entirely
# ("consumption" for Nifty India Consumption). Where the obvious slug 404s,
# try these alternates before giving up.
SLUG_ALTERNATES = {
    "Nifty Private Bank": ["nifty_privatebank", "niftyprivatebank",
                           "niftypvtbank", "nifty_pvtbank"],
    "Nifty Financial Services": ["niftyfinance", "nifty_financialservices",
                                 "niftyfinancialservices", "niftyfinservice",
                                 "nifty_finance"],
    "Nifty Infrastructure": ["niftyinfra", "nifty_infra", "niftyinfrastructure"],
    "Nifty Services Sector": ["niftyservice", "niftyservicesector",
                              "nifty_services", "niftyservicessector"],
    "Nifty Capital Markets": ["niftycapitalmarkets", "nifty_capitalmarkets",
                              "niftycapitalmarket", "nifty_capital_markets"],
    "Nifty India Manufacturing": ["niftyindiamanufacturing",
                                  "nifty_indiamanufacturing"],
}


# Further NSE sectoral / thematic indices, kept SEPARATE from
# SECTOR_INDEX_SLUGS on purpose.
#
# SECTOR_INDEX_SLUGS decides each stock's primary `sector` on the scan
# results, and that assignment is first-match-wins. Adding names to it would
# silently relabel stocks — a name that reads "Nifty Auto" today could become
# "Nifty EV & New Age Automotive" tomorrow, changing what your saved scans
# mean. These are for the all-sectors performance view only; tagging is
# untouched.
#
# The slugs are inferred from NSE's naming pattern and NOT verified against
# the live site. Any that 404 are skipped silently and simply do not appear,
# which is why they are safe to guess at.
EXTRA_SECTOR_SLUGS = {
    "Nifty India Railways PSU": "niftyindiarailwayspsu",
    "Nifty India Digital": "niftyindiadigital",
    "Nifty Mobility": "niftymobility",
    "Nifty EV & New Age Automotive": "niftyevnewageautomotive",
    "Nifty Transportation & Logistics": "niftytransportationlogistics",
    "Nifty Housing": "niftyhousing",
    "Nifty Core Housing": "niftycorehousing",
    "Nifty Non-Cyclical Consumer": "niftynoncyclicalconsumer",
    "Nifty MidSmall Healthcare": "niftymidsmallhealthcare",
    "Nifty MidSmall IT & Telecom": "niftymidsmallitandtelecom",
    "Nifty MidSmall Financial Services": "niftymidsmallfinancialservices",
}


def all_sector_slugs() -> dict:
    """Every index worth charting: the tagging set plus the extras."""
    out = dict(SECTOR_INDEX_SLUGS)
    out.update(EXTRA_SECTOR_SLUGS)
    return out


def _slugs_for(name: str) -> list:
    """Primary slug first, then any known alternates."""
    out = []
    primary = SECTOR_INDEX_SLUGS.get(name)
    if primary:
        out.append(primary)
    for alt in SLUG_ALTERNATES.get(name, []):
        if alt not in out:
            out.append(alt)
    return out


def _sector_cache_path(slug: str):
    try:
        from config import DATA_DIR as _D
    except ImportError:
        from pathlib import Path
        _D = Path(__file__).resolve().parent / "data_cache"
    return _D / f"sector_{slug}_symbols.csv"


def load_sector_members(name: str, *, refresh: bool = False) -> list[str]:
    """Constituents of one sectoral index. Cached; [] if unavailable."""
    slug = SECTOR_INDEX_SLUGS.get(name)
    if not slug:
        return []
    path = _sector_cache_path(slug)
    if not refresh and path.is_file():
        try:
            d = pd.read_csv(path)
            if not d.empty and "symbol" in d.columns:
                return d["symbol"].astype(str).str.upper().str.strip().tolist()
        except Exception:
            pass
    try:
        import universes
        for cand in _slugs_for(name):
            for url in universes._candidate_urls(cand):
                try:
                    raw = universes._fetch_csv(url)
                    d = universes._normalise(raw)
                    if d.empty:
                        continue
                    path.parent.mkdir(parents=True, exist_ok=True)
                    d.to_csv(path, index=False)
                    log.info("  %s: %d members from %s", name, len(d), url)
                    return d["symbol"].tolist()
                except Exception:
                    continue
        tried = sum(len(universes._candidate_urls(c)) for c in _slugs_for(name))
        log.warning("%s: no constituent file found (%d URLs tried)", name, tried)
    except Exception as e:
        log.warning("%s: %s", name, e)
    return []


def refresh_sector_members() -> dict:
    """Download every sectoral constituent list. name -> member count."""
    out = {}
    for name in SECTOR_INDEX_SLUGS:
        print(f"fetching {name} ...", flush=True)
        out[name] = len(load_sector_members(name, refresh=True))
    return out


def sector_map(refresh: bool = False) -> dict:
    """
    symbol -> primary sector name.

    A stock in several indices gets the FIRST one in SECTOR_INDEX_SLUGS order,
    so it lands in the most specific bucket rather than a catch-all.
    """
    out: dict = {}
    for name in SECTOR_INDEX_SLUGS:
        for sym in load_sector_members(name, refresh=refresh):
            out.setdefault(str(sym).upper().strip(), name)
    return out


def sector_map_all(refresh: bool = False) -> dict:
    """symbol -> every sector it belongs to."""
    out: dict = {}
    for name in SECTOR_INDEX_SLUGS:
        for sym in load_sector_members(name, refresh=refresh):
            out.setdefault(str(sym).upper().strip(), []).append(name)
    return out


# ---------------------------------------------------------------------------
def _returns(close: pd.Series) -> dict:
    """Percentage return over each window. NaN where history is too short."""
    s = close.dropna().astype(float)
    out = {}
    for label, n in WINDOWS.items():
        if len(s) < n + 1:
            out[label] = np.nan
            continue
        a, b = float(s.iloc[-n - 1]), float(s.iloc[-1])
        out[label] = (b / a - 1.0) * 100.0 if a > 0 else np.nan
    return out


def _fetch_index(ticker: str, days: int = 420) -> Optional[pd.Series]:
    """Close series for one index from Yahoo. None on any failure."""
    try:
        import yfinance as yf
        df = yf.download(ticker, period=f"{days}d", interval="1d",
                         progress=False, auto_adjust=True, threads=False)
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "get_level_values"):
            try:
                df.columns = df.columns.get_level_values(0)
            except Exception:
                pass
        df.columns = [str(c).lower() for c in df.columns]
        if "close" not in df.columns:
            return None
        s = df["close"].dropna()
        return s if len(s) > 30 else None
    except Exception as e:
        log.debug("index fetch failed %s: %s", ticker, e)
        return None


def _basket_returns(symbols: list[str], loader) -> dict:
    """
    Equal-weighted mean of constituent returns.

    Equal-weighted, NOT free-float cap weighted like the real index — so
    treat the level as indicative and the ranking as the signal.
    """
    per = {w: [] for w in WINDOWS}
    used = 0
    for sym in symbols:
        try:
            bars = loader(sym)
        except Exception:
            continue
        if bars is None or getattr(bars, "empty", True):
            continue
        cols = {str(c).lower(): c for c in bars.columns}
        if "close" not in cols:
            continue
        r = _returns(bars[cols["close"]])
        if all(np.isnan(v) for v in r.values()):
            continue
        used += 1
        for w, v in r.items():
            if not np.isnan(v):
                per[w].append(v)
    out = {w: (float(np.mean(v)) if v else np.nan) for w, v in per.items()}
    out["_constituents"] = used
    return out


# ---------------------------------------------------------------------------
def sector_performance(loader=None, *, include_baskets: bool = True,
                       progress=None) -> pd.DataFrame:
    """
    One row per sector, one column per window, plus the method used.

    `loader(symbol) -> daily OHLCV` is only needed for the BASKET path; pass
    data_loader.load_daily. Without it, index-only.
    """
    rows = []

    bench = _fetch_index(BENCHMARK[1])
    bench_ret = _returns(bench) if bench is not None else {w: np.nan for w in WINDOWS}

    total = len(SECTOR_TICKERS)
    for i, (name, ticker) in enumerate(SECTOR_TICKERS.items(), 1):
        if progress:
            progress(i, total, name)
        s = _fetch_index(ticker)
        if s is None:
            continue
        r = _returns(s)
        r.update({"sector": name, "method": "INDEX", "source": ticker,
                  "constituents": np.nan})
        rows.append(r)

    fetched = {r["sector"] for r in rows}

    if include_baskets and loader is not None:
        by_bucket: dict[str, list[str]] = {}
        # prefer exact sectoral-index membership
        for name in SECTOR_INDEX_SLUGS:
            mem = load_sector_members(name)
            if mem:
                by_bucket[f"{name} (basket)"] = mem
        if not by_bucket:
            try:
                import universes
                imap = universes.industry_map()
            except Exception:
                imap = {}
            for sym, ind in imap.items():
                bucket = INDUSTRY_BUCKETS.get(ind)
                if bucket:
                    by_bucket.setdefault(bucket, []).append(sym)
        if by_bucket:
            for bucket, syms in sorted(by_bucket.items()):
                # skip a basket whose index equivalent already came through
                stem = bucket.replace(" (basket)", "")
                if any(stem.lower() in f.lower() for f in fetched):
                    continue
                if len(syms) < 3:
                    continue
                r = _basket_returns(syms, loader)
                n = r.pop("_constituents", 0)
                if n < 3:
                    continue
                r.update({"sector": bucket, "method": "BASKET",
                          "source": f"{n} constituents", "constituents": n})
                rows.append(r)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    cols = ["sector"] + WINDOW_ORDER + ["method", "source", "constituents"]
    df = df[[c for c in cols if c in df.columns]]

    # relative-to-benchmark columns: the actual rotation signal
    for w in WINDOW_ORDER:
        if w in df.columns and not np.isnan(bench_ret.get(w, np.nan)):
            df[f"{w} vs Nifty"] = df[w] - bench_ret[w]

    df.attrs["benchmark"] = dict(bench_ret)
    df.attrs["benchmark_name"] = BENCHMARK[0]
    return df.sort_values("1D", ascending=False).reset_index(drop=True)


def sector_performance_for_universe(symbols, loader, *, min_members: int = 4,
                                    progress=None) -> pd.DataFrame:
    """
    Sector returns computed from ONE universe's own constituents.

    This exists because of a real mismatch. `sector_performance()` reads the
    published NSE sectoral indices, and those indices are large-cap: Nifty
    Auto is ~15 names, all of them big. Scan NIFTY Smallcap 250 and rank
    sectors off that table and you are asking "how did large-cap autos do?"
    to decide whether a smallcap auto breakout is in a working sector. Those
    are different questions and they routinely disagree.

    Here, sector returns are the equal-weighted mean of the sector's members
    WITHIN the scanned universe. Scan Smallcap 250 and "Nifty Auto" means the
    smallcap auto names, not MARUTI.

    Equal-weighted and computed from cached daily bars, so levels will not
    match any published figure. Ranking is the point.

    Sectors with fewer than `min_members` names in the universe are dropped —
    a two-stock "sector" return is one stock's noise.
    """
    if not symbols or loader is None:
        return pd.DataFrame()

    smap = sector_map()
    if not smap:
        return pd.DataFrame()

    buckets: dict[str, list[str]] = {}
    for s in symbols:
        sec = smap.get(str(s).upper().strip(), "")
        if sec:
            buckets.setdefault(sec, []).append(str(s).upper().strip())

    buckets = {k: v for k, v in buckets.items() if len(v) >= min_members}
    if not buckets:
        return pd.DataFrame()

    bench = _fetch_index(BENCHMARK[1])
    bench_ret = _returns(bench) if bench is not None else {w: np.nan for w in WINDOWS}

    rows = []
    total = len(buckets)
    for i, (name, syms) in enumerate(sorted(buckets.items()), 1):
        if progress:
            progress(i, total, name)
        r = _basket_returns(syms, loader)
        n = r.pop("_constituents", 0)
        if n < min_members:
            continue
        r.update({"sector": name, "method": "UNIVERSE",
                  "source": f"{n} of {len(syms)} in universe", "constituents": n})
        rows.append(r)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    cols = ["sector"] + WINDOW_ORDER + ["method", "source", "constituents"]
    df = df[[c for c in cols if c in df.columns]]
    for w in WINDOW_ORDER:
        if w in df.columns and not np.isnan(bench_ret.get(w, np.nan)):
            df[f"{w} vs Nifty"] = df[w] - bench_ret[w]

    df.attrs["benchmark"] = dict(bench_ret)
    df.attrs["benchmark_name"] = BENCHMARK[0]
    df.attrs["basis"] = "universe"
    return df.sort_values("1D", ascending=False).reset_index(drop=True)


def _member_returns(symbols: list, loader) -> dict:
    """Per-window returns for each member. {window: [ret, ...]}"""
    per = {w: [] for w in WINDOWS}
    for s in symbols:
        try:
            d = loader(s)
        except Exception:
            continue
        if d is None or getattr(d, "empty", True) or "close" not in d:
            continue
        c = d["close"].astype(float).dropna()
        for w, n in WINDOWS.items():
            if len(c) > n:
                prev = float(c.iloc[-n - 1])
                if prev:
                    per[w].append((float(c.iloc[-1]) / prev - 1.0) * 100.0)
    return per


def all_sector_performance(loader, *, min_members: int = 3,
                           progress=None) -> pd.DataFrame:
    """
    Every sectoral index, advancing and declining alike, with breadth.

    One row per index. Returns are the equal-weighted mean of that index's
    constituents; `advancing` counts how many members are up over each
    window, which is the part a single index number hides — a sector can be
    green on two heavyweights while most of its names fall.

    Equal-weighted, so levels differ from NSE's free-float cap-weighted
    published figures. The ordering is what a rotation view needs.
    """
    slugs = all_sector_slugs()
    bench = _fetch_index(BENCHMARK[1])
    bench_ret = _returns(bench) if bench is not None else {w: np.nan for w in WINDOWS}

    rows, total = [], len(slugs)
    for i, name in enumerate(slugs, 1):
        if progress:
            progress(i, total, name)
        members = load_sector_members(name)
        if len(members) < min_members:
            continue
        per = _member_returns(members, loader)
        n_used = max((len(v) for v in per.values()), default=0)
        if n_used < min_members:
            continue
        row = {"sector": name, "members": len(members), "with_data": n_used}
        for w in WINDOW_ORDER:
            vals = per.get(w) or []
            row[w] = float(np.mean(vals)) if vals else np.nan
            row[f"{w} adv"] = int(sum(1 for v in vals if v > 0)) if vals else 0
            row[f"{w} tot"] = len(vals)
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    for w in WINDOW_ORDER:
        if w in df.columns and not np.isnan(bench_ret.get(w, np.nan)):
            df[f"{w} vs Nifty"] = df[w] - bench_ret[w]
    df.attrs["benchmark"] = dict(bench_ret)
    df.attrs["benchmark_name"] = BENCHMARK[0]
    return df.sort_values("1D", ascending=False).reset_index(drop=True)


def leading_sectors(perf: pd.DataFrame, window: str = "1M",
                    top_n: int = 5, positive_only: bool = False) -> list[str]:
    """
    Names of the top N sectors by return over `window`.

    `positive_only` drops sectors that are down over the window. In a broad
    selloff the "top 5" can all be negative, and calling the least-bad sector
    a leader is how a top-down screen talks you into a falling market.
    """
    if perf is None or perf.empty or window not in perf.columns:
        return []
    d = perf
    if positive_only:
        d = d[d[window].fillna(-1e9) > 0]
        if d.empty:
            return []
    return d.nlargest(top_n, window)["sector"].tolist()


def tag_scan_with_sector(scan: pd.DataFrame) -> pd.DataFrame:
    """
    Add an `industry` column to scan results from the cached constituent
    lists — the join that lets you keep only setups in leading sectors.
    """
    if scan is None or scan.empty or "symbol" not in scan.columns:
        return scan
    out = scan.copy()
    # exact sectoral-index membership first
    try:
        smap = sector_map()
        amap = sector_map_all()
    except Exception:
        smap, amap = {}, {}
    if smap:
        out["sector"] = out["symbol"].map(lambda s: smap.get(str(s).upper(), ""))
        out["sectors_all"] = out["symbol"].map(
            lambda s: ", ".join(amap.get(str(s).upper(), [])))
    # NSE macro-industry as a secondary label
    try:
        import universes
        imap = universes.industry_map()
        out["industry"] = out["symbol"].map(lambda s: imap.get(str(s).upper(), ""))
    except Exception:
        pass
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    res = refresh_sector_members()
    print()
    okc = sum(1 for v in res.values() if v)
    for n, c in res.items():
        print(f"  {n:<30}{c:>4} members{'   <- FAILED' if not c else ''}")
    print(f"\n{okc}/{len(res)} sectoral indices resolved")
    sm = sector_map()
    print(f"symbol -> sector map: {len(sm)} symbols")
