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

# Reference palette (dataviz skill): categorical slots 1-5, diverging blue/red poles.
PALETTES = {
    "light": {"series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"],
              "pos": "#2a78d6", "neg": "#e34948", "text": "#52514e"},
    "dark": {"series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"],
             "pos": "#3987e5", "neg": "#e66767", "text": "#c3c2b7"},
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
EDINET_DOC_TYPES = {
    "120": "Annual report", "130": "Amended annual report", "140": "Quarterly report",
    "150": "Amended quarterly report", "160": "Semiannual report",
    "170": "Amended semiannual report", "180": "Extraordinary report",
    "350": "Large-shareholding report", "360": "Change report",
}
# EDINET's document viewer; the API's own download links need the subscription key.
EDINET_VIEWER = "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{},,"
FIN_ITEMS = {  # edinet_fins item -> column (¥bn)
    "total_assets": "Total assets", "net_assets": "Net assets",
    "equity_parent": "Shareholders' equity", "cash": "Cash & equivalents",
    "cf_operating": "Operating CF", "cf_investing": "Investing CF", "cf_financing": "Financing CF",
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


def doc_type_name(code) -> str:
    return EDINET_DOC_TYPES.get(code, code or "")


def edinet_sections(code: str) -> None:
    """Filings, large shareholders and balance sheet / cash flow from EDINET for one stock."""
    if counts["edinet_docs"] == 0:
        st.info("No EDINET filings yet. Add a free `EDINET_API_KEY` to `.env`, then run "
                "`uv run jquantster sync` for filings, large shareholders and balance sheets.")
        return

    st.subheader("Filings")
    docs = q("""
        SELECT d.submit_date, d.doc_type_code, d.filer_name, d.doc_description, d.doc_id
        FROM edinet_docs d
        WHERE COALESCE(d.withdrawal_status, '0') NOT IN ('1', '2')
          AND (d.edinet_code IN (SELECT edinet_code FROM edinet_codes WHERE sec_code = ?1)
               OR (d.doc_type_code IN ('350', '360') AND d.issuer_edinet_code IN
                   (SELECT edinet_code FROM edinet_codes WHERE sec_code = ?1)))
        ORDER BY d.submit_datetime DESC, d.doc_id DESC LIMIT 30""", code)
    if docs.empty:
        st.caption("No EDINET filings by or about this company in the synced period.")
    else:
        st.dataframe(pd.DataFrame({
            "Date": docs.submit_date, "Type": docs.doc_type_code.map(doc_type_name),
            "Filer": docs.filer_name, "Description": docs.doc_description,
            "Document": docs.doc_id.map(EDINET_VIEWER.format),
        }), hide_index=True, width="stretch", column_config={
            "Document": st.column_config.LinkColumn("Document", display_text="Open in EDINET"),
        })

    st.subheader("Large shareholders")
    h = q("""
        SELECT h.*, COALESCE(d.edinet_code, h.holder) AS holder_id, d.submit_datetime
        FROM edinet_holdings h LEFT JOIN edinet_docs d USING (doc_id)
        WHERE h.code = ? AND h.holding_ratio IS NOT NULL
        ORDER BY h.submit_date, d.submit_datetime, h.doc_id""", code)
    if h.empty:
        st.caption("No large-shareholding (5%) reports about this company in the synced period.")
    else:
        h["prev_report"] = h.groupby("holder_id").holding_ratio.shift()
        latest = h.groupby("holder_id").tail(1).copy()
        latest["change"] = latest.holding_ratio - latest.prev_holding_ratio.fillna(latest.prev_report)
        latest = latest.sort_values("holding_ratio", ascending=False)
        st.dataframe(pd.DataFrame({
            "Holder": latest.holder, "Ratio (%)": latest.holding_ratio,
            "Change (pt)": latest.change, "Shares": latest.shares_held,
            "Report date": latest.submit_date,
            "Status": latest.holding_ratio.map(lambda r: "Below 5% (exited)" if r < 5 else ""),
        }), hide_index=True, width="stretch", column_config={
            "Ratio (%)": st.column_config.NumberColumn(format="%.2f"),
            "Change (pt)": st.column_config.NumberColumn(format="%+.2f"),
            "Shares": st.column_config.NumberColumn(format="%,.0f"),
        })
        top = latest.head(len(PAL["series"]))
        names = list(top.holder)
        hist = h[h.holder_id.isin(top.holder_id)].copy()
        hist["holder"] = hist.holder_id.map(dict(zip(top.holder_id, top.holder)))
        hist["date"] = pd.to_datetime(hist.submit_date)
        color = alt.Color("holder:N", scale=alt.Scale(domain=names, range=PAL["series"][:len(names)]),
                          legend=alt.Legend(title=None, orient="top"))
        lo, hi = min(hist.holding_ratio.min(), 5.0), hist.holding_ratio.max()
        pad = max(0.5, (hi - lo) * 0.1)
        y_scale = alt.Scale(domain=[max(0.0, lo - pad), hi + pad], nice=False)
        base = alt.Chart(hist).encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%b %Y", tickCount="month")),
            y=alt.Y("holding_ratio:Q", title="Holding ratio (%)", scale=y_scale),
            color=color)
        lines = base.mark_line(strokeWidth=2, interpolate="step-after")
        points = base.mark_point(filled=True, size=70, opacity=1).encode(tooltip=[
            alt.Tooltip("holder:N", title="Holder"), alt.Tooltip("submit_date:N", title="Reported"),
            alt.Tooltip("holding_ratio:Q", title="Ratio %", format=".2f"),
            alt.Tooltip("shares_held:Q", title="Shares", format=",.0f")])
        threshold = alt.Chart(pd.DataFrame({"y": [5.0]})).mark_rule(
            color="gray", strokeDash=[4, 4], opacity=0.6).encode(y=alt.Y("y:Q", scale=y_scale))
        layers = [threshold, lines, points]
        if len(names) <= 4:  # direct labels at each holder's latest report
            ends = hist.groupby("holder").tail(1)
            layers.append(alt.Chart(ends).mark_text(
                align="right", dx=-6, dy=-10, fontSize=11, color=PAL["text"]).encode(
                x="date:T", y=alt.Y("holding_ratio:Q", scale=y_scale), text="holder:N"))
        st.altair_chart(alt.layer(*layers).properties(height=280), width="stretch")
        st.caption("Ratio per report, held until the next one; dashed line at the 5% reporting "
                   f"threshold. Chart shows the {len(names)} largest holders by latest ratio.")

    st.subheader("Balance sheet & cash flow")
    f = q("""
        SELECT l.period_end, l.item, l.value, l.basis, l.doc_type_code, l.submit_date
        FROM edinet_fins_latest l WHERE l.code = ?""", code)
    if f.empty:
        st.caption("No balance-sheet or cash-flow figures from annual or semiannual reports yet.")
        return
    wide = f.pivot_table(index="period_end", columns="item", values="value", aggfunc="first") / 1e9
    meta = (f.sort_values("submit_date").groupby("period_end")
            .agg(basis=("basis", "last"), doc=("doc_type_code", "last")))
    out = pd.DataFrame({"Period end": wide.index})
    for item, label in FIN_ITEMS.items():
        if item in wide:  # items no report had are left out rather than shown empty
            out[f"{label} (¥bn)"] = wide[item].values
    out["Basis"] = meta.basis.reindex(wide.index).map(
        {"consolidated": "Consolidated", "non_consolidated": "Non-consolidated"}).values
    out["Report"] = meta.doc.reindex(wide.index).map(doc_type_name).values
    out = out.sort_values("Period end", ascending=False)
    st.dataframe(out, hide_index=True, width="stretch", column_config={
        c: st.column_config.NumberColumn(format="%,.1f") for c in out.columns if "¥bn" in c})
    st.caption("From EDINET XBRL. Shareholders' equity is J-GAAP ShareholdersEquity (excludes "
               "accumulated other comprehensive income) or IFRS equity attributable to owners.")


