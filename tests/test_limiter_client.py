import httpx
import pytest

from jquantster import db
from jquantster.client import AuthError, JQuantsClient, NotEntitled, RateLimiter
from fake_api import FakeAPI


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def make_client(conn, api, clock, rpm=4, fins=3):
    limiter = RateLimiter(conn, clock=clock, sleep=clock.sleep)
    return JQuantsClient("k", limiter, rpm, fins, transport=httpx.MockTransport(api))


def test_limiter_never_exceeds_budget_in_any_minute(conn):
    clock = Clock()
    limiter = RateLimiter(conn, clock=clock, sleep=clock.sleep)
    stamps = []
    for _ in range(13):
        limiter.acquire([("plan", 4)])
        stamps.append(clock.t)
        clock.t += 1  # each request takes a second
    for t in stamps:
        assert sum(t <= s < t + 60 for s in stamps) <= 4
    assert stamps[4] - stamps[0] >= 60


def test_limit_is_shared_across_limiter_instances(conn, tmp_path):
    clock = Clock()
    a = RateLimiter(conn, clock=clock, sleep=clock.sleep)
    b = RateLimiter(db.connect(tmp_path / "t.db"), clock=clock, sleep=clock.sleep)
    for _ in range(2):
        a.acquire([("plan", 4)])
        b.acquire([("plan", 4)])
    assert clock.slept == []
    a.acquire([("plan", 4)])
    assert clock.slept and clock.slept[0] >= 59


def test_fins_endpoints_use_their_own_lower_bucket(conn):
    clock, api = Clock(), FakeAPI()
    client = make_client(conn, api, clock, rpm=10, fins=2)
    for _ in range(3):
        client.get_all("/fins/summary", code="7203")
    assert clock.slept  # third fins call waited even though the plan bucket had room


def test_pagination_is_followed(conn):
    clock, api = Clock(), FakeAPI()
    rows = make_client(conn, api, clock).get_all(
        "/equities/bars/daily", code="7203", **{"from": "2026-01-05", "to": "2026-01-16"})
    assert len(rows) == 10 and len(api.calls) == 2
    assert api.calls[1].url.params["pagination_key"] == "next"


def test_429_sets_cooldown_and_retries(conn):
    clock, api = Clock(), FakeAPI()
    api.fail_429_once = True
    rows = make_client(conn, api, clock).get_all("/equities/master")
    assert len(rows) == 1200
    assert any(s >= 119 for s in clock.slept)


def test_403_plan_and_bad_key(conn):
    clock = Clock()
    with pytest.raises(NotEntitled):
        make_client(conn, FakeAPI(), clock).get_all("/equities/investor-types")
    limiter = RateLimiter(conn, clock=clock, sleep=clock.sleep)
    bad = JQuantsClient("wrong", limiter, 4, 3, transport=httpx.MockTransport(FakeAPI()))
    with pytest.raises(AuthError):
        bad.get_all("/equities/master")


def test_missing_key_fails_fast(conn):
    with pytest.raises(AuthError):
        JQuantsClient("", RateLimiter(conn), 4, 3)
