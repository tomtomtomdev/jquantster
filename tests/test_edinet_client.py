from datetime import date

import httpx
import pytest

from jquantster import db
from jquantster.client import AuthError, JQuantsError, RateLimiter
from jquantster.config import EDINET_RPM
from jquantster.edinet import EdinetClient
from fake_edinet import KEY, FakeEdinet
from test_limiter_client import Clock


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def make_client(conn, api, clock, key=KEY):
    limiter = RateLimiter(conn, clock=clock, sleep=clock.sleep)
    return EdinetClient(key, limiter, transport=httpx.MockTransport(api))


def test_list_documents_sends_key_as_query_param(conn):
    api = FakeEdinet()
    docs = make_client(conn, api, Clock()).list_documents(date(2026, 6, 20))
    assert [d["docID"] for d in docs] == ["S100ABCD"]
    p = api.calls[0].url.params
    assert p["Subscription-Key"] == KEY and p["date"] == "2026-06-20" and p["type"] == "2"
    assert "Subscription-Key" not in api.calls[0].headers


def test_missing_key_fails_fast(conn):
    with pytest.raises(AuthError):
        EdinetClient("", RateLimiter(conn))


def test_bad_key_raises_auth_error(conn):
    with pytest.raises(AuthError):
        make_client(conn, FakeEdinet(), Clock(), key="wrong").list_documents(date(2026, 6, 20))


def test_403_raises_auth_error(conn):
    api = lambda req: httpx.Response(403, json={"message": "Forbidden"})
    with pytest.raises(AuthError):
        make_client(conn, api, Clock()).list_documents(date(2026, 6, 20))


def test_metadata_status_not_200_is_an_error(conn):
    api = FakeEdinet()
    api.bad_status = "400"
    with pytest.raises(JQuantsError, match="400"):
        make_client(conn, api, Clock()).list_documents(date(2026, 6, 20))


def test_5xx_is_retried(conn):
    api, clock = FakeEdinet(), Clock()
    api.fail_5xx = 2
    docs = make_client(conn, api, clock).list_documents(date(2026, 6, 20))
    assert len(docs) == 1 and len(api.calls) == 3 and len(clock.slept) == 2


def test_uses_edinet_bucket_not_plan_bucket(conn):
    clock = Clock()
    client = make_client(conn, FakeEdinet(), clock)
    for _ in range(EDINET_RPM):
        client.list_documents(date(2026, 6, 20))
    assert clock.slept == []
    buckets = {b for (b,) in conn.execute("SELECT bucket FROM api_calls")}
    assert buckets == {"edinet"}
    client.list_documents(date(2026, 6, 20))
    assert clock.slept and clock.slept[0] >= 59
    # J-Quants calls aren't held up by EDINET traffic
    before = len(clock.slept)
    RateLimiter(conn, clock=clock, sleep=clock.sleep).acquire([("plan", 4)])
    assert len(clock.slept) == before


def test_download_csv_parses_utf16_tsv(conn):
    api = FakeEdinet()
    files = make_client(conn, api, Clock()).download_csv("S100ABCD")
    assert api.calls[0].url.params["type"] == "5"
    name = "jpcrp030000-asr-001_E02144-000_2026-03-31_01_2026-06-20.csv"
    assert set(files) == {name, "jpaud-aar-cn-001_E02144-000_2026-03-31_01_2026-06-20.csv"}
    rows = files[name]
    assert len(rows) == 2
    assert rows[0]["要素ID"] == "jppfs_cor:Assets"
    assert rows[0]["値"] == "90000000000000"
    assert rows[1]["連結・個別"] == "連結"


def test_download_of_unknown_doc_is_an_error(conn):
    with pytest.raises(JQuantsError, match="404"):
        make_client(conn, FakeEdinet(), Clock()).download_csv("S100NOPE")
