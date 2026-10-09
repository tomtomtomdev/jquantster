from datetime import date

import httpx
import pytest

from jquantster import db
from jquantster.client import RateLimiter
from jquantster.config import JGB_CURRENT_URL, JGB_HISTORY_URL, MACRO_RPM
from jquantster.macro import BUCKET, MacroClient, MacroError, boj_start
from fake_macro import FakeMacro
from test_limiter_client import Clock


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def make_client(conn, api, clock):
    limiter = RateLimiter(conn, clock=clock, sleep=clock.sleep)
    return MacroClient(limiter, transport=httpx.MockTransport(api))


def test_jgb_csv_parses_rows_and_stops_at_footer(conn):
    rows = make_client(conn, FakeMacro(), Clock()).jgb_csv(JGB_CURRENT_URL)
    assert [d for d, _ in rows] == [date(2026, 10, 1), date(2026, 10, 2)]
    first = rows[0][1]
    assert list(first)[:2] == ["1Y", "2Y"] and list(first)[-1] == "40Y"
    assert first["10Y"] == 3.092 and first["40Y"] == 4.125


def test_jgb_csv_dash_is_missing(conn):
    rows = make_client(conn, FakeMacro(), Clock()).jgb_csv(JGB_HISTORY_URL)
    d, yields = rows[0]
    assert d == date(1974, 9, 24)
    assert yields["9Y"] == 8.127 and yields["10Y"] is None and yields["40Y"] is None
    assert rows[-1][0] == date(2026, 9, 30)


def test_jgb_csv_wrong_header_is_an_error(conn):
    api = FakeMacro()
    api.files[JGB_CURRENT_URL] = b"<html>maintenance</html>"
    with pytest.raises(MacroError, match="header"):
        make_client(conn, api, Clock()).jgb_csv(JGB_CURRENT_URL)


def test_boj_series_several_codes_in_one_call(conn):
    api = FakeMacro()
    got = make_client(conn, api, Clock()).boj_series(
        "CO", ["TK99F1000601GCQ01000", "TK99F2000601GCQ11000"], date(2025, 10, 1), "QUARTERLY")
    assert len(api.calls) == 1
    p = api.calls[0].url.params
    assert p["db"] == "CO" and p["code"] == "TK99F1000601GCQ01000,TK99F2000601GCQ11000"
    assert p["format"] == "json" and p["lang"] == "en" and p["startDate"] == "202504"
    meta, obs = got["TK99F1000601GCQ01000"]
    assert meta["frequency"] == "QUARTERLY" and meta["unit"] == "% points"
    assert meta["last_update"] == "2026-10-02"
    # YYYYQQ survey dates become quarter ends
    assert obs == [(date(2025, 12, 31), 15.0), (date(2026, 3, 31), 17.0),
                   (date(2026, 6, 30), 22.0), (date(2026, 9, 30), 24.0)]
    assert got["TK99F2000601GCQ11000"][1][-1] == (date(2026, 12, 31), 30.0)


def test_boj_daily_drops_nulls(conn):
    api = FakeMacro()
    got = make_client(conn, api, Clock()).boj_series("FM08", ["FXERD01"], date(2026, 10, 1))
    assert api.calls[0].url.params["startDate"] == "202610"
    assert got["FXERD01"][1] == [(date(2026, 10, 1), 157.56), (date(2026, 10, 2), 157.94),
                                 (date(2026, 10, 5), 157.74)]


def test_boj_follows_next_position(conn):
    api = FakeMacro()
    api.page_size = 2
    got = make_client(conn, api, Clock()).boj_series("FM08", ["FXERD01"], date(2026, 10, 1))
    assert [c.url.params.get("startPosition") for c in api.calls] == [None, "2", "4"]
    assert len(got["FXERD01"][1]) == 3


def test_boj_error_status_raises_with_message(conn):
    with pytest.raises(MacroError, match="Nonexistent series code"):
        make_client(conn, FakeMacro(), Clock()).boj_series("FM01", ["NOPE"], date(2026, 9, 1))


def test_5xx_is_retried_and_rate_limited(conn):
    api, clock = FakeMacro(), Clock()
    api.fail_5xx = 1
    client = make_client(conn, api, clock)
    client.jgb_csv(JGB_CURRENT_URL)
    assert len(api.calls) == 2 and client.calls == 2 and len(clock.slept) == 1
    buckets = {b for (b,) in conn.execute("SELECT bucket FROM api_calls")}
    assert buckets == {BUCKET} and MACRO_RPM <= 30


def test_404_is_an_error(conn):
    with pytest.raises(MacroError, match="404"):
        make_client(conn, FakeMacro(), Clock()).jgb_csv("https://www.mof.go.jp/nope.csv")


def test_boj_start_formats():
    assert boj_start(date(2026, 9, 15), "DAILY") == "202609"
    assert boj_start(date(2026, 9, 15), "MONTHLY") == "202609"
    assert boj_start(date(2026, 9, 15), "QUARTERLY") == "202603"
    assert boj_start(date(2026, 1, 1), "QUARTERLY") == "202601"
