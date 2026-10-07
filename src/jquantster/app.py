"""Streamlit dashboard over the local SQLite database. Read-only: run `jquantster sync` to fill it."""

from __future__ import annotations

import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from jquantster import db
from jquantster.config import load_settings

st.set_page_config(page_title="Jquantster", page_icon="📈", layout="wide")

# Reference palette (dataviz skill): categorical slots 1-4, diverging blue/red poles.
PALETTES = {
    "light": {"series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"], "pos": "#2a78d6", "neg": "#e34948"},
    "dark": {"series": ["#3987e5", "#d95926", "#199e70", "#c98500"], "pos": "#3987e5", "neg": "#e66767"},
}
theme = getattr(getattr(st.context, "theme", None), "type", None) or "light"
PAL = PALETTES["dark" if theme == "dark" else "light"]
settings = load_settings()

INVESTORS = {
    "Frgn": "Foreigners", "Ind": "Individuals", "TrstBnk": "Trust banks",
    "InvTr": "Investment trusts", "Prop": "Proprietary", "BusCo": "Business cos",
    "InsCo": "Insurance cos", "Bank": "City & regional banks", "SecCo": "Securities cos",
    "OthFin": "Other financials", "OthCo": "Other cos",
}
MARKETS_EN = {"プライム": "Prime", "スタンダード": "Standard", "グロース": "Growth",
              "TOKYO PRO MARKET": "Tokyo Pro", "その他": "Other"}
SECTIONS = {
    "TSEPrime": "Prime", "TSEStandard": "Standard", "TSEGrowth": "Growth",
    "TSE1st": "1st Section (to 2022-04)", "TSE2nd": "2nd Section (to 2022-04)",
    "TSEMothers": "Mothers (to 2022-04)", "TSEJASDAQ": "JASDAQ (to 2022-04)",
    "TokyoNagoya": "Tokyo & Nagoya",
}


@st.cache_resource
def _connect(path: str):
    return db.connect(Path(path))


def conn():
    return _connect(str(settings.db_path))


def q(sql: str, *params) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn(), params=params)


def hover_line(df: pd.DataFrame, x: str, y: str, color: str, y_title: str, tooltip) -> alt.LayerChart:
    """Single-series line with a crosshair + tooltip on the nearest date."""
    nearest = alt.selection_point(nearest=True, on="pointerover", fields=[x], empty=False)
    base = alt.Chart(df).encode(x=alt.X(f"{x}:T", title=None))
    line = base.mark_line(strokeWidth=2, color=color).encode(
        y=alt.Y(f"{y}:Q", title=y_title, scale=alt.Scale(zero=False))
    )
    rule = base.mark_rule(color="gray", opacity=0.5).encode(
        opacity=alt.condition(nearest, alt.value(0.6), alt.value(0)), tooltip=tooltip
    ).add_params(nearest)
    dot = line.mark_point(size=60, filled=True, color=color).encode(
        opacity=alt.condition(nearest, alt.value(1), alt.value(0))
    )
    return alt.layer(line, rule, dot)


# ── sidebar ──────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("Jquantster")
    st.caption("J-Quants (JPX) data, stored locally")
    st.markdown(f"**Plan:** {db.get_meta(conn(), 'plan', settings.plan.name)}  \n"
                f"**Readable window:** {db.get_meta(conn(), 'window', '—')}  \n"
                f"**Last sync:** {db.get_meta(conn(), 'last_sync', 'never')}")
    counts = {t: q(f"SELECT COUNT(*) n FROM {t}").n[0]
              for t in ("issues", "daily_bars", "fins_summary", "investor_types")}
    st.dataframe(pd.DataFrame({"rows": counts}), width="stretch")
    ent = q("SELECT dataset, status FROM entitlements ORDER BY dataset")
    if not ent.empty:
        st.caption("Dataset access")
        st.dataframe(ent, hide_index=True, width="stretch")
    st.caption("Refresh data from a terminal:")
    st.code("uv run jquantster sync", language="bash")

if counts["daily_bars"] == 0 and counts["investor_types"] == 0:
    st.info("The database is empty. Add your API key to `.env`, then run `uv run jquantster sync`.")
    st.stop()

tab_stock, tab_market, tab_flows = st.tabs(["Stock", "Market", "Investor flows"])

