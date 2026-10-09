from datetime import date
from pathlib import Path

import httpx
import pytest

from jquantster import db
from jquantster.client import JQuantsClient, RateLimiter
from jquantster.config import PLANS, Settings
from jquantster.sync import Syncer
from fake_api import FakeAPI

TODAY = date(2026, 10, 7)
APP = Path(__file__).parents[1] / "src" / "jquantster" / "app.py"


def run_sync(tmp_path, plan, entitled, market_days=2):
    settings = Settings("k", PLANS[plan], ("7203", "6758"), tmp_path / "s.db")
    conn = db.connect(settings.db_path)
    limiter = RateLimiter(conn, sleep=lambda s: None)
    client = JQuantsClient("k", limiter, 10_000, 10_000, transport=httpx.MockTransport(FakeAPI(entitled)))
    results = Syncer(settings, conn, client, log=lambda m: None, today=TODAY).run_all(
        settings.watchlist, market_days)
    return conn, client, {r.job: r for r in results}


def test_free_plan_sync(tmp_path):
    conn, client, res = run_sync(tmp_path, "free", {"calendar", "master", "bars", "fins"})
    assert res["investor types"].status == "skipped"
    edinet = ("edinet filings", "edinet holdings", "edinet financials")
    assert all(res[j].status == "skipped" for j in edinet)  # no EDINET key
    macro = ("macro jgb", "macro boj")
    assert all(res[j].status == "skipped" for j in macro)  # no macro client given
    assert all(r.status == "ok" for j, r in res.items()
               if j not in ("investor types", *edinet, *macro))
    # free plan: nothing newer than 12 weeks ago
    newest = conn.execute("SELECT MAX(date) FROM daily_bars").fetchone()[0]
    assert newest <= PLANS["free"].window(TODAY)[1].isoformat()
    # second run only fetches what is new: nothing for watchlist/market bars
    calls_before = client.calls
    s2 = Syncer(Settings("k", PLANS["free"], ("7203", "6758"), tmp_path / "s.db"), conn, client,
                log=lambda m: None, today=TODAY)
    assert s2.watchlist_bars(("7203", "6758")).calls == 0
    assert s2.market_bars(2).calls == 0
    assert client.calls == calls_before


def test_light_plan_keeps_corrections_and_view_picks_latest(tmp_path):
    conn, _, res = run_sync(tmp_path, "light", {"calendar", "master", "bars", "fins", "investor_types"})
    assert res["investor types"].status == "ok"
    assert conn.execute("SELECT COUNT(*) FROM investor_types").fetchone()[0] == 11
    latest = conn.execute("SELECT COUNT(*), MAX(FrgnBal) FROM investor_types_latest").fetchone()
    assert tuple(latest) == (10, 9e6)


def test_plan_says_yes_but_api_says_no(tmp_path):
    conn, _, res = run_sync(tmp_path, "light", {"calendar", "master", "bars"})
    assert res["investor types"].status == "not_entitled"
    assert res["watchlist financials"].status == "not_entitled"
    row = conn.execute("SELECT status FROM entitlements WHERE dataset='investor_types'").fetchone()
    assert row[0] == "not_entitled"