MACRO_SOURCE = "Source: Ministry of Finance Japan (JGB yields); Bank of Japan (call rate)."
RANGES = {"1Y": 1, "5Y": 5, "Max": None}  # years shown; ranges over 2 years are weekly


def tenor_years(tenor: str) -> int:
    return int(tenor.removesuffix("Y"))


def weekly_if_long(df: pd.DataFrame, years: int | None, by: str | None = None) -> pd.DataFrame:
    """Daily rows for short ranges; the last value of each week beyond 2 years, which keeps
    charts light (27 years of daily data is ~7,000 points per series)."""
    if years is not None and years <= 2:
        return df
    keys = [by] if by else []
    return (df.set_index("date").groupby(keys + [pd.Grouper(freq="W-FRI")]).last()
            .reset_index().dropna())


def clip_years(df: pd.DataFrame, years: int | None) -> pd.DataFrame:
    if years is None or df.empty:
        return df
    return df[df.date >= df.date.max() - pd.DateOffset(years=years)]


def date_x(years: int | None) -> alt.X:
    """Year ticks over long ranges, month ticks for a year."""
    if years is not None and years <= 2:
        return alt.X("date:T", title=None, axis=alt.Axis(format="%b %Y"))
    return alt.X("date:T", title=None, axis=alt.Axis(format="%Y", tickCount="year"))