# ── Stock ────────────────────────────────────────────────────────────────
with tab_stock:
    stocks = q("""
        SELECT b.code, COALESCE(i.co_name_en, i.co_name, '') AS name, COUNT(*) AS n
        FROM daily_bars b LEFT JOIN issues i USING (code)
        GROUP BY b.code HAVING n > 20 ORDER BY b.code""")
    if stocks.empty:
        st.info("No stock history yet. Set JQUANTS_WATCHLIST in `.env` and run a sync.")
    else:
        labels = {r.code: f"{r.code[:4]}  {r.name}" for r in stocks.itertuples()}
        code = st.selectbox("Stock", list(labels), format_func=labels.get)
        bars = q("SELECT * FROM daily_bars WHERE code = ? AND c IS NOT NULL ORDER BY date", code)
        bars["date"] = pd.to_datetime(bars["date"])
        last, first = bars.iloc[-1], bars.iloc[0]
        prev = bars.iloc[-2] if len(bars) > 1 else last

        c1, c2, c3, c4 = st.columns(4)
        c1.metric(f"Close {last.date:%Y-%m-%d}", f"¥{last.c:,.0f}",
                  f"{(last.adj_c / prev.adj_c - 1):+.2%} on the day")
        c2.metric(f"Return since {first.date:%Y-%m-%d}", f"{(last.adj_c / first.adj_c - 1):+.1%}")
        c3.metric("Low – high (adjusted)", f"¥{bars.adj_l.min():,.0f}–{bars.adj_h.max():,.0f}")
        c4.metric("Avg daily turnover, last 20 days", f"¥{bars.va.tail(20).mean() / 1e9:,.1f}bn")

        tooltip = [alt.Tooltip("date:T", title="Date"),
                   alt.Tooltip("adj_c:Q", title="Adj. close", format=",.1f"),
                   alt.Tooltip("c:Q", title="Close", format=",.0f"),
                   alt.Tooltip("vo:Q", title="Volume", format=",.0f")]
        price = hover_line(bars, "date", "adj_c", PAL["series"][0], "Adjusted close (¥)", tooltip)
        volume = alt.Chart(bars).mark_bar(color=PAL["series"][0], opacity=0.55).encode(
            x=alt.X("date:T", title=None), y=alt.Y("vo:Q", title="Volume"),
            tooltip=tooltip[:1] + tooltip[3:],
        )
        st.altair_chart(alt.vconcat(price.properties(height=320), volume.properties(height=90))
                        .resolve_scale(x="shared"), width="stretch")
        st.caption("Prices adjusted for splits and rights issues (not dividends). "
                   "Adjusted history is recalculated by JPX after each new corporate action.")

        fins = q("SELECT raw FROM fins_summary WHERE code = ? ORDER BY disc_date DESC", code)
        if not fins.empty:
            st.subheader("Reported results")
            f = pd.DataFrame([json.loads(r) for r in fins.raw])
            cols = [c for c in ("DiscDate", "DocType", "CurPerType", "CurPerEn",
                                "Sales", "OP", "NP", "EPS") if c in f.columns]
            f = f[cols]
            for c in ("Sales", "OP", "NP"):
                if c in f:
                    f[c] = pd.to_numeric(f[c], errors="coerce") / 1e9
            if "EPS" in f:
                f["EPS"] = pd.to_numeric(f["EPS"], errors="coerce")
            st.dataframe(f, hide_index=True, width="stretch", column_config={
                "DiscDate": "Disclosed", "DocType": "Document", "CurPerType": "Period",
                "CurPerEn": "Period end",
                "Sales": st.column_config.NumberColumn("Sales (¥bn)", format="%.1f"),
                "OP": st.column_config.NumberColumn("Operating profit (¥bn)", format="%.1f"),
                "NP": st.column_config.NumberColumn("Net profit (¥bn)", format="%.1f"),
                "EPS": st.column_config.NumberColumn("EPS (¥)", format="%.2f"),
            })

# ── Market ───────────────────────────────────────────────────────────────
with tab_market:
    days = q("SELECT date FROM daily_bars GROUP BY date HAVING COUNT(*) > 1000 ORDER BY date DESC").date
    if len(days) < 2:
        st.info("Needs two full trading days. Run `uv run jquantster sync --market-days 2`.")
    else:
        day = st.selectbox("Trading day", list(days[:-1]))
        prev_day = days[days < day].iloc[0]
        m = q("""
            SELECT b.code, COALESCE(i.co_name_en, i.co_name) AS name, i.mkt_nm AS market,
                   i.s33_nm AS sector, b.c AS close, b.va / 1e8 AS turnover,
                   b.adj_c / p.adj_c - 1 AS chg
            FROM daily_bars b
            JOIN daily_bars p ON p.code = b.code AND p.date = ?
            LEFT JOIN issues i ON i.code = b.code
            WHERE b.date = ? AND b.c IS NOT NULL AND p.adj_c > 0""", prev_day, day)
        m["code"] = m.code.str[:4]
        m[["name", "market", "sector"]] = m[["name", "market", "sector"]].fillna("")
        m["market"] = m.market.replace(MARKETS_EN)
        breadth = (m.chg > 0).sum(), (m.chg < 0).sum()
        c1, c2, c3 = st.columns(3)
        c1.metric("Advancers / decliners", f"{breadth[0]:,} / {breadth[1]:,}")
        c2.metric("Median change", f"{m.chg.median():+.2%}")
        c3.metric("Total turnover", f"¥{m.turnover.sum() / 1e4:,.2f}tn")

        cfg = {
            "chg": st.column_config.NumberColumn("Change", format="percent"),
            "close": st.column_config.NumberColumn("Close (¥)", format="%,.0f"),
            "turnover": st.column_config.NumberColumn("Turnover (¥100m)", format="%,.1f"),
            "name": st.column_config.TextColumn("name", width="medium"),
        }
        liquid = m[m.turnover >= 1]  # ≥ ¥100m traded, so thin stocks don't top the lists
        short = ["code", "name", "chg", "turnover"]
        left, right = st.columns(2)
        left.markdown("**Top gainers** (turnover ≥ ¥100m)")
        left.dataframe(liquid.nlargest(15, "chg"), hide_index=True, column_config=cfg, column_order=short,
                       width="stretch")
        right.markdown("**Top decliners** (turnover ≥ ¥100m)")
        right.dataframe(liquid.nsmallest(15, "chg"), hide_index=True, column_config=cfg, column_order=short,
                       width="stretch")
        st.markdown("**Most traded**")
        st.dataframe(m.nlargest(20, "turnover"), hide_index=True, column_config=cfg, width="stretch",
                     column_order=["code", "name", "market", "sector", "chg", "close", "turnover"])

