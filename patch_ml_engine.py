#!/usr/bin/env python3
"""
patch_ml_engine.py — make inference use the same features as training.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/patch_ml_engine.py

Rewrites the feature block inside ml_engine.predict_confidence so it calls
ml_features.latest_features() instead of recomputing indicators with its own
(inconsistent) formulas. Idempotent; backs up before touching anything;
verifies the result still parses and imports before keeping it.
"""
import ast
import datetime
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "ml_engine.py")

NEW_BLOCK = '''    try:
        # PATCHED: use the shared feature module so training and inference
        # compute identical features. Upstream used close.diff().abs() as a
        # stand-in for true range here while training used the real formula.
        from ml_features import latest_features
        feat_df = latest_features(bars_df)
        if feat_df is None:
            return None
        if "volume_ratio" in row and row.get("volume_ratio") is not None:
            try:
                feat_df = feat_df.copy()
                feat_df["volume_ratio"] = float(row["volume_ratio"])
            except (TypeError, ValueError):
                pass

        probs = model.predict_proba(feat_df)[0]
        return float(probs[1])
    except Exception:
        return None
'''


def main():
    if not os.path.isfile(TARGET):
        print(f"FAIL: {TARGET} not found"); return 2

    src = open(TARGET).read()

    if "from ml_features import latest_features" in src:
        print("already patched — nothing to do"); return 0

    if not os.path.isfile(os.path.join(HERE, "ml_features.py")):
        print("FAIL: ml_features.py must sit beside ml_engine.py"); return 2

    lines = src.splitlines(keepends=True)

    # locate predict_confidence, then its final `try:` block
    fstart = next((i for i, l in enumerate(lines)
                   if l.startswith("def predict_confidence")), None)
    if fstart is None:
        print("FAIL: predict_confidence not found"); return 2

    tstart = next((i for i in range(fstart, len(lines))
                   if lines[i].rstrip() == "    try:"), None)
    if tstart is None:
        print("FAIL: try block not found"); return 2

    # end = last line of the function (next top-level def/EOF)
    fend = next((i for i in range(tstart + 1, len(lines))
                 if lines[i] and not lines[i][0].isspace()), len(lines))
    while fend > tstart and not lines[fend - 1].strip():
        fend -= 1

    tail = "".join(lines[tstart:fend])
    if "close.diff().abs()" not in tail:
        print("WARN: expected buggy line not present; layout differs. Aborting"
              " rather than guessing."); return 2

    patched = "".join(lines[:tstart]) + NEW_BLOCK + "".join(lines[fend:])

    try:
        ast.parse(patched)
    except SyntaxError as e:
        print(f"FAIL: patched file would not parse ({e}); nothing written"); return 3

    stamp = datetime.datetime.now().strftime("%F-%H%M%S")
    shutil.copy(TARGET, f"{TARGET}.orig-{stamp}")
    open(TARGET, "w").write(patched)
    print(f"backed up  {TARGET}.orig-{stamp}")

    # prove it still imports and the function is callable
    sys.path.insert(0, HERE)
    try:
        import importlib
        m = importlib.import_module("ml_engine")
        importlib.reload(m)
        assert callable(m.predict_confidence)
        print("verified   ml_engine imports, predict_confidence callable")
    except Exception as e:
        shutil.copy(f"{TARGET}.orig-{stamp}", TARGET)
        print(f"FAIL: import broke ({e}); ORIGINAL RESTORED"); return 4

    removed = len(re.findall(r"close\.diff\(\)\.abs\(\)", src))
    print(f"patched    removed {removed} use(s) of the wrong true-range formula")
    print("\nNext: retrain so the model matches these features —")
    print("  sudo -u breakout /opt/breakoutscanner/.venv/bin/python \\")
    print("       /opt/breakoutscanner/train_ml.py --universe fno")
    return 0


if __name__ == "__main__":
    sys.exit(main())
