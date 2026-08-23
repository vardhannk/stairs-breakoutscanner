"""
advice.py — turn a snapshot row into plain English and arithmetic.

The screener's job ends with a list of symbols. This module does the part
that was previously left to you: say WHY each name is on the list, what is
wrong with it, and — given your risk budget — what the position arithmetic
comes to.

WHAT THIS IS, AND IS NOT
------------------------
`plan()` is a CALCULATOR. You supply the rupees you are willing to lose on
one trade; it divides by the stop distance and reports a share count. That is
arithmetic, and it is arithmetic you would otherwise do wrong at 9:20am.

It is NOT a prediction. Nothing here knows whether the trade works. The
`verdict` is a description of the SETUP's condition — extended, thin, clean —
never a claim about the outcome. Roughly 38% of these breakouts historically
reached +3% before −2.5%, which means being on this list is not an edge by
itself; it is a starting point that still needs a chart and a decision.

Everything is a pure function of one row plus your inputs. No database, no
network, no Streamlit — so every number below can be checked by hand.
"""

from __future__ import annotations

import math

# The stop convention. 2x the average daily range means normal noise does not
# take you out, while an actual failure does. It is a CONVENTION, not an
# optimised parameter — see the module note in patterns.py about the
# difference. Change it here and everything downstream follows.
DEFAULT_STOP_ATR_MULT = 2.0

EXTENDED_PCT = 3.0        # more than this past the level and the move has gone
THIN_TURNOVER_CR = 5.0    # below this it is not reliably tradeable
WIDE_ADR_PCT = 6.0        # above this, position size shrinks a lot


