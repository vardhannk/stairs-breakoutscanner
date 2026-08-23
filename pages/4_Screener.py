"""
Screener — predefined scans, a custom builder, and multiple scans.

Goes in /opt/breakoutscanner/pages/4_Screener.py

Queries the snapshot table built by build_snapshot.py, so every scan is a
SQL WHERE clause and returns in milliseconds. Nothing is computed here.

Three tabs, matching how the work actually goes:

  Predefined    twelve scanners, rules visible and editable
  Build         pick fields, set thresholds, save
  Multiple      run several at once and see which each name passed

If the snapshot has not been built the page says so and gives the command
rather than silently showing an empty table.
"""

import json
import os
import sys

import pandas as pd
import streamlit as st

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

st.set_page_config(page_title="Screener", page_icon="🔍", layout="wide")

try:
    import scan_engine as SE
    import ui_theme as T
    from build_snapshot import TABLE, connect, db_path
except Exception as e:                                  # pragma: no cover
    st.error(f"Could not import a required module: {e}")
    st.stop()

T.apply()
st.title("🔍 Screener")

DB = db_path()
if not os.path.isfile(DB):
    st.warning("**No snapshot yet.** Build one first — it takes a few minutes "
               "and only needs doing once a day:")
    st.code("sudo -u breakout /opt/breakoutscanner/.venv/bin/python \\\n"
            "     /opt/breakoutscanner/build_snapshot.py "
            '--universe "NIFTY Smallcap 250"', language="bash")
    st.caption("Needs duckdb:  sudo -u breakout "
               "/opt/breakoutscanner/.venv/bin/pip install duckdb")
    st.stop()


# NOT cached: a long-lived read connection blocks build_snapshot from ever
# getting a write lock, so an open browser tab would stop the nightly build.
# Opening read-only costs a millisecond; holding one costs you the pipeline.
def _conn():
    return connect(DB, read_only=True)


conn = _conn()
try:
    # .fetchall() is not optional here. sqlite3's execute() returns a CURSOR,
    # which is iterable, so `for r in conn.execute(...)` works. DuckDB's
    # execute() returns the CONNECTION, which is not — iterating it raises
    # "'DuckDBPyConnection' object is not iterable". Always fetch explicitly.
    dates = [r[0] for r in conn.execute(
        f"SELECT DISTINCT date FROM {TABLE} ORDER BY date DESC LIMIT 30"
    ).fetchall()]
except Exception as e:
    st.error(f"Snapshot unreadable ({e}). Rebuild it.")
    st.stop()
if not dates:
    st.warning("Snapshot is empty. Run build_snapshot.py.")
    st.stop()

c1, c2, c3 = st.columns([1, 1, 2])
with c1:
    as_of = st.selectbox("Snapshot date", dates, index=0)
with c2:
    # DuckDB wants parameters as a list, not a tuple
    n_rows = conn.execute(f"SELECT count(*) FROM {TABLE} WHERE date=?",
                          [as_of]).fetchone()[0]
    st.metric("Symbols", f"{n_rows:,}")
with c3:
    st.caption(f"`{DB}` · {len(dates)} day(s) of history. Each extra day makes "
               "an honest backtest possible; a single day makes none.")

SHOW = [
    "symbol",
    "rs_rating",
    "sector",
    "close",
    "ret_1m",
    "pct_of_52w_high",
    "minervini_score",
    "adr_pct_5d",
    "turnover_30d_cr",
    "volume_ratio",
]


def render(rows, extra=()):
    if not rows:
        st.info("No matches. That is a result — loosen a threshold to see "
                "which one is binding.")
        return
    df = pd.DataFrame(rows)
    cols = [c for c in list(extra) + SHOW if c in df.columns]
    st.success(f"{len(df)} matches")
    st.dataframe(df[cols].round(2), hide_index=True, use_container_width=True)
    st.download_button("Download CSV", df[cols].to_csv(index=False).encode(),
                       file_name="scan.csv", mime="text/csv")


tab1, tab2, tab3 = st.tabs(["Predefined", "Build your own", "Multiple scans"])

