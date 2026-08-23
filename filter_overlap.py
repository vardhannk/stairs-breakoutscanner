#!/usr/bin/env python3
"""
filter_overlap.py — which filters actually narrow, and which just repeat.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/filter_overlap.py

READ-ONLY against the snapshot. Places no orders, writes nothing.

THE QUESTION IT ANSWERS
-----------------------
"Do I need Minervini AND O'Neil AND Mansfield, or am I applying one idea
three times?"

That is not answerable by reasoning about the definitions, because the answer
depends on your universe. It IS answerable by counting: run each filter alone,
then run it on top of RS Rating >= 80, and see how many rows it removes that
RS had not already removed.

    marginal keep = survivors(RS80 AND X) / survivors(RS80)

    ~1.00   X removes nothing RS had not. Redundant — drop it.
    ~0.50   X halves the RS survivors. Genuinely additive.
    ~0.00   X and RS almost never agree. Suspicious, not clever.

WHY RS RATING IS THE ANCHOR
---------------------------
Minervini's eighth criterion IS "RS Rating >= 70" — his template embeds
O'Neil's measure rather than offering an alternative to it. Mansfield measures
the same relative outperformance one derivative earlier. So RS Rating is not
one opinion among three; it is the shared core, and the honest test is what
each of the others adds ON TOP of it.

Jaccard overlap is reported too: |A∩B| / |A∪B|. Above ~0.8 two filters are
selecting the same stocks under different names.
"""

from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

G, R, Y, C, X = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m"

# The RS line you asked for. Every marginal figure is measured on top of this.
RS_ANCHOR = 80

# Candidate filters, each a (label, pandas predicate). Grouped by what they
# are actually claiming, which is the point of the exercise.
def build_filters(rs: int):
    return {
        # ── relative strength: three names, one idea ───────────────────
        f"RS Rating >= {rs}":      ("RS", lambda d: d["rs_rating"] >= rs),
        "Minervini >= 7/8":        ("RS", lambda d: d["minervini_score"] >= 7),
        "Mansfield RS > 0":        ("RS", lambda d: d["mansfield_rs"] > 0),
        "1m rank >= 90":           ("RS", lambda d: d["ret_1m_rank"] >= 90),

        # ── trend structure ────────────────────────────────────────────
        "Above 200 EMA":           ("Trend", lambda d: d["above_200ema"].astype(bool)),
        "MA stacked 20>50>200":    ("Trend", lambda d: d["ma_stacked"].astype(bool)),
        "Within 15% of 52w high":  ("Trend", lambda d: d["pct_from_52w_high"] <= 15),

        # ── liquidity ──────────────────────────────────────────────────
        "Turnover >= Rs 5 cr":     ("Liquidity", lambda d: d["turnover_30d_cr"] >= 5),
        "Turnover >= Rs 25 cr":    ("Liquidity", lambda d: d["turnover_30d_cr"] >= 25),

        # ── setup / timing: the only genuinely independent layer ───────
        "VCP":                     ("Setup", lambda d: d["is_vcp"].astype(bool)),
        "Tight range":             ("Setup", lambda d: d["tight_range_d"].astype(bool)),
        "Near resistance (<5%)":   ("Setup", lambda d: d["pct_to_resistance"] <= 5),
        "Inside bar":              ("Setup", lambda d: d["is_inside_bar_d"].astype(bool)),
        "21 EMA shakeout":         ("Setup", lambda d: d["shakeout_21ema"].astype(bool)),
    }


