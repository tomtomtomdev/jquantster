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
    assert all(r.status == "ok" for j, r in res.items() if j != "investor types")
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
