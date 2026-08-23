#!/usr/bin/env python3
"""
validate_snapshot.py — prove the snapshot is trustworthy before building on it.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/validate_snapshot.py
    ... --recompute 15     # independently recompute N symbols from raw bars

READ-ONLY. Opens the snapshot read-only and places no orders. Changes nothing.

WHY THIS EXISTS
---------------
Every screen, every scanner and every chart sits on these numbers. The errors
that matter here are not crashes — they are silent:

    rupees stored where crore was meant            turnover off by 10,000,000x
    a fraction stored where a percent was meant    every threshold off by 100x
    a rank that is not actually ranked             RS Rating that means nothing
    a pattern that fires on everything             a scanner that filters nothing

None of those raise. All of them survive into a frontend and quietly poison
every decision made afterwards. "Looks about right" does not catch a 100x
error in a column you have never seen before, which is why this checks
arithmetic rather than asking you to.

THE STRONGEST CHECK IS THE LAST ONE
-----------------------------------
--recompute reloads raw bars for a sample of symbols and recomputes several
fields by a DIFFERENT route than build_snapshot used, then compares. Agreement
between two independent computations is real evidence. Everything above it is
a plausibility check, which is weaker: a value can be plausible and wrong.
"""

from __future__ import annotations

import argparse
import os
import random
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

G, R, Y, C, X = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m"
_n = {"ok": 0, "warn": 0, "fail": 0}


def ok(m, d=""):
    _n["ok"] += 1
    print(f"  {G}ok  {X} {m}" + (f"   {C}{d}{X}" if d else ""))


def warn(m, d=""):
    _n["warn"] += 1
    print(f"  {Y}warn{X} {m}" + (f"   {d}" if d else ""))


def bad(m, d=""):
    _n["fail"] += 1
    print(f"  {R}FAIL{X} {m}" + (f"   {d}" if d else ""))


def hdr(t):
    print(f"\n{C}══ {t} {'═' * max(0, 58 - len(t))}{X}")


# ── expected domains ───────────────────────────────────────────────────────
# (low, high, "what it means if outside") — deliberately WIDE. These catch
# order-of-magnitude mistakes, not judgement calls about thresholds.
DOMAINS = {
    "rs_rating":         (1, 99, "O'Neil rank is 1-99 by definition"),
    "minervini_score":   (0, 8, "eight criteria, so 0-8"),
    "pct_of_52w_high":   (0, 105, "a percentage of the 52w high"),
    "pct_from_52w_high": (0, 100, "distance below the high, in percent"),
    "pct_from_52w_low":  (0, 100000, "can be huge for a multibagger"),
    "adr_pct_5d":        (0, 30, "above 30% daily range is not a stock"),
    "adr_pct_20d":       (0, 30, ""),
    "atr_pct":           (0, 30, ""),
    "turnover_30d_cr":   (0, 50000, "in CRORE. Rupees would be ~1e7 bigger"),
    "close":             (0.5, 500000, "rupees per share"),
    "volume_ratio":      (0, 500, "today vs 20-day average"),
    "vcp_contractions":  (0, 6, ""),
    "sector_breadth_rs80": (0, 100, "a percentage"),
}

# Columns that must read as PERCENT, not fraction. The classic silent bug:
# 0.07 meaning 7% passes every range check and breaks every threshold.
PERCENTISH = ["ret_1m", "ret_3m", "ret_6m", "ret_12m",
              "pct_of_52w_high", "adr_pct_5d", "turnover_30d_cr"]

