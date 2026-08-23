"""
scan_engine.py — compile a JSON rule tree into parameterised SQL.

This is what turns "Create Screener" and "Multiple Scans" into a database
query instead of a Python loop over 2,000 symbols. A scan becomes a WHERE
clause over the precomputed snapshot table and returns in milliseconds.

    from scan_engine import compile_scanner, run_scan, run_multiple

    d = {"logic": "AND", "rules": [
            {"field": "rs_rating",       "op": ">=", "value": 80},
            {"field": "pct_of_52w_high", "op": ">=", "value": 85},
            {"field": "is_vcp",          "op": "is_true"}]}
    rows = run_scan(conn, d, on_date="2026-08-07")

SECURITY MODEL — read this before adding a field
------------------------------------------------
User-defined scanners are user input that becomes SQL. There is exactly one
defence and it is not escaping: **nothing reaches the query as text.**

  * field names are looked up in FIELDS. A name that is not a key is rejected.
    The SQL identifier comes from the table, never from the request.
  * operators are looked up in OPS. Same rule.
  * values are bound parameters, and are coerced to the field's declared type
    first, so "1; DROP TABLE" fails as a float long before it reaches SQL.

Never add a code path that formats a user string into the query. `eval()`,
f-strings around field names, and "just this one dynamic ORDER BY" are how
screeners get owned.

Adding a field means adding a FIELDS entry. If it is not in FIELDS it does
not exist, which also means the UI can be generated from the same dictionary
and cannot drift out of sync with what the engine accepts.
"""

from __future__ import annotations

from dataclasses import dataclass, field as _dc_field
from typing import Any


# ── field registry ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Field:
    column: str          # actual SQL column; NEVER taken from user input
    kind: str            # num | int | bool | text
    label: str           # what the UI shows
    unit: str = ""
    group: str = "Technicals"
    help: str = ""