def _f(row, key, default=float("nan")):
    v = row.get(key, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def explain(row) -> list[str]:
    """Why this name is on today's list, in words rather than columns."""
    out = []
    rs = _f(row, "rs_rating")
    r1 = _f(row, "ret_1m_rank")
    bp = _f(row, "breakout_pct")
    tf = str(row.get("breakout_timeframes") or "")
    to = _f(row, "turnover_30d_cr")
    hi = _f(row, "pct_from_52w_high")
    sec = str(row.get("sector") or "")

    if row.get("is_breakout"):
        where = f" on {tf}" if tf else ""
        out.append(f"Broke out today{where}"
                   + (f", now {bp:.1f}% above the level it cleared" if bp == bp else ""))
    if rs == rs:
        out.append(f"Stronger than {rs:.0f}% of the market over the last "
                   f"3–12 months (RS {rs:.0f})")
    if r1 == r1:
        out.append(f"Still leading right now — top {100 - r1:.0f}% over the "
                   f"last month")
    if hi == hi:
        out.append(f"Trading {hi:.1f}% below its 52-week high"
                   if hi > 0.5 else "At or very near its 52-week high")
    if to == to:
        out.append(f"₹{to:,.0f} crore traded on an average day — liquid enough "
                   f"to get in and out")
    if sec:
        out.append(f"Sector: {sec}")
    return out


def cautions(row) -> list[str]:
    """
    What is wrong with it, or worth checking before acting.

    Deliberately separate from explain(). A list that only tells you why a
    stock qualified is a sales pitch; the reasons to hesitate belong on the
    same card, not buried in a column you have to know to look at.
    """
    out = []
    bp = _f(row, "breakout_pct")
    to = _f(row, "turnover_30d_cr")
    adr = _f(row, "adr_pct_5d")
    vol = _f(row, "volume_ratio")

    if bp == bp and bp > EXTENDED_PCT:
        out.append(f"**Extended** — already {bp:.1f}% past the breakout level. "
                   f"Entering here means a wider stop for the same trade.")
    if to == to and to < THIN_TURNOVER_CR:
        out.append(f"**Thin** — only ₹{to:.1f} crore traded daily. Your own "
                   f"order may move the price.")
    if adr == adr and adr > WIDE_ADR_PCT:
        out.append(f"**Volatile** — {adr:.1f}% average daily range. A sensible "
                   f"stop is far away, so the position must be small.")
    if vol == vol and vol < 1.0:
        out.append(f"Volume today was {vol:.1f}× average — a breakout without "
                   f"participation is weaker than one with it.")
    if not row.get("breakout_strong_close", True):
        out.append("Closed in the lower part of the day's range.")
    if not str(row.get("sector") or ""):
        out.append("No sector mapping — often a recent listing with little history.")
    return out


def verdict(row) -> tuple[str, str]:
    """
    (label, why) describing the SETUP, never the outcome.

    "Extended" says where price is relative to the level. It does not say the
    trade will fail, and "Candidate" does not say it will work.
    """
    bp = _f(row, "breakout_pct")
    to = _f(row, "turnover_30d_cr")
    if to == to and to < THIN_TURNOVER_CR:
        return "Skip", "too thin to trade cleanly"
    if bp == bp and bp > EXTENDED_PCT:
        return "Wait", "the move already happened — a pullback gives a tighter stop"
    if not row.get("is_breakout"):
        return "Watch", "strong, but no trigger yet"
    return "Candidate", "broke out today, and nothing obvious is wrong"


def plan(row, risk_rupees: float, capital: float = 0.0,
         stop_mult: float = DEFAULT_STOP_ATR_MULT) -> dict:
    """
    Position arithmetic from YOUR risk budget.

        stop        = entry − (stop_mult × ADR% × entry)
        risk/share  = entry − stop
        shares      = risk_rupees ÷ risk/share        (rounded DOWN)

    Rounded down on purpose: rounding up quietly means risking more than the
    number you set, which is the one thing position sizing exists to prevent.

    Targets are stated in R — multiples of what you are risking — because
    "+₹2,000" means nothing without knowing what was at stake to get it.
    """
    entry = _f(row, "close")
    adr = _f(row, "adr_pct_5d")
    if adr != adr:
        adr = _f(row, "atr_pct")
    out = {"entry": entry, "stop": float("nan"), "risk_per_share": float("nan"),
           "shares": 0, "notional": 0.0, "stop_pct": float("nan"),
           "targets": {}, "pct_of_capital": float("nan"), "warnings": []}
    if entry != entry or entry <= 0 or adr != adr or adr <= 0:
        out["warnings"].append("Not enough data to size this position.")
        return out

    stop = entry * (1 - stop_mult * adr / 100.0)
    rps = entry - stop
    if rps <= 0:
        out["warnings"].append("Stop distance computed as zero.")
        return out

    shares = int(math.floor(max(0.0, risk_rupees) / rps))
    notional = shares * entry

    out.update({
        "stop": stop, "risk_per_share": rps, "shares": shares,
        "notional": notional, "stop_pct": (entry - stop) / entry * 100.0,
        "targets": {f"{m}R": entry + m * rps for m in (1, 2, 3)},
    })

    if shares == 0:
        out["warnings"].append(
            f"₹{risk_rupees:,.0f} of risk is not enough for even one share at "
            f"a ₹{rps:,.0f} stop distance. Either widen the risk budget or "
            f"skip this one — do not narrow the stop to make it fit.")
    if capital > 0:
        pct = notional / capital * 100.0
        out["pct_of_capital"] = pct
        if pct > 25:
            out["warnings"].append(
                f"This position would be {pct:.0f}% of your capital. The RISK "
                f"is still ₹{risk_rupees:,.0f}, but a gap through the stop "
                f"would cost far more than that.")
    return out


def next_steps(row) -> list[str]:
    """
    The part no screener can do, stated so it does not get skipped.

    Every name on the list passed the same numeric tests, so the numbers
    cannot rank them further. What separates them is the chart, and that
    judgement is yours.
    """
    return [
        "Open the chart. Is the base tight and orderly, or messy?",
        "Is this the first breakout from the base, or the third push of a run "
        "that has already gone a long way?",
        "Decide your exit BEFORE entering — the stop below is a starting "
        "point, not a plan.",
        "If two names look equally good, the one in a leading sector is the "
        "better use of the same risk.",
    ]
