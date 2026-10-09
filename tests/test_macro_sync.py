from datetime import date

import httpx
import pytest

from jquantster import cli, db
from jquantster.client import AuthError, JQuantsClient, RateLimiter
from jquantster.config import BOJ_BASE_URL, JGB_CURRENT_URL, JGB_HISTORY_URL, PLANS, Settings
from jquantster.macro import MacroClient
from jquantster.sync import Syncer
from fake_api import FakeAPI
from fake_macro import FakeMacro, jgb_csv

TODAY = date(2026, 10, 9)
START = date(2026, 9, 1)  # keeps the 1974 history row out


def settings(tmp_path, enabled=True):
    return Settings("k", PLANS["light"], ("7203",), tmp_path / "m.db",
                    macro_enabled=enabled, macro_history_start=START)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "m.db")


def jquants_client(conn, api=None):
    api = api or FakeAPI({"calendar", "master", "bars", "fins", "investor_types"})
    return JQuantsClient("k", RateLimiter(conn, sleep=lambda s: None), 10_000, 10_000,
                         transport=httpx.MockTransport(api))


def syncer(tmp_path, conn, api, today=TODAY, enabled=True, jq=None):
    macro = MacroClient(RateLimiter(conn, sleep=lambda s: None),
                        transport=httpx.MockTransport(api)) if api is not None else None
    return Syncer(settings(tmp_path, enabled), conn, jq or jquants_client(conn),
                  log=lambda m: None, today=today, macro=macro)


def urls(api):
    return [str(c.url.copy_with(query=None)) for c in api.calls]


def test_jgb_first_run_reads_history_from_start_and_current_month(tmp_path, conn):
    api = FakeMacro()
    res = syncer(tmp_path, conn, api).macro_jgb()
    assert res.status == "ok" and res.rows == 4 and res.calls == 2
    assert urls(api) == [JGB_HISTORY_URL, JGB_CURRENT_URL]
    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM jgb_yields ORDER BY date")]
    assert dates == ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]
    assert conn.execute("SELECT yield_pct FROM jgb_yields WHERE date = '2026-10-02' "
                        "AND tenor = '10Y'").fetchone()[0] == 3.097
    assert conn.execute("SELECT COUNT(*) FROM jgb_yields").fetchone()[0] == 4 * 15


def test_jgb_missing_tenors_are_not_stored(tmp_path, conn):
    api = FakeMacro()
    s = syncer(tmp_path, conn, api)
    s.s = Settings(**{**s.s.__dict__, "macro_history_start": date(1974, 1, 1)})
    s.macro_jgb()
    tenors = [r[0] for r in conn.execute(
        "SELECT tenor FROM jgb_yields WHERE date = '1974-09-24'")]
    assert "9Y" in tenors and "10Y" not in tenors and len(tenors) == 9


def test_jgb_later_run_same_month_reads_only_current(tmp_path, conn):
    syncer(tmp_path, conn, FakeMacro()).macro_jgb()
    api = FakeMacro()
    api.files[JGB_CURRENT_URL] = jgb_csv("Interest Rate (October 2026)", [
        "2026/10/2,1.65,1.919,2.065,2.259,2.397,2.528,2.657,2.821,2.957,3.097,3.626,3.925,4.171,4.148,4.168",
        "2026/10/8,1.6,1.9,2.0,2.2,2.3,2.5,2.6,2.8,2.9,3.089,3.6,3.9,4.1,4.1,4.1",
    ])
    res = syncer(tmp_path, conn, api).macro_jgb()
    assert urls(api) == [JGB_CURRENT_URL] and res.rows == 2
    assert conn.execute("SELECT MAX(date) FROM jgb_yields").fetchone()[0] == "2026-10-08"