FIELDS: dict[str, Field] = {
    # ── identity ───────────────────────────────────────────────────────
    "symbol":  Field("symbol", "text", "Symbol", group="Miscellaneous"),
    "sector":  Field("sector", "text", "Sector", group="Miscellaneous"),
    "industry": Field("industry", "text", "Industry", group="Miscellaneous"),
    "basic_industry": Field("basic_industry", "text", "Basic Industry",
                            group="Miscellaneous",
                            help="Finest NSE level, e.g. Electrical-Power Equipment"),

    # ── price & liquidity ──────────────────────────────────────────────
    "close": Field("close", "num", "Stock Price", "₹"),
    "avg_vol_10d": Field("avg_vol_10d", "num", "10-day Avg Volume", "shares"),
    "turnover_30d_cr": Field("turnover_30d_cr", "num",
                             "Avg Price × Volume, 30d", "₹ Cr",
                             help="Traded value. The liquidity filter that "
                                  "actually matters — a big % move on ₹2 lakh "
                                  "of turnover is not tradeable."),
    "market_cap_cr": Field("market_cap_cr", "num", "Market Cap", "₹ Cr"),
    "free_float_pct": Field("free_float_pct", "num", "Free Float", "%"),

    # ── moving averages ────────────────────────────────────────────────
    "above_10ema": Field("above_10ema", "bool", "Above 10 EMA"),
    "above_21ema": Field("above_21ema", "bool", "Above 21 EMA"),
    "above_50ema": Field("above_50ema", "bool", "Above 50 EMA"),
    "above_200ema": Field("above_200ema", "bool", "Above 200 EMA"),
    "ma_stacked": Field("ma_stacked", "bool", "MA Order 20 ≥ 50 ≥ 200",
                        help="Textbook uptrend alignment"),
    "sma200_rising_months": Field("sma200_rising_months", "num",
                                  "200 DMA Rising For", "months"),

    # ── relative strength ──────────────────────────────────────────────
    "rs_rating": Field("rs_rating", "int", "RS Rating", "1-99",
                       help="O'Neil percentile. 80+ was his threshold."),
    "mansfield_rs": Field("mansfield_rs", "num", "Mansfield RS",
                          help="Zero is the line — catches stocks TURNING "
                               "outperformer, which a plain return misses."),
    "ret_1m": Field("ret_1m", "num", "1 Month Return", "%"),
    "ret_3m": Field("ret_3m", "num", "3 Month Return", "%"),
    "ret_6m": Field("ret_6m", "num", "6 Month Return", "%"),
    "ret_12m": Field("ret_12m", "num", "12 Month Return", "%"),
    "rs_leads_price": Field("rs_leads_price", "bool", "RS High Before Price High"),
    # Universe-relative ranks. screen.py computed these and build_snapshot
    # discarded them, because write() only persists columns registered here —
    # so "1m rank >= 90" silently matched nothing on a 40-symbol snapshot
    # where four names should have qualified.
    "ret_1m_rank": Field("ret_1m_rank", "num", "1m Return Rank", "0-100",
                         help="Percentile of the 1-month return across the "
                              "scanned universe. Needs only 22 bars, so it "
                              "works on recent listings where RS Rating cannot."),
    "rs_rank": Field("rs_rank", "num", "RS Rank", "0-100",
                     help="Percentile of 3-month outperformance vs NIFTY."),

    # ── position in range ──────────────────────────────────────────────
    "pct_of_52w_high": Field("pct_of_52w_high", "num", "% of 52w High", "%"),
    "pct_from_52w_high": Field("pct_from_52w_high", "num", "% from 52w High", "%"),
    "pct_from_52w_low": Field("pct_from_52w_low", "num", "% from 52w Low", "%"),
    "pct_from_ath": Field("pct_from_ath", "num", "% from All-Time High", "%"),

    # ── volatility ─────────────────────────────────────────────────────
    "adr_pct_5d": Field("adr_pct_5d", "num", "ADR% (5 day)", "%",
                        help="Average daily range. Position sizing input: a "
                             "2% ADR name needs a different stop to a 9% one."),
    "adr_pct_20d": Field("adr_pct_20d", "num", "ADR% (20 day)", "%"),
    "atr_pct": Field("atr_pct", "num", "ATR as % of price", "%"),

    # ── trend template ─────────────────────────────────────────────────
    "minervini_score": Field("minervini_score", "int", "Minervini Score", "0-8"),

    # ── patterns (from patterns.py) ────────────────────────────────────
    "is_vcp": Field("is_vcp", "bool", "VCP"),
    "vcp_contractions": Field("vcp_contractions", "int", "VCP Contractions"),
    "pct_to_resistance": Field("pct_to_resistance", "num",
                               "% to Horizontal Resistance", "%"),
    "resistance_touches": Field("resistance_touches", "int", "Resistance Touches"),
    "is_inside_bar_d": Field("is_inside_bar_d", "bool", "Inside Bar (Daily)"),
    "is_inside_bar_w": Field("is_inside_bar_w", "bool", "Inside Bar (Weekly)"),
    "is_flag": Field("is_flag", "bool", "Flag"),
    "is_pennant": Field("is_pennant", "bool", "Pennant"),
    "shakeout_10ema": Field("shakeout_10ema", "bool", "10 EMA Shakeout"),
    "shakeout_21ema": Field("shakeout_21ema", "bool", "21 EMA Shakeout"),
    "shakeout_50ema": Field("shakeout_50ema", "bool", "50 EMA Shakeout"),
    "shakeout_200ema": Field("shakeout_200ema", "bool", "200 EMA Shakeout"),
    "tight_range_d": Field("tight_range_d", "bool", "Tight Setup (Daily)"),
    "range_pct_5d": Field("range_pct_5d", "num", "5-day Range", "%"),
    "gap_open_pct": Field("gap_open_pct", "num", "Gap %", "%"),
    "gap_unfilled": Field("gap_unfilled", "bool", "Gap Unfilled"),
    "volume_ratio": Field("volume_ratio", "num", "Volume vs Average", "×"),

    # ── breakout (from breakout.py — the ORIGINAL detector, not a rewrite) ─
    "is_breakout": Field("is_breakout", "bool", "Broke Out",
                         help="Fired on any of 1D/1W/1M. This is breakout.py "
                              "itself, so it agrees with the classic scan."),
    "breakout_direction": Field("breakout_direction", "text", "Direction",
                                help="bullish = broke resistance, "
                                     "bearish = broke support"),
    "breakout_pct": Field("breakout_pct", "num", "Break %", "%",
                          help="How far past the level the close is. Small is "
                               "early; large means the move happened already."),
    "breakout_level": Field("breakout_level", "num", "Break Level", "₹"),
    "breakout_strong_close": Field("breakout_strong_close", "bool",
                                   "Strong Close"),
    "breakout_1d": Field("breakout_1d", "bool", "Breakout (Daily)"),
    "breakout_1w": Field("breakout_1w", "bool", "Breakout (Weekly)"),
    "breakout_1m": Field("breakout_1m", "bool", "Breakout (Monthly)"),
    "breakout_timeframes": Field("breakout_timeframes", "text",
                                 "Breakout Timeframes",
                                 help="Which fired, e.g. '1D, 1W'. More "
                                      "timeframes agreeing is confluence."),

    # ── listing ────────────────────────────────────────────────────────
    "days_since_listing": Field("days_since_listing", "int", "Days Since Listing"),
    "is_recent_listing": Field("is_recent_listing", "bool", "Recent IPO (<1yr)"),

    # ── sector rotation, used AS A FILTER ──────────────────────────────
    # The screenshot's "Leading, Improving industries wrt Nifty 50" — top-down
    # context as a bottom-up screening input. Cheap once sector quadrants are
    # computed, and it removes strong-stock-in-dead-sector results.
    "sector_quadrant": Field("sector_quadrant", "text", "Sector Quadrant",
                             help="Leading | Improving | Weakening | Lagging"),
    "sector_breadth_rs80": Field("sector_breadth_rs80", "num",
                                 "% of Sector above RS 80", "%"),
    "circuit_suspect": Field("circuit_suspect", "bool", "Circuit Locked"),
}