# ── Investor flows ───────────────────────────────────────────────────────
with tab_flows:
    flows = q("SELECT * FROM investor_types_latest ORDER BY st_date")
    if flows.empty:
        status = ent.set_index("dataset").status.get("investor_types", "not synced") if not ent.empty else "not synced"
        st.info("Trading by type of investors needs the **Light plan or higher** "
                f"(current status: {status}). Set `JQUANTS_PLAN` in `.env` after upgrading, then sync.")
    else:
        present = [s for s in SECTIONS if s in set(flows.section)]
        section = st.selectbox("Market section", present, format_func=SECTIONS.get)
        f = flows[flows.section == section].copy()
        f["week"] = pd.to_datetime(f.en_date)
        span_days = (f.week.max() - f.week.min()).days
        week_axis = alt.X("week:T", title=None,
                          axis=alt.Axis(format="%b %Y" if span_days > 180 else "%b %d"))
        st.caption("Weekly, published on the 4th business day after the week. Net = purchases − sales. "
                   "Values in ¥bn (J-Quants reports thousand yen). Corrected weeks use the latest version.")

        who = st.selectbox("Investor type", list(INVESTORS), format_func=INVESTORS.get)
        f["net"] = f[f"{who}Bal"] / 1e6
        f["side"] = f.net.map(lambda v: "Net buy" if v >= 0 else "Net sell")
        st.altair_chart(
            # One bar per week, sized so bars stay ~70% of the slot at any history length.
            alt.Chart(f).mark_bar(cornerRadiusEnd=4, width=max(1.0, min(24.0, 0.7 * 900 / len(f)))).encode(
                x=week_axis,
                y=alt.Y("net:Q", title=f"{INVESTORS[who]} weekly net (¥bn)"),
                color=alt.Color("side:N", scale=alt.Scale(domain=["Net buy", "Net sell"],
                                range=[PAL["pos"], PAL["neg"]]), legend=alt.Legend(title=None, orient="top")),
                tooltip=[alt.Tooltip("st_date:N", title="Week from"), alt.Tooltip("en_date:N", title="to"),
                         alt.Tooltip("net:Q", title="Net ¥bn", format=",.1f"),
                         alt.Tooltip("pub_date:N", title="Published")],
            ).properties(height=280),
            width="stretch",
        )

        # Cumulative net for the four groups that usually drive the market.
        main = ["Frgn", "Ind", "TrstBnk", "InvTr"]
        long = f.melt(id_vars=["week"], value_vars=[f"{k}Bal" for k in main], var_name="k", value_name="v")
        long["investor"] = long.k.str.removesuffix("Bal").map(INVESTORS)
        long = long.sort_values("week")
        long["cum"] = long.groupby("investor").v.cumsum() / 1e6
        names = [INVESTORS[k] for k in main]
        color = alt.Color("investor:N", scale=alt.Scale(domain=names, range=PAL["series"]),
                          legend=alt.Legend(title=None, orient="top"))
        nearest = alt.selection_point(nearest=True, on="pointerover", fields=["week"], empty=False)
        base = alt.Chart(long).encode(x=week_axis)
        lines = base.mark_line(strokeWidth=2).encode(
            y=alt.Y("cum:Q", title="Cumulative net since first week shown (¥bn)"), color=color)
        wide = long.pivot(index="week", columns="investor", values="cum").reset_index()
        rule = alt.Chart(wide).mark_rule(color="gray").encode(
            x="week:T", opacity=alt.condition(nearest, alt.value(0.6), alt.value(0)),
            tooltip=[alt.Tooltip("week:T", title="Week ending")]
                    + [alt.Tooltip(f"{n}:Q", format=",.0f") for n in names],
        ).add_params(nearest)
        st.altair_chart(alt.layer(lines, rule).properties(height=320), width="stretch")

        with st.expander("Table"):
            tbl = f[["st_date", "en_date", "pub_date"] + [f"{k}Bal" for k in INVESTORS]].copy()
            for k in INVESTORS:
                tbl[f"{k}Bal"] = tbl[f"{k}Bal"] / 1e6
            st.dataframe(tbl.rename(columns={f"{k}Bal": v for k, v in INVESTORS.items()})
                         .sort_values("st_date", ascending=False), hide_index=True, width="stretch")