def yield_curve_chart(jgb: pd.DataFrame) -> alt.Chart:
    """Latest curve against 1 month, 1 year and 3 years earlier (nearest date on or before)."""
    dates = jgb.date.drop_duplicates().sort_values()
    latest = dates.iloc[-1]
    picks = {"Latest": latest}
    for label, offset in (("1 month earlier", pd.DateOffset(months=1)),
                          ("1 year earlier", pd.DateOffset(years=1)),
                          ("3 years earlier", pd.DateOffset(years=3))):
        before = dates[dates <= latest - offset]
        if not before.empty and before.iloc[-1] not in picks.values():
            picks[label] = before.iloc[-1]
    curves = []
    for label, d in picks.items():
        c = jgb[jgb.date == d].copy()
        c["curve"] = f"{label} ({d:%Y-%m-%d})" if label != "Latest" else f"{d:%Y-%m-%d}"
        curves.append(c)
    curves = pd.concat(curves)
    curves["years"] = curves.tenor.map(tenor_years)
    names = list(dict.fromkeys(curves.curve))
    color = alt.Color("curve:N", scale=alt.Scale(domain=names, range=PAL["series"][:len(names)]),
                      legend=alt.Legend(title=None, orient="top", labelLimit=0))
    base = alt.Chart(curves).encode(
        x=alt.X("years:Q", title="Tenor (years)", scale=alt.Scale(domain=[0, 40]),
                axis=alt.Axis(values=[1, 2, 5, 10, 20, 30, 40])),
        y=alt.Y("yield_pct:Q", title="Yield (%)", scale=alt.Scale(zero=False)),
        color=color,
        tooltip=[alt.Tooltip("curve:N", title="Date"), alt.Tooltip("tenor:N", title="Tenor"),
                 alt.Tooltip("yield_pct:Q", title="Yield %", format=".3f")])
    return alt.layer(base.mark_line(strokeWidth=2), base.mark_point(filled=True, size=40)
                     ).properties(height=300)


