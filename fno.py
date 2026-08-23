"""
fno.py — which NSE stocks have futures and options.

Goes in /opt/breakoutscanner/fno.py, beside funds.py.

WHY THIS IS DERIVED, NOT A LIST
===============================
The F&O universe changes. SEBI adds and removes names on a review cycle, and
a hardcoded list is wrong within a quarter and wrong silently — the column
would simply stop matching reality while every screen kept returning results.

So it is derived from the same Kite instrument dump the universe already
uses. Every stock future carries the underlying's symbol in its `name`
column:

    segment=NFO, instrument_type=FUT  ->  name = RELIANCE, TCS, ...

That is the exchange's own record of what is in F&O today, and it costs one
HTTP request that is already cached for other reasons.

INDEX FUTURES ARE EXCLUDED
==========================
NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50 and SENSEX all appear as
FUT rows too. They are indices, not stocks — there is no equity row for them
to join to, and treating them as scannable symbols would put a non-tradeable
underlying into a stock screen. Same class of mistake as the ETFs.

WHY YOU WANT THIS COLUMN
========================
F&O names are the liquid end of the market: roughly 180-230 stocks, all with
real turnover, options chains, and — relevant to the Quality page — by far
the best fundamental coverage on Yahoo. A shortlist restricted to F&O is the
one where "no data" is rare rather than the norm.

It is also the only part of the universe where a breakout can be expressed
with defined risk, which is a different decision from buying the stock.
"""

from __future__ import annotations

import io
import json
import os
import time

INSTRUMENTS_URL = "https://api.kite.trade/instruments"
CACHE = "nse_fno.json"
LIST_OUT = "nse_fno.txt"
CACHE_DAYS = 7

# Index underlyings that trade in NFO but are not stocks.
INDICES = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    "SENSEX", "BANKEX", "SENSEX50", "NIFTYIT", "NIFTYINFRA",
}

_MEM: dict[str, set] = {}


def _cache_dir(app: str) -> str:
    d = os.path.join(app, "data_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _fetch() -> set:
    import urllib.request

    import pandas as pd
    raw = urllib.request.urlopen(INSTRUMENTS_URL, timeout=60).read()
    dump = pd.read_csv(io.BytesIO(raw), low_memory=False)
    fut = dump[(dump["segment"].astype(str).str.upper() == "NFO-FUT")
               | ((dump["exchange"].astype(str).str.upper() == "NFO")
                  & (dump["instrument_type"].astype(str).str.upper() == "FUT"))]
    names = {str(n).strip().upper() for n in fut["name"].dropna()}
    return {n for n in names if n and n not in INDICES}


def fno_symbols(app: str, write: bool = True) -> set:
    """
    Underlying symbols that have stock futures. Cached for CACHE_DAYS.

    Returns an empty set rather than raising if the dump is unreachable: a
    missing membership column must not stop a build. An empty set means
    in_fno is false everywhere, which is visibly wrong on the page rather
    than quietly wrong in a filter.
    """
    key = os.path.abspath(app)
    if key in _MEM:
        return _MEM[key]

    path = os.path.join(_cache_dir(app), CACHE)
    syms: set = set()
    fresh = (os.path.exists(path)
             and time.time() - os.path.getmtime(path) < CACHE_DAYS * 86400)
    if fresh:
        try:
            with open(path) as fh:
                syms = set(json.load(fh))
        except Exception:
            syms = set()
    if not syms:
        try:
            syms = _fetch()
            with open(path, "w") as fh:
                json.dump(sorted(syms), fh)
        except Exception:
            if os.path.exists(path):
                try:
                    with open(path) as fh:
                        syms = set(json.load(fh))
                except Exception:
                    syms = set()

    if write and syms:
        try:
            with open(os.path.join(_cache_dir(app), LIST_OUT), "w") as fh:
                fh.write("# NSE stocks with futures, derived from the Kite "
                         "instrument dump\n")
                fh.write("# generated %s — %d underlyings, index futures "
                         "excluded\n" % (time.strftime("%F %T"), len(syms)))
                for s in sorted(syms):
                    fh.write(s + "\n")
        except Exception:
            pass

    _MEM[key] = syms
    return syms


def in_fno(symbol: str, app: str) -> bool:
    return str(symbol).upper().strip() in fno_symbols(app)


if __name__ == "__main__":                              # pragma: no cover
    import sys
    a = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.dirname(os.path.abspath(__file__))
    s = fno_symbols(a)
    print("%d F&O underlyings" % len(s))
    print(", ".join(sorted(s)[:25]), "...")
    print("written to", os.path.join(a, "data_cache", LIST_OUT))
