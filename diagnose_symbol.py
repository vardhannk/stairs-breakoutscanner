#!/usr/bin/env python3
"""
diagnose_symbol.py — why is a given stock never in the scan results?

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/diagnose_symbol.py KSHINTL

READ-ONLY. Loads universes, fetches bars, runs the detector, reads the
snapshot. Writes nothing.

THERE ARE ONLY FOUR REASONS A STOCK NEVER APPEARS
-------------------------------------------------
and they need completely different fixes, so guessing between them is
worthless:

  1. NOT IN ANY UNIVERSE      the scanner only ever iterates index
                              constituent lists. A stock outside every list
                              is not "filtered out" — it is never looked at.
                              Fix: add a universe. No amount of loosening
                              filters will help.

  2. NO PRICE DATA            in a universe, but the data source returns
                              nothing or too few bars. Fix: data source.

  3. DETECTOR SAYS NO         data is fine, the breakout rules genuinely do
                              not fire. Fix: understand which rule, and
                              decide whether the rule is wrong.

  4. FILTERED AFTER DETECTION detected, but a later screen (RS, turnover,
                              1m rank) removed it. Fix: the screen.

This prints which one it is, with the evidence.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

G, R, Y, C, B, X = ("\033[32m", "\033[31m", "\033[33m",
                    "\033[36m", "\033[1m", "\033[0m")


def hdr(s): print(f"\n{C}{'=' * 66}\n== {s}\n{'=' * 66}{X}")
def ok(s):  print(f"  {G}ok  {X} {s}")
def bad(s): print(f"  {R}no  {X} {s}")
def warn(s): print(f"  {Y}?   {X} {s}")


def discover_universes(universes) -> list[str]:
    """The app must enumerate its index lists somewhere; find it rather than
    hardcode a guess that could silently miss the very list in question."""
    names: list[str] = []
    for attr in dir(universes):
        if attr.startswith("_"):
            continue
        try:
            v = getattr(universes, attr)
        except Exception:
            continue
        if isinstance(v, dict) and v and all(isinstance(k, str) for k in v):
            names += [k for k in v if "nifty" in k.lower() or "index" in k.lower()]
        elif isinstance(v, (list, tuple)) and v and all(isinstance(i, str) for i in v):
            names += [i for i in v if "nifty" in i.lower()]
    # Known-good fallbacks seen in this app's own output.
    names += ["NIFTY 50", "NIFTY Next 50", "NIFTY 100", "NIFTY 500",
              "NIFTY Midcap 150", "NIFTY Smallcap 250", "NIFTY Microcap 250",
              "NIFTY Total Market"]
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--timeframes", default="1D,1W,1M")
    a = ap.parse_args()
    sym = a.symbol.strip().upper()

    hdr(f"diagnosing {sym}")

    # ── 1. is it in ANY universe? ──────────────────────────────────────────
    hdr("1. universe membership — the scanner only sees these lists")
    try:
        import universes
    except Exception as e:
        bad(f"cannot import universes: {e}")
        return 2

    names = discover_universes(universes)
    print(f"  checking {len(names)} known index lists\n")
    found_in: list[str] = []
    total_symbols: set[str] = set()
    for n in names:
        try:
            syms = universes.load_index_symbols(n)
        except Exception as e:
            print(f"    {Y}{n:<26} could not load ({str(e)[:40]}){X}")
            continue
        if not syms:
            print(f"    {Y}{n:<26} empty{X}")
            continue
        up = {str(s).upper() for s in syms}
        total_symbols |= up
        mark = f"{G}CONTAINS IT{X}" if sym in up else ""
        print(f"    {n:<26} {len(up):>5} symbols  {mark}")
        if sym in up:
            found_in.append(n)

    print(f"\n  union of every list: {len(total_symbols)} distinct symbols")

    if not found_in:
        print(f"""
  {R}{B}CAUSE FOUND: {sym} is in NONE of these lists.{X}

  {B}This is not a filter problem.{X} The scanner iterates a universe and
  examines each member. A symbol outside every list is never fetched,
  never measured, and never evaluated — so no change to RS thresholds,
  breakout sensitivity or any screen will ever surface it.

  NSE has roughly 2,000 listed equities. The largest list here holds
  about 750. Anything below that market-cap cut is invisible by
  construction.

  The fix is a universe that is not an index: the full NSE equity list,
  or a personal watchlist of symbols you hold. Say which and I will
  build it.""")
    else:
        ok(f"{sym} IS in: {', '.join(found_in)}")
        print("  So membership is not the reason. Continuing.")

    # ── 2. does price data exist? ──────────────────────────────────────────
    hdr("2. price data")
    load_bars = None
    for modname in ("data", "datafeed", "bars", "screen", "scanner", "universes"):
        try:
            mod = __import__(modname)
        except Exception:
            continue
        if hasattr(mod, "load_bars"):
            load_bars = getattr(mod, "load_bars")
            ok(f"using load_bars from {modname}.py")
            break
    if load_bars is None:
        warn("could not locate load_bars — skipping data and detector checks")
        print("     find it with:  grep -rn 'def load_bars' /opt/breakoutscanner")
    else:
        for tf in [t.strip() for t in a.timeframes.split(",") if t.strip()]:
            try:
                df = load_bars(sym, tf)
            except Exception as e:
                bad(f"{tf}: load_bars raised {type(e).__name__}: {e}")
                continue
            if df is None or len(df) == 0:
                bad(f"{tf}: no bars returned")
                continue
            try:
                lo, hi = str(df.index[0])[:10], str(df.index[-1])[:10]
            except Exception:
                lo = hi = "?"
            last = ""
            for col in ("close", "Close", "CLOSE"):
                if col in df.columns:
                    last = f"  last close {df[col].iloc[-1]:.2f}"
                    break
            ok(f"{tf}: {len(df)} bars  {lo} -> {hi}{last}")

        # ── 3. what does the detector say? ─────────────────────────────────
        hdr("3. the breakout detector, run directly on this symbol")
        try:
            import scanner
            # timeframes is REQUIRED and positional. Calling scan_universe([sym])
            # raised TypeError even though the signature was printed earlier in
            # this same session — pass it explicitly rather than hope.
            tfs = [t.strip() for t in a.timeframes.split(",") if t.strip()]
            res = scanner.scan_universe([sym], tfs)
            if res is None or getattr(res, "empty", True):
                print(f"  {Y}The detector found NO breakout for {sym} today.{X}")
                print("  That is a legitimate answer — a stock in a long uptrend")
                print("  is not breaking out on most individual days. A breakout")
                print("  is an EVENT, not a description of the trend.")
            else:
                ok(f"detector returned {len(res)} row(s):")
                print(res.to_string(index=False, max_colwidth=20))
        except Exception as e:
            bad(f"scan_universe failed: {type(e).__name__}: {e}")

    # ── 4. is it in the snapshot? ──────────────────────────────────────────
    hdr("4. presence in the nightly snapshot")
    try:
        from build_snapshot import TABLE, connect, db_path
        p = db_path()
        if not os.path.isfile(p):
            warn("no snapshot file yet")
        else:
            conn = connect(p, read_only=True)
            row = conn.execute(
                f"SELECT date, close, rs_rating, ret_1m_rank, is_breakout, "
                f"breakout_direction, turnover_30d_cr FROM {TABLE} "
                f"WHERE upper(symbol)=? ORDER BY date DESC LIMIT 3", [sym]
            ).fetchall()
            if row:
                ok(f"{sym} has {len(row)} snapshot row(s):")
                for r in row:
                    print(f"     {r}")
            else:
                bad(f"{sym} has NO row in the snapshot")
                print("     Consistent with it not being in the built universe.")
    except Exception as e:
        warn(f"snapshot check skipped: {type(e).__name__}: {e}")

    hdr("summary")
    if not found_in:
        print(f"""  {B}{sym} is not in any index list the app loads.{X}

  Sections 2-4 above, if they ran, are academic: the scanner would never
  reach this symbol regardless of what they say.

  {B}Worth knowing:{X} this is a systematic blind spot, not a one-off. Every
  stock outside the top ~750 by market cap is invisible to this app, and
  that is precisely where a 362 -> 900 move is most likely to happen.""")
    else:
        print(f"  {sym} IS in the universe. The reason is in sections 2-4 above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
