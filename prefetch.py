#!/usr/bin/env python3
"""
prefetch.py — fill the daily price cache in batches, once, deeply.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
      /opt/breakoutscanner/prefetch.py --check --limit 200
    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
      /opt/breakoutscanner/prefetch.py --apply

IT ACTS ON THE APP IT LIVES IN. --app defaults to this file's own directory,
so the copy in /opt/breakoutscanner refreshes that cache and the copy in
/opt/breakoutscanner-lab refreshes the lab's. Check the first line of output
— it prints the directory it resolved — before trusting a long run.

    wiring (/opt/breakoutscanner)       the original
    wiring (/opt/breakoutscanner-lab)   the lab

This is not a detail. The default used to be hardcoded to the lab, so after
promotion the original's copy silently refreshed the LAB's cache. Three runs
went to the wrong app, and the only visible symptom was the build afterwards
reporting 1M: 0 — because the original's cache stayed shallow and a monthly
series of 19 bars cannot satisfy a 20-bar lookback.

THE PROBLEM IT SOLVES
=====================
A full build takes ~1,480s for ~3,300 symbols. That is not the symbol count,
it is one line in data_loader.load_daily:

    min_rows = min(60, days // 3) if days <= 500 else int(days * 0.45)

The 1D and 1W paths ask for days=400, so min_rows=60 and the cache is used.
The 1M path asks for days=1500, so min_rows=675 — about 2.7 years of trading
days. A cached file with less than that fails the check and makes a network
call, every symbol, every run. Recently listed stocks can NEVER satisfy it, so
for them the cache is permanently useless.

Every fetch is also wrapped in a module-global _YF_LOCK, so those calls are
strictly serialised. Threading the build would change nothing.

    3,300 symbols x ~0.4s serialised = ~1,320s.   Observed: 1,480s.

WHAT THIS DOES INSTEAD
======================
yf.download() has always accepted a LIST of tickers; the app calls it one
symbol at a time. This fetches ~100 per request, in one deep window that
satisfies both the 400- and 1500-day callers, and writes the per-symbol CSVs
the app already reads.

    ~3,300 serialised requests  ->  ~34 batched requests

After this runs, load_daily() finds a deep, fresh cache and returns without
touching the network — for the 1M path as well as 1D and 1W.

WHY A SEPARATE PASS RATHER THAN CHANGING load_daily
===================================================
Because the two jobs are different. Fetching is an I/O problem that wants
batching and retries; reading is a hot path called three times per symbol per
build. Keeping "get the data" out of "use the data" means the build becomes a
pure cache reader, and a fetch failure is visible here as a named symbol
rather than a silently missing row 20 minutes later.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta

# Default to the directory THIS FILE lives in, not a hardcoded path.
#
# This used to be `LAB = "/opt/breakoutscanner-lab"`, so once the script was
# promoted into /opt/breakoutscanner it still refreshed the LAB's cache — from
# the original's directory, with the original's symbol list, silently. Three
# prefetch runs went to the wrong app before the header line gave it away.
#
# A script that is meant to be copied between installations must not carry one
# installation's path as a default. Where it sits is the only reliable signal
# of which app it belongs to.
DEFAULT_APP = os.path.dirname(os.path.abspath(__file__))

G, R, Y, C, B, X = ("\033[32m", "\033[31m", "\033[33m",
                    "\033[36m", "\033[1m", "\033[0m")


def hdr(s): print(f"\n{C}{'=' * 66}\n== {s}\n{'=' * 66}{X}")
def ok(s):  print(f"  {G}ok  {X} {s}")
def bad(s): print(f"  {R}FAIL{X} {s}")
def warn(s): print(f"  {Y}warn{X} {s}")


def split_batch(df, tickers: list[str]):
    """yf.download returns different shapes for one ticker vs many.

    Many  -> MultiIndex columns, (ticker, field) or (field, ticker) depending
             on group_by and yfinance version.
    One   -> flat columns.

    Guessing which would be exactly the kind of assumption that has cost us
    time today, so this inspects the object instead.
    """
    out: dict = {}
    if df is None or len(df) == 0:
        return out
    cols = df.columns
    if not hasattr(cols, "levels"):
        if len(tickers) == 1:
            out[tickers[0]] = df
        return out

    lvl0 = set(map(str, cols.get_level_values(0)))
    ticker_on_level0 = len(lvl0 & set(tickers)) > 0
    for t in tickers:
        try:
            sub = df[t] if ticker_on_level0 else df.xs(t, axis=1, level=1)
        except Exception:
            continue
        sub = sub.dropna(how="all")
        if len(sub):
            out[t] = sub
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default=DEFAULT_APP,
                    help="app directory (defaults to where this script lives)")
    ap.add_argument("--symbols-file", default=None)
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--days", type=int, default=2400,
                    help="calendar days of history; must cover "
                         "MONTHLY_LOOKBACK_DAYS * 1.6")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pause", type=float, default=1.0,
                    help="seconds between batches, to stay under rate limits")
    ap.add_argument("--check", action="store_const", const="check", dest="mode")
    ap.add_argument("--apply", action="store_const", const="apply", dest="mode")
    a = ap.parse_args()
    mode = a.mode or "check"

    app = os.path.abspath(a.app)
    if app.rstrip("/") == "/opt/breakoutscanner":
        warn("--app points at the ORIGINAL app, not the lab.")
        warn("That is allowed but not the default. Ctrl-C now if unintended.")
        time.sleep(4)
    sys.path.insert(0, app)

    hdr(f"1. wiring  ({app})")
    try:
        import pandas as pd
        import yfinance as yf
        import data_loader as DL
    except Exception as e:
        bad(f"import failed: {e}")
        return 2
    ok(f"yfinance {getattr(yf, '__version__', '?')}, pandas {pd.__version__}")

    cache = getattr(DL, "CACHE_DAILY", None)
    if cache is None:
        from config import CACHE_DAILY as cache
    cache = str(cache)
    ok(f"cache dir {cache}")
    if not cache.startswith(app):
        bad(f"cache {cache} is outside {app} — refusing, this would write "
            f"into another app's data")
        return 2

    for fn in ("yahoo_ticker", "_normalize_ohlcv"):
        if not hasattr(DL, fn):
            bad(f"data_loader.{fn} missing — the writer must match the reader")
            return 2
    ok("yahoo_ticker and _normalize_ohlcv available")

    syms_file = a.symbols_file or os.path.join(app, "nse_all_equity.txt")
    if not os.path.isfile(syms_file):
        bad(f"{syms_file} not found")
        return 2
    with open(syms_file) as fh:
        syms = sorted({l.strip().upper() for l in fh
                       if l.strip() and not l.startswith("#")})
    if a.limit:
        syms = syms[:a.limit]
    ok(f"{len(syms):,} symbols from {os.path.basename(syms_file)}")

    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=a.days)
    n_batches = (len(syms) + a.batch - 1) // a.batch
    print(f"\n  window     {start} -> {end}  ({a.days} calendar days)")
    print(f"  batches    {n_batches} x {a.batch}")
    print(f"  {B}vs the current path: {len(syms):,} serialised requests{X}")

    if mode == "check":
        hdr("2. one batch, to prove the shape handling")
        probe = syms[:min(8, len(syms))]
        tick = [DL.yahoo_ticker(s) for s in probe]
        t0 = time.time()
        try:
            raw = yf.download(tick, start=start.isoformat(), end=end.isoformat(),
                              interval="1d", auto_adjust=True, progress=False,
                              group_by="ticker", threads=True)
        except Exception as e:
            bad(f"batch download failed: {type(e).__name__}: {e}")
            return 3
        el = time.time() - t0
        parts = split_batch(raw, tick)
        ok(f"{len(probe)} tickers in {el:.1f}s -> {len(parts)} frames")
        for s, t in list(zip(probe, tick))[:5]:
            d = parts.get(t)
            if d is None or d.empty:
                warn(f"{s:<12} no data")
            else:
                nd = DL._normalize_ohlcv(d)
                print(f"       {s:<12} {len(nd):>5} bars  "
                      f"{str(nd.index[0])[:10]} -> {str(nd.index[-1])[:10]}  "
                      f"cols={list(nd.columns)}")
        per = el / max(len(probe), 1)
        print(f"\n  {per:.3f}s per symbol batched; a full run would be roughly "
              f"{per * len(syms) / 60:.1f} min")
        print(f"\n{Y}--check only. Nothing written.{X}")
        return 0

    # ── the real pass ──────────────────────────────────────────────────────
    hdr("2. fetching")
    os.makedirs(cache, exist_ok=True)
    written = failed = 0
    no_data: list[str] = []
    t0 = time.time()
    for i in range(0, len(syms), a.batch):
        chunk = syms[i:i + a.batch]
        tick = [DL.yahoo_ticker(s) for s in chunk]
        try:
            raw = yf.download(tick, start=start.isoformat(), end=end.isoformat(),
                              interval="1d", auto_adjust=True, progress=False,
                              group_by="ticker", threads=True)
            parts = split_batch(raw, tick)
        except Exception as e:
            bad(f"batch {i // a.batch + 1}: {type(e).__name__}: {str(e)[:70]}")
            failed += len(chunk)
            continue

        for s, t in zip(chunk, tick):
            d = parts.get(t)
            if d is None or len(d) == 0:
                no_data.append(s)
                continue
            try:
                nd = DL._normalize_ohlcv(d)
                if nd is None or nd.empty:
                    no_data.append(s)
                    continue
                p = os.path.join(cache, f"{s}.csv")
                # Merge rather than overwrite: an existing cache may reach
                # further back than this window, and throwing that away would
                # quietly shorten the history the indicators run on.
                if os.path.isfile(p):
                    try:
                        old = DL._read_csv_cache(p)
                        if old is not None and not old.empty:
                            nd = pd.concat([old, nd])
                            nd = nd[~nd.index.duplicated(keep="last")].sort_index()
                    except Exception:
                        pass
                nd.to_csv(p)
                written += 1
            except Exception as e:
                bad(f"{s}: {type(e).__name__}: {str(e)[:50]}")
                failed += 1

        done = min(i + a.batch, len(syms))
        el = time.time() - t0
        print(f"  {done}/{len(syms)}  {el:5.0f}s  "
              f"eta {el / done * (len(syms) - done):4.0f}s", flush=True)
        if a.pause:
            time.sleep(a.pause)

    el = time.time() - t0
    hdr("3. result")
    ok(f"{written:,} cached, {len(no_data)} no data, {failed} errors, {el:.0f}s")
    print(f"       {el / max(len(syms), 1):.3f}s per symbol "
          f"(the serialised path was ~0.45s)")
    if no_data:
        p = os.path.join(cache, "..", "prefetch_no_data.txt")
        try:
            with open(p, "w") as fh:
                fh.write("\n".join(sorted(no_data)) + "\n")
            print(f"       symbols without data written to {os.path.abspath(p)}")
        except OSError:
            pass
        print(f"       e.g. {', '.join(sorted(no_data)[:8])}")

    print(f"""
  {B}Now run the build. It should read cache and finish in a fraction of
  the time — that comparison is the point, so time it:{X}

      time sudo -u breakout /opt/breakoutscanner/.venv/bin/python \\
        {app}/build_snapshot.py --symbols-file {syms_file}

  {Y}If it is still slow, the cache is being rejected rather than missing,
  and the min_rows threshold is the next thing to change — not this.{X}""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
