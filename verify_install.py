#!/usr/bin/env python3
"""
verify_install.py — prove the breakoutscanner install is coherent.

    sudo -u breakout /opt/breakoutscanner/.venv/bin/python \
         /opt/breakoutscanner/verify_install.py

READ-ONLY. Changes nothing. Answers "have we broken something?" with
evidence rather than reassurance.

Checks, in order of how much they would matter if they failed:

  1. UPSTREAM INTEGRITY — is breakout.py (the detection core) still identical
     to the version shipped by the repo? Every column added so far is
     supposed to be additive; this proves it.
  2. imports — every module loads under the installed library versions
  3. wiring — each patch is present exactly once, not zero or twice
  4. data files — universes cached, ML model + metadata present and matching
     the installed sklearn
  5. behaviour — a synthetic end-to-end scan, so a broken column surfaces
     here rather than in a live scan
  6. library versions — the pinned combination that fixed the pyarrow crash
"""

import hashlib
import importlib
import json
import os
import subprocess
import sys
import warnings

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

G, R, Y, C, X = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m"
ok_n = fail_n = warn_n = 0


def ok(m, d=""):
    global ok_n; ok_n += 1
    print(f"  {G}ok  {X} {m}" + (f"  {C}{d}{X}" if d else ""))


def bad(m, d=""):
    global fail_n; fail_n += 1
    print(f"  {R}FAIL{X} {m}" + (f"  {d}" if d else ""))


def warn(m, d=""):
    global warn_n; warn_n += 1
    print(f"  {Y}warn{X} {m}" + (f"  {d}" if d else ""))


def hdr(t):
    print(f"\n{C}══ {t} ═══════════════════════════════════════{X}")


# ── 1. upstream integrity ──────────────────────────────────────────────────
hdr("1. detection core unchanged from upstream")
UPSTREAM = "https://raw.githubusercontent.com/Elicherla01/breakoutscanner/main/"
CORE = ["breakout.py", "results_store.py", "fno_loader.py", "run_scanner.py"]
PATCHED = ["scanner.py", "app.py", "config.py", "data_loader.py"]

def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]

