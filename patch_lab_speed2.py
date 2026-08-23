#!/usr/bin/env python3
"""
patch_lab_speed2.py — skip on evidence, not on a counter.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
      /opt/breakoutscanner-lab/patch_lab_speed2.py --check
    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
      /opt/breakoutscanner-lab/patch_lab_speed2.py --apply

LAB ONLY. Replaces the skip block installed by patch_lab_speed.py.

WHY THE FIRST ATTEMPT DID NOT WORK
==================================
It skipped symbols with 3+ consecutive failures recorded in no_data.json.
After applying it, the file read:

    31 entries; 0 at 3+     every value == 1

so nothing was ever skipped. The counter resets, and it resets for a reason
that is structural rather than incidental:

    _counts = {s: int(_prior.get(s, 0)) + 1 for s in _failed_now}

The map is rebuilt from symbols that FAILED this run. The moment the skip
starts working, a skipped symbol is never attempted, never fails, and so
falls out of the map entirely — which un-skips it next run, which re-adds it,
for ever. A counter that forgets whatever it successfully acts on cannot
reach its own threshold. Raising or lowering the strike count does not fix
that; the feedback loop is the defect.

WHAT THIS DOES INSTEAD
======================
prefetch.py already asks for every symbol in cheap batched requests and
writes prefetch_no_data.txt listing the ones the source returned nothing for.
That is direct evidence, gathered by the component whose job is fetching, and
it is rewritten on every prefetch — so a symbol that starts being carried
disappears from the list on its own, with no counter to maintain and no state
for the build to corrupt.

The build reads that list and skips those symbols. Ownership is clean:
prefetch decides what is obtainable, the build consumes the answer.

ALSO FIXED
==========
no_data.json now carries forward entries for symbols that were skipped rather
than dropping them. It is only used for reporting now, but a statistic that
silently resets is worse than no statistic — that is what sent me looking in
the wrong place for twenty minutes.
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


def hdr(s): print(f"\n{C}{'=' * 66}\n== {s}\n{'=' * 66}{X}")
def ok(s):  print(f"  {G}ok  {X} {s}")
def bad(s): print(f"  {R}FAIL{X} {s}")


# The block patch_lab_speed.py inserted — replaced wholesale.
OLD_SKIP = '''    try:
        import json as _json_nd          # local: json is not imported at module scope
        _ndp = os.path.join(os.path.dirname(db_path()), "no_data.json")
        with open(_ndp) as _fh:
            _nd = _json_nd.load(_fh)
        _skip = {s for s, n in _nd.items() if int(n) >= 3}
        if _skip:
            _before = len(syms)
            syms = [s for s in syms if s not in _skip]
            if verbose and _before != len(syms):
                print(f"  skipping {_before - len(syms)} symbol(s) with no data "
                      f"for 3+ runs (prefetch still retries them)")
    except Exception:
        pass
'''

NEW_SKIP = '''    # ── skip what prefetch proved is unobtainable ──────────────────────────
    # prefetch.py asks for every symbol in batched requests and writes
    # prefetch_no_data.txt for the ones the source returns nothing for. That
    # is evidence from the component that owns fetching, refreshed on every
    # prefetch run, so a symbol that starts being carried leaves the list by
    # itself.
    #
    # This replaces a strike counter in no_data.json which could never fire:
    # it was rebuilt each run from symbols that FAILED, so a symbol the skip
    # actually skipped stopped failing, dropped out of the map, and was
    # un-skipped next run. Every value sat at 1 for ever.
    #
    # Cost of not skipping: build() calls load_daily once per timeframe, so
    # each unobtainable symbol is three lookups that each wait out a network
    # timeout. On 300 symbols that was 31 x 3 = 93 pointless round trips.
    _skipped_no_data = 0
    try:
        _ndp = os.path.join(os.path.dirname(db_path()), "prefetch_no_data.txt")
        with open(_ndp) as _fh:
            _skip = {l.strip().upper() for l in _fh
                     if l.strip() and not l.startswith("#")}
        if _skip:
            _before = len(syms)
            syms = [s for s in syms if s.upper() not in _skip]
            _skipped_no_data = _before - len(syms)
            if verbose and _skipped_no_data:
                print(f"  skipping {_skipped_no_data} symbol(s) prefetch found "
                      f"no data for (it retries them on its next run)")
    except FileNotFoundError:
        if verbose:
            print("  no prefetch_no_data.txt yet — run prefetch.py to build it")
    except Exception:
        pass
'''

OLD_COUNTS = '''    _failed_now = {s for s, _ in skipped}
    _counts = {s: int(_prior.get(s, 0)) + 1 for s in _failed_now}
'''

NEW_COUNTS = '''    _failed_now = {s for s, _ in skipped}
    _counts = {s: int(_prior.get(s, 0)) + 1 for s in _failed_now}
    # Carry forward symbols that were SKIPPED rather than attempted. Without
    # this the map only ever contains this run's failures, so anything the
    # skip removed silently disappears from the record and the counts reset.
    _attempted = set(syms)
    for _s, _n in _prior.items():
        if _s not in _counts and _s not in _attempted:
            _counts[_s] = int(_n)
'''


def patch(path, old, new, mode, marker, label):
    src = open(path, errors="replace").read()
    if marker in src:
        ok(f"{label}: already applied")
        return True, src
    n = src.count(old)
    if n != 1:
        bad(f"{label}: anchor matched {n} times, expected 1")
        return False, src
    out = src.replace(old, new, 1)
    ok(f"{label}: anchor matched")
    return True, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default=LAB)
    ap.add_argument("--check", action="store_const", const="check", dest="mode")
    ap.add_argument("--apply", action="store_const", const="apply", dest="mode")
    a = ap.parse_args()
    mode = a.mode or "check"

    app = os.path.abspath(a.app)
    if app.rstrip("/") == "/opt/breakoutscanner":
        bad("lab-only patch; refusing to touch the original app")
        return 2
    bs = os.path.join(app, "build_snapshot.py")
    if not os.path.isfile(bs):
        bad(f"missing {bs}")
        return 2

    hdr(f"target {bs}")
    src = open(bs, errors="replace").read()
    if "no_data.json" not in src and "prefetch_no_data" not in src:
        bad("patch_lab_speed.py does not appear to have been applied first")
        return 3
    ok("previous patch present")

    hdr("1. skip source: strike counter -> prefetch evidence")
    good, src = patch(bs, OLD_SKIP, NEW_SKIP, mode,
                      "skip what prefetch proved", "skip block")
    if not good:
        return 3

    hdr("2. no_data.json stops forgetting skipped symbols")
    if "Carry forward symbols that were SKIPPED" in src:
        ok("counter fix: already applied")
    elif src.count(OLD_COUNTS) == 1:
        src = src.replace(OLD_COUNTS, NEW_COUNTS, 1)
        ok("counter fix: anchor matched")
    else:
        bad(f"counter anchor matched {src.count(OLD_COUNTS)} times, expected 1")
        return 3

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

    # Is there anything to skip yet?
    hdr("3. the evidence file")
    p = os.path.join(app, "data_cache", "prefetch_no_data.txt")
    if os.path.isfile(p):
        n = sum(1 for l in open(p) if l.strip() and not l.startswith("#"))
        ok(f"{p} lists {n} symbol(s)")
        if n < 50:
            print(f"       {Y}Only {n} — that file came from a --limit run.")
            print(f"       Run prefetch over the FULL universe before the full")
            print(f"       build, or most unobtainable symbols stay unknown.{X}")
    else:
        bad(f"{p} does not exist — run prefetch.py --apply first")
        print("       Without it this patch changes nothing.")

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
    ok(f"written (backup {os.path.basename(b)})")
    print(f"""
  {B}Re-run the same 300. Baseline 159s, unchanged through two attempts:{X}

    time sudo -u breakout /opt/breakoutscanner/.venv/bin/python \\
      {app}/build_snapshot.py \\
      --symbols-file {app}/nse_all_equity.txt --limit 300

  Expect a "skipping N symbol(s)" line and NO "possibly delisted"
  messages. If those messages still appear, the skip still is not
  reaching the fetch path and the next step is to instrument
  load_daily rather than patch around it again.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
