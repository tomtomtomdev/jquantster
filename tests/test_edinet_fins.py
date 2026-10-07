from datetime import date

import pytest

from jquantster import cli, db
from jquantster.edinet import parse_fins
from jquantster.sync import Syncer
from fake_edinet import (DOC, FINS_IFRS_SEMI, FINS_JGAAP, FINS_JGAAP_AMENDED, FINS_NONCON,
                         FINS_UNKNOWN, HEADER, FakeEdinet, filing, fin)
from test_edinet_sync import jquants_client, settings, syncer

TODAY = date(2026, 10, 7)
CODES = ("7203", "1301")


def report(doc_id, day, **fields):
    """A Toyota (E02144, 72030) report with a CSV."""
    return {**DOC, "docID": doc_id, "submitDateTime": f"{day} 09:00", **fields}


FN_ANNUAL = report("S100FN01", "2026-06-20")
FN_AMENDED = report("S100FN02", "2026-07-15", docTypeCode="130",
                    docDescription="訂正有価証券報告書－第122期")
FN_SEMI = report("S100FN03", "2026-10-06", docTypeCode="160", periodEnd="2026-09-30",
                 docDescription="半期報告書－第123期")
FN_NONCON = filing("S100FN04", "2026-06-25", edinetCode="E33333", secCode="13010",
                   filerName="極洋", docTypeCode="120", periodEnd="2026-03-31", csvFlag="1")
FN_OTHER_CO = filing("S100FN05", "2026-06-25", edinetCode="E99999", secCode="99990",
                     filerName="他社株式会社", docTypeCode="120", periodEnd="2026-03-31",
                     csvFlag="1")
FN_WITHDRAWN = report("S100FN06", "2026-06-21", withdrawalStatus="2")
FN_NO_CSV = report("S100FN07", "2026-06-22", csvFlag="0")
FN_EXTRAORDINARY = report("S100FN08", "2026-06-23", docTypeCode="180", docDescription="臨時報告書")
FN_UNPARSEABLE = report("S100FN09", "2025-06-20", periodEnd="2025-03-31")
FN_MISSING = report("S100FN10", "2026-10-06", docTypeCode="140", periodEnd="2026-06-30")
# a holder's report about Toyota: the company is the issuer, not the filer
FN_HOLDING = filing("S100FN11", "2026-10-02", edinetCode="E11111", docTypeCode="350",
                    issuerEdinetCode="E02144", csvFlag="1")

ALL = [FN_ANNUAL, FN_AMENDED, FN_SEMI, FN_NONCON, FN_OTHER_CO, FN_WITHDRAWN, FN_NO_CSV,
       FN_EXTRAORDINARY, FN_UNPARSEABLE, FN_MISSING, FN_HOLDING]


def fake():
    api = FakeEdinet({})
    api.csvs.update({"S100FN01": FINS_JGAAP, "S100FN02": FINS_JGAAP_AMENDED,
                     "S100FN03": FINS_IFRS_SEMI, "S100FN04": FINS_NONCON,
                     "S100FN05": FINS_JGAAP, "S100FN06": FINS_JGAAP, "S100FN07": FINS_JGAAP,
                     "S100FN08": FINS_JGAAP, "S100FN09": FINS_UNKNOWN,
                     "S100FN11": FINS_JGAAP})  # S100FN10 is a 404
    return api


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "e.db")
    db.upsert_edinet_docs(c, ALL, "2026-10-06")
    return c


def run(tmp_path, conn, api=None):
    s = syncer(tmp_path, conn, api or fake())
    return s.edinet_financials(CODES)


def downloaded(api):
    return [r.url.path.rsplit("/", 1)[-1] for r in api.calls if "/documents/" in r.url.path]


def fins(conn, doc_id, table="edinet_fins"):
    return {r["item"]: (r["value"], r["basis"]) for r in conn.execute(
        f"SELECT * FROM {table} WHERE doc_id = ?", (doc_id,))}


C, N = "consolidated", "non_consolidated"


