#!/usr/bin/env python3
"""
patch_lab_cache_read.py — make the price cache readable.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
      /opt/breakoutscanner-lab/patch_lab_cache_read.py --check
    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
      /opt/breakoutscanner-lab/patch_lab_cache_read.py --apply

LAB ONLY. Refuses to touch /opt/breakoutscanner.

THE BUG
=======
    def _read_csv_cache(path):
        df = pd.read_csv(path, parse_dates=["date"], index_col="date")

Every cache file on disk starts:

    Date,open,high,low,close,volume
    2020-01-20,282.48,286.37,265.28,276.64,92184

`Date`, capital D. The reader asks for `date`. pandas raises ValueError, the
caller catches it broadly, and load_daily falls through to the network.

Every read. Every symbol. Every timeframe. Every run. A 144 MB cache that has
never once been used, on both the original app and this copy — ZYDUSWELL.csv,
which prefetch never touched, has the identical header.

This is the 25-minute build. Not the symbol count, not min_rows, not the
retry skip. Those were real issues but they sat downstream of a read that
always threw, so none of them could ever have shown an effect — which is
exactly what the measurements kept saying, three times, before I stopped
inferring and counted the network calls.

THE FIX
=======
Find the date column case-insensitively, and fall back to the first column if
there is no obvious name. The writer is left alone: rewriting 3,367 files to
change one header is a bigger, riskier change than teaching the reader to
accept what a decade of files already contain — and it would not help any
cache written before the change.

WHY NOT JUST LOWERCASE THE COLUMNS
==================================
Because `parse_dates` and `index_col` are applied by read_csv at parse time,
before any post-processing could rename anything. The name has to be right on
the way in.
"""

from __future__ import annotations

import argparse
import os
import py_compile
import shutil
import sys
import tempfile
from datetime import datetime

LAB = "/opt/breakoutscanner-lab"
G, R, Y, C, B, X = ("\033[32m", "\033[31m", "\033[33m",
                    "\033[36m", "\033[1m", "\033[0m")


def hdr(s): print(f"\n{C}{'=' * 68}\n== {s}\n{'=' * 68}{X}")
def ok(s):  print(f"  {G}ok  {X} {s}")
def bad(s): print(f"  {R}FAIL{X} {s}")


OLD = '''def _read_csv_cache(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"], index_col="date")
    return _normalize_ohlcv(df)
'''

NEW = '''def _read_csv_cache(path: Path) -> pd.DataFrame:
    """Read a cached OHLCV CSV, whatever the date column is called.

    This used to be:

        pd.read_csv(path, parse_dates=["date"], index_col="date")

    while every file the app has ever written begins `Date,open,high,...`
    with a capital D. pandas raised ValueError on every read, the caller
    swallowed it, and load_daily fell through to the network — so the price
    cache was written diligently and never once read. That single letter is
    what made a full build re-download all 3,300 symbols every time.

    Matching case-insensitively, with a positional fallback, means the cache
    stays readable regardless of which pandas or yfinance version wrote it.
    """
    df = pd.read_csv(path)
    if df.empty:
        return _normalize_ohlcv(df)
    col = next((c for c in df.columns if str(c).strip().lower() == "date"),
               df.columns[0])
    df[col] = pd.to_datetime(df[col], errors="coerce")
    df = df.dropna(subset=[col]).set_index(col)
    df.index.name = "date"
    return _normalize_ohlcv(df)
'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default=LAB)
    ap.add_argument("--check", action="store_const", const="check", dest="mode")
    ap.add_argument("--apply", action="store_const", const="apply", dest="mode")
    a = ap.parse_args()
    mode = a.mode or "check"

    app = os.path.abspath(a.app)
    if app.rstrip("/") == "/opt/breakoutscanner":
        bad("lab-only; refusing to modify the original app")
        return 2
    dl = os.path.join(app, "data_loader.py")
    if not os.path.isfile(dl):
        bad(f"missing {dl}")
        return 2

    hdr(f"target {dl}")
    src = open(dl, errors="replace").read()
    if "whatever the date column is called" in src:
        ok("already applied")
        return 0
    n = src.count(OLD)
    if n != 1:
        bad(f"anchor matched {n} times, expected 1")
        print("  The function may have been edited. Show me:")
        print(f"    sudo sed -n '/def _read_csv_cache/,/^def /p' {dl}")
        return 3
    ok("anchor matched")

    out = src.replace(OLD, NEW, 1)
    t = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    t.write(out)
    t.close()
    try:
        py_compile.compile(t.name, cfile=t.name + "c", doraise=True)
        ok("result compiles")
    except py_compile.PyCompileError as e:
        bad(f"does not compile: {e}")
        os.unlink(t.name)
        return 4
    finally:
        if os.path.exists(t.name + "c"):
            os.unlink(t.name + "c")

    # Prove the new reader parses a real cache file before writing anything.
    hdr("does the new reader actually parse a real file?")
    import pandas as pd
    cache = os.path.join(app, "data_cache", "prices_daily")
    sample = None
    try:
        for f in sorted(os.listdir(cache)):
            if f.endswith(".csv"):
                sample = os.path.join(cache, f)
                break
    except OSError:
        pass
    if not sample:
        bad("no cache file to test against")
    else:
        print(f"  sample {os.path.basename(sample)}")
        try:
            pd.read_csv(sample, parse_dates=["date"], index_col="date")
            print(f"  {Y}old reader unexpectedly succeeded — header may differ "
                  f"from what was diagnosed{X}")
        except Exception as e:
            ok(f"old reader fails as expected: {type(e).__name__}")
        df = pd.read_csv(sample)
        col = next((c for c in df.columns if str(c).strip().lower() == "date"),
                   df.columns[0])
        df[col] = pd.to_datetime(df[col], errors="coerce")
        df = df.dropna(subset=[col]).set_index(col)
        ok(f"new reader: {len(df)} rows, {str(df.index[0])[:10]} -> "
           f"{str(df.index[-1])[:10]}, columns {list(df.columns)}")

    if mode == "check":
        os.unlink(t.name)
        print(f"\n{Y}--check only. Nothing written.{X}")
        return 0

    b = f"{dl}.backup-{datetime.now():%F-%H%M%S}"
    shutil.copy2(dl, b)
    st = os.stat(dl)
    shutil.copyfile(t.name, dl)
    os.chmod(dl, st.st_mode)
    os.unlink(t.name)
    hdr("applied")
    ok(f"written (backup {os.path.basename(b)})")
    print(f"""
  {B}Confirm with the probe first — it counts network calls, so it cannot
  be fooled by a coincidence in elapsed time:{X}

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \\
      {app}/probe_cache.py

  Expect 0 network calls and "cache" for every symbol. Then:

    time sudo -u breakout /opt/breakoutscanner/.venv/bin/python \\
      {app}/build_snapshot.py \\
      --symbols-file {app}/nse_all_equity.txt --limit 300

  {Y}Baseline 159s. If the cache is genuinely readable this should be
  seconds, because no download will happen at all.{X}

  The same bug is in the original app. Once this is proven here, it is
  a one-line change there and the single biggest win available.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