# ── operators ──────────────────────────────────────────────────────────────
OPS: dict[str, str] = {
    ">=": ">=", "<=": "<=", ">": ">", "<": "<", "=": "=", "!=": "<>",
}
SPECIAL_OPS = {"between", "in", "not_in", "is_true", "is_false", "is_null",
               "not_null"}


class ScanError(ValueError):
    """Rejected scanner definition. The message is safe to show a user."""


def _coerce(f: Field, v: Any) -> Any:
    """
    Force the value into the field's declared type.

    This is a security control, not a convenience. "1 OR 1=1" is not a float,
    so it dies here rather than reaching the database.
    """
    try:
        if f.kind == "num":
            return float(v)
        if f.kind == "int":
            return int(v)
        if f.kind == "bool":
            if isinstance(v, str):
                return 1 if v.strip().lower() in ("1", "true", "yes", "y") else 0
            return 1 if bool(v) else 0
        return str(v)
    except (TypeError, ValueError):
        raise ScanError(f"{f.label}: {v!r} is not a valid "
                        f"{'number' if f.kind in ('num', 'int') else f.kind}")


def compile_rule(node: dict) -> tuple[str, list]:
    """One leaf or one nested group -> (sql_fragment, params)."""
    if not isinstance(node, dict):
        raise ScanError("each rule must be an object")

    if "logic" in node:
        logic = str(node["logic"]).upper()
        if logic not in ("AND", "OR"):
            raise ScanError("logic must be AND or OR")
        rules = node.get("rules") or []
        if not rules:
            raise ScanError("a group needs at least one rule")
        parts, params = [], []
        for r in rules:
            s, p = compile_rule(r)
            parts.append(s)
            params += p
        return "(" + f" {logic} ".join(parts) + ")", params

    name = node.get("field")
    if name not in FIELDS:
        raise ScanError(f"unknown field {name!r}")
    f = FIELDS[name]
    col = f.column                       # from the registry, never the request
    op = node.get("op", ">=")

    if op in SPECIAL_OPS:
        if op == "is_true":
            return f"{col} = 1", []
        if op == "is_false":
            return f"({col} = 0 OR {col} IS NULL)", []
        if op == "is_null":
            return f"{col} IS NULL", []
        if op == "not_null":
            return f"{col} IS NOT NULL", []
        if op == "between":
            lo, hi = node.get("value") or [None, None]
            return f"{col} BETWEEN ? AND ?", [_coerce(f, lo), _coerce(f, hi)]
        vals = node.get("value") or []
        if not isinstance(vals, (list, tuple)) or not vals:
            raise ScanError(f"{f.label}: '{op}' needs a non-empty list")
        holes = ",".join("?" for _ in vals)
        neg = "NOT " if op == "not_in" else ""
        return f"{col} {neg}IN ({holes})", [_coerce(f, v) for v in vals]

    if op not in OPS:
        raise ScanError(f"unsupported operator {op!r}")
    return f"{col} {OPS[op]} ?", [_coerce(f, node.get("value"))]


def compile_scanner(definition: dict, table: str = "indicator_snapshot",
                    on_date: str | None = None,
                    universe: list[str] | None = None,
                    order_by: str = "rs_rating",
                    descending: bool = True,
                    limit: int = 500) -> tuple[str, list]:
    """Full SELECT. `order_by` is validated against FIELDS like everything else."""
    where, params = compile_rule(definition)

    if on_date:
        where = f"date = ? AND {where}"
        params = [on_date] + params
    if universe:
        holes = ",".join("?" for _ in universe)
        where = f"symbol IN ({holes}) AND {where}"
        params = [str(s) for s in universe] + params

    if order_by not in FIELDS:
        raise ScanError(f"cannot sort by {order_by!r}")
    direction = "DESC" if descending else "ASC"
    limit = max(1, min(int(limit), 5000))

    sql = (f"SELECT * FROM {table} WHERE {where} "
           f"ORDER BY {FIELDS[order_by].column} {direction} LIMIT {limit}")
    return sql, params


