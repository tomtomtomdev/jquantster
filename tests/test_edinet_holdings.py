from datetime import date

import pytest

from jquantster import cli, db
from jquantster.edinet import parse_holdings
from jquantster.sync import Syncer
from fake_edinet import (DOC, HEADER, HOLDINGS_350, HOLDINGS_360, HOLDINGS_UNKNOWN, KEY,
                         FakeEdinet, filing, lvh)
from test_edinet_sync import jquants_client, settings, syncer

TODAY = date(2026, 10, 7)
CODES = ("7203",)


def holding(doc_id, day, **fields):
    return filing(doc_id, day, **{"edinetCode": "E11111", "filerName": "大株主ホールディングス",
                                  "docTypeCode": "350", "issuerEdinetCode": "E02144",
                                  "subjectEdinetCode": "E02144", "csvFlag": "1", **fields})


TOYOTA = {**DOC, "docID": "S100CO01", "submitDateTime": "2026-10-01 09:00"}
OTHER_CO = filing("S100CO02", "2026-10-01", edinetCode="E99999", secCode="99990",
                  filerName="他社株式会社", docTypeCode="120", csvFlag="1")
HD_350 = holding("S100HD01", "2026-10-02")
HD_360 = holding("S100HD02", "2026-10-05", docTypeCode="360", docDescription="変更報告書")
HD_WITHDRAWN = holding("S100HD03", "2026-10-05", withdrawalStatus="2")
HD_WITHDRAWAL_NOTICE = holding("S100HD06", "2026-10-06", withdrawalStatus="1")
HD_NO_CSV = holding("S100HD07", "2026-10-06", csvFlag="0")
HD_OTHER_ISSUER = holding("S100HD08", "2026-10-06", issuerEdinetCode="E99999")
HD_UNKNOWN_ISSUER = holding("S100HD09", "2026-10-06", issuerEdinetCode="E00000")
HD_UNPARSEABLE = holding("S100HD04", "2026-10-06")
HD_MISSING = holding("S100HD05", "2026-10-06")

ALL = [TOYOTA, OTHER_CO, HD_350, HD_360, HD_WITHDRAWN, HD_WITHDRAWAL_NOTICE, HD_NO_CSV,
       HD_OTHER_ISSUER, HD_UNKNOWN_ISSUER, HD_UNPARSEABLE, HD_MISSING]


def fake():
    api = FakeEdinet({})
    api.csvs.update({"S100HD01": HOLDINGS_350, "S100HD02": HOLDINGS_360,
                     "S100HD03": HOLDINGS_350, "S100HD06": HOLDINGS_350,
                     "S100HD08": HOLDINGS_350, "S100HD09": HOLDINGS_350,
                     "S100HD04": HOLDINGS_UNKNOWN})  # S100HD05 is a 404
    return api


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "e.db")
    db.upsert_edinet_docs(c, ALL, "2026-10-06")
    return c


def downloaded(api):
    return [r.url.path.rsplit("/", 1)[-1] for r in api.calls if "/documents/" in r.url.path]


def holdings(conn):
    return {r["doc_id"]: dict(r) for r in conn.execute("SELECT * FROM edinet_holdings")}


def test_ratios_extracted_and_normalised_to_percent(tmp_path, conn):
    res = syncer(tmp_path, conn, fake()).edinet_holdings(CODES)
    assert res.status == "ok" and res.rows == 2
    h = holdings(conn)
    assert set(h) == {"S100HD01", "S100HD02"}
    # joint holders: the member-less (total) context wins over each holder's figures
    assert h["S100HD01"] == {
        "doc_id": "S100HD01", "code": "72030", "holder": "大株主ホールディングス",
        "submit_date": "2026-10-02", "holding_ratio": pytest.approx(5.12),
        "prev_holding_ratio": None, "shares_held": 512_000_000.0,
        "obligation_date": "2026-09-28", "doc_type_code": "350"}
    # quoted element IDs and values, thousands separators
    assert h["S100HD02"]["holding_ratio"] == pytest.approx(6.05)
    assert h["S100HD02"]["prev_holding_ratio"] == pytest.approx(5.12)
    assert h["S100HD02"]["shares_held"] == 605_000_000.0
    assert h["S100HD02"]["obligation_date"] == "2026-09-30"
    assert h["S100HD02"]["doc_type_code"] == "360"


def test_ratio_given_as_percent_is_kept():
    rows = [dict(zip(HEADER, r)) for r in [
        lvh("jplvh_cor:HoldingRatioOfShareCertificatesEtc", "", "FilingDateInstant", "6.05", "pure"),
        lvh("jplvh_cor:HoldingRatioOfShareCertificatesEtcPerLastReport", "", "FilingDateInstant",
            "4.98%"),
    ]]
    got = parse_holdings({"x.csv": rows})
    assert got["holding_ratio"] == pytest.approx(6.05)
    assert got["prev_holding_ratio"] == pytest.approx(4.98)
    rows = [dict(zip(HEADER, lvh("jplvh_cor:HoldingRatioOfShareCertificatesEtc", "",
                                 "FilingDateInstant", "0.85", "Percent", "％")))]
    assert parse_holdings({"x.csv": rows})["holding_ratio"] == pytest.approx(0.85)