def macro_tab() -> None:
    jgb = q("SELECT date, tenor, yield_pct FROM jgb_yields ORDER BY date")
    call = q("SELECT date, value FROM macro_obs WHERE key = 'call_rate' ORDER BY date")
    if jgb.empty and call.empty:
        st.info("No macro data yet. Run `uv run jquantster sync`: JGB yields (Ministry of "
                "Finance) and Bank of Japan series need no key. Check `MACRO_ENABLED` isn't "
                "set to 0 in `.env`.")
        return
    jgb["date"] = pd.to_datetime(jgb.date)
    call["date"] = pd.to_datetime(call.date)

    ten_two = jgb[jgb.tenor.isin(["2Y", "10Y"])].pivot(index="date", columns="tenor",
                                                         values="yield_pct").dropna()
    cols = st.columns(4)
    if not ten_two.empty:
        last = ten_two.iloc[-1]
        month_ago = ten_two[ten_two.index <= ten_two.index[-1] - pd.DateOffset(months=1)]
        prev = month_ago.iloc[-1] if not month_ago.empty else None

        def bp(t):
            return f"{(last[t] - prev[t]) * 100:+.0f}bp vs 1M ago" if prev is not None else None
        cols[0].metric("JGB 10Y", f"{last['10Y']:.3f}%", bp("10Y"), delta_color="off")
        cols[1].metric("JGB 2Y", f"{last['2Y']:.3f}%", bp("2Y"), delta_color="off")
        spread = last["10Y"] - last["2Y"]
        cols[2].metric("10Y − 2Y", f"{spread:+.2f}pt")
    if not call.empty:
        cols[3].metric("Call rate", f"{call.value.iloc[-1]:.3f}%")

    if not jgb.empty:
        st.subheader("JGB yield curve")
        st.altair_chart(yield_curve_chart(jgb), width="stretch")
        st.caption(f"As of {jgb.date.max():%Y-%m-%d}. Constant-maturity yields, each curve "
                   "on the nearest business day on or before the date.")

    years = RANGES[st.radio("Range", list(RANGES), index=1, horizontal=True)]

    if not ten_two.empty:
        st.subheader("10Y and 2Y yields")
        long = clip_years(ten_two.reset_index(), years).melt(
            id_vars="date", value_vars=["10Y", "2Y"], var_name="tenor", value_name="yield_pct")
        long = weekly_if_long(long, years, by="tenor")
        color = alt.Color("tenor:N", scale=alt.Scale(domain=["10Y", "2Y"],
                          range=PAL["series"][:2]), legend=alt.Legend(title=None, orient="top"))
        st.altair_chart(alt.Chart(long).mark_line(strokeWidth=2).encode(
            x=date_x(years), y=alt.Y("yield_pct:Q", title="Yield (%)"),
            color=color, tooltip=[alt.Tooltip("date:T", title="Date"),
                                  alt.Tooltip("tenor:N", title="Tenor"),
                                  alt.Tooltip("yield_pct:Q", title="Yield %", format=".3f")],
        ).properties(height=260), width="stretch")
        sp = clip_years(ten_two.reset_index(), years)
        sp = weekly_if_long(sp.assign(spread=sp["10Y"] - sp["2Y"])[["date", "spread"]], years)
        zero = alt.Chart(pd.DataFrame({"y": [0.0]})).mark_rule(color="gray", opacity=0.6
                                                               ).encode(y="y:Q")
        st.altair_chart(alt.layer(zero, alt.Chart(sp).mark_area(
            line={"color": PAL["series"][2]}, color=PAL["series"][2], opacity=0.25).encode(
            x=date_x(years), y=alt.Y("spread:Q", title="10Y − 2Y (pt)"),
            tooltip=[alt.Tooltip("date:T", title="Date"),
                     alt.Tooltip("spread:Q", title="10Y − 2Y", format="+.3f")],
        )).properties(height=140), width="stretch")
        st.caption(f"As of {ten_two.index[-1]:%Y-%m-%d}. "
                   + ("Daily." if years is not None and years <= 2 else "Weekly (Friday)."))

    if not call.empty:
        st.subheader("Overnight call rate")
        c = weekly_if_long(clip_years(call, years), years)
        st.altair_chart(alt.Chart(c).mark_line(interpolate="step-after", strokeWidth=2,
                                               color=PAL["series"][0]).encode(
            x=date_x(years), y=alt.Y("value:Q", title="Call rate (%)"),
            tooltip=[alt.Tooltip("date:T", title="Date"),
                     alt.Tooltip("value:Q", title="Call rate %", format=".3f")],
        ).properties(height=200), width="stretch")
        st.caption(f"Uncollateralized overnight, daily average. As of {call.date.max():%Y-%m-%d}.")

    st.caption(MACRO_SOURCE)


# ── sidebar ──────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("Jquantster")
    st.caption("J-Quants (JPX) data, stored locally")
    st.markdown(f"**Plan:** {db.get_meta(conn(), 'plan', settings.plan.name)}  \n"
                f"**Readable window:** {db.get_meta(conn(), 'window', '—')}  \n"
                f"**Last sync:** {db.get_meta(conn(), 'last_sync', 'never')}")
    counts = {t: q(f"SELECT COUNT(*) n FROM {t}").n[0]
              for t in ("issues", "daily_bars", "fins_summary", "investor_types",
                        "edinet_docs", "edinet_holdings", "edinet_fins", "jgb_yields",
                        "macro_obs")}
    st.dataframe(pd.DataFrame({"rows": counts}), width="stretch")
    ent = q("SELECT dataset, status FROM entitlements ORDER BY dataset")
    if not ent.empty:
        st.caption("Dataset access")
        st.dataframe(ent, hide_index=True, width="stretch")
    st.caption("Refresh data from a terminal:")
    st.code("uv run jquantster sync", language="bash")

if not any(counts[t] for t in ("daily_bars", "investor_types", "jgb_yields", "macro_obs")):
    st.info("The database is empty. Add your API key to `.env`, then run `uv run jquantster sync`.")
    st.stop()

tab_stock, tab_market, tab_flows, tab_macro = st.tabs(
    ["Stock", "Market", "Investor flows", "Macro"])

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

        edinet_sections(code)

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

# ── Macro ────────────────────────────────────────────────────────────────
with tab_macro:
    macro_tab()
