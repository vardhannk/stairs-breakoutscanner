#!/usr/bin/env python3
"""
patch_lab_speed.py — make the build actually use the cache prefetch just filled.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
      /opt/breakoutscanner-lab/patch_lab_speed.py --check
    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
      /opt/breakoutscanner-lab/patch_lab_speed.py --apply

LAB ONLY. Refuses to run against /opt/breakoutscanner.

THE MEASUREMENT THAT MOTIVATED THIS
===================================
    prefetch   300 symbols    48s   0.159s each   (batched, cache filled deep)
    build      300 symbols   159s   0.530s each   (unchanged)

A full, fresh, deep cache made no difference to the build. So the cache is
being REJECTED, not missed. Two reasons, both in that run's log.


FIX 1 — data_loader.load_daily: min_rows
----------------------------------------
    min_rows = min(60, days // 3) if days <= 500 else int(days * 0.45)

1D and 1W ask for days=400 -> min_rows=60, cache used. 1M asks for days=1500
-> min_rows=675, roughly 2.7 years of trading days. Any symbol holding less
falls through to a network call.

The check conflates two questions. "Is this cache a real file rather than a
truncated write?" is answered by an absolute floor. "Did we get 2.7 years?"
cannot be answered by fetching at all when the company listed eight months
ago — 3BBLACKBIO has 82 bars and will never have 675. Demanding them
guarantees a permanent cache miss for precisely the recent listings this
universe was expanded to find.

min_rows becomes a floor. Depth is prefetch's job; it fetches a 2400-day
window once per day in batches, so whatever history exists is already here.


FIX 2 — build_snapshot: stop re-asking for data that does not exist
-------------------------------------------------------------------
31 of those 300 symbols have no data at Yahoo. build() calls load_daily once
per timeframe, so each becomes 3 failed lookups, each waiting out a network
timeout: 93 round trips spent confirming something already recorded in
no_data.json from previous runs.

Symbols at or past NO_DATA_STRIKES consecutive failures are skipped, still
counted as known-unavailable so coverage arithmetic is unchanged, and still
retried by prefetch — which is batched and cheap, so a symbol that starts
being carried reappears without the build paying for the attempt.
"""

from __future__ import annotations

import argparse
import json
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


DL_OLD = """    min_rows = min(60, days // 3) if days <= 500 else int(days * 0.45)
"""

DL_NEW = '''    # A FLOOR, not a target.
    #
    # This used to be `int(days * 0.45)` whenever days > 500, so the 1M path
    # (days=1500) demanded 675 cached rows — about 2.7 years of trading days.
    # A stock listed eight months ago has 82. It can never pass, so its cache
    # was rejected on every run, for ever, and every build paid a network
    # round trip to rediscover that.
    #
    # The question this check should answer is "is this a real cache file or a
    # truncated write?", which an absolute floor answers. How DEEP the cache
    # goes is prefetch.py's responsibility: it pulls a 2400-day window in
    # batches once a day, so whatever history exists is already on disk by the
    # time a build asks for it.
    min_rows = 60
'''

BS_OLD = """    if limit:
        syms = syms[:limit]
"""

BS_NEW = '''    # ── skip symbols already known to have no data ─────────────────────────
    # build() calls load_daily once per timeframe, so a symbol the data source
    # does not carry costs THREE failed lookups per run, each waiting out a
    # network timeout. On a 300-symbol slice that was 31 symbols = 93 round
    # trips spent confirming what no_data.json already recorded.
    #
    # They are skipped here but NOT forgotten: they still appear in the
    # coverage arithmetic as known-unavailable, and prefetch.py still asks for
    # them in its batched pass. So a stock that starts being carried comes
    # back on its own, while the build stops paying for the question.
    try:
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

    if limit:
        syms = syms[:limit]
'''


def patch(path: str, old: str, new: str, mode: str, marker: str) -> bool:
    src = open(path, errors="replace").read()
    if marker in src:
        ok(f"{os.path.basename(path)}: already applied")
        return True
    n = src.count(old)
    if n != 1:
        bad(f"{os.path.basename(path)}: anchor matched {n} times, expected 1")
        return False
    out = src.replace(old, new, 1)
    t = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    t.write(out)
    t.close()
    try:
        py_compile.compile(t.name, cfile=t.name + "c", doraise=True)
    except py_compile.PyCompileError as e:
        bad(f"{os.path.basename(path)}: does not compile: {e}")
        os.unlink(t.name)
        return False
    finally:
        if os.path.exists(t.name + "c"):
            os.unlink(t.name + "c")
    ok(f"{os.path.basename(path)}: anchor matched, result compiles")
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
    ap.add_argument("--app", default=LAB)
    ap.add_argument("--check", action="store_const", const="check", dest="mode")
    ap.add_argument("--apply", action="store_const", const="apply", dest="mode")
    a = ap.parse_args()
    mode = a.mode or "check"

    app = os.path.abspath(a.app)
    if app.rstrip("/") == "/opt/breakoutscanner":
        bad("this is a lab-only patch; refusing to modify the original app")
        return 2
    if not os.path.isdir(app):
        bad(f"{app} not found")
        return 2

    hdr(f"target {app}")
    dl = os.path.join(app, "data_loader.py")
    bs = os.path.join(app, "build_snapshot.py")
    for p in (dl, bs):
        if not os.path.isfile(p):
            bad(f"missing {p}")
            return 2
        ok(os.path.basename(p))

    # The new block needs `os` at module scope; json is imported locally
    # because build_snapshot.py does not import it at the top, and adding a
    # module-level import would be a second, unrelated edit to the same file.
    bs_src = open(bs, errors="replace").read()
    if "import os" not in bs_src:
        bad("build_snapshot.py has no `import os`")
        return 3
    ok("build_snapshot.py imports os (json is imported locally in the block)")

    hdr("1. data_loader.py — min_rows becomes a floor")
    if not patch(dl, DL_OLD, DL_NEW, mode, "A FLOOR, not a target"):
        return 3

    hdr("2. build_snapshot.py — skip known-no-data symbols")
    if not patch(bs, BS_OLD, BS_NEW, mode, "skip symbols already known"):
        return 3

    if mode == "check":
        print(f"\n{Y}--check only. Nothing written.{X}")
        return 0

    hdr("3. what to measure")
    ndp = os.path.join(app, "data_cache", "no_data.json")
    known = 0
    try:
        with open(ndp) as fh:
            known = sum(1 for v in json.load(fh).values() if int(v) >= 3)
    except Exception:
        pass
    print(f"""  no_data.json currently marks {known} symbol(s) as unavailable.

  {B}Re-run the same 300 and compare against 159s:{X}

    time sudo -u breakout /opt/breakoutscanner/.venv/bin/python \\
      {app}/build_snapshot.py \\
      --symbols-file {app}/nse_all_equity.txt --limit 300

  {Y}One honest caveat: no_data.json counts only reach 3 after three runs.
  If those 31 symbols are still on strike 1 or 2, they will be retried
  this time and fix 2 will not show its full effect yet. Fix 1 should be
  visible immediately either way.{X}""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