# A pattern firing on nearly everything, or on nothing, is broken either way.
PATTERN_BANDS = {
    "is_vcp": (0.5, 25), "is_flag": (0.0, 20), "is_pennant": (0.0, 20),
    "is_inside_bar_d": (2, 40), "shakeout_21ema": (0.0, 25),
    "shakeout_50ema": (0.0, 20), "tight_range_d": (0.5, 40),
    "gap_unfilled": (0.0, 60), "rs_leads_price": (0.0, 60),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--recompute", type=int, default=10,
                    help="independently recompute N random symbols (0 = skip)")
    a = ap.parse_args()

    import scan_engine as SE
    from build_snapshot import TABLE, connect, db_path

    path = db_path()
    if not os.path.isfile(path):
        print(f"{R}  FAIL{X} no snapshot at {path} — run build_snapshot.py first")
        return 2
    conn = connect(path, read_only=True)

    as_of = a.date or conn.execute(
        f"SELECT max(date) FROM {TABLE}").fetchone()[0]
    df = conn.execute(f"SELECT * FROM {TABLE} WHERE date = ?",
                      [as_of]).fetch_df()
    print(f"\n{C}snapshot {path}{X}")
    print(f"  {as_of} · {len(df)} symbols · {df.shape[1]} columns")

    if df.empty:
        bad("snapshot is empty for that date")
        return 2

    # ── 1. schema ──────────────────────────────────────────────────────────
    hdr("1. every registry field exists")
    missing = [f.column for f in SE.FIELDS.values() if f.column not in df.columns]
    if missing:
        bad(f"{len(missing)} registry columns absent", ", ".join(missing[:6]))
    else:
        ok(f"all {len(SE.FIELDS)} columns present")

    # ── 2. how much is actually populated ──────────────────────────────────
    hdr("2. fill rates")
    empty, thin = [], []
    for f in SE.FIELDS.values():
        if f.column not in df.columns or f.kind == "text":
            continue
        filled = df[f.column].notna().mean() * 100
        if filled == 0:
            empty.append(f.column)
        elif filled < 50:
            thin.append((f.column, filled))
    if empty:
        warn(f"{len(empty)} columns entirely NULL", ", ".join(empty))
        print("       (expected for fields nothing computes yet — "
              "market_cap_cr, free_float_pct, sector_quadrant)")
    for col, pct in thin:
        warn(f"{col} only {pct:.0f}% populated")
    if not empty and not thin:
        ok("every numeric column is populated")

    # ── 3. domains ─────────────────────────────────────────────────────────
    hdr("3. values inside their possible range")
    for col, (lo, hi, why) in DOMAINS.items():
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if s.empty:
            continue
        out = s[(s < lo) | (s > hi)]
        if len(out):
            bad(f"{col}: {len(out)} of {len(s)} outside [{lo}, {hi}]",
                f"min {s.min():.4g} max {s.max():.4g}" + (f" — {why}" if why else ""))
        else:
            ok(f"{col:<22}", f"min {s.min():>10.4g}  med {s.median():>10.4g}  "
                             f"max {s.max():>10.4g}")

    # ── 4. percent vs fraction ─────────────────────────────────────────────
    hdr("4. percentages are percentages, not fractions")
    for col in PERCENTISH:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce").dropna().abs()
        if s.empty or len(s) < 5:
            continue
        med = s.median()
        if col in ("ret_1m", "ret_3m", "ret_6m", "ret_12m") and med < 0.5 and s.max() < 5:
            bad(f"{col} looks like a FRACTION", f"median {med:.4f} — expected e.g. 7.0 for 7%")
        elif col == "turnover_30d_cr" and med > 1e5:
            bad("turnover_30d_cr looks like RUPEES not crore",
                f"median {med:.4g} — divide by 1e7")
        elif col == "pct_of_52w_high" and med < 1.5:
            bad("pct_of_52w_high looks like a FRACTION", f"median {med:.4f}")
        else:
            ok(f"{col:<22}", f"median {med:.4g}")

    # ── 5. is the rank actually a rank ─────────────────────────────────────
    hdr("5. percentile ranks are uniformly distributed")
    # This one is strong. A percentile rank over N symbols MUST be roughly
    # uniform — that is what percentile means. If rs_rating clusters, the
    # ranking step did not run, ran on one row, or ran per-group by accident.
    for col in ("rs_rating", "ret_1m_rank", "rs_rank"):
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if len(s) < 20:
            warn(f"{col}: only {len(s)} values — too few to judge uniformity")
            continue
        deciles = pd.cut(s, bins=10).value_counts(normalize=True)
        worst = deciles.max()
        if worst > 0.35:
            bad(f"{col} is NOT uniform", f"{worst*100:.0f}% of values in one decile "
                                         f"— ranking is probably broken")
        elif s.nunique() < len(s) * 0.3:
            warn(f"{col} has many ties", f"{s.nunique()} distinct of {len(s)}")
        else:
            ok(f"{col:<22}", f"{s.nunique()} distinct, largest decile "
                             f"{worst*100:.0f}%")

    # ── 6. internal consistency ────────────────────────────────────────────
    hdr("6. columns agree with each other")
    checks = []
    if {"pct_of_52w_high", "pct_from_52w_high"} <= set(df.columns):
        d = (df["pct_of_52w_high"] + df["pct_from_52w_high"] - 100).abs()
        checks.append(("pct_of_52w_high + pct_from_52w_high == 100",
                       (d.dropna() > 1.0).sum(), len(d.dropna())))
    if {"close", "dma50", "above_50ema"} <= set(df.columns):
        # EMA and SMA differ, so only flag gross disagreement
        gross = ((df["above_50ema"].astype(bool)) &
                 (df["close"] < df["dma50"] * 0.90)).sum()
        checks.append(("above_50ema agrees with close vs 50 DMA", gross, len(df)))
    if {"is_vcp", "vcp_contractions"} <= set(df.columns):
        checks.append(("is_vcp implies >= 2 contractions",
                       ((df["is_vcp"].astype(bool)) &
                        (df["vcp_contractions"] < 2)).sum(), len(df)))
    if {"minervini_score", "above_200ema"} <= set(df.columns):
        checks.append(("Minervini 8/8 implies above the 200 EMA",
                       ((df["minervini_score"] >= 8) &
                        (~df["above_200ema"].astype(bool))).sum(), len(df)))
    for label, n_bad, n_tot in checks:
        (ok if n_bad == 0 else bad)(label, f"{n_bad} violations of {n_tot}")

    # ── 7. do the patterns fire sanely ─────────────────────────────────────
    hdr("7. pattern hit rates are plausible")
    for col, (lo, hi) in PATTERN_BANDS.items():
        if col not in df.columns:
            continue
        rate = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(bool).mean() * 100
        if rate > hi:
            bad(f"{col:<20} fires on {rate:.1f}%", f"above {hi}% it is not a filter")
        elif rate < lo:
            warn(f"{col:<20} fires on {rate:.1f}%", f"below {lo}% — check it works at all")
        else:
            ok(f"{col:<20}", f"{rate:.1f}% of the universe")

    # ── 8. independent recomputation ───────────────────────────────────────
    if a.recompute:
        hdr(f"8. recompute {a.recompute} symbols from raw bars, by another route")
        try:
            from data_loader import load_daily
        except Exception as e:
            warn(f"cannot load bars ({e}) — skipping the strongest check")
        else:
            syms = random.sample(list(df["symbol"]), min(a.recompute, len(df)))
            worst = {}
            for s in syms:
                row = df[df["symbol"] == s].iloc[0]
                try:
                    b = load_daily(s, use_cache=True)
                except Exception:
                    continue
                if b is None or b.empty or len(b) < 60:
                    continue
                c = b["close"].astype(float)
                px = float(c.iloc[-1])
                # The 52-week reference is the highest HIGH, matching
                # screen.py. Using the highest CLOSE here is exactly the bug
                # this check caught in build_snapshot — worth stating, since
                # a validator that quietly encodes the same mistake as the
                # code it audits is worse than no validator.
                hi52 = float(b["high"].astype(float).iloc[-252:].max())
                exp = {
                    "close": px,
                    "turnover_30d_cr": float(
                        b["volume"].astype(float).iloc[-31:-1].mean()) * px / 10_000_000,
                    "pct_from_52w_high": (hi52 - px) / hi52 * 100,
                    "adr_pct_5d": float(((b["high"].astype(float)
                                          - b["low"].astype(float))
                                         / c).iloc[-5:].mean() * 100),
                }
                for k, want in exp.items():
                    if k not in row or pd.isna(row[k]):
                        continue
                    got = float(row[k])
                    # Relative error alone is useless near zero: a stock at
                    # its high has pct_from_52w_high ≈ 0.05, and a 0.02
                    # difference reads as 40% while being irrelevant.
                    # Whichever tolerance is kinder wins.
                    abs_err = abs(got - want)
                    rel_err = abs_err / max(abs(want), 1e-9) * 100
                    err = min(rel_err, abs_err * 10 if abs(want) < 1 else rel_err)
                    worst[k] = max(worst.get(k, 0.0), err)
            if not worst:
                warn("no symbols could be recomputed — is the bar cache populated?")
            for k, err in sorted(worst.items()):
                if err > 5:
                    bad(f"{k:<22} differs by up to {err:.1f}%",
                        "two routes disagree — one of them is wrong")
                elif err > 0.5:
                    warn(f"{k:<22} differs by up to {err:.2f}%")
                else:
                    ok(f"{k:<22}", f"matches within {err:.3f}%")

    # ── spot check ─────────────────────────────────────────────────────────
    hdr("spot check — eyeball these against charts you know")
    cols = [c for c in (
        "symbol",
        "rs_rating",
        "sector",
        "close",
        "ret_1m",
        "pct_from_52w_high",
        "adr_pct_5d",
        "turnover_30d_cr",
        "minervini_score",
        "volume_ratio",
    ) if c in df.columns]
    top = df.nlargest(min(8, len(df)), "rs_rating")[cols] \
        if "rs_rating" in df.columns else df[cols].head(8)
    print(top.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

    # ── summary ────────────────────────────────────────────────────────────
    hdr("summary")
    print(f"  {G}{_n['ok']} ok{X}   {Y}{_n['warn']} warn{X}   {R}{_n['fail']} fail{X}")
    if _n["fail"]:
        print(f"\n  {R}Do not build on this snapshot.{X} Each FAIL above is a "
              f"number that is\n  wrong in a way no dashboard would reveal.")
        return 1
    if _n["warn"]:
        print(f"\n  {Y}Usable, with the warnings understood.{X} Empty columns are "
              f"expected\n  for fields nothing computes yet.")
    else:
        print(f"\n  {G}Snapshot is coherent.{X} Safe to build on.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
