#!/usr/bin/env python3
"""
patch_dashboard.py — surface the new columns in the UI, with help and a
selection-criteria panel.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/patch_dashboard.py --check
    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/patch_dashboard.py --apply

Three edits to app.py:

  1. _style_results() — extend the rename map from columns_help.COLUMNS, so
     Table view shows "RS Rating" instead of "rs_rating"

  2. after the results table — a "📖 Column reference" expander explaining
     every field, and a "🎯 Selection criteria" panel showing which rules are
     active and what each one removes

  3. nothing is filtered automatically. The panel reports; you decide.

Idempotent, backs up, restores on failure.
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
HELP = os.path.join(HERE, "columns_help.py")
STAMP = datetime.datetime.now().strftime("%F-%H%M%S")

OK, NO, SK, WN = "\033[32m  ok  \033[0m", "\033[31m fail \033[0m", \
                 "\033[36m skip \033[0m", "\033[33m warn \033[0m"

# 1 ── extend the rename map -------------------------------------------------
RENAME_ANCHOR = """        "ml_confidence": "ML Confidence",
    }
    return out.rename(columns={k: v for k, v in rename.items() if k in out.columns})"""

RENAME_NEW = '''        "ml_confidence": "ML Confidence",
    }
    # friendly names for the screen columns (added by patch_dashboard.py)
    try:
        import columns_help as _ch
        for _k, _v in _ch.rename_map().items():
            rename.setdefault(_k, _v)
    except Exception:
        pass
    return out.rename(columns={k: v for k, v in rename.items() if k in out.columns})'''

# 2 ── help + criteria panel after the table --------------------------------
TABLE_ANCHOR = """                else:
                    styled = _style_results(df)
                    st.dataframe(styled, use_container_width=True, hide_index=True, key=f"df_{key}")"""

TABLE_NEW = '''                else:
                    styled = _style_results(df)
                    st.dataframe(styled, use_container_width=True, hide_index=True, key=f"df_{key}")
                    # column reference + selection criteria (patch_dashboard.py)
                    try:
                        import columns_help as _ch
                        _ch.render_criteria_panel(df)
                        _ch.render_help()
                    except Exception as _che:
                        st.caption(f"column help unavailable: {_che}")'''


def backup(p):
    b = f"{p}.bak-dash-{STAMP}"
    shutil.copy2(p, b)
    print(f"       backup -> {b}")
    return b


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if not os.path.isfile(HELP):
        print(f"{NO} columns_help.py must sit beside app.py")
        return 2
    if not os.path.isfile(APP):
        print(f"{NO} {APP} not found")
        return 2

    src = orig = open(APP).read()

    print("\n\033[1m== 1. friendly column names in Table view ==\033[0m")
    if "columns_help" in src and "rename.setdefault" in src:
        print(f"{SK} already patched")
    elif RENAME_ANCHOR not in src:
        print(f"{NO} _style_results() rename map not found — not touching app.py")
        return 3
    else:
        src = src.replace(RENAME_ANCHOR, RENAME_NEW, 1)
        print(f"{OK} rename map extended from columns_help.COLUMNS")

    print("\n\033[1m== 2. column reference + selection criteria panel ==\033[0m")
    if "render_criteria_panel" in src:
        print(f"{SK} already patched")
    elif TABLE_ANCHOR not in src:
        print(f"{NO} results-table block not found — not touching app.py")
        return 3
    else:
        src = src.replace(TABLE_ANCHOR, TABLE_NEW, 1)
        print(f"{OK} panels added below the results table")

    if src == orig:
        print(f"\n{SK} nothing to do")
        return 0

    print("\n\033[1m== validating ==\033[0m")
    try:
        ast.parse(src)
        print(f"{OK} app.py parses")
    except SyntaxError as e:
        print(f"{NO} would not parse: {e} — nothing written")
        return 4

    if not a.apply:
        print(f"\n{WN} --check only. Re-run with --apply to write.")
        return 0

    b = backup(APP)
    open(APP, "w").write(src)
    r = subprocess.run([sys.executable, "-m", "py_compile", APP],
                       capture_output=True, text=True)
    if r.returncode != 0:
        shutil.copy2(b, APP)
        print(f"{NO} py_compile failed; ORIGINAL RESTORED\n{r.stderr}")
        return 5
    print(f"{OK} written, py_compile clean")

    # generate the standalone reference doc too
    try:
        sys.path.insert(0, HERE)
        import columns_help
        doc = os.path.join(HERE, "COLUMNS.md")
        open(doc, "w").write(columns_help.to_markdown())
        print(f"{OK} wrote {doc}")
    except Exception as e:
        print(f"{WN} could not write COLUMNS.md: {e}")

    print("\nNext:  sudo systemctl restart breakoutscanner")
    print("Then open a Table view — two expanders appear below the results.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
