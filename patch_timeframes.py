#!/usr/bin/env python3
"""
patch_timeframes.py — drop 1H from the default timeframe selection.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/patch_timeframes.py --check
    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/patch_timeframes.py --apply

Changes DEFAULTS only, in two places:

  main sidebar        default ["1H","1D","1W","1M"] -> ["1D","1W","1M"]
  confluence filter   same, and its "min matching timeframes" slider max
                      drops from 4 to 3 so it cannot exceed the options

1H remains SELECTABLE — it is only removed from what is ticked on load.
Nothing in config.py or breakout.py changes, so 1H detection is untouched.

Side benefit: a scan is now 3 fetches per symbol instead of 4, so a 250-name
universe drops from 1,000 symbol-timeframe combinations to 750 — a quarter
less memory in bar_cache and a quarter less time.

Idempotent, backs up, verifies the result parses.
"""

import argparse
import ast
import datetime
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "app.py")
STAMP = datetime.datetime.now().strftime("%F-%H%M%S")

OK, NO, SK, WN = "\033[32m  ok  \033[0m", "\033[31m fail \033[0m", \
                 "\033[36m skip \033[0m", "\033[33m warn \033[0m"

EDITS = [
    # (label, old, new)
    ("main sidebar default",
     '''    selected_tfs = st.multiselect(
        "Timeframes",
        options=list(TIMEFRAME_ORDER),
        default=["1H", "1D", "1W", "1M"],''',
     '''    selected_tfs = st.multiselect(
        "Timeframes",
        options=list(TIMEFRAME_ORDER),
        # 1H left selectable but off by default — intraday bars repaint and
        # add a 4th fetch per symbol for little benefit on swing timeframes
        default=["1D", "1W", "1M"],'''),

    ("confluence filter default",
     '''        req_tfs = st.multiselect(
            "Require breakout in timeframes",
            options=["1H", "1D", "1W", "1M"],
            default=["1H", "1D", "1W", "1M"],''',
     '''        req_tfs = st.multiselect(
            "Require breakout in timeframes",
            options=["1H", "1D", "1W", "1M"],
            default=["1D", "1W", "1M"],'''),
]

SLIDER_OLD = '''        min_tfs_count = st.slider(
            "Min matching timeframes",
            1,
            4,'''
SLIDER_NEW = '''        min_tfs_count = st.slider(
            "Min matching timeframes",
            1,
            3,'''


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if not os.path.isfile(APP):
        print(f"{NO} {APP} not found")
        return 2

    src = orig = open(APP).read()
    print()

    for label, old, new in EDITS:
        if new in src:
            print(f"{SK} {label} — already patched")
        elif old in src:
            src = src.replace(old, new, 1)
            print(f"{OK} {label} — 1H removed from default")
        else:
            print(f"{NO} {label} — expected text not found; not touching app.py")
            return 3

    if SLIDER_NEW in src:
        print(f"{SK} confluence slider — already 1..3")
    elif SLIDER_OLD in src:
        src = src.replace(SLIDER_OLD, SLIDER_NEW, 1)
        print(f"{OK} confluence slider max 4 -> 3")
    else:
        print(f"{WN} confluence slider not found — leaving it alone")

    if src == orig:
        print(f"\n{SK} nothing to do")
        return 0

    try:
        ast.parse(src)
        print(f"{OK} app.py parses")
    except SyntaxError as e:
        print(f"{NO} would not parse: {e} — nothing written")
        return 4

    if not a.apply:
        print(f"\n{WN} --check only. Re-run with --apply to write.")
        return 0

    b = f"{APP}.bak-tf-{STAMP}"
    shutil.copy2(APP, b)
    open(APP, "w").write(src)
    print(f"       backup -> {b}")

    r = subprocess.run([sys.executable, "-m", "py_compile", APP],
                       capture_output=True, text=True)
    if r.returncode != 0:
        shutil.copy2(b, APP)
        print(f"{NO} py_compile failed; ORIGINAL RESTORED\n{r.stderr}")
        return 5
    print(f"{OK} written, py_compile clean")
    print("\nNext:  sudo systemctl restart breakoutscanner")
    print("1H stays in the dropdown — tick it when you want it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