def test_no_member_context_falls_back_to_total_then_first():
    def ratio_rows(*ctx_values):
        return {"x.csv": [dict(zip(HEADER, lvh("jplvh_cor:HoldingRatioOfShareCertificatesEtc",
                                               "", c, v, "pure"))) for c, v in ctx_values]}
    got = parse_holdings(ratio_rows(("FilingDateInstant_JointHolder1Member", "0.02"),
                                    ("FilingDateInstant_TotalOfFilerAndJointHoldersMember", "0.07")))
    assert got["holding_ratio"] == pytest.approx(7.0)
    got = parse_holdings(ratio_rows(("FilingDateInstant_FilerLargeVolumeHolder1Member", "0.04"),
                                    ("FilingDateInstant_JointHolder1Member", "0.02")))
    assert got["holding_ratio"] == pytest.approx(4.0)
    assert parse_holdings({"x.csv": []}) is None


def test_only_watchlist_issuers_and_live_csv_docs_fetched(tmp_path, conn):
    api = fake()
    syncer(tmp_path, conn, api).edinet_holdings(CODES)
    got = downloaded(api)
    assert sorted(got) == ["S100HD01", "S100HD02", "S100HD04", "S100HD05"]
    for doc in ("S100HD03", "S100HD06", "S100HD07", "S100HD08", "S100HD09"):
        assert doc not in got


def test_parsed_docs_not_refetched(tmp_path, conn):
    syncer(tmp_path, conn, fake()).edinet_holdings(CODES)
    api = fake()
    res = syncer(tmp_path, conn, api).edinet_holdings(CODES)
    assert res.status == "ok" and res.calls == 0 and downloaded(api) == []
    assert len(holdings(conn)) == 2


def test_unparseable_and_missing_docs_recorded_and_not_retried(tmp_path, conn):
    res = syncer(tmp_path, conn, fake()).edinet_holdings(CODES)
    assert res.status == "ok"
    state = {r["doc_id"]: r["status"] for r in conn.execute(
        "SELECT doc_id, status FROM edinet_parsed WHERE job = 'holdings'")}
    assert state == {"S100HD01": "ok", "S100HD02": "ok",
                     "S100HD04": "unparseable", "S100HD05": "not_found"}
    assert res.message == "4 new reports, 2 unparseable or missing"
    api = fake()
    syncer(tmp_path, conn, api).edinet_holdings(CODES)
    assert downloaded(api) == []


def test_interrupted_run_resumes_per_doc(tmp_path, conn):
    api = fake()
    api.fail_docs = {"S100HD02"}  # docs come oldest first: HD01 succeeds, HD02 keeps failing
    res = syncer(tmp_path, conn, api).edinet_holdings(CODES)
    assert res.status == "error"
    assert set(holdings(conn)) == {"S100HD01"}
    api = fake()
    res = syncer(tmp_path, conn, api).edinet_holdings(CODES)
    assert res.status == "ok"
    assert sorted(downloaded(api)) == ["S100HD02", "S100HD04", "S100HD05"]
    assert set(holdings(conn)) == {"S100HD01", "S100HD02"}


def test_withdrawn_later_is_dropped(tmp_path, conn):
    syncer(tmp_path, conn, fake()).edinet_holdings(CODES)
    db.upsert_edinet_docs(conn, [{**HD_350, "withdrawalStatus": "2"}], "2026-10-02")
    syncer(tmp_path, conn, fake()).edinet_holdings(CODES)
    assert set(holdings(conn)) == {"S100HD02"}


def test_skipped_without_key(tmp_path, conn):
    res = Syncer(settings(tmp_path, key=""), conn, jquants_client(conn), log=lambda m: None,
                 today=TODAY).edinet_holdings(CODES)
    assert res.status == "skipped" and "EDINET_API_KEY" in res.message and res.calls == 0
    assert holdings(conn) == {}


def test_run_all_runs_holdings_after_filings(tmp_path):
    conn = db.connect(tmp_path / "e.db")
    api = fake()
    api.docs = {"2026-10-01": [TOYOTA], "2026-10-02": [HD_350]}
    results = syncer(tmp_path, conn, api).run_all(CODES, 0)
    jobs = [r.job for r in results]
    assert jobs[-2:] == ["edinet filings", "edinet holdings"]
    assert all(r.status == "ok" for r in results)
    assert set(holdings(conn)) == {"S100HD01"}


def test_status_shows_edinet_holdings(tmp_path, conn, monkeypatch, capsys):
    syncer(tmp_path, conn, fake()).edinet_holdings(CODES)
    monkeypatch.setenv("JQUANTS_DB", str(tmp_path / "e.db"))
    assert cli.main(["status"]) == 0
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("edinet_holdings"))
    assert line.split()[-1] == "2"
