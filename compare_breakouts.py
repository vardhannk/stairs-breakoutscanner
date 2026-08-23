#!/usr/bin/env python3
"""
compare_breakouts.py — run the REAL scan and the snapshot, then diff them.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/compare_breakouts.py

READ-ONLY. Runs scanner.scan_universe() exactly as the app does, reads the
snapshot read-only, and reports symbol-by-symbol disagreement.

WHY THIS RATHER THAN MORE TUNING
--------------------------------
Three rounds of adjusting parameters produced 12, then 12, then 12 on the
daily timeframe against a live scan reporting ~96. Each round was a guess
about which argument mattered. This stops guessing: it calls the app's own
scan_universe, so whatever settings that path uses are the settings under
test, and prints the DIFFERENCE rather than a count.

A count tells you the numbers disagree. A diff tells you WHICH symbols and
what the scan said about them — which is the thing that identifies the cause.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

G, R, Y, C, X = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="NIFTY Smallcap 250")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeframes", default="1D,1W,1M")
    a = ap.parse_args()
    tfs = [t.strip() for t in a.timeframes.split(",") if t.strip()]

    import universes
    import scanner
    from build_snapshot import TABLE, connect, db_path

    syms = universes.load_index_symbols(a.universe)
    if a.limit:
        syms = syms[:a.limit]
    print(f"\n{C}universe {a.universe}: {len(syms)} symbols{X}")

    # ── 1. the app's own scan, called the way the app calls it ─────────────
    import inspect
    sig = inspect.signature(scanner.scan_universe)
    print(f"{C}scan_universe{X}({', '.join(sig.parameters)})")

    kw = {}
    if "timeframes" in sig.parameters:
        kw["timeframes"] = tfs
    if "use_cache" in sig.parameters:
        kw["use_cache"] = True
    print(f"  calling with: {kw}\n")

    try:
        live = scanner.scan_universe(syms, **kw)
    except Exception as e:
        print(f"{R}  scan_universe failed: {type(e).__name__}: {e}{X}")
        return 2
    if live is None or getattr(live, "empty", True):
        print(f"{Y}  the live scan returned nothing{X}")
        return 1

    print(f"{G}  live scan: {len(live)} rows{X}")
    for col in ("timeframe", "direction", "mode"):
        if col in live.columns:
            print(f"    {col:<12} {dict(live[col].value_counts())}")

    live_bull = live
    if "direction" in live.columns:
        live_bull = live[live["direction"].astype(str)
                         .str.lower().str.startswith("bull")]
    live_syms = set(live_bull["symbol"].astype(str).str.upper())
    print(f"    {len(live_syms)} distinct BULLISH symbols")

    # ── 2. the snapshot ────────────────────────────────────────────────────
    path = db_path()
    if not os.path.isfile(path):
        print(f"{R}  no snapshot — run build_snapshot.py first{X}")
        return 2
    conn = connect(path, read_only=True)
    as_of = conn.execute(f"SELECT max(date) FROM {TABLE}").fetchone()[0]
    snap_all = conn.execute(
        f"SELECT symbol, is_breakout, breakout_direction, breakout_timeframes, "
        f"breakout_pct, breakout_level FROM {TABLE} WHERE date = ?",
        [as_of]).fetch_df()

    # RESTRICT TO THE SYMBOLS THE LIVE SCAN ACTUALLY LOOKED AT.
    #
    # Without this, --limit 60 compares 60 scanned symbols against all 250
    # snapshot rows, and every breakout among the other 190 is reported as
    # "the snapshot found something the scan didn't". That is not a
    # disagreement, it is a different question. The first run of this script
    # made exactly that mistake and produced 13 phantom differences.
    scanned = {str(s).upper() for s in syms}
    snap = snap_all[snap_all["symbol"].astype(str).str.upper().isin(scanned)]

    def _bull(df):
        return set(df[(df["is_breakout"].astype(bool)) &
                      (df["breakout_direction"].astype(str).str.lower()
                       .str.startswith("bull"))]["symbol"]
                   .astype(str).str.upper())

    snap_syms = _bull(snap)
    print(f"\n{G}  snapshot {as_of}: {len(snap_all)} rows total, "
          f"{len(_bull(snap_all))} bullish breakouts{X}")
    print(f"  restricted to the {len(scanned)} scanned symbols: "
          f"{len(snap)} rows, {len(snap_syms)} bullish")
    missing_rows = scanned - set(snap_all["symbol"].astype(str).str.upper())
    if missing_rows:
        print(f"{Y}  {len(missing_rows)} scanned symbols have NO snapshot row "
              f"at all: {sorted(missing_rows)[:10]}{X}")

    # ── 3. the diff ────────────────────────────────────────────────────────
    only_live = sorted(live_syms - snap_syms)
    only_snap = sorted(snap_syms - live_syms)
    both = live_syms & snap_syms

    print(f"\n{C}══ agreement ══════════════════════════════════════════{X}")
    print(f"  both agree            {len(both)}")
    print(f"  live scan ONLY        {len(only_live)}")
    print(f"  snapshot ONLY         {len(only_snap)}")

    if only_live:
        print(f"\n{Y}  in the live scan but NOT the snapshot "
              f"(first 15):{X}")
        cols = [c for c in ("symbol", "timeframe", "direction", "mode",
                            "close", "level", "breakout_pct", "lookback",
                            "volume_ratio", "bar_time")
                if c in live_bull.columns]
        show = live_bull[live_bull["symbol"].astype(str).str.upper()
                         .isin(only_live[:15])][cols]
        print(show.to_string(index=False, max_colwidth=18))
        print(f"\n  {C}Look at `timeframe`, `mode` and `lookback` above.{X}")
        print(f"  Those are the settings the live scan used. If they differ")
        print(f"  from the snapshot's, that is the whole explanation.")

    if only_snap:
        print(f"\n{Y}  in the snapshot but NOT the live scan (first 15):{X}")
        s = snap[snap["symbol"].astype(str).str.upper().isin(only_snap[:15])]
        print(s[["symbol", "breakout_timeframes", "breakout_pct",
                 "breakout_level"]].to_string(index=False))

    print(f"\n{C}══ verdict ═══════════════════════════════════════════{X}")
    if not only_live and not only_snap:
        print(f"{G}  IDENTICAL on the {len(scanned)} scanned symbols — the "
              f"snapshot reproduces the live scan exactly.{X}")
        return 0

    if not only_live:
        # The direction that matters. Extra names in the snapshot mean it is
        # more permissive; MISSING names would mean the migration lost signal.
        print(f"{G}  The snapshot missed NOTHING the live scan found.{X}")
        print(f"{Y}  It found {len(only_snap)} the live scan did not, so it is "
              f"slightly more permissive — not lossy.{X}")
        return 0

    print(f"{R}  The snapshot MISSED {len(only_live)} breakouts the live scan "
          f"found. That is signal loss.{X}")
    print(f"  The first table above shows the settings the live scan used "
          f"for them.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