@pytest.mark.parametrize("plan,entitled", [
    ("free", {"calendar", "master", "bars", "fins"}),
    ("light", {"calendar", "master", "bars", "fins", "investor_types"}),
])
def test_dashboard_renders(tmp_path, monkeypatch, plan, entitled):
    from streamlit.testing.v1 import AppTest
    run_sync(tmp_path, plan, entitled)
    monkeypatch.setenv("JQUANTS_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("JQUANTS_PLAN", plan)
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not at.exception, at.exception
    assert len(at.tabs) == 3
    assert len(at.metric) >= 6  # stock + market headline numbers
    flows_info = [i.value for i in at.info]
    if plan == "free":
        assert any("Light plan" in v for v in flows_info)
    else:
        assert not any("Light plan" in v for v in flows_info)


def test_dashboard_empty_db(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("JQUANTS_DB", str(tmp_path / "empty.db"))
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not at.exception
    assert "database is empty" in at.info[0].value


# ── EDINET sections in the Stock tab ─────────────────────────────────────
def seed_edinet(conn):
    """Toyota (72030) filings, two holders' reports about it and two periods of figures."""
    from fake_edinet import DOC, filing

    def holder_doc(doc_id, day, holder, edinet_code, type_code="350"):
        return filing(doc_id, day, edinetCode=edinet_code, filerName=holder,
                      docTypeCode=type_code, issuerEdinetCode="E02144", csvFlag="1",
                      docDescription="大量保有報告書" if type_code == "350" else "変更報告書")

    annual = {**DOC, "docID": "S100UI01", "submitDateTime": "2026-06-20 09:00"}
    semi = {**DOC, "docID": "S100UI02", "submitDateTime": "2026-10-06 09:00",
            "docTypeCode": "160", "periodEnd": "2026-09-30", "docDescription": "半期報告書"}
    holders = [  # doc, ratio, previous ratio, shares
        (holder_doc("S100UH01", "2025-12-01", "Alpha Capital", "E11111"), 6.1, None, 1.0e8),
        (holder_doc("S100UH02", "2026-05-01", "Alpha Capital", "E11111", "360"), 7.25, 6.1, 1.2e8),
        (holder_doc("S100UH03", "2026-01-15", "Beta Trust", "E22222"), 5.5, None, 0.9e8),
        (holder_doc("S100UH04", "2026-08-03", "Beta Trust", "E22222", "360"), 4.8, 5.5, 0.8e8),
    ]
    other = filing("S100UO01", "2026-06-25", edinetCode="E99999", secCode="99990",
                   filerName="他社株式会社", docTypeCode="120", csvFlag="1")
    docs = [annual, semi, other, *(h[0] for h in holders)]
    db.upsert_edinet_docs(conn, docs, "2026-10-06")
    for d, ratio, prev, shares in holders:
        row = {"doc_id": d["docID"], "code": "72030", "filer_name": d["filerName"],
               "submit_date": d["submitDateTime"][:10], "doc_type_code": d["docTypeCode"]}
        db.save_edinet_holding(conn, row, {"holding_ratio": ratio, "prev_holding_ratio": prev,
                                           "shares_held": shares,
                                           "obligation_date": row["submit_date"]}, "ok")
    for d, total, basis in ((annual, 90e12, "consolidated"), (semi, 95e12, "non_consolidated")):
        row = {"doc_id": d["docID"], "code": "72030", "period_end": d["periodEnd"],
               "doc_type_code": d["docTypeCode"]}
        db.save_edinet_fins(conn, row, {"total_assets": (total, basis), "net_assets": (35e12, basis),
                                        "cash": (8e12, basis), "cf_operating": (4e12, basis)}, "ok")


def frame_with(at, column):
    return next(d.value for d in at.dataframe if column in d.value.columns)


def stock_app(tmp_path, monkeypatch, seed):
    from streamlit.testing.v1 import AppTest
    conn, _, _ = run_sync(tmp_path, "free", {"calendar", "master", "bars", "fins"})
    if seed:
        seed_edinet(conn)
    monkeypatch.setenv("JQUANTS_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("JQUANTS_PLAN", "free")
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not at.exception, at.exception
    return at


def test_dashboard_edinet_sections(tmp_path, monkeypatch):
    at = stock_app(tmp_path, monkeypatch, seed=True)
    at = at.selectbox[0].set_value("72030").run()
    assert not at.exception, at.exception
    heads = [s.value for s in at.subheader]
    assert {"Filings", "Large shareholders", "Balance sheet & cash flow"} <= set(heads)

    filings = frame_with(at, "Document")
    assert list(filings.Date) == sorted(filings.Date, reverse=True)
    assert set(filings.Type) == {"Annual report", "Semiannual report",
                                 "Large-shareholding report", "Change report"}
    assert "S100UO01" not in "".join(filings.Document)  # another company's filing
    assert filings.Document.iloc[0].endswith("S100UI02,,")

    holders = frame_with(at, "Holder")
    assert list(holders.Holder) == ["Alpha Capital", "Beta Trust"]  # by latest ratio
    alpha, beta = holders.iloc[0], holders.iloc[1]
    assert alpha["Ratio (%)"] == 7.25 and round(alpha["Change (pt)"], 2) == 1.15
    assert beta["Status"] == "Below 5% (exited)" and alpha["Status"] == ""
    specs = [c.proto.spec for c in at.get("vega_lite_chart")]
    assert any("Holding ratio" in s and "Alpha Capital" in s for s in specs)

    bs = frame_with(at, "Total assets (¥bn)")
    assert list(bs["Period end"]) == ["2026-09-30", "2026-03-31"]
    assert list(bs["Total assets (¥bn)"]) == [95_000, 90_000]
    assert list(bs.Basis) == ["Non-consolidated", "Consolidated"]
    assert list(bs.Report) == ["Semiannual report", "Annual report"]
    assert "EDINET_API_KEY" not in " ".join(i.value for i in at.info)


def test_dashboard_edinet_nothing_for_this_stock(tmp_path, monkeypatch):
    at = stock_app(tmp_path, monkeypatch, seed=True)  # first stock is 67580: no EDINET rows
    assert at.selectbox[0].value == "67580"
    captions = " ".join(c.value for c in at.caption)
    assert "No EDINET filings" in captions and "No large-shareholding" in captions
    assert "No balance-sheet" in captions
    assert "EDINET_API_KEY" not in " ".join(i.value for i in at.info)


def test_dashboard_edinet_not_synced(tmp_path, monkeypatch):
    at = stock_app(tmp_path, monkeypatch, seed=False)
    infos = [i.value for i in at.info if "EDINET" in i.value]
    assert len(infos) == 1 and "EDINET_API_KEY" in infos[0]
    assert "Filings" not in [s.value for s in at.subheader]