def run_scan(conn, definition: dict, **kw) -> list[dict]:
    sql, params = compile_scanner(definition, **kw)
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── multiple scans ─────────────────────────────────────────────────────────
def run_multiple(conn, scanners: dict[str, dict], op: str = "AND",
                 **kw) -> list[dict]:
    """
    Run several scanners and report which ones each symbol passed.

    Matches the "Scanners Passed" column in the reference UI. Deliberately
    runs each scanner separately and intersects in Python rather than building
    one giant WHERE: each scan is already milliseconds, and this way a symbol
    can be shown with the LIST of scanners it satisfied. A combined query
    would only tell you that it passed, not what it passed — which is the
    interesting part when you are stacking four different setups.
    """
    op = str(op).upper()
    if op not in ("AND", "OR"):
        raise ScanError("op must be AND or OR")

    hits: dict[str, dict] = {}
    passed: dict[str, list[str]] = {}
    for name, definition in scanners.items():
        for row in run_scan(conn, definition, **kw):
            sym = row.get("symbol")
            hits.setdefault(sym, row)
            passed.setdefault(sym, []).append(name)

    n = len(scanners)
    out = []
    for sym, names in passed.items():
        if op == "AND" and len(names) < n:
            continue
        row = dict(hits[sym])
        row["scanners_passed"] = ", ".join(sorted(names))
        row["scanners_passed_n"] = len(names)
        out.append(row)
    out.sort(key=lambda r: (-r["scanners_passed_n"],
                            -(r.get("rs_rating") or 0)))
    return out


