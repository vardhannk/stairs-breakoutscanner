#!/usr/bin/env python3
"""
patch_lab_fixes.py — the two defects left after the cache fix.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
      /opt/breakoutscanner-lab/patch_lab_fixes.py --check
    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
      /opt/breakoutscanner-lab/patch_lab_fixes.py --apply

LAB ONLY. Backend only — no page, layout or UI file is touched.


FIX 1 — coverage reported 101.4%
================================
    ok  300 of 296 obtainable symbols (101.4% coverage)

Symbols that prefetch found no data for are removed from `syms` before the
loop. They are then subtracted AGAIN when computing what was obtainable,
because the known-unavailable set is built from no_data.json rather than from
what this run actually attempted. Double subtraction, hence a denominator
smaller than the numerator.

Only symbols that were attempted AND are known-unavailable should reduce the
denominator. A coverage figure over 100% is not merely untidy: the whole
point of that number is to be trusted at a glance, and one that can exceed
100% cannot be.


FIX 2 — silently serving stale prices
=====================================
    fresh = df_cached.index[-1].date() >= date.today() - timedelta(days=3)

A cache up to three days old is returned unchanged. While the cache was
unreadable this never mattered — the code threw before reaching it. Making
the reader work made it live, and it immediately produced a wrong answer:
monthly breakouts collapsed from 22 to 1 because RELIANCE and TCS were
served 13 August prices in a snapshot labelled 14 August.

This does NOT tighten the freshness window. On a market holiday that would
send every symbol to the network for a fetch returning nothing — trading a
silent error for a guaranteed slowdown.

Instead the build measures what it actually used: the most recent bar date
across all symbols, and how many symbols are behind it. Costs nothing, no
network, and turns a silent wrong answer into a visible one. prefetch keeps
the cache current; this reports honestly when it did not.
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


EDITS = [
    # ── track the last bar date per symbol ─────────────────────────────────
    ("initialise the last-bar map",
     """    skipped: list[tuple[str, str]] = []
    rows, t0, failed = [], time.time(), 0
""",
     """    skipped: list[tuple[str, str]] = []
    # Last bar date per symbol, so the build can report the age of what it
    # actually used rather than assuming the cache was current.
    _last_bar: dict = {}
    rows, t0, failed = [], time.time(), 0
"""),

    ("record each symbol's last bar",
     """        m["symbol"] = s
""",
     """        try:
            _last_bar[s] = bars.index[-1].date()
        except Exception:
            pass
        m["symbol"] = s
"""),

    # ── fix the denominator ────────────────────────────────────────────────
    ("coverage denominator counts only attempted symbols",
     """    _obtainable = max(len(syms) - len(_known), 1)
""",
     """    # Only symbols this run ATTEMPTED can reduce the denominator. Symbols
    # the prefetch skip removed are not in `syms` at all, so subtracting them
    # here counted them twice and produced 300 of 296 = 101.4%.
    _known_here = {s for s in _known if s in _attempted}
    _obtainable = max(len(syms) - len(_known_here), 1)
"""),

    # ── report staleness ───────────────────────────────────────────────────
    ("report how old the data actually is",
     """        if cov < 95:
""",
     """        # How current is the data we just used? The cache is allowed to be
        # up to three days old by load_daily, so "built successfully" and
        # "built from today's prices" are different claims. Stale input was
        # what turned 22 monthly breakouts into 1, with no error anywhere.
        if _last_bar:
            _newest = max(_last_bar.values())
            _behind = {s: d for s, d in _last_bar.items() if d < _newest}
            print(f"        data as of {_newest}"
                  + (f", {len(_behind)} symbol(s) behind it" if _behind else
                     " for every symbol"))
            if _behind:
                _worst = min(_behind.values())
                _pct = 100.0 * len(_behind) / max(len(_last_bar), 1)
                _tag = R if _pct > 10 else Y
                print(f"  {_tag}stale{X} {len(_behind)} symbol(s) "
                      f"({_pct:.0f}%) use prices older than {_newest}, "
                      f"oldest {_worst}")
                if _pct > 10:
                    print(f"        This snapshot MIXES dates. Run prefetch.py "
                          f"before the build; a breakout computed on an old "
                          f"bar is wrong, not merely late.")
                    for _s, _d in sorted(_behind.items(), key=lambda kv: kv[1])[:8]:
                        print(f"          {_s:<14} {_d}")
        if cov < 95:
"""),
]


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
    bs = os.path.join(app, "build_snapshot.py")
    if not os.path.isfile(bs):
        bad(f"missing {bs}")
        return 2

    hdr(f"target {bs}   (backend only — no UI file is touched)")
    src = open(bs, errors="replace").read()

    if "_attempted" not in src:
        bad("expected `_attempted` from the earlier carry-forward patch; "
            "apply patch_lab_speed2.py first")
        return 3
    ok("prerequisite patches present")

    applied = 0
    for label, old, new in EDITS:
        marker = new.strip().splitlines()[0]
        if marker in src and old not in src:
            ok(f"{label}: already applied")
            applied += 1
            continue
        n = src.count(old)
        if n != 1:
            bad(f"{label}: anchor matched {n} times, expected 1")
            return 3
        src = src.replace(old, new, 1)
        ok(f"{label}: anchor matched")
        applied += 1

    t = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    t.write(src)
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

    if mode == "check":
        os.unlink(t.name)
        print(f"\n{Y}--check only. Nothing written.{X}")
        return 0

    b = f"{bs}.backup-{datetime.now():%F-%H%M%S}"
    shutil.copy2(bs, b)
    st = os.stat(bs)
    shutil.copyfile(t.name, bs)
    os.chmod(bs, st.st_mode)
    os.unlink(t.name)
    hdr("applied")
    ok(f"{applied} edits written (backup {os.path.basename(b)})")
    print(f"""
  {B}One full-universe build to confirm both fixes and the 8-minute
  projection. Run prefetch FIRST and let it finish:{X}

    sudo -u breakout {app}/prefetch.py --apply
    time sudo -u breakout /opt/breakoutscanner/.venv/bin/python \\
      {app}/build_snapshot.py --symbols-file {app}/nse_all_equity.txt

  Expect: coverage at or below 100%, "data as of <date> for every
  symbol", and 1M breakouts in the low twenties.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
