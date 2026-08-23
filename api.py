"""
api.py — the HTTP contract. Every frontend you will ever build talks to this.

    uvicorn api:app --host 127.0.0.1 --port 8600
    open http://127.0.0.1:8600/docs        # generated, always current

WHY THIS EXISTS BEFORE ANY FRONTEND
-----------------------------------
Frameworks are replaceable; the contract is not. Next.js today, a React
Native app in a year, a terminal client when you want one — all of them speak
JSON over HTTP, and none of them should know that screening is pandas or that
storage is DuckDB.

That is also the honest answer to "how do I avoid rebuilding": not by picking
a frontend that lasts forever, but by making sure the frontend never contains
anything you would mind throwing away. No business logic lives here either —
this module validates, calls, and serialises. Every decision worth arguing
about is in screen.py, patterns.py or scan_engine.py, where it can be tested
without a web server.

DESIGN RULES
------------
1. The API is READ-ONLY over market data. Nothing here places an order, and
   nothing imports the trading app. Screening and execution stay separate
   processes with separate blast radii.
2. Field and scanner metadata is SERVED, not hardcoded in the frontend. The
   Create Scanner form is generated from /meta/fields, so a field added to
   scan_engine.FIELDS appears in the UI with no frontend change.
3. DuckDB is opened read-only, one connection, a cursor per request. DuckDB
   connections are not thread-safe; cursors are the supported way to fan out.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field as PField

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import scan_engine as SE                                  # noqa: E402
from build_snapshot import TABLE, connect, db_path        # noqa: E402

app = FastAPI(
    title="Screener API",
    version="0.1.0",
    description="Read-only market screening. Places no orders.",
)

# Next.js dev server runs on 3000. In production both sit behind the same
# nginx origin, so this list stays short on purpose — a permissive CORS
# policy on an authenticated API is a way to hand your session to any page
# the browser happens to be visiting.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

def cur():
    """
    A NEW read-only connection per request, closed by garbage collection.

    Deliberately not cached. DuckDB permits one writer OR many readers, so a
    connection held open by this service blocks build_snapshot from ever
    getting a write lock — the nightly build fails for as long as the API is
    running, which is always. Opening read-only costs about a millisecond;
    holding one open costs you the data pipeline.
    """
    path = db_path()
    if not os.path.isfile(path):
        raise HTTPException(503, "No snapshot yet. Run build_snapshot.py first.")
    return connect(path, read_only=True)


def rows(c, sql: str, params: list | None = None) -> list[dict]:
    c.execute(sql, params or [])
    cols = [d[0] for d in c.description]
    return [dict(zip(cols, r)) for r in c.fetchall()]


# ── models ─────────────────────────────────────────────────────────────────
class Rule(BaseModel):
    field: str | None = None
    op: str | None = None
    value: Any = None
    logic: Literal["AND", "OR"] | None = None
    rules: list["Rule"] | None = None


class ScanRequest(BaseModel):
    definition: Rule
    on_date: str | None = None
    universe: list[str] | None = None
    order_by: str = "rs_rating"
    descending: bool = True
    limit: int = PField(200, ge=1, le=5000)


class MultiScanRequest(BaseModel):
    scanners: list[str] = PField(..., description="names from /meta/scanners")
    op: Literal["AND", "OR"] = "OR"
    on_date: str | None = None
    limit: int = PField(200, ge=1, le=5000)


# ── meta ───────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    path = db_path()
    out = {"ok": True, "snapshot": os.path.isfile(path), "path": path}
    if out["snapshot"]:
        try:
            c = cur()
            out["dates"] = [r["date"] for r in rows(
                c, f"SELECT DISTINCT date FROM {TABLE} ORDER BY date DESC LIMIT 5")]
            out["symbols"] = rows(
                c, f"SELECT count(*) n FROM {TABLE} WHERE date=(SELECT max(date) "
                   f"FROM {TABLE})")[0]["n"]
        except Exception as e:
            out["ok"] = False
            out["error"] = str(e)[:200]
    return out


@app.get("/api/meta/fields")
def meta_fields():
    """
    Drives the Create Scanner form.

    Served rather than duplicated in the frontend so the two cannot disagree
    about what exists — the usual way a screener ends up with a control that
    silently does nothing.
    """
    return {"groups": SE.ui_schema(),
            "operators": list(SE.OPS) + sorted(SE.SPECIAL_OPS)}


@app.get("/api/meta/scanners")
def meta_scanners():
    return [{"name": n, "definition": d} for n, d in SE.PREDEFINED.items()]


@app.get("/api/snapshot/dates")
def snapshot_dates(limit: int = Query(60, ge=1, le=500)):
    return [r["date"] for r in rows(
        cur(), f"SELECT DISTINCT date FROM {TABLE} ORDER BY date DESC LIMIT {int(limit)}")]


# ── scanning ───────────────────────────────────────────────────────────────
@app.post("/api/scan")
def scan(req: ScanRequest):
    try:
        return {"rows": SE.run_scan(
            cur(), req.definition.model_dump(exclude_none=True), table=TABLE,
            on_date=req.on_date, universe=req.universe,
            order_by=req.order_by, descending=req.descending, limit=req.limit)}
    except SE.ScanError as e:
        # 422, not 500: a rejected scanner is the user describing something
        # invalid, not the server failing. The message is safe to display.
        raise HTTPException(422, str(e))


@app.post("/api/scan/multiple")
def scan_multiple(req: MultiScanRequest):
    unknown = [s for s in req.scanners if s not in SE.PREDEFINED]
    if unknown:
        raise HTTPException(422, f"unknown scanner(s): {', '.join(unknown)}")
    try:
        return {"rows": SE.run_multiple(
            cur(), {s: SE.PREDEFINED[s] for s in req.scanners}, op=req.op,
            table=TABLE, on_date=req.on_date, limit=req.limit)}
    except SE.ScanError as e:
        raise HTTPException(422, str(e))


# ── market analytics ───────────────────────────────────────────────────────
@app.get("/api/sectors")
def sectors(on_date: str | None = None,
            metric: Literal["breadth_rs80", "near_high", "above_50ema"]
            = "breadth_rs80"):
    """
    Sector participation, not sector average.

    "23% of Power names are above RS 80" answers a different question from
    "Power returned 1.8%". An index can be green on two heavyweights while
    most of its constituents fall; breadth cannot hide that, and it is the
    number worth ranking sectors by.
    """
    c = cur()
    on_date = on_date or rows(c, f"SELECT max(date) d FROM {TABLE}")[0]["d"]
    expr = {
        "breadth_rs80": "COUNT(*) FILTER (WHERE rs_rating >= 80)",
        "near_high": "COUNT(*) FILTER (WHERE pct_from_52w_high <= 5)",
        "above_50ema": "COUNT(*) FILTER (WHERE above_50ema)",
    }[metric]
    return {"as_of": on_date, "metric": metric, "rows": rows(c, f"""
        SELECT sector,
               COUNT(*)                          AS members,
               {expr}                            AS hits,
               ROUND(100.0 * {expr} / COUNT(*), 1) AS pct,
               ROUND(AVG(ret_1m), 2)             AS avg_ret_1m,
               ROUND(AVG(rs_rating), 1)          AS avg_rs
        FROM {TABLE}
        WHERE date = ? AND sector IS NOT NULL AND sector <> ''
        GROUP BY sector
        HAVING COUNT(*) >= 3
        ORDER BY pct DESC""", [on_date])}


@app.get("/api/breadth")
def breadth(on_date: str | None = None):
    """Whole-market participation — the health check behind every scan."""
    c = cur()
    on_date = on_date or rows(c, f"SELECT max(date) d FROM {TABLE}")[0]["d"]
    return rows(c, f"""
        SELECT ? AS as_of,
               COUNT(*)                                       AS universe,
               COUNT(*) FILTER (WHERE above_50ema)            AS above_50ema,
               COUNT(*) FILTER (WHERE above_200ema)           AS above_200ema,
               COUNT(*) FILTER (WHERE rs_rating >= 80)        AS rs80_plus,
               COUNT(*) FILTER (WHERE pct_from_52w_high <= 5) AS near_52w_high,
               COUNT(*) FILTER (WHERE ret_1m > 0)             AS up_1m
        FROM {TABLE} WHERE date = ?""", [on_date, on_date])[0]


@app.get("/api/symbol/{symbol}")
def symbol_detail(symbol: str, on_date: str | None = None):
    c = cur()
    on_date = on_date or rows(c, f"SELECT max(date) d FROM {TABLE}")[0]["d"]
    got = rows(c, f"SELECT * FROM {TABLE} WHERE date = ? AND symbol = ?",
               [on_date, symbol.upper()])
    if not got:
        raise HTTPException(404, f"{symbol} not in the {on_date} snapshot")
    return got[0]


@app.get("/api/symbol/{symbol}/history")
def symbol_history(symbol: str, days: int = Query(120, ge=2, le=2000)):
    """
    This symbol's snapshot values over time — how its RS and structure have
    evolved, not its price. Price bars come from /bars below.
    """
    return {"symbol": symbol.upper(), "rows": rows(cur(), f"""
        SELECT date, close, rs_rating, ret_1m, pct_from_52w_high,
               minervini_score, adr_pct_5d, turnover_30d_cr
        FROM {TABLE} WHERE symbol = ?
        ORDER BY date DESC LIMIT {int(days)}""", [symbol.upper()])}


@app.get("/api/symbol/{symbol}/bars")
def symbol_bars(symbol: str, days: int = Query(250, ge=20, le=2500)):
    """
    OHLCV for charting, shaped for TradingView Lightweight Charts.

    Candles want {time, open, high, low, close}; the volume histogram wants
    {time, value, color}. Emitting both series here rather than in the browser
    keeps the chart component dumb, which is the point — a frontend that has
    to reshape data is a frontend that has business logic in it.

    `time` is an ISO date string, which Lightweight Charts accepts directly
    for daily bars and which stays readable in the network tab.

    Reads the scanner's own bar cache. No network call, so this is fast and
    cannot be rate-limited mid-session.
    """
    try:
        from data_loader import load_daily
    except Exception as e:                                  # pragma: no cover
        raise HTTPException(503, f"bar loader unavailable: {e}")

    try:
        df = load_daily(symbol.upper(), use_cache=True)
    except Exception as e:
        raise HTTPException(502, f"could not load bars for {symbol}: {e}")
    if df is None or getattr(df, "empty", True):
        raise HTTPException(404, f"no bars cached for {symbol.upper()}")

    df = df.tail(int(days))
    candles, volumes = [], []
    for ts, r in df.iterrows():
        t = str(getattr(ts, "date", lambda: ts)())[:10]
        try:
            o, h, l, c = (float(r["open"]), float(r["high"]),
                          float(r["low"]), float(r["close"]))
        except (KeyError, TypeError, ValueError):
            continue
        candles.append({"time": t, "open": o, "high": h, "low": l, "close": c})
        v = r.get("volume")
        if v is not None and v == v:
            volumes.append({"time": t, "value": float(v),
                            # up/down colouring decided here so the chart does
                            # not need to know which close preceded which
                            "color": "#26a69a" if c >= o else "#ef5350"})
    return {"symbol": symbol.upper(), "bars": len(candles),
            "candles": candles, "volumes": volumes}


@app.get("/api/universes")
def universes_list():
    """Index segments available to scan, with cached constituent counts."""
    try:
        import universes as U
    except Exception as e:                                  # pragma: no cover
        raise HTTPException(503, f"universes unavailable: {e}")
    out = []
    for name in U.INDEX_REGISTRY:
        try:
            n = len(U.load_index_symbols(name))
        except Exception:
            n = 0
        out.append({"name": name, "symbols": n,
                    "expected": U.EXPECTED_COUNTS.get(name)})
    return out