def test_jgaap_annual_items_mapped(tmp_path, conn):
    res = run(tmp_path, conn)
    assert res.status == "ok"
    assert fins(conn, "S100FN01") == {
        "total_assets": (90e12, C),  # not the segment, prior-year or non-consolidated figure
        "net_assets": (35e12, C),
        "equity_parent": (33e12, C),
        "cash": (9e12, C),
        "cf_operating": (4.2e12, C),
        "cf_investing": (-3.1e12, C),
        "cf_financing": (-1.5e12, C),  # summary of business results, as a fallback
    }
    row = conn.execute("SELECT * FROM edinet_fins WHERE doc_id = 'S100FN01' "
                       "AND item = 'total_assets'").fetchone()
    assert (row["code"], row["period_end"], row["doc_type_code"]) == ("72030", "2026-03-31", "120")


def test_ifrs_semiannual_items_mapped(tmp_path, conn):
    run(tmp_path, conn)
    assert fins(conn, "S100FN03") == {
        "total_assets": (92e12, C),
        "net_assets": (36e12, C),
        "equity_parent": (34e12, C),  # not the share-capital component member
        "cash": (9.5e12, C),
        "cf_operating": (2.1e12, C),  # not the prior interim period
        "cf_investing": (-1.6e12, C),
        "cf_financing": (-0.4e12, C),
    }
    row = conn.execute("SELECT * FROM edinet_fins WHERE doc_id = 'S100FN03' LIMIT 1").fetchone()
    assert (row["period_end"], row["doc_type_code"]) == ("2026-09-30", "160")


def test_non_consolidated_only_report(tmp_path, conn):
    run(tmp_path, conn)
    assert fins(conn, "S100FN04") == {"total_assets": (5e11, N), "net_assets": (2e11, N)}
    assert conn.execute("SELECT code FROM edinet_fins WHERE doc_id = 'S100FN04'").fetchone()[0] \
        == "13010"


def rows(*r):
    return {"x.csv": [dict(zip(HEADER, x)) for x in r]}


def test_consolidated_preferred_in_any_order():
    noncon = fin("jppfs_cor:Assets", "CurrentYearInstant_NonConsolidatedMember", "当期末", "個別", "2")
    cons = fin("jppfs_cor:Assets", "CurrentYearInstant", "当期末", "連結", "9")
    summary = fin("jpcrp_cor:TotalAssetsSummaryOfBusinessResults", "CurrentYearInstant", "当期末",
                  "連結", "8")
    for order in ([noncon, cons, summary], [summary, cons, noncon], [cons, noncon]):
        assert parse_fins(rows(*order)) == {"total_assets": (9.0, C)}
    # a consolidated summary figure beats a non-consolidated statement
    assert parse_fins(rows(noncon, summary)) == {"total_assets": (8.0, C)}
    assert parse_fins(rows(noncon)) == {"total_assets": (2.0, N)}


def test_prior_and_segment_contexts_ignored():
    got = parse_fins(rows(
        fin("jppfs_cor:Assets", "Prior1YearInstant", "前期末", "連結", "1"),
        fin("jppfs_cor:Assets", "CurrentYearInstant_AutomotiveReportableSegmentMember",
            "当期末", "連結", "2"),
        fin("jppfs_cor:Assets", "Prior1YearInstant_NonConsolidatedMember", "前期末", "個別", "3"),
        # odd context ID, but the relative year says it's the prior period
        fin("jppfs_cor:NetAssets", "CurrentYearInstant", "前期末", "連結", "4"),
        fin("jppfs_cor:Assets", "InterimInstant", "中間期末", "連結", "－"),  # nil
    ))
    assert got == {}


def test_amendment_supersedes_in_latest_view(tmp_path, conn):
    run(tmp_path, conn)
    # both reports kept
    assert fins(conn, "S100FN02") == {"total_assets": (91e12, C)}
    assert fins(conn, "S100FN01")["total_assets"] == (90e12, C)
    latest = {r["item"]: (r["value"], r["doc_id"]) for r in conn.execute(
        "SELECT * FROM edinet_fins_latest WHERE code = '72030' AND period_end = '2026-03-31'")}
    assert latest["total_assets"] == (91e12, "S100FN02")
    assert latest["net_assets"] == (35e12, "S100FN01")  # not in the amendment: original stands
    assert len(latest) == 7
    semi = conn.execute("SELECT COUNT(*) FROM edinet_fins_latest WHERE period_end = '2026-09-30'")
    assert semi.fetchone()[0] == 7


