"""Sync jobs: each pulls one dataset into SQLite using as few calls as possible.

Strategy:
  - watchlist stocks: one call per code returns its whole history (`code` + `from`/`to`)
  - market snapshot: one call per trading day returns every stock (`date`)
  - incremental: each job resumes from the newest date already stored
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import db
from .client import AuthError, JQuantsClient, JQuantsError, NotEntitled
from .config import Settings

TSE_OPEN = ("1", "2")  # HolDiv: business day, half-day session


@dataclass
class JobResult:
    job: str
    status: str  # ok | skipped | not_entitled | error
    rows: int = 0
    calls: int = 0
    message: str = ""


class Syncer:
    def __init__(self, settings: Settings, conn, client: JQuantsClient,
                 log: Callable[[str], None] = print, today: date | None = None):
        self.s = settings
        self.conn = conn
        self.client = client
        self.log = log
        self.start, self.end = settings.plan.window(today)

    # -- helpers ---------------------------------------------------------
    def _run(self, job: str, dataset: str, fn: Callable[[], tuple[int, str]]) -> JobResult:
        if dataset not in self.s.plan.datasets:
            msg = f"not in the {self.s.plan.name} plan"
            self.log(f"· {job}: skipped ({msg})")
            db.set_entitlement(self.conn, dataset, "not_in_plan", msg)
            return JobResult(job, "skipped", message=msg)
        calls_before = self.client.calls
        log_id = self.conn.execute(
            "INSERT INTO sync_log (job, started_at) VALUES (?, datetime('now'))", (job,)
        ).lastrowid
        try:
            rows, note = fn()
            result = JobResult(job, "ok", rows, self.client.calls - calls_before, note)
            db.set_entitlement(self.conn, dataset, "ok")
        except NotEntitled as e:
            result = JobResult(job, "not_entitled", 0, self.client.calls - calls_before, str(e))
            db.set_entitlement(self.conn, dataset, "not_entitled", str(e))
        except AuthError:
            raise
        except JQuantsError as e:
            result = JobResult(job, "error", 0, self.client.calls - calls_before, str(e))
        self.conn.execute(
            "UPDATE sync_log SET finished_at = datetime('now'), status = ?, rows = ?, "
            "calls = ?, message = ? WHERE id = ?",
            (result.status, result.rows, result.calls, result.message, log_id),
        )
        mark = "✓" if result.status == "ok" else "✗"
        self.log(f"{mark} {job}: {result.rows} rows, {result.calls} calls"
                 + (f" ({result.message})" if result.message else ""))
        return result

    def trading_days(self, start: date, end: date) -> list[date]:
        rows = self.conn.execute(
            "SELECT date, hol_div FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        if rows:
            return [date.fromisoformat(r["date"]) for r in rows if r["hol_div"] in TSE_OPEN]
        # No calendar yet: weekdays (holidays just come back empty).
        n = (end - start).days + 1
        return [d for d in (start + timedelta(i) for i in range(n)) if d.weekday() < 5]

    # -- jobs ------------------------------------------------------------
    def calendar(self) -> JobResult:
        def fn():
            rows = self.client.get_all("/markets/calendar", **{
                "from": self.start.isoformat(),
                "to": (self.end + timedelta(days=30)).isoformat(),
            })
            return db.upsert_calendar(self.conn, rows), ""
        return self._run("calendar", "calendar", fn)

    def master(self) -> JobResult:
        def fn():
            # On delayed plans the newest readable master is at the end of the window.
            as_of = None if self.s.plan.delay_days == 0 else self.end.isoformat()
            rows = self.client.get_all("/equities/master", date=as_of)
            return db.upsert_issues(self.conn, rows), f"as of {as_of or 'today'}"
        return self._run("master", "master", fn)

    def watchlist_bars(self, codes: tuple[str, ...]) -> JobResult:
        def fn():
            total = 0
            for code in codes:
                last = self.conn.execute(
                    "SELECT MAX(date) FROM daily_bars WHERE code = ?", (_code5(code),)
                ).fetchone()[0]
                start = date.fromisoformat(last) + timedelta(days=1) if last else self.start
                if start > self.end:
                    continue
                rows = self.client.get_all("/equities/bars/daily", code=code, **{
                    "from": start.isoformat(), "to": self.end.isoformat(),
                })
                total += db.upsert_bars(self.conn, rows)
            return total, f"{len(codes)} codes"
        return self._run("watchlist bars", "bars", fn)

    def market_bars(self, days: int) -> JobResult:
        """All stocks for the latest `days` trading days the plan can read."""
        def fn():
            have = {
                r[0] for r in self.conn.execute(
                    "SELECT date FROM daily_bars GROUP BY date HAVING COUNT(*) > 1000"
                )
            }
            lookback = self.end - timedelta(days=days * 2 + 10)
            targets = [d for d in self.trading_days(lookback, self.end)][-days:]
            todo = [d for d in targets if d.isoformat() not in have]
            total = 0
            for d in todo:
                total += db.upsert_bars(
                    self.conn, self.client.get_all("/equities/bars/daily", date=d.isoformat())
                )
            return total, f"{len(todo)} new days" if todo else "up to date"
        return self._run("market bars", "bars", fn)

    def watchlist_fins(self, codes: tuple[str, ...]) -> JobResult:
        def fn():
            total = 0
            for code in codes:
                total += db.upsert_fins(
                    self.conn, self.client.get_all("/fins/summary", code=code)
                )
            return total, f"{len(codes)} codes"
        return self._run("watchlist financials", "fins_summary", fn)

    def investor_types(self) -> JobResult:
        def fn():
            last = self.conn.execute("SELECT MAX(st_date) FROM investor_types").fetchone()[0]
            # Re-read 3 weeks back so corrected weeks (new PubDate) are picked up.
            start = max(self.start, date.fromisoformat(last) - timedelta(days=21)) if last else self.start
            rows = self.client.get_all("/equities/investor-types", **{
                "from": start.isoformat(), "to": self.end.isoformat(),
            })
            return db.upsert_investor_types(self.conn, rows), f"from {start}"
        return self._run("investor types", "investor_types", fn)

    def run_all(self, codes: tuple[str, ...], market_days: int) -> list[JobResult]:
        db.set_meta(self.conn, "plan", self.s.plan.name)
        db.set_meta(self.conn, "window", f"{self.start}..{self.end}")
        results = [
            self.calendar(),
            self.master(),
            self.watchlist_bars(codes),
            self.watchlist_fins(codes),
            self.market_bars(market_days) if market_days > 0 else None,
            self.investor_types(),
        ]
        db.set_meta(self.conn, "last_sync", datetime.now().isoformat(timespec="seconds"))
        return [r for r in results if r]


def _code5(code: str) -> str:
    """The API returns 5-digit codes; 4-digit input means the common stock (suffix 0)."""
    return code + "0" if len(code) == 4 else code