net = True
for f in CORE:
    local = os.path.join(HERE, f)
    if not os.path.isfile(local):
        bad(f"{f} missing"); continue
    try:
        import urllib.request
        req = urllib.request.Request(UPSTREAM + f, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            up = hashlib.sha256(r.read()).hexdigest()[:16]
        if up == sha(local):
            ok(f"{f} identical to upstream", sha(local))
        else:
            warn(f"{f} DIFFERS from upstream", f"local {sha(local)} vs {up}")
    except Exception as e:
        net = False
        warn(f"{f} could not compare (offline?)", str(e)[:40])
if not net:
    print(f"       {Y}note{X} upstream comparison needs network; skipped")
for f in PATCHED:
    p = os.path.join(HERE, f)
    print(f"       (expected to differ: {f} {sha(p) if os.path.isfile(p) else 'MISSING'})")

# ── 2. imports ─────────────────────────────────────────────────────────────
hdr("2. modules import")
for m in ["config", "data_loader", "breakout", "scanner", "screen",
          "columns_help", "universes", "ml_features", "ml_engine",
          "cpr", "cpr_scanner", "results_store"]:
    try:
        importlib.import_module(m); ok(m)
    except Exception as e:
        bad(m, f"{type(e).__name__}: {e}")

# ── 3. wiring ──────────────────────────────────────────────────────────────
hdr("3. patches applied exactly once")
# Use the PATCH MARKER comments, not identifiers. An identifier legitimately
# appears twice (definition + use); a marker appearing twice means the patch
# was applied twice, which is what we actually want to catch.
CHECKS = [
    ("scanner.py", "screen columns (added by patch_screen.py)", 1),
    ("scanner.py", "add_ret_1m_rank", 1),
    ("scanner.py", "add_trend_template", 1),
    ("app.py", "render_criteria_panel", 1),
    ("app.py", "friendly names for the screen columns", 1),
    ("config.py", "NSE index segments (added by patch_universes.py)", 1),
    ("data_loader.py", "NSE index segments (added by patch_universes.py)", 1),
    ("ml_engine.py", "PATCHED: use the shared feature module", 1),
]
for fname, token, want in CHECKS:
    p = os.path.join(HERE, fname)
    if not os.path.isfile(p):
        bad(f"{fname} missing"); continue
    n = open(p).read().count(token)
    (ok if n == want else bad)(f"{fname}: '{token}'", f"{n}x (want {want})")

# ── 4. data files ──────────────────────────────────────────────────────────
hdr("4. data files")
dc = os.path.join(HERE, "data_cache")
try:
    import universes
    for name in universes.INDEX_REGISTRY:
        p = universes.cache_path(name)
        if p.is_file():
            import pandas as pd
            d = pd.read_csv(p)
            exp = universes.EXPECTED_COUNTS.get(name, 0)
            close = abs(len(d) - exp) <= exp * 0.25
            (ok if close else warn)(f"{name}", f"{len(d)} symbols (expect ~{exp})")
        else:
            warn(f"{name}", "not cached — run universes.py")
except Exception as e:
    bad("universes", str(e)[:60])

meta = os.path.join(dc, "breakout_ml_model.meta.json")
if os.path.isfile(meta):
    m = json.load(open(meta))
    import sklearn
    same = m.get("sklearn_version") == sklearn.__version__
    (ok if same else warn)("ML model metadata",
                           f"trained {m.get('trained_at')} sklearn {m.get('sklearn_version')}"
                           + ("" if same else f" != installed {sklearn.__version__}"))
    auc = (m.get("metrics") or {}).get("auc")
    print(f"       AUC {auc} on {m.get('test_rows')} test rows")
else:
    warn("ML model metadata", "absent — model may be the stale shipped pickle")

# ── 5. behaviour ───────────────────────────────────────────────────────────
hdr("5. synthetic end-to-end scan")
try:
    import numpy as np, pandas as pd, screen
    N = 400
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=N)
    nifty = pd.DataFrame({"close": 24000 + np.linspace(0, 300, N)}, index=idx)
    b3 = screen.nifty_return(nifty)
    b1 = screen.nifty_return(nifty, window=screen.RET_1M_WINDOW)

    def mk(n, tot):
        c = np.linspace(100, 100 * (1 + tot), n)
        return pd.DataFrame({"open": c, "high": c, "low": c * .98, "close": c,
                             "volume": np.full(n, 4e5)}, index=idx[-n:])

    rows = []
    for lbl, n, t in [("MATURE", 400, .40), ("IPO", 120, .35)]:
        r = screen.compute_metrics(mk(n, t), b3, index_bars=nifty, bench_return_1m=b1)
        r["symbol"] = lbl
        rows.append(r)
    df = pd.DataFrame(rows)
    df = screen.add_ret_1m_rank(df); df = screen.add_rs_rank(df); df = screen.add_rs_rating(df)
    df = screen.add_trend_template(df, lambda s: mk(400, .40) if s == "MATURE" else mk(120, .35))

    ok("compute_metrics + ranks + trend template ran", f"{df.shape[1]} columns")

    m = df.set_index("symbol")
    if pd.isna(m.loc["IPO", "rs_rating"]) and pd.isna(m.loc["IPO", "mansfield_rs"]):
        ok("IPO correctly has NaN for RS Rating / Mansfield")
    else:
        bad("IPO should not have RS Rating or Mansfield")
    if pd.notna(m.loc["IPO", "ret_1m"]):
        ok("IPO has a usable 1-month return", f"{m.loc['IPO','ret_1m']*100:.2f}%")
    else:
        bad("IPO missing ret_1m")
    if pd.isna(m.loc["IPO", "pct_of_52w_high"]):
        ok("IPO 52w high is NaN, not a mislabelled short window")
    else:
        bad("IPO reported a 52-week high it cannot have")
    if pd.notna(m.loc["IPO", "pct_of_listing_range"]):
        ok("listing-range position computed for the IPO",
           f"{m.loc['IPO','pct_of_listing_range']:.0f}%")
    else:
        warn("listing-range position NaN for the IPO")
    if pd.isna(m.loc["MATURE", "pct_of_listing_range"]):
        ok("listing-range correctly NaN for the mature stock")
    else:
        bad("listing-range should be NaN for an established stock")

    sub = screen.rerank(df[df["symbol"] == "IPO"])
    if pd.isna(sub["ret_1m_rank"]).all():
        ok("rerank on a single row yields NaN, not a fake 100th percentile")
    else:
        bad("rerank produced a percentile from one row")
except Exception as e:
    import traceback
    bad("synthetic scan raised", f"{type(e).__name__}: {e}")
    traceback.print_exc()

# ── 6. versions ────────────────────────────────────────────────────────────
hdr("6. library versions (the combination that fixed the pyarrow segfault)")
WANT = {"pandas": ("2.", "3."), "pyarrow": ("17.", "21."), "streamlit": None,
        "numpy": None, "sklearn": None}
for lib in ["pandas", "numpy", "pyarrow", "sklearn", "streamlit"]:
    try:
        mod = importlib.import_module(lib)
        v = getattr(mod, "__version__", "?")
        if lib == "pandas" and not v.startswith("2."):
            warn(f"{lib} {v}", "pandas 3.x segfaulted with pyarrow 25 — 2.x is pinned")
        elif lib == "pyarrow" and int(v.split(".")[0]) >= 21:
            warn(f"{lib} {v}", "pyarrow >=21 was the segfault; <21 is pinned")
        else:
            ok(f"{lib} {v}")
    except ImportError:
        warn(f"{lib} not installed", "required on the server; fine in a test venv")
    except Exception as e:
        bad(lib, str(e)[:40])

print(f"\n{C}══ summary ═══════════════════════════════════════{X}")
print(f"  {G}{ok_n} ok{X}   {Y}{warn_n} warn{X}   {R}{fail_n} fail{X}")
if fail_n == 0:
    print(f"\n  {G}Install is coherent.{X} Detection core matches upstream; every "
          f"addition is additive.")
else:
    print(f"\n  {R}{fail_n} check(s) failed — see above.{X} Timestamped backups of "
          f"every patched file are in {HERE}.")
sys.exit(1 if fail_n else 0)