def test_only_watchlist_filers_and_live_csv_docs_fetched(tmp_path, conn):
    api = fake()
    run(tmp_path, conn, api)
    assert sorted(downloaded(api)) == ["S100FN01", "S100FN02", "S100FN03", "S100FN04",
                                       "S100FN09", "S100FN10"]
    assert {r[0] for r in conn.execute("SELECT code FROM edinet_fins")} == {"72030", "13010"}


def test_parsed_docs_not_refetched(tmp_path, conn):
    res = run(tmp_path, conn)
    assert res.rows == 4  # documents stored
    state = {r["doc_id"]: r["status"] for r in conn.execute(
        "SELECT doc_id, status FROM edinet_parsed WHERE job = 'financials'")}
    assert state == {"S100FN01": "ok", "S100FN02": "ok", "S100FN03": "ok", "S100FN04": "ok",
                     "S100FN09": "unparseable", "S100FN10": "not_found"}
    api = fake()
    res = run(tmp_path, conn, api)
    assert res.status == "ok" and res.calls == 0 and downloaded(api) == []


def test_interrupted_run_resumes_per_doc(tmp_path, conn):
    api = fake()
    api.fail_docs = {"S100FN04"}  # oldest first: FN09 (2025), FN01 succeed, then FN04 fails
    res = run(tmp_path, conn, api)
    assert res.status == "error"
    assert {r[0] for r in conn.execute("SELECT doc_id FROM edinet_fins")} == {"S100FN01"}
    api = fake()
    assert run(tmp_path, conn, api).status == "ok"
    assert sorted(downloaded(api)) == ["S100FN02", "S100FN03", "S100FN04", "S100FN10"]


def test_withdrawn_later_is_dropped(tmp_path, conn):
    run(tmp_path, conn)
    db.upsert_edinet_docs(conn, [{**FN_AMENDED, "withdrawalStatus": "2"}], "2026-07-15")
    run(tmp_path, conn)
    assert fins(conn, "S100FN02") == {}
    latest = conn.execute("SELECT doc_id FROM edinet_fins_latest WHERE period_end = '2026-03-31' "
                          "AND item = 'total_assets' AND code = '72030'").fetchone()[0]
    assert latest == "S100FN01"


def test_skipped_without_key(tmp_path, conn):
    res = Syncer(settings(tmp_path, key=""), conn, jquants_client(conn), log=lambda m: None,
                 today=TODAY).edinet_financials(CODES)
    assert res.status == "skipped" and "EDINET_API_KEY" in res.message and res.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM edinet_fins").fetchone()[0] == 0


def test_run_all_runs_financials_after_holdings(tmp_path):
    conn = db.connect(tmp_path / "e.db")
    api = fake()
    api.docs = {"2026-10-06": [FN_SEMI]}
    results = syncer(tmp_path, conn, api).run_all(("7203",), 0)
    assert [r.job for r in results][-3:] == ["edinet filings", "edinet holdings",
                                              "edinet financials"]
    assert all(r.status == "ok" for r in results)
    assert set(fins(conn, "S100FN03")) >= {"total_assets", "cash"}


def test_status_and_estimate_show_edinet_fins(tmp_path, conn, monkeypatch, capsys):
    monkeypatch.setenv("JQUANTS_DB", str(tmp_path / "e.db"))
    todo = db.edinet_fins_docs_todo(conn, CODES)
    assert [d["doc_id"] for d in todo] == ["S100FN09", "S100FN01", "S100FN04", "S100FN02",
                                           "S100FN03", "S100FN10"]
    run(tmp_path, conn)
    assert db.edinet_fins_docs_todo(conn, CODES) == []
    assert cli.main(["status"]) == 0
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("edinet_fins"))
    assert line.split()[-1] == "17"  # 7 + 1 + 7 + 2 rows
