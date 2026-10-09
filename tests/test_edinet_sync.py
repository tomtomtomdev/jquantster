from datetime import date, timedelta

import httpx
import pytest

from jquantster import cli, db
from jquantster.client import JQuantsClient, RateLimiter
from jquantster.config import PLANS, Settings
from jquantster.edinet import EdinetClient
from jquantster.sync import Syncer
from fake_api import FakeAPI
from fake_edinet import DOC, KEY, FakeEdinet, filing

TODAY = date(2026, 10, 7)  # a Wednesday
HISTORY = 14  # 2026-09-23 .. 2026-10-07: 11 weekdays

COMPANY_OLD = {**DOC, "docID": "S100CO00", "filerName": "トヨタ旧社名",
               "submitDateTime": "2026-09-24 09:00", "docTypeCode": "160"}
COMPANY = {**DOC, "docID": "S100CO01", "submitDateTime": "2026-10-01 09:00"}
HOLDER = filing("S100HD01", "2026-10-02", edinetCode="E11111", filerName="大株主ホールディングス",
                docTypeCode="350", issuerEdinetCode="E02144", subjectEdinetCode="E02144",
                docDescription="大量保有報告書", csvFlag="1")
DOCS = {"2026-09-24": [COMPANY_OLD, filing("S1F2026-09-24", "2026-09-24")],
        "2026-10-01": [COMPANY], "2026-10-02": [HOLDER]}


def settings(tmp_path, key=KEY, plan="light"):
    return Settings("k", PLANS[plan], ("7203",), tmp_path / "e.db",
                    edinet_api_key=key, edinet_history_days=HISTORY)


def edinet_client(conn, api, key=KEY):
    return EdinetClient(key, RateLimiter(conn, sleep=lambda s: None),
                        transport=httpx.MockTransport(api))


def jquants_client(conn, entitled=("calendar", "master", "bars", "fins", "investor_types")):
    return JQuantsClient("k", RateLimiter(conn, sleep=lambda s: None), 10_000, 10_000,
                         transport=httpx.MockTransport(FakeAPI(set(entitled))))


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "e.db")


def syncer(tmp_path, conn, api=None, today=TODAY, key=KEY):
    edinet = edinet_client(conn, api, key) if api is not None else None
    return Syncer(settings(tmp_path, key), conn, jquants_client(conn), log=lambda m: None,
                  today=today, edinet=edinet)


def weekdays(start, end):
    n = (end - start).days + 1
    return [(start + timedelta(i)).isoformat() for i in range(n) if (start + timedelta(i)).weekday() < 5]


def test_first_run_covers_history_days_of_weekdays(tmp_path, conn):
    api = FakeEdinet(DOCS)
    res = syncer(tmp_path, conn, api).edinet_filings()
    expected = weekdays(TODAY - timedelta(HISTORY), TODAY)
    assert len(expected) == 11
    assert res.status == "ok" and res.calls == 11
    assert api.dates == expected
    # one row per filing: 2 on 09-24, 1 filler on each of the other 10 days except 10-01/10-02
    assert res.rows == conn.execute("SELECT COUNT(*) FROM edinet_docs").fetchone()[0] == 2 + 8 + 2
    row = conn.execute("SELECT * FROM edinet_docs WHERE doc_id = 'S100HD01'").fetchone()
    assert row["submit_date"] == "2026-10-02"
    assert row["doc_type_code"] == "350" and row["issuer_edinet_code"] == "E02144"
    assert row["csv_flag"] == "1" and row["withdrawal_status"] == "0"


def test_second_run_rereads_only_last_three_days(tmp_path, conn):
    syncer(tmp_path, conn, FakeEdinet(DOCS)).edinet_filings()
    api = FakeEdinet(DOCS)
    res = syncer(tmp_path, conn, api).edinet_filings()
    assert api.dates == ["2026-10-05", "2026-10-06", "2026-10-07"]
    assert res.calls == 3
    # two days later: from (newest stored - 3 days) through the real today
    api = FakeEdinet(DOCS)
    syncer(tmp_path, conn, api, today=TODAY + timedelta(2)).edinet_filings()
    assert api.dates == weekdays(date(2026, 10, 5), date(2026, 10, 9))


def test_uses_calendar_when_it_covers_the_range(tmp_path, conn):
    start = TODAY - timedelta(HISTORY)
    db.upsert_calendar(conn, [
        {"Date": (start + timedelta(i)).isoformat(),
         "HolDiv": "0" if (start + timedelta(i)).weekday() >= 5 or i == 0 else "1"}
        for i in range(HISTORY + 1)])  # 2026-09-23 is a holiday (Autumnal Equinox Day)
    api = FakeEdinet(DOCS)
    res = syncer(tmp_path, conn, api).edinet_filings()
    assert res.calls == 10 and "2026-09-23" not in api.dates


def test_skipped_without_key(tmp_path, conn):
    res = Syncer(settings(tmp_path, key=""), conn, jquants_client(conn), log=lambda m: None,
                 today=TODAY).edinet_filings()
    assert res.status == "skipped" and "EDINET_API_KEY" in res.message and res.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM edinet_docs").fetchone()[0] == 0
    ent = conn.execute("SELECT status FROM entitlements WHERE dataset = 'edinet'").fetchone()
    assert ent[0] != "not_in_plan"


def test_sec_code_mapping(tmp_path, conn):
    syncer(tmp_path, conn, FakeEdinet(DOCS)).edinet_filings()
    rows = [tuple(r) for r in conn.execute("SELECT edinet_code, sec_code, filer_name FROM edinet_codes")]
    assert rows == [("E02144", "72030", "トヨタ自動車株式会社")]  # latest name wins
    assert db.edinet_codes_for(conn, ["7203", "6758"]) == {"72030": "E02144"}
    assert db.edinet_codes_for(conn, ["72030"]) == {"72030": "E02144"}
    # a holder's report about the company is found through the issuer code
    held = conn.execute(
        "SELECT d.doc_id FROM edinet_docs d JOIN edinet_codes c ON c.edinet_code = d.issuer_edinet_code "
        "WHERE c.sec_code = ?", (db.code5("7203"),)).fetchall()
    assert [r[0] for r in held] == ["S100HD01"]


def test_run_all_includes_edinet_after_jquants(tmp_path, conn):
    results = syncer(tmp_path, conn, FakeEdinet(DOCS)).run_all(("7203",), 0)
    assert [r.job for r in results if r.job.startswith("edinet")] == ["edinet filings", "edinet holdings",
                                              "edinet financials"]
    assert all(r.status == "ok" for r in results if not r.job.startswith("macro"))


def test_edinet_auth_error_does_not_abort_run_all(tmp_path, conn):
    results = syncer(tmp_path, conn, FakeEdinet(DOCS), key="wrong").run_all(("7203",), 0)
    res = {r.job: r for r in results}
    assert res["edinet filings"].status == "error"
    assert "EDINET" in res["edinet filings"].message
    assert all(r.status == "ok" for j, r in res.items()
               if j != "edinet filings" and not j.startswith("macro"))
    assert db.get_meta(conn, "last_sync")


def test_status_shows_edinet_docs(tmp_path, conn, monkeypatch, capsys):
    syncer(tmp_path, conn, FakeEdinet(DOCS)).edinet_filings()
    monkeypatch.setenv("JQUANTS_DB", str(tmp_path / "e.db"))
    assert cli.main(["status"]) == 0
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("edinet_docs"))
    assert line.split()[-1] == "12"