def safe(df: pd.DataFrame, fn) -> pd.Series:
    try:
        return fn(df).fillna(False).astype(bool)
    except Exception:
        return pd.Series(False, index=df.index)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--rs", type=int, default=RS_ANCHOR)
    a = ap.parse_args()

    from build_snapshot import TABLE, connect, db_path
    path = db_path()
    if not os.path.isfile(path):
        print(f"{R}  no snapshot at {path} — run build_snapshot.py first{X}")
        return 2
    conn = connect(path, read_only=True)
    as_of = a.date or conn.execute(f"SELECT max(date) FROM {TABLE}").fetchone()[0]
    df = conn.execute(f"SELECT * FROM {TABLE} WHERE date = ?", [as_of]).fetch_df()
    n = len(df)
    if n < 20:
        print(f"{Y}  only {n} symbols — too few for overlap to mean anything{X}")
        if n == 0:
            return 2

    FILTERS = build_filters(a.rs)
    masks = {k: safe(df, fn) for k, (_, fn) in FILTERS.items()}
    groups = {k: g for k, (g, _) in FILTERS.items()}
    anchor_key = f"RS Rating >= {a.rs}"
    anchor = masks[anchor_key]
    n_anchor = int(anchor.sum())

    print(f"\n{C}snapshot {as_of} · {n} symbols{X}")
    print(f"{C}anchor: {anchor_key} · {n_anchor} survivors "
          f"({n_anchor/n*100:.0f}% of universe){X}")

    # ── 1. each filter alone ───────────────────────────────────────────────
    print(f"\n{C}══ each filter on its own ═══════════════════════════════{X}")
    print(f"  {'filter':<26} {'group':<10} {'kept':>6} {'% of universe':>14}")
    for k, m in sorted(masks.items(), key=lambda kv: -kv[1].sum()):
        c = int(m.sum())
        print(f"  {k:<26} {groups[k]:<10} {c:>6} {c/n*100:>13.1f}%")

    # ── 2. what each adds ON TOP of RS ─────────────────────────────────────
    print(f"\n{C}══ marginal effect on top of {anchor_key} ══════════════{X}")
    print(f"  {'filter':<26} {'kept':>6} {'of ' + str(n_anchor):>8} "
          f"{'marginal':>9}   verdict")
    if n_anchor == 0:
        print(f"  {Y}nothing clears RS {a.rs} in this snapshot{X}")
    else:
        rows = []
        for k, m in masks.items():
            if k == anchor_key:
                continue
            both = int((anchor & m).sum())
            keep = both / n_anchor
            rows.append((keep, k, both))
        for keep, k, both in sorted(rows):
            if keep >= 0.90:
                verdict = f"{Y}redundant — RS already did this{X}"
            elif keep >= 0.60:
                verdict = "mildly additive"
            elif keep >= 0.10:
                verdict = f"{G}genuinely additive{X}"
            else:
                verdict = f"{R}almost disjoint — check it works{X}"
            print(f"  {k:<26} {both:>6} {'':>8} {keep*100:>8.0f}%   {verdict}")

    # ── 3. pairwise similarity inside the RS family ────────────────────────
    print(f"\n{C}══ are the RS measures the same thing? (Jaccard) ════════{X}")
    rs_keys = [k for k, g in groups.items() if g == "RS"]
    print("  " + " " * 26 + "".join(f"{k.split()[0][:9]:>10}" for k in rs_keys))
    for a_k in rs_keys:
        line = f"  {a_k:<26}"
        for b_k in rs_keys:
            A, B = masks[a_k], masks[b_k]
            u = int((A | B).sum())
            j = int((A & B).sum()) / u if u else 0.0
            col = G if (a_k == b_k or j < 0.5) else (Y if j < 0.8 else R)
            line += f"{col}{j:>10.2f}{X}"
        print(line)
    print(f"  {R}red{X} = above 0.80, selecting the same stocks under "
          f"different names")

    # ── 4. the recommended stack, as a funnel ──────────────────────────────
    print(f"\n{C}══ one RS + one structure + liquidity + a setup ═════════{X}")
    stack = [
        ("universe", pd.Series(True, index=df.index)),
        (f"RS Rating >= {a.rs}", masks[anchor_key]),
        ("above 200 EMA", masks["Above 200 EMA"]),
        ("turnover >= Rs 5 cr", masks["Turnover >= Rs 5 cr"]),
    ]
    cur = pd.Series(True, index=df.index)
    for label, m in stack:
        cur = cur & m
        print(f"  {label:<26} {int(cur.sum()):>6}")
    print(f"  {'':<26} {'':>6}   then ONE setup:")
    for label in ("VCP", "Tight range", "Near resistance (<5%)",
                  "21 EMA shakeout", "Inside bar"):
        print(f"    + {label:<24} {int((cur & masks[label]).sum()):>6}")

    print(f"\n  Steps 1-3 answer 'is this a strong stock'. The setup answers")
    print(f"  'is now the moment'. Stacking four variations of step 1 and")
    print(f"  skipping step 4 is the common mistake.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
