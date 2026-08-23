#!/usr/bin/env python3
"""
patch_all_nse.py — put every NSE-listed stock in the main app's scan picker,
and stop larger universes being silently truncated.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/patch_all_nse.py --check
    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/patch_all_nse.py --apply

TWO CHANGES
===========

1. config.py — add "NSE All Equity" to UNIVERSE_CHOICES
------------------------------------------------------
The sidebar is built from `list(UNIVERSE_CHOICES)`. resolve_universe_symbols()
ALREADY resolves anything in universes.INDEX_REGISTRY, and "NSE All Equity"
was registered there, so the resolver has worked all along — the name simply
never appeared in the dropdown. Appended at the end of the file rather than
edited into the existing tuple: append-only edits cannot disturb a line that
already works.

2. data_loader.py — stop the NIFTY 500 slider capping every universe
--------------------------------------------------------------------
    _cap = max_symbols if max_symbols is not None else len(_syms)

max_symbols comes from a slider whose range is the length of the NIFTY 500
list, and which app.py DISABLES for every choice except NIFTY 500:

    disabled=universe_choice != UNIVERSE_NIFTY500

A disabled slider still returns its value, so ~500 is passed regardless, and
this line applies it to whatever was chosen. NIFTY Total Market holds 752
symbols, so choosing it scans an even sample of about 500 and quietly reports
mode "even". With 3,641 symbols the truncation would be far worse: seven of
every eight stocks skipped, no error, a plausible-looking result.

The fix treats the cap as "unset" when it is at least the size of the NIFTY
500 pool it came from — i.e. the slider is at maximum or disabled — and scans
the whole chosen universe instead. A deliberately reduced slider still works
for NIFTY 500, which is the only case it was ever meant for.

NOT CHANGED
===========
The runtime. This page scans live, one symbol at a time; 3,641 symbols is
roughly half an hour and Streamlit may drop the session first. The snapshot
pages (Today, Screener) already cover all 3,641 and answer instantly, because
the work happened overnight. This makes the live option available and honest,
not fast.
"""

from __future__ import annotations

import argparse
import os
import py_compile
import shutil
import sys
import tempfile
from datetime import datetime

APP = "/opt/breakoutscanner"
sys.path.insert(0, APP)

G, R, Y, C, B, X = ("\033[32m", "\033[31m", "\033[33m",
                    "\033[36m", "\033[1m", "\033[0m")

NAME = "NSE All Equity"

CONFIG_ADDITION = '''

# ── every listed NSE equity ────────────────────────────────────────────────
# Added by patch_all_nse.py. Appended rather than merged into the tuple above
# so that nothing already working can be disturbed by the edit.
#
# resolve_universe_symbols() already resolves any name in
# universes.INDEX_REGISTRY, where "NSE All Equity" is registered, so this is
# the only thing that was missing: the sidebar is built from UNIVERSE_CHOICES
# and the name was not in it.
UNIVERSE_ALL_NSE = "NSE All Equity"
UNIVERSE_CHOICES = UNIVERSE_CHOICES + (UNIVERSE_ALL_NSE,)
'''

CAP_OLD = """                _cap = max_symbols if max_symbols is not None else len(_syms)
"""

CAP_NEW = '''                # max_symbols arrives from a slider whose range is the
                # NIFTY 500 length, and app.py DISABLES that slider for every
                # choice except NIFTY 500 — but a disabled slider still
                # returns its value, so ~500 was applied to whatever universe
                # was picked. NIFTY Total Market (752) was therefore scanned
                # as an even sample of ~500, reported quietly as mode "even".
                # Against 3,641 symbols it would skip seven of every eight.
                #
                # Treat the cap as unset when it is at least as large as the
                # pool it came from, which is exactly the disabled/at-maximum
                # case. A deliberately lowered slider still applies, which is
                # the only thing it was ever for.
                _pool_n = len(nifty500) if nifty500 is not None else 0
                _capped = (max_symbols is not None
                           and (not _pool_n or max_symbols < _pool_n))
                _cap = max_symbols if _capped else len(_syms)
'''


def hdr(s): print(f"\n{C}{'=' * 66}\n== {s}\n{'=' * 66}{X}")
def ok(s):  print(f"  {G}ok  {X} {s}")
def bad(s): print(f"  {R}FAIL{X} {s}")
def warn(s): print(f"  {Y}warn{X} {s}")


