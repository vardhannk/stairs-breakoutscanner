"""
funds.py — tell an ETF from a company.

Goes in /opt/breakoutscanner/funds.py, beside build_snapshot.py.

WHY THIS EXISTS
===============
On NSE an ETF lists in series EQ and appears in Kite's instrument dump with
instrument_type=EQ, exactly like an ordinary share. There is no flag. So 301
funds sat in the 3,051-symbol universe, were scanned for breakouts, and were
given RS Ratings and Minervini scores.

The measured damage, as of the 2026-08-14 snapshot:

  - NV20 held RS 99 — the highest-rated symbol in the entire universe was
    the Kotak Nifty 50 Value 20 ETF.
  - Fifteen funds sat at RS >= 80, O'Neil's threshold and Minervini's
    criterion 8. Twelve of them were silver ETFs: SILVERAG, TATSILV,
    SILVERBETA, SILVERADD, SILVERCASE, HDFCSILVER, SILVERIETF, SILVER360,
    ESILVER, SILVER, SBISILVER, SILVERBEES. That is not twelve ideas. It is
    silver, twelve times.
  - Fifteen gold ETFs sat just below, at 71-74.
  - MONQ50 scored Minervini 8/8 at RS 90, passing every gate the Daily
    Screen applies.

Removing them from the RS pool moves a real stock by only +1 to +2 points, so
the percentile distortion is minor. The duplication is not: a screen showing
twelve strong metals names was showing one commodity in twelve wrappers.

CLASSIFICATION IS A HEURISTIC, NOT A REGISTRY
=============================================
NSE publishes no machine-readable ETF flag in the instrument dump, so this
matches on symbol and name. Two false positives were found and fixed while
building it, both of the same kind — a pattern that matched an English word
inside a real company's name:

    \\bAMC\\b   flagged HDFCAMC, UTIAMC and ABSLAMC. Asset managers are
              ordinary listed companies. Pattern removed.

    BEES      flagged FIRSTCRY, whose listed entity is BRAINBEES SOLUTIONS.
              Now requires a word boundary, so "Brainbees" no longer matches
              while "Nifty BeES" still does.

That is the same family of error as deleting 360ONE for starting with a digit
and BAJAJ-AUTO for containing a hyphen. Assume there are more, which is why
nothing here deletes anything: symbols are FLAGGED, and the flag is a column
you can filter on or ignore.
"""

from __future__ import annotations

import json
import os
import re
import time

# Symbol-level giveaways. BEES is Nippon's ETF brand; ETF/IETF are literal.
SYM_PAT = re.compile(r"(BEES$|ETF$|IETF$|^LIQUID|^GOLDSHARE)", re.I)

# Name-level giveaways from the Kite dump's `name` column.
# NOT \bAMC\b — see the module docstring.
NAME_PAT = re.compile(r"(EXCHANGE TRADED|\bETF\b|\bBEES\b|MUTUAL FUND|"
                      r"INDEX FUND)", re.I)

# Trackers whose symbol and name match neither pattern.
KNOWN = {
    "NV20", "MON100", "MOM100", "MOM50", "MAFANG", "HNGSNGBEES", "CPSEETF",
    "PSUBNKBEES", "SETFNIF50", "SETFNIFBK", "SETFGOLD", "UTISENSETF",
    "MOGSEC", "LICNETFN50", "LICNETFSEN", "LICNMFET", "AXISNIFTY",
    "ICICIB22", "EQUAL50ADD", "ALPHA", "ALPHAETF", "MIDCAPETF",
    "SILVERBEES", "GOLDBEES", "NIFTYBEES", "JUNIORBEES", "BANKBEES",
    "INFRABEES", "SHARIABEES", "QNIFTY", "NETF", "NETFNIF50",
}

INSTRUMENTS_URL = "https://api.kite.trade/instruments"
NAME_CACHE = "kite_names.json"
FUND_LIST = "nse_funds.txt"
CACHE_DAYS = 7

_MEM: dict[str, set] = {}


def classify(symbol: str, name: str = "") -> str | None:
    """Why this looks like a fund, or None if it looks like a company."""
    s = (symbol or "").upper().strip()
    n = (name or "").upper().strip()
    if not s:
        return None
    if s in KNOWN:
        return "known tracker"
    if SYM_PAT.search(s):
        return "symbol pattern"
    if n and NAME_PAT.search(n):
        return "name pattern"
    return None


def _cache_dir(app: str) -> str:
    d = os.path.join(app, "data_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _names(app: str) -> dict:
    """
    tradingsymbol -> name, from Kite's public dump, cached for CACHE_DAYS.

    Cached because build_snapshot must not depend on a network call it does
    not need. A stale name map costs nothing: fund names do not change, and a
    newly listed ETF is caught by the symbol patterns in the meantime.
    """
    path = os.path.join(_cache_dir(app), NAME_CACHE)
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < CACHE_DAYS * 86400:
            try:
                with open(path) as fh:
                    return json.load(fh)
            except Exception:
                pass
    try:
        import io
        import urllib.request

        import pandas as pd
        raw = urllib.request.urlopen(INSTRUMENTS_URL, timeout=60).read()
        dump = pd.read_csv(io.BytesIO(raw), low_memory=False)
        dump = dump[(dump["segment"] == "NSE")
                    & (dump["instrument_type"].astype(str).str.upper() == "EQ")]
        out = {}
        for ts, nm in zip(dump["tradingsymbol"], dump["name"]):
            out[str(ts).split("-")[0].upper()] = str(nm or "")
        with open(path, "w") as fh:
            json.dump(out, fh)
        return out
    except Exception:
        # No network, or the dump moved. Symbol patterns still work; they
        # catch most funds on their own. Better a partial flag than a build
        # that fails over a cosmetic column.
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    return json.load(fh)
            except Exception:
                pass
        return {}


def fund_symbols(app: str, symbols=None, write: bool = True) -> set:
    """
    The set of symbols that look like funds. Computed once per process.

    Also writes data_cache/nse_funds.txt so the classification can be read
    and argued with rather than taken on trust.
    """
    key = os.path.abspath(app)
    if key in _MEM:
        return _MEM[key]

    names = _names(app)
    pool = list(symbols) if symbols is not None else list(names)
    found, why = set(), {}
    for s in pool:
        u = str(s).upper().strip()
        r = classify(u, names.get(u, ""))
        if r:
            found.add(u)
            why[u] = r

    if write:
        try:
            path = os.path.join(_cache_dir(app), FUND_LIST)
            with open(path, "w") as fh:
                fh.write("# symbols classified as ETFs/funds by funds.py\n")
                fh.write("# generated %s from %d names\n"
                         % (time.strftime("%F %T"), len(names)))
                fh.write("# these are FLAGGED, not deleted — is_fund in the "
                         "snapshot\n")
                for s in sorted(found):
                    fh.write("%-14s %s\n" % (s, why[s]))
        except Exception:
            pass

    _MEM[key] = found
    return found


def is_fund(symbol: str, app: str) -> bool:
    return str(symbol).upper().strip() in fund_symbols(app)


if __name__ == "__main__":                              # pragma: no cover
    import sys
    a = sys.argv[1] if len(sys.argv) > 1 else "/opt/breakoutscanner"
    f = fund_symbols(a)
    print("%d symbols classified as funds" % len(f))
    print("written to %s" % os.path.join(a, "data_cache", FUND_LIST))
