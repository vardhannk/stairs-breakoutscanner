#!/usr/bin/env python3
"""
patch_universes.py — expose the new NSE index segments in the scanner.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/patch_universes.py --check
    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/patch_universes.py --apply

Two small edits:

  config.py       append the five names to UNIVERSE_CHOICES, so the sidebar
                  picks them up (app.py builds its dropdown from that tuple)

  data_loader.py  resolve_universe_symbols() gains a lookup into
                  universes.INDEX_REGISTRY before its NIFTY 500 fallback

Existing universes are untouched: NIFTY 10 / 50 / F&O / 500 keep reading
exactly what they read today. Idempotent, backs up, verifies imports.
"""

import argparse
import ast
import datetime
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.py")
LOADER = os.path.join(HERE, "data_loader.py")
UNIV = os.path.join(HERE, "universes.py")
STAMP = datetime.datetime.now().strftime("%F-%H%M%S")

OK, NO, SK, WN = "\033[32m  ok  \033[0m", "\033[31m fail \033[0m", \
                 "\033[36m skip \033[0m", "\033[33m warn \033[0m"

CONFIG_ADD = '''

# ── NSE index segments (added by patch_universes.py) ───────────────────
# Non-overlapping bands so a 750-stock universe can be scanned 250 at a
# time instead of all at once. Names must match universes.INDEX_SLUGS.
UNIVERSE_NIFTY100 = "NIFTY 100"
UNIVERSE_MIDCAP150 = "NIFTY Midcap 150"
UNIVERSE_SMALLCAP250 = "NIFTY Smallcap 250"
UNIVERSE_MICROCAP250 = "NIFTY Microcap 250"
UNIVERSE_TOTALMARKET = "NIFTY Total Market"

UNIVERSE_CHOICES = UNIVERSE_CHOICES + (
    UNIVERSE_NIFTY100,
    UNIVERSE_MIDCAP150,
    UNIVERSE_SMALLCAP250,
    UNIVERSE_MICROCAP250,
    UNIVERSE_TOTALMARKET,
)
# ───────────────────────────────────────────────────────────────────────
'''

LOADER_ANCHOR = """    pool = nifty500 if nifty500 is not None else load_universe_symbols()"""

LOADER_ADD = '''    # ── NSE index segments (added by patch_universes.py) ──────────────
    try:
        from universes import INDEX_REGISTRY, load_index_symbols
        if choice in INDEX_REGISTRY:
            _syms = load_index_symbols(choice)
            if _syms:
                _cap = max_symbols if max_symbols is not None else len(_syms)
                _sel = select_scan_universe(_syms, _cap)
                return _sel, ("full" if _cap >= len(_syms) else "even"), len(_syms)
            # unavailable (NSE moved the file / no cache) -> fall through to
            # NIFTY 500 rather than returning an empty scan
    except Exception:
        pass
    # ──────────────────────────────────────────────────────────────────

    pool = nifty500 if nifty500 is not None else load_universe_symbols()'''


def backup(p):
    b = f"{p}.bak-univ-{STAMP}"
    shutil.copy2(p, b)
    print(f"       backup -> {b}")


def patch_config(apply_):
    print("\n\033[1m== config.py ==\033[0m")
    src = open(CONFIG).read()
    if "UNIVERSE_SMALLCAP250" in src:
        print(f"{SK} already patched")
        return False
    if "UNIVERSE_CHOICES" not in src:
        print(f"{NO} UNIVERSE_CHOICES not found")
        return None
    new = src.rstrip("\n") + "\n" + CONFIG_ADD
    try:
        ast.parse(new)
    except SyntaxError as e:
        print(f"{NO} would not parse: {e}")
        return None
    print(f"{OK} will add 5 names to UNIVERSE_CHOICES")
    if apply_:
        backup(CONFIG)
        open(CONFIG, "w").write(new)
        print(f"{OK} written")
    return True


def patch_loader(apply_):
    print("\n\033[1m== data_loader.py ==\033[0m")
    src = open(LOADER).read()
    if "INDEX_REGISTRY" in src:
        print(f"{SK} already patched")
        return False
    if LOADER_ANCHOR not in src:
        print(f"{NO} resolve_universe_symbols() shape differs — anchor not found")
        return None
    new = src.replace(LOADER_ANCHOR, LOADER_ADD, 1)
    try:
        ast.parse(new)
    except SyntaxError as e:
        print(f"{NO} would not parse: {e}")
        return None
    print(f"{OK} will route index names to universes.load_index_symbols()")
    if apply_:
        backup(LOADER)
        open(LOADER, "w").write(new)
        print(f"{OK} written")
    return True


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    for f in (CONFIG, LOADER, UNIV):
        if not os.path.isfile(f):
            print(f"{NO} missing {f}")
            return 2

    r1 = patch_config(a.apply)
    r2 = patch_loader(a.apply)
    if r1 is None or r2 is None:
        print(f"\n{NO} an anchor did not match — nothing written to that file")
        return 3

    if not a.apply:
        print(f"\n{WN} --check only. Re-run with --apply to write.")
        return 0

    print("\n\033[1m== verifying ==\033[0m")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import config, data_loader, universes\n"
        "print('UNIVERSE_CHOICES:'); [print('   ', c) for c in config.UNIVERSE_CHOICES]\n"
    ) % HERE
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if r.returncode != 0:
        for f in (CONFIG, LOADER):
            shutil.copy2(f"{f}.bak-univ-{STAMP}", f)
        print(f"{NO} imports broke; ORIGINALS RESTORED\n{r.stderr}")
        return 4
    print(r.stdout.rstrip())
    print(f"\n{OK} patched.")
    print("""
    Next — download the constituent lists (needs network, ~5 small CSVs):

      sudo -u breakout /opt/breakoutscanner/.venv/bin/python \\
           /opt/breakoutscanner/universes.py

    then:  sudo systemctl restart breakoutscanner
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