def patch(path: str, old: str, new: str, expect: int, mode: str,
          append: bool = False) -> bool:
    src = open(path, errors="replace").read()
    if append:
        if new.strip().splitlines()[-1] in src:
            ok(f"{os.path.basename(path)}: already applied")
            return True
        out = src + new
    else:
        got = src.count(old)
        if got != expect:
            if new.strip().splitlines()[-1].strip() in src:
                ok(f"{os.path.basename(path)}: already applied")
                return True
            bad(f"{os.path.basename(path)}: expected {expect} match(es) of the "
                f"anchor, found {got}")
            return False
        out = src.replace(old, new)
        ok(f"{os.path.basename(path)}: anchor matched")

    t = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    t.write(out)
    t.close()
    try:
        py_compile.compile(t.name, cfile=t.name + "c", doraise=True)
    except py_compile.PyCompileError as e:
        bad(f"{os.path.basename(path)}: result does not compile: {e}")
        os.unlink(t.name)
        return False
    finally:
        if os.path.exists(t.name + "c"):
            os.unlink(t.name + "c")
    ok(f"{os.path.basename(path)}: patched result compiles")

    if mode == "apply":
        b = f"{path}.backup-{datetime.now():%F-%H%M%S}"
        shutil.copy2(path, b)
        st = os.stat(path)
        shutil.copyfile(t.name, path)
        os.chmod(path, st.st_mode)
        ok(f"{os.path.basename(path)}: written (backup {os.path.basename(b)})")
    os.unlink(t.name)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_const", const="check", dest="mode")
    ap.add_argument("--apply", action="store_const", const="apply", dest="mode")
    a = ap.parse_args()
    mode = a.mode or "check"

    cfg = os.path.join(APP, "config.py")
    dl = os.path.join(APP, "data_loader.py")

    hdr("1. preconditions")
    for p in (cfg, dl):
        if os.path.isfile(p):
            ok(os.path.basename(p))
        else:
            bad(f"missing {p}")
            return 2
    try:
        import universes
        n = len(universes.load_index_symbols(NAME))
    except Exception as e:
        bad(f"universes: {e}")
        return 2
    if n < 1000:
        bad(f"{NAME!r} loads only {n} symbols — run build_nse_universe.py "
            f"and register_universe.py first")
        return 2
    ok(f"{NAME!r} resolves to {n:,} symbols")

    hdr("2. config.py — add the choice")
    if not patch(cfg, "", CONFIG_ADDITION, 0, mode, append=True):
        return 3

    hdr("3. data_loader.py — stop the silent truncation")
    if not patch(dl, CAP_OLD, CAP_NEW, 1, mode):
        return 3

    if mode == "check":
        print(f"\n{Y}--check only. Nothing written.{X}")
        print(f"Apply with:  {sys.argv[0]} --apply")
        return 0

    # ── the only proof that counts ─────────────────────────────────────────
    hdr("4. does the app resolve it to the FULL list?")
    for m in ("config", "data_loader"):
        sys.modules.pop(m, None)
    import config
    import data_loader
    if NAME not in config.UNIVERSE_CHOICES:
        bad(f"{NAME!r} still not in UNIVERSE_CHOICES")
        return 4
    ok(f"picker now offers {len(config.UNIVERSE_CHOICES)} universes, "
       f"including {NAME!r}")

    pool = data_loader.load_universe_symbols()
    syms, sample, total = data_loader.resolve_universe_symbols(
        NAME, pool, max_symbols=len(pool))
    print(f"       resolved: {len(syms):,} symbols, mode={sample!r}, "
          f"total={total:,}")
    if len(syms) < n:
        bad(f"returned {len(syms):,} of {n:,} — still truncating")
        return 4
    ok(f"full universe returned ({len(syms):,}), mode={sample!r}")

    # And confirm the old truncation is genuinely gone for Total Market.
    tm = "NIFTY Total Market"
    if tm in config.UNIVERSE_CHOICES:
        s2, m2, t2 = data_loader.resolve_universe_symbols(
            tm, pool, max_symbols=len(pool))
        print(f"       {tm}: {len(s2)} of {t2}, mode={m2!r}")
        if len(s2) < t2:
            warn(f"{tm} still truncated — {len(s2)} of {t2}")
        else:
            ok(f"{tm} no longer sampled: full {t2} symbols")

    hdr("done")
    print(f"""  Restart to pick it up:

      sudo systemctl restart breakoutscanner

  {B}"{NAME}" now appears in the Symbol universe dropdown.{X}

  {Y}Expect it to be slow. That page scans live, symbol by symbol —
  3,641 of them is roughly half an hour, and Streamlit may drop the
  session before it finishes. The Today and Screener pages already
  cover all 3,641 instantly, because the snapshot did the work
  overnight. Use this option when you specifically want a live
  intraday scan; use the snapshot pages for everything else.{X}""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
