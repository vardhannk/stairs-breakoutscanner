#!/usr/bin/env python3
"""
probe_cache.py — does load_daily actually use the cache? Count, do not infer.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
      /opt/breakoutscanner-lab/probe_cache.py

READ-ONLY apart from whatever load_daily itself writes. Lab by default.

WHY
===
Three changes in, the build still costs ~0.5s per symbol even for symbols
with a deep, fresh cache file on disk. Every explanation so far has been a
plausible story checked against a total runtime, and totals have been
confounded twice already — once by a shifted --limit window, once by symbols
prefetch never covered.

So this stops reasoning about elapsed time and counts the thing that matters
directly: it wraps fetch_daily and fetch_daily_range, calls load_daily exactly
as build_snapshot does, and reports per symbol whether the network was
touched.

WHAT THE ANSWER MEANS
=====================
  0 fetches, fast            cache works; the cost is elsewhere (ranking,
                             trend template, pattern computation) and the
                             next thing to profile is the compute stage.

  fetches on the 1M call     min_rows is still rejecting, or `fresh` is
                             false because the cached last bar is older
                             than 3 days.

  fetches on every call      the cache file is not being READ at all —
                             _read_csv_cache raising, or a path mismatch
                             between what prefetch writes and what
                             load_daily looks for.

Each needs a different fix, which is exactly why guessing between them has
cost three rounds.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

LAB = "/opt/breakoutscanner-lab"
G, R, Y, C, B, X = ("\033[32m", "\033[31m", "\033[33m",
                    "\033[36m", "\033[1m", "\033[0m")


def hdr(s): print(f"\n{C}{'=' * 70}\n== {s}\n{'=' * 70}{X}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default=LAB)
    ap.add_argument("-n", type=int, default=12, help="symbols to probe")
    a = ap.parse_args()
    app = os.path.abspath(a.app)
    sys.path.insert(0, app)

    import pandas as pd
    import data_loader as DL
    from config import CACHE_DAILY, LOOKBACK_DAYS, MONTHLY_LOOKBACK_DAYS

    hdr(f"1. environment  ({app})")
    print(f"  CACHE_DAILY             {CACHE_DAILY}")
    print(f"  LOOKBACK_DAYS           {LOOKBACK_DAYS}")
    print(f"  MONTHLY_LOOKBACK_DAYS   {MONTHLY_LOOKBACK_DAYS}")
    cache = str(CACHE_DAILY)
    files = sorted(f for f in os.listdir(cache) if f.endswith(".csv"))
    print(f"  cached files            {len(files):,}")

    # min_rows as the patched code computes it, for both call sites.
    def min_rows(days):
        return 60  # post-patch; printed for the record

    # ── count network calls ────────────────────────────────────────────────
    calls = {"fetch_daily": 0, "fetch_daily_range": 0}
    orig_fd, orig_fdr = DL.fetch_daily, DL.fetch_daily_range

    def wrap_fd(*args, **kw):
        calls["fetch_daily"] += 1
        return orig_fd(*args, **kw)

    def wrap_fdr(*args, **kw):
        calls["fetch_daily_range"] += 1
        return orig_fdr(*args, **kw)

    DL.fetch_daily = wrap_fd
    DL.fetch_daily_range = wrap_fdr

    syms = [f[:-4] for f in files[:a.n]]
    hdr(f"2. load_daily on {len(syms)} cached symbols, as build_snapshot calls it")
    print(f"  {'symbol':<14}{'bars':>7}{'last bar':>13}"
          f"{'400d':>8}{'net':>5}{'1500d':>8}{'net':>5}   verdict")

    tot_net = 0
    tot_t = 0.0
    for s in syms:
        p = os.path.join(cache, f"{s}.csv")
        try:
            cached = DL._read_csv_cache(p)
            nbars = len(cached)
            last = str(cached.index[-1])[:10] if nbars else "-"
        except Exception as e:
            nbars, last = -1, f"READ FAIL {type(e).__name__}"

        before = sum(calls.values())
        t0 = time.time()
        DL.load_daily(s, days=LOOKBACK_DAYS, use_cache=True)
        t1 = time.time()
        n1 = sum(calls.values()) - before

        before = sum(calls.values())
        t2 = time.time()
        DL.load_daily(s, days=MONTHLY_LOOKBACK_DAYS, use_cache=True)
        t3 = time.time()
        n2 = sum(calls.values()) - before

        tot_net += n1 + n2
        tot_t += (t1 - t0) + (t3 - t2)
        verdict = ("cache" if n1 + n2 == 0 else
                   ("1M only" if n1 == 0 else "BOTH fetch"))
        col = G if n1 + n2 == 0 else (Y if n1 == 0 else R)
        print(f"  {s:<14}{nbars:>7}{last:>13}"
              f"{t1 - t0:>7.2f}s{n1:>5}{t3 - t2:>7.2f}s{n2:>5}   {col}{verdict}{X}")

    hdr("3. verdict")
    print(f"  network calls   {tot_net} across {len(syms)} symbols "
          f"({tot_net / max(len(syms), 1):.1f} per symbol)")
    print(f"  time            {tot_t:.1f}s  ({tot_t / max(len(syms), 1):.3f}s per symbol)")
    print(f"  breakdown       fetch_daily={calls['fetch_daily']}  "
          f"fetch_daily_range={calls['fetch_daily_range']}")

    if tot_net == 0:
        print(f"""
  {G}The cache IS being used.{X} Then the ~0.5s per symbol in the build is
  NOT download time, and the next thing to measure is the compute stage —
  compute_metrics, compute_patterns and the breakout detector, which run
  over three timeframes per symbol. Profile those before changing anything
  else.""")
    elif calls["fetch_daily_range"] > calls["fetch_daily"]:
        print(f"""
  {Y}Mostly incremental fetches.{X} The cache is being READ but judged
  out of date: load_daily takes the incremental branch whenever the last
  cached bar is before today, which on any day the market has traded
  means every symbol pays one round trip. That is the remaining cost,
  and prefetch should be the thing that satisfies it.""")
    else:
        print(f"""
  {R}Full downloads.{X} The cache is being rejected outright. Look at the
  `bars` and `last bar` columns above: a READ FAIL means the file cannot
  be parsed, and a stale last bar means `fresh` is false. Neither is
  fixed by another threshold change.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