def test_jgb_new_month_rereads_history_until_it_covers_last_month(tmp_path, conn):
    syncer(tmp_path, conn, FakeMacro()).macro_jgb()  # history through 2026-09-30
    api = FakeMacro()  # MOF hasn't added October to the history file yet
    syncer(tmp_path, conn, api, today=date(2026, 11, 4)).macro_jgb()
    assert urls(api) == [JGB_HISTORY_URL, JGB_CURRENT_URL]
    api.files[JGB_HISTORY_URL] = jgb_csv("Interest Rate", [
        "2026/10/30,1.6,1.9,2.0,2.2,2.3,2.5,2.6,2.8,2.9,3.1,3.6,3.9,4.1,4.1,4.1"])
    syncer(tmp_path, conn, api, today=date(2026, 11, 5)).macro_jgb()
    api.calls.clear()
    syncer(tmp_path, conn, api, today=date(2026, 11, 6)).macro_jgb()
    assert urls(api) == [JGB_CURRENT_URL]


def test_boj_first_run_one_call_per_database(tmp_path, conn):
    api = FakeMacro()
    res = syncer(tmp_path, conn, api).macro_boj()
    assert res.status == "ok" and res.calls == 3
    by_db = {c.url.params["db"]: c.url.params for c in api.calls}
    assert set(by_db) == {"FM08", "FM01", "CO"}
    assert by_db["FM08"]["startDate"] == "202609" and by_db["CO"]["startDate"] == "202603"
    assert by_db["CO"]["code"].count(",") == 3
    meta = conn.execute("SELECT * FROM macro_series WHERE key = 'usdjpy'").fetchone()
    assert meta["code"] == "FXERD01" and meta["unit"] == "Yen per U.S. Dollar"
    assert meta["frequency"] == "DAILY" and meta["last_update"] == "2026-10-09"
    obs = conn.execute("SELECT date, value FROM macro_obs WHERE key = 'tankan_lm' "
                       "ORDER BY date").fetchall()
    assert [tuple(r) for r in obs][-1] == ("2026-09-30", 24.0)
    assert res.rows == conn.execute("SELECT COUNT(*) FROM macro_obs").fetchone()[0]


def test_boj_later_run_rereads_last_month(tmp_path, conn):
    syncer(tmp_path, conn, FakeMacro()).macro_boj()
    api = FakeMacro()
    syncer(tmp_path, conn, api).macro_boj()
    by_db = {c.url.params["db"]: c.url.params["startDate"] for c in api.calls}
    # newest stored: usdjpy 2026-10-05, call rate 2026-09-24, Tankan actual 2026-09-30
    assert by_db == {"FM08": "202609", "FM01": "202608", "CO": "202603"}


def test_boj_error_fails_the_job_only(tmp_path, conn):
    api = FakeMacro()
    del api.series[("FM01", "STRDCLUCON")]
    s = syncer(tmp_path, conn, api)
    assert s.macro_boj().status == "error"
    assert s.macro_jgb().status == "ok"


def test_disabled_skips_without_calls(tmp_path, conn):
    s = syncer(tmp_path, conn, None, enabled=False)
    results = [s.macro_jgb(), s.macro_boj()]
    assert [r.status for r in results] == ["skipped", "skipped"]
    assert conn.execute("SELECT status FROM entitlements WHERE dataset = 'macro'"
                        ).fetchone()[0] == "disabled"


def test_run_all_includes_macro_jobs(tmp_path, conn):
    results = syncer(tmp_path, conn, FakeMacro()).run_all(("7203",), 1)
    jobs = {r.job: r.status for r in results}
    assert jobs["macro jgb"] == "ok" and jobs["macro boj"] == "ok"


def test_macro_still_syncs_when_jquants_key_is_bad(tmp_path, conn):
    bad = lambda req: httpx.Response(403, json={"message": "Invalid API key"})
    api = FakeMacro()
    with pytest.raises(AuthError):
        syncer(tmp_path, conn, api, jq=jquants_client(conn, bad)).run_all(("7203",), 1)
    assert conn.execute("SELECT COUNT(*) FROM jgb_yields").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM macro_obs").fetchone()[0] > 0


def test_status_lists_macro_tables(tmp_path, conn, monkeypatch, capsys):
    syncer(tmp_path, conn, FakeMacro()).macro_jgb()
    monkeypatch.setattr(cli, "load_settings", lambda: settings(tmp_path))
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "jgb_yields" in out and "macro_obs" in out