# ── predefined ─────────────────────────────────────────────────────────────
with tab1:
    name = st.selectbox("Scanner", list(SE.PREDEFINED))
    definition = SE.PREDEFINED[name]
    with st.expander("Rules — every predefined scanner is readable and editable"):
        st.json(definition)
        st.caption("There is no privileged built-in path: these use the same "
                   "format your own saved scanners use.")
    if st.button("Run", type="primary", use_container_width=True):
        try:
            render(SE.run_scan(conn, definition, table=TABLE, on_date=as_of))
        except SE.ScanError as e:
            st.error(str(e))

# ── custom builder ─────────────────────────────────────────────────────────
with tab2:
    st.caption("Fields come from the same registry the engine validates "
               "against, so a control here cannot silently do nothing.")
    schema = SE.ui_schema()
    logic = st.radio("Combine with", ["AND", "OR"], horizontal=True)

    rules = []
    for group in schema:
        with st.expander(group["group"], expanded=(group["group"] == "Technicals")):
            for f in group["fields"]:
                spec = SE.FIELDS[f["field"]]
                cc1, cc2, cc3 = st.columns([2.4, 1.1, 1.5])
                with cc1:
                    on = st.checkbox(f["label"], key=f"use_{f['field']}",
                                     help=f["help"] or None)
                if not on:
                    continue
                if spec.kind == "bool":
                    with cc2:
                        want = st.selectbox("", ["yes", "no"],
                                            key=f"b_{f['field']}",
                                            label_visibility="collapsed")
                    rules.append({"field": f["field"],
                                  "op": "is_true" if want == "yes" else "is_false"})
                elif spec.kind == "text":
                    with cc2:
                        val = st.text_input("", key=f"t_{f['field']}",
                                            label_visibility="collapsed")
                    if val:
                        rules.append({"field": f["field"], "op": "=", "value": val})
                else:
                    with cc2:
                        op = st.selectbox("", [">=", "<=", ">", "<", "=", "between"],
                                          key=f"o_{f['field']}",
                                          label_visibility="collapsed")
                    with cc3:
                        if op == "between":
                            a, b = st.columns(2)
                            lo = a.number_input("", key=f"lo_{f['field']}",
                                                label_visibility="collapsed")
                            hi = b.number_input("", key=f"hi_{f['field']}",
                                                value=100.0,
                                                label_visibility="collapsed")
                            rules.append({"field": f["field"], "op": "between",
                                          "value": [lo, hi]})
                        else:
                            v = st.number_input("", key=f"v_{f['field']}",
                                                label_visibility="collapsed")
                            rules.append({"field": f["field"], "op": op, "value": v})

    if rules:
        definition = {"logic": logic, "rules": rules}
        st.code(json.dumps(definition, indent=2), language="json")
        b1, b2 = st.columns([1, 3])
        with b1:
            run = st.button("Run scan", type="primary", use_container_width=True)
        with b2:
            st.download_button("Save this scanner (JSON)",
                               json.dumps(definition, indent=2).encode(),
                               file_name="scanner.json", mime="application/json",
                               use_container_width=True)
        if run:
            try:
                render(SE.run_scan(conn, definition, table=TABLE, on_date=as_of))
            except SE.ScanError as e:
                st.error(str(e))
    else:
        st.info("Tick a field above to start building.")

# ── multiple ───────────────────────────────────────────────────────────────
with tab3:
    picked = st.multiselect("Scanners to include", list(SE.PREDEFINED),
                            default=list(SE.PREDEFINED)[:3])
    op = st.radio("Operation between scanners", ["AND", "OR"], horizontal=True,
                  help="OR is usually more useful: it shows which setups each "
                       "name qualifies for, rather than demanding all of them.")
    if picked and st.button("Run all", type="primary", use_container_width=True):
        try:
            rows = SE.run_multiple(conn, {k: SE.PREDEFINED[k] for k in picked},
                                   op=op, table=TABLE, on_date=as_of)
            render(rows, extra=["symbol", "scanners_passed_n", "scanners_passed"])
        except SE.ScanError as e:
            st.error(str(e))
