#!/usr/bin/env python3
"""
train_ml.py — train the breakout confidence model properly.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/train_ml.py --universe fno

What this fixes versus the upstream `train_confidence_model`:

  1. Features come from ml_features.compute_features(), the same function
     inference uses. Upstream computed true range differently in the two
     paths, so the shipped model scores on a distribution it never saw.

  2. Trains on a real universe (F&O / NIFTY 500) instead of 20 hardcoded
     symbols, and does not abort after 3 failed downloads.

  3. CHRONOLOGICAL train/test split with an embargo gap. A random split
     leaks the future into training and yields impressive, worthless scores.
     Labels look 10 bars ahead, so the last `horizon` bars before the split
     date are dropped entirely to stop the leak across the boundary.

  4. Reports honest metrics against the base rate. A model that always
     predicts the majority class is the bar to clear; if the model does not
     beat it, the script says so and refuses to save unless --force.

  5. class_weight='balanced' so an imbalanced label set does not produce a
     model that just predicts "no" forever.

Memory: symbols are processed one at a time and only feature rows are kept,
so this is far lighter than a full scan and is safe on a 1 GB box.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import warnings
from datetime import datetime

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                              # noqa: E402
import pandas as pd                                             # noqa: E402
from sklearn.ensemble import RandomForestClassifier             # noqa: E402
from sklearn.metrics import (accuracy_score, brier_score_loss,  # noqa: E402
                             precision_score, recall_score, roc_auc_score)

from config import DATA_DIR                                     # noqa: E402
from ml_features import (FEATURE_COLS, MIN_BARS, compute_features,  # noqa: E402
                         label_outcome, normalise)

MODEL_PATH = DATA_DIR / "breakout_ml_model.pkl"
META_PATH = DATA_DIR / "breakout_ml_model.meta.json"


# ---------------------------------------------------------------------------
def build_dataset(symbols, lookback=20, vol_mult=1.25, horizon=10, quiet=False):
    """Walk history per symbol, record features at each breakout bar."""
    from data_loader import load_daily

    rows, ok, empty = [], 0, 0
    for n, sym in enumerate(symbols, 1):
        if not quiet and n % 25 == 0:
            print(f"    {n}/{len(symbols)} symbols, {len(rows)} events so far")
        try:
            df = load_daily(sym, days=1500, use_cache=True)
        except Exception:
            empty += 1
            continue
        if df is None or df.empty or len(df) < MIN_BARS + horizon + lookback:
            empty += 1
            continue

        d = normalise(df).dropna(subset=["close", "high", "low"])
        if "volume" not in d.columns:
            empty += 1
            continue

        feats = compute_features(d)
        close = d["close"].astype(float)
        high, low = d["high"].astype(float), d["low"].astype(float)
        volr = feats["volume_ratio"]
        ok += 1

        start = max(MIN_BARS, lookback + 1)
        for i in range(start, len(d) - horizon):
            resist = high.iloc[i - lookback:i].max()
            support = low.iloc[i - lookback:i].min()
            c = close.iloc[i]
            vr = volr.iloc[i]
            if not np.isfinite(vr) or vr < vol_mult:
                continue
            if c > resist:
                direction = "bullish"
            elif c < support:
                direction = "bearish"
            else:
                continue

            f = feats.iloc[i]
            if f.isna().any():
                continue
            y = label_outcome(close, i, direction, horizon=horizon)
            if y is None:
                continue

            rec = {c_: float(f[c_]) for c_ in FEATURE_COLS}
            rec["target"] = int(y)
            rec["date"] = d.index[i]
            rec["symbol"] = sym
            rec["direction"] = direction
            rows.append(rec)

    if not quiet:
        print(f"    usable symbols: {ok}   skipped: {empty}   events: {len(rows)}")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
def chronological_split(df, test_frac=0.25, horizon=10):
    """
    Split by DATE, not at random. Then drop the `horizon` bars immediately
    before the boundary: their labels are computed from bars that fall on the
    test side, so keeping them leaks the future into training.
    """
    df = df.sort_values("date").reset_index(drop=True)
    cut = df["date"].quantile(1.0 - test_frac)
    embargo_end = cut
    embargo_start = cut - pd.Timedelta(days=int(horizon * 1.6))

    train = df[df["date"] < embargo_start]
    test = df[df["date"] >= embargo_end]
    dropped = len(df) - len(train) - len(test)
    return train, test, cut, dropped


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Train the breakout confidence model")
    ap.add_argument("--universe", choices=["nifty50", "fno", "nifty500"], default="fno")
    ap.add_argument("--max", type=int, default=0, help="cap symbol count (0 = all)")
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--lookback", type=int, default=20)
    ap.add_argument("--vol-mult", type=float, default=1.25)
    ap.add_argument("--test-frac", type=float, default=0.25)
    ap.add_argument("--trees", type=int, default=200)
    ap.add_argument("--max-depth", type=int, default=6)
    ap.add_argument("--force", action="store_true",
                    help="save even if the model fails to beat the base rate")
    a = ap.parse_args()

    from data_loader import load_nifty50_symbols, load_universe_symbols
    from fno_loader import load_fno_symbols

    print("\n=== 1. universe ===")
    syms = {"nifty50": load_nifty50_symbols,
            "fno": load_fno_symbols,
            "nifty500": load_universe_symbols}[a.universe]()
    if a.max:
        syms = syms[:a.max]
    print(f"    {a.universe}: {len(syms)} symbols")

    print("\n=== 2. building dataset (uses local cache; slow on first run) ===")
    data = build_dataset(syms, a.lookback, a.vol_mult, a.horizon)
    if len(data) < 300:
        print(f"\n    ABORT: only {len(data)} events. Need >=300 to say anything "
              f"meaningful. Run a scan first to populate data_cache, or widen "
              f"--universe.")
        return 2

    base = data["target"].mean()
    print(f"    events {len(data)}   win rate {base:.1%}   "
          f"span {data['date'].min().date()} .. {data['date'].max().date()}")

    print("\n=== 3. chronological split (no shuffling) ===")
    tr, te, cut, dropped = chronological_split(data, a.test_frac, a.horizon)
    print(f"    boundary        {cut.date()}")
    print(f"    train           {len(tr):>6}  ({tr['date'].min().date()} .. {tr['date'].max().date()})")
    print(f"    embargo dropped {dropped:>6}  <- prevents label leakage")
    print(f"    test            {len(te):>6}  ({te['date'].min().date()} .. {te['date'].max().date()})")
    if len(te) < 100 or len(tr) < 200:
        print("\n    ABORT: split too small to evaluate.")
        return 2

    print("\n=== 4. training ===")
    clf = RandomForestClassifier(
        n_estimators=a.trees, max_depth=a.max_depth,
        min_samples_leaf=20, class_weight="balanced",
        random_state=42, n_jobs=1)
    clf.fit(tr[FEATURE_COLS], tr["target"])

    prob = clf.predict_proba(te[FEATURE_COLS])[:, 1]
    pred = (prob >= 0.5).astype(int)
    te_base = te["target"].mean()
    majority = max(te_base, 1 - te_base)

    auc = roc_auc_score(te["target"], prob) if te["target"].nunique() > 1 else float("nan")
    acc = accuracy_score(te["target"], pred)

    print("\n=== 5. OUT-OF-SAMPLE RESULTS ===")
    print(f"    test win rate (base) {te_base:>7.1%}")
    print(f"    always-majority      {majority:>7.1%}   <- the bar to beat")
    print(f"    model accuracy       {acc:>7.1%}")
    print(f"    ROC AUC              {auc:>7.3f}   (0.50 = coin flip)")
    print(f"    precision            {precision_score(te['target'], pred, zero_division=0):>7.1%}")
    print(f"    recall               {recall_score(te['target'], pred, zero_division=0):>7.1%}")
    print(f"    Brier score          {brier_score_loss(te['target'], prob):>7.3f}   (lower better)")

    print("\n    top-decile check — does a high score actually mean anything?")
    q = te.assign(p=prob).sort_values("p", ascending=False)
    for name, sub in (("top 10%", q.head(max(10, len(q)//10))),
                      ("bottom 10%", q.tail(max(10, len(q)//10)))):
        print(f"      {name:<11} win rate {sub['target'].mean():>6.1%}  (n={len(sub)})")

    print("\n    feature importance")
    for f, imp in sorted(zip(FEATURE_COLS, clf.feature_importances_),
                         key=lambda x: -x[1]):
        print(f"      {f:<14} {imp:.3f}")

    # A point estimate of AUC on a few hundred rows is very noisy — on ~100
    # test rows the standard error is around 0.06, so 0.56 can appear from
    # pure chance. Bootstrap the interval and judge on the LOWER bound.
    lo, hi = float("nan"), float("nan")
    if te["target"].nunique() > 1:
        rng = np.random.default_rng(42)
        yv, pv, boots = te["target"].to_numpy(), prob, []
        for _ in range(1000):
            s = rng.integers(0, len(yv), len(yv))
            if len(np.unique(yv[s])) > 1:
                boots.append(roc_auc_score(yv[s], pv[s]))
        if boots:
            lo, hi = np.percentile(boots, [2.5, 97.5])
    print(f"\n    AUC 95% CI           [{lo:.3f}, {hi:.3f}]   (n={len(te)})")

    verdict_ok = (lo == lo) and lo > 0.50
    print("\n=== 6. verdict ===")
    if verdict_ok:
        print(f"    Lower CI bound {lo:.3f} > 0.50 — signal is statistically distinguishable")
        print(f"    from chance. Real, but check the top-decile numbers for whether")
        print(f"    it is large enough to matter.")
    else:
        print(f"    AUC {auc:.3f}, but the 95% interval includes 0.50.")
        print("    NO DEMONSTRABLE EDGE — this result is consistent with chance.")
        print("    A point estimate above 0.55 on a small test set is not evidence;")
        print("    that is exactly how noise looks. Either gather more events")
        print("    (--universe nifty500) or accept that these five features do not")
        print("    predict breakout follow-through. Both are honest outcomes.")
        if not a.force:
            print("\n    Not saving. Re-run with --force to override.")
            return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, "wb") as fh:
        pickle.dump(clf, fh)
    import sklearn
    meta = {
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "sklearn_version": sklearn.__version__,
        "universe": a.universe, "symbols": len(syms), "events": len(data),
        "train_rows": len(tr), "test_rows": len(te), "embargo_dropped": dropped,
        "split_date": str(cut.date()),
        "features": FEATURE_COLS,
        "metrics": {"auc": None if auc != auc else round(float(auc), 4),
                    "accuracy": round(float(acc), 4),
                    "test_base_rate": round(float(te_base), 4),
                    "majority_baseline": round(float(majority), 4)},
    }
    META_PATH.write_text(json.dumps(meta, indent=2))
    print(f"\n    saved {MODEL_PATH}")
    print(f"    saved {META_PATH}  (sklearn {sklearn.__version__} — clears the version warning)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
