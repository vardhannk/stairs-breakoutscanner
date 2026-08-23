#!/usr/bin/env python3
"""
patch_screen.py — wire screen.py into the breakout scanner.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/patch_screen.py --check
    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/patch_screen.py --apply

Adds the screening criteria as COLUMNS on every scan result. Nothing is
filtered automatically — you see price, rs_vs_nifty, rs_rank, avg_vol_10d,
turnover, above_50dma and circuit_suspect alongside each breakout and decide
what to cut. Filtering silently would hide why a name disappeared.

RS is always computed from DAILY bars even when scanning 1H/1W/1M, because
"3-month relative strength" is a daily-timeframe concept. Daily bars for the
scanned symbols are usually already in the cache; where they aren't, they're
loaded once.

Idempotent. Backs up. Verifies the result parses and imports.
"""

import argparse
import ast
import datetime
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCANNER = os.path.join(HERE, "scanner.py")
SCREEN = os.path.join(HERE, "screen.py")
STAMP = datetime.datetime.now().strftime("%F-%H%M%S")

ANCHOR = """    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)"""

REPLACEMENT = '''    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # ── screen columns (added by patch_screen.py) ────────────────
    # Columns only. No rows are dropped here — filtering happens in the UI
    # so you can always see what a criterion would remove.
    try:
        import screen as _screen
        from data_loader import load_bars as _load_bars, load_daily as _load_daily

        _nifty = _load_bars("NIFTY", "1D", use_cache=use_cache)
        _bench = _screen.nifty_return(_nifty)
        _bench1m = _screen.nifty_return(_nifty, window=_screen.RET_1M_WINDOW)

        _daily_cache = {}
        def _daily_for(sym):
            if sym in _daily_cache:
                return _daily_cache[sym]
            d = bar_cache.get((sym, "1D"))
            if d is None or d.empty:
                try:
                    d = _load_daily(sym, use_cache=use_cache)
                except Exception:
                    d = None
            _daily_cache[sym] = d
            return d

        _metrics = []
        for _sym in df["symbol"]:
            try:
                _metrics.append(_screen.compute_metrics(
                    _daily_for(_sym), _bench, index_bars=_nifty,
                    bench_return_1m=_bench1m))
            except Exception:
                _metrics.append({})
        _mdf = pd.DataFrame(_metrics, index=df.index)
        for _c in _mdf.columns:
            df[_c] = _mdf[_c]
        # sector membership from NSE sectoral-index constituent lists
        try:
            import sectors as _sec
            _sm = _sec.sector_map()
            if _sm:
                _all = _sec.sector_map_all()
                df["sector"] = df["symbol"].map(
                    lambda _s: _sm.get(str(_s).upper(), ""))
                df["sectors_all"] = df["symbol"].map(
                    lambda _s: ", ".join(_all.get(str(_s).upper(), [])))
        except Exception:
            pass

        df = _screen.add_ret_1m_rank(df)  # "top monthly gainers"
        df = _screen.add_rs_rank(df)      # percentile of rs_vs_nifty
        df = _screen.add_rs_rating(df)    # O'Neil 1-99 from oneil_score
        # Minervini Trend Template — after rs_rating, criterion 8 needs it
        df = _screen.add_trend_template(df, _daily_for)

        # All-time high: needs deeper history than load_daily fetches, so it
        # is fetched per RESULT row (a handful), never for the whole universe.
        # Capped so a very wide scan cannot turn into hundreds of downloads.
        if len(df) <= 60:
            import yfinance as _yf
            from data_loader import yahoo_ticker as _yt
            _deep = {}
            def _deep_for(sym):
                if sym in _deep:
                    return _deep[sym]
                d = None
                try:
                    raw = _yf.download(_yt(sym), period="max", interval="1d",
                                       progress=False, auto_adjust=True, threads=False)
                    if raw is not None and not raw.empty:
                        if hasattr(raw.columns, "get_level_values"):
                            try:
                                raw.columns = raw.columns.get_level_values(0)
                            except Exception:
                                pass
                        raw.columns = [str(c).lower() for c in raw.columns]
                        d = raw
                except Exception:
                    d = None
                _deep[sym] = d
                return d
            df = _screen.add_ath(df, _deep_for)
        df.attrs["nifty_3m_return_pct"] = (
            round(_bench * 100.0, 2) if _bench == _bench else None)
    except Exception as _se:
        # screen columns are additive — never let them break a scan
        try:
            import logging
            logging.getLogger(__name__).warning("screen columns skipped: %s", _se)
        except Exception:
            pass
    # ───────────────────────────────────────────────────────────────────
'''


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if not os.path.isfile(SCREEN):
        print("FAIL: screen.py must sit beside scanner.py"); return 2
    if not os.path.isfile(SCANNER):
        print(f"FAIL: {SCANNER} not found"); return 2

    src = open(SCANNER).read()

    # The marker was renamed (dropped a word) after the first deploys, so an
    # installed scanner.py may carry either form. Try both or the upgrade path
    # cannot find the old block's start.
    START_CANDIDATES = [
        "    # \u2500\u2500 screen columns (added by patch_screen.py)",
        "    # \u2500\u2500 mentor screen columns (added by patch_screen.py)",
    ]
    END = "    # \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"

    if "screen columns" in src:
        # Distinguish "current" from "stale": an older run of this script may
        # have inserted a block that predates later columns. Re-running is the
        # only way to pick those up, so strip the old block and re-insert.
        stale = [t for t in ("add_ret_1m_rank", "bench_return_1m",
                             "add_trend_template", "sector_map")
                 if t not in src]
        if not stale:
            print("ok   already patched (current)"); return 0
        print(f"warn existing patch is STALE — missing: {', '.join(stale)}")
        i = -1
        for _c in START_CANDIDATES:
            i = src.find(_c)
            if i != -1:
                break
        j = src.find(END, i) if i != -1 else -1
        if i == -1 or j == -1:
            print("FAIL could not locate the old block's boundaries.")
            print("     Restore from a scanner.py.bak-screen-* backup and re-run.")
            return 3
        j = src.find("\n", j) + 1
        src = src[:i] + src[j:]
        print("ok   removed the stale block; will re-insert the current one")
        if not a.apply:
            print("\nwarn --check only. Re-run with --apply to write.")
            return 0

    if ANCHOR not in src:
        print("FAIL: scan_universe() does not match the expected shape.")
        print("      Looked for the 'if not rows: return pd.DataFrame()' block.")
        return 3

    patched = src.replace(ANCHOR, REPLACEMENT, 1)

    try:
        ast.parse(patched)
        print("ok   patched scanner.py parses")
    except SyntaxError as e:
        print(f"FAIL: would not parse ({e}); nothing written"); return 4

    print("     will add columns: price, ret_3m, rs_vs_nifty, rs_rank,")
    print("                       avg_vol_10d, turnover_10d, vol_today_extrapolated,")
    print("                       dma50, above_50dma, circuit_suspect")

    if not a.apply:
        print("\nwarn --check only. Re-run with --apply to write.")
        return 0

    b = f"{SCANNER}.bak-screen-{STAMP}"
    shutil.copy2(SCANNER, b)
    open(SCANNER, "w").write(patched)
    print(f"ok   backed up -> {b}")

    r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0,%r); import scanner" % HERE],
                       capture_output=True, text=True)
    if r.returncode != 0:
        shutil.copy2(b, SCANNER)
        print(f"FAIL: scanner.py no longer imports; ORIGINAL RESTORED\n{r.stderr}")
        return 5
    print("ok   scanner.py imports cleanly")
    print("\nNext:  sudo systemctl restart breakoutscanner")
    print("Then run a scan — the new columns appear in the table view.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
