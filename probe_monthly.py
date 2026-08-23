#!/usr/bin/env python3
"""
probe_monthly.py — why did 1M breakouts drop from 21 to 1?

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
      /opt/breakoutscanner-lab/probe_monthly.py

READ-ONLY. Lab by default. Makes a few network calls to build the comparison.

THE OBSERVATION
===============
Making the price cache readable took a 300-symbol build from 199s to 50s with
zero downloads. In the same change:

    1D    5 -> 6      unchanged in substance
    1W   17 -> 17     unchanged
    1M   21 ->  1     collapsed

Speed came from reading the cache instead of downloading. If the two paths
returned the same bars, the counts would not move. They moved on exactly one
timeframe, so the cache path and the network path must disagree about the
monthly series.

WHAT THIS COMPARES
==================
For each symbol, side by side:

    load_bars(sym, "1M", use_cache=True)    the cache path, now used
    load_bars(sym, "1M", use_cache=False)   the network path, used before

and prints bar counts, the last three monthly closes, index dtype and
timezone. A difference in any of those is the answer; identical output on
both would mean the cause is upstream in compute_patterns instead.

WHY dtype AND tz ARE PRINTED
============================
yfinance builds a DatetimeIndex that may carry a timezone. The cache reader
rebuilds it with pd.to_datetime from a CSV, which is naive. Monthly resampling
is sensitive to that: a tz-aware index anchored to an exchange timezone can
place a bar in a different month than the same instants read back as naive
UTC, which would shift the most recent monthly bar and change whether it
counts as a breakout.
"""

from __future__ import annotations

import argparse
import os
import sys

LAB = "/opt/breakoutscanner-lab"
G, R, Y, C, B, X = ("\033[32m", "\033[31m", "\033[33m",
                    "\033[36m", "\033[1m", "\033[0m")


def hdr(s): print(f"\n{C}{'=' * 72}\n== {s}\n{'=' * 72}{X}")


def describe(df):
    if df is None or len(df) == 0:
        return {"n": 0, "dtype": "-", "tz": "-", "last": "-", "closes": "-"}
    idx = df.index
    tz = getattr(idx, "tz", None)
    closes = ""
    if "close" in df.columns:
        closes = ", ".join(f"{v:.2f}" for v in df["close"].tail(3))
    return {
        "n": len(df),
        "dtype": str(idx.dtype),
        "tz": str(tz) if tz is not None else "naive",
        "last": str(idx[-1])[:19],
        "closes": closes,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default=LAB)
    ap.add_argument("--symbols", default="RELIANCE,TCS,3MINDIA,20MICRONS")
    a = ap.parse_args()
    app = os.path.abspath(a.app)
    sys.path.insert(0, app)

    import data_loader as DL

    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    hdr(f"1M bars: cache path vs network path  ({app})")

    for s in syms:
        print(f"\n{B}  {s}{X}")
        try:
            cached = DL.load_bars(s, "1M", use_cache=True)
        except Exception as e:
            print(f"    cache   ERROR {type(e).__name__}: {e}")
            cached = None
        try:
            fresh = DL.load_bars(s, "1M", use_cache=False)
        except Exception as e:
            print(f"    network ERROR {type(e).__name__}: {e}")
            fresh = None

        c, f = describe(cached), describe(fresh)
        print(f"    {'':<10}{'bars':>6}{'index dtype':>26}{'tz':>10}"
              f"{'last bar':>22}")
        print(f"    {'cache':<10}{c['n']:>6}{c['dtype']:>26}{c['tz']:>10}"
              f"{c['last']:>22}")
        print(f"    {'network':<10}{f['n']:>6}{f['dtype']:>26}{f['tz']:>10}"
              f"{f['last']:>22}")
        print(f"    last 3 closes  cache   {c['closes']}")
        print(f"    last 3 closes  network {f['closes']}")

        flags = []
        if c["n"] != f["n"]:
            flags.append(f"BAR COUNT differs ({c['n']} vs {f['n']})")
        if c["tz"] != f["tz"]:
            flags.append(f"TIMEZONE differs ({c['tz']} vs {f['tz']})")
        if c["last"] != f["last"]:
            flags.append("LAST BAR differs")
        if c["closes"] != f["closes"]:
            flags.append("CLOSES differ")
        if flags:
            for fl in flags:
                print(f"    {R}-> {fl}{X}")
        else:
            print(f"    {G}-> identical{X}")

    # The daily series underneath, since 1M is derived from it.
    hdr("the daily series the monthly resample is built from")
    print(f"  {'symbol':<12}{'cache bars':>12}{'net bars':>10}"
          f"{'cache tz':>12}{'net tz':>10}{'cache last':>14}{'net last':>14}")
    for s in syms:
        try:
            cd = DL.load_daily(s, days=1500, use_cache=True)
            fd = DL.load_daily(s, days=1500, use_cache=False)
        except Exception as e:
            print(f"  {s:<12} ERROR {type(e).__name__}: {e}")
            continue
        c, f = describe(cd), describe(fd)
        mark = "" if (c["n"] == f["n"] and c["tz"] == f["tz"]) else "   <-- differs"
        print(f"  {s:<12}{c['n']:>12}{f['n']:>10}{c['tz']:>12}{f['tz']:>10}"
              f"{c['last'][:10]:>14}{f['last'][:10]:>14}{mark}")

    hdr("how to read this")
    print("""  BAR COUNT differs on 1M but not on daily
      -> resample_monthly is treating the two indexes differently.
         Almost certainly tz-awareness: month boundaries move.

  TIMEZONE differs
      -> the direct cause. The cache reader must reproduce whatever
         the network path produces, not merely parse the dates.

  daily bar counts differ
      -> the cache is shorter than a fresh download, so the monthly
         series simply has less to work with. Prefetch depth is then
         the thing to change.

  everything identical
      -> the difference is not in the data, and the next place to look
         is compute_patterns / detect_breakout on the 1M timeframe.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