# ── predefined library ─────────────────────────────────────────────────────
# RS Rating >= 80 is the standard strength line throughout. One relative-
# strength measure, not three: Minervini embeds O'Neil's RS as its own eighth
# criterion, and Mansfield measures the same outperformance a derivative
# earlier. Stacking them narrows almost nothing — run filter_overlap.py
# against your own snapshot to see it rather than take my word.
#
# Each is just a definition — the same format a user's saved scanner uses.
# There is no privileged built-in path, so anything shipped here can be
# opened, inspected and edited in the UI. A screener you cannot read the
# rules of is asking for trust it has not earned.
PREDEFINED: dict[str, dict] = {
    "Horizontal Resistance": {"logic": "AND", "rules": [
        {"field": "pct_to_resistance", "op": "between", "value": [0, 5]},
        {"field": "resistance_touches", "op": ">=", "value": 3},
        {"field": "turnover_30d_cr", "op": ">=", "value": 5},
    ]},
    "Tight Setup (Daily)": {"logic": "AND", "rules": [
        {"field": "tight_range_d", "op": "is_true"},
        {"field": "above_50ema", "op": "is_true"},
        {"field": "turnover_30d_cr", "op": ">=", "value": 5},
    ]},
    "VCP": {"logic": "AND", "rules": [
        {"field": "is_vcp", "op": "is_true"},
        {"field": "vcp_contractions", "op": ">=", "value": 2},
        {"field": "rs_rating", "op": ">=", "value": 80},
    ]},
    "RS High Before Price High": {"logic": "AND", "rules": [
        {"field": "rs_leads_price", "op": "is_true"},
        {"field": "turnover_30d_cr", "op": ">=", "value": 5},
    ]},
    "Flags & Pennants": {"logic": "AND", "rules": [
        {"logic": "OR", "rules": [
            {"field": "is_flag", "op": "is_true"},
            {"field": "is_pennant", "op": "is_true"}]},
        {"field": "rs_rating", "op": ">=", "value": 80},
    ]},
    "Inside Bar (Daily)": {"logic": "AND", "rules": [
        {"field": "is_inside_bar_d", "op": "is_true"},
        {"field": "above_21ema", "op": "is_true"},
    ]},
    "21 EMA Shakeout": {"logic": "AND", "rules": [
        {"field": "shakeout_21ema", "op": "is_true"},
        {"field": "rs_rating", "op": ">=", "value": 80},
    ]},
    "Gap Up (Unfilled)": {"logic": "AND", "rules": [
        {"field": "gap_open_pct", "op": ">=", "value": 3},
        {"field": "gap_unfilled", "op": "is_true"},
    ]},
    "Volume Gainers": {"logic": "AND", "rules": [
        {"field": "volume_ratio", "op": ">=", "value": 3},
        {"field": "avg_vol_10d", "op": ">=", "value": 100000},
    ]},
    "Recent IPO Setups": {"logic": "AND", "rules": [
        {"field": "is_recent_listing", "op": "is_true"},
        {"field": "turnover_30d_cr", "op": ">=", "value": 5},
        {"field": "ret_1m", "op": ">=", "value": 0},
    ]},
    # No rs_rating rule here on purpose: Minervini's EIGHTH criterion is
    # "RS Rating >= 70", so the score already contains it. Adding an RS gate
    # on top is the same test twice, and reads as two independent conditions.
    "Minervini Trend Template": {"logic": "AND", "rules": [
        {"field": "minervini_score", "op": ">=", "value": 7},
        {"field": "turnover_30d_cr", "op": ">=", "value": 5},
    ]},
    # ── THE DAILY SCREEN ────────────────────────────────────────────
    # One route, not a menu. Established strength (RS Rating), current
    # strength (1m rank), the entry trigger (breakout), and enough turnover
    # to actually trade it. Everything else in this library is a variation
    # on one of those four ideas — see filter_overlap.py for the evidence.
    "★ Daily Screen — breakout in a strong stock": {"logic": "AND", "rules": [
        {"field": "is_breakout", "op": "is_true"},
        {"field": "breakout_direction", "op": "=", "value": "bullish"},
        {"field": "rs_rating", "op": ">=", "value": 80},
        {"field": "ret_1m_rank", "op": ">=", "value": 90},
        {"field": "turnover_30d_cr", "op": ">=", "value": 5},
    ]},
    # Same, minus the timing trigger — the watchlist of names to be ready
    # for when they DO break out.
    "★ Watchlist — strong, not yet broken out": {"logic": "AND", "rules": [
        {"field": "is_breakout", "op": "is_false"},
        {"field": "rs_rating", "op": ">=", "value": 80},
        {"field": "ret_1m_rank", "op": ">=", "value": 90},
        {"field": "turnover_30d_cr", "op": ">=", "value": 5},
    ]},
    # Recent listings cannot have an RS Rating. O'Neil's measure needs 12
    # months of history; a stock listed eight months ago has none, so the
    # value is NULL — not zero, and not weak. The Daily Screen above requires
    # rs_rating >= 80, which no recent listing can ever satisfy, so they were
    # invisible to it by construction. On the full NSE universe that is 676
    # of 3,044 rows.
    #
    # This does NOT loosen the Daily Screen. Weakening the main screen to fit
    # a subpopulation would quietly lower the bar for everything. Instead the
    # strength judgement moves onto the one measure that IS available: the
    # 1-month return percentile, which needs only 22 bars. The threshold is
    # 80 — the same percentile O'Neil used for RS — rather than a number
    # chosen to make any particular stock appear.
    #
    # rs_rating IS NULL is the guard that keeps this list to genuinely young
    # listings, so an established weak stock cannot arrive through here.
    "★ Recent Listings — breaking out, no RS history yet":
        {"logic": "AND", "rules": [
            {"field": "is_breakout", "op": "is_true"},
            {"field": "breakout_direction", "op": "=", "value": "bullish"},
            {"field": "rs_rating", "op": "is_null"},
            {"field": "ret_1m_rank", "op": ">=", "value": 80},
            {"field": "turnover_30d_cr", "op": ">=", "value": 5},
        ]},
    "Breakouts today (all)": {"logic": "AND", "rules": [
        {"field": "is_breakout", "op": "is_true"},
        {"field": "breakout_direction", "op": "=", "value": "bullish"},
    ]},
    "Leading Sector Momentum": {"logic": "AND", "rules": [
        {"field": "sector_quadrant", "op": "in", "value": ["Leading", "Improving"]},
        {"field": "rs_rating", "op": ">=", "value": 80},
        {"field": "pct_of_52w_high", "op": ">=", "value": 85},
    ]},
}


def ui_schema() -> list[dict]:
    """
    Field metadata for building the Create Screener panel.

    Generated from FIELDS so the form and the engine cannot disagree about
    what exists — the usual way a screener ends up with a control that
    silently does nothing.
    """
    groups: dict[str, list] = {}
    for name, f in FIELDS.items():
        groups.setdefault(f.group, []).append({
            "field": name, "label": f.label, "kind": f.kind,
            "unit": f.unit, "help": f.help,
        })
    return [{"group": g, "fields": sorted(v, key=lambda x: x["label"])}
            for g, v in groups.items()]
