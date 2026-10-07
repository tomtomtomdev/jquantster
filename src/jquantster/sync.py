"""Sync jobs: each pulls one dataset into SQLite using as few calls as possible.

Strategy:
  - watchlist stocks: one call per code returns its whole history (`code` + `from`/`to`)
  - market snapshot: one call per trading day returns every stock (`date`)
  - incremental: each job resumes from the newest date already stored
  - EDINET: one documents.json call per business day lists every filing; filter locally
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

from . import db
from .client import AuthError, JQuantsClient, JQuantsError, NotEntitled
from .config import Settings
from .db import code5 as _code5
from .edinet import EdinetClient

TSE_OPEN = ("1", "2")  # HolDiv: business day, half-day session
EDINET_REREAD_DAYS = 3  # late filings and withdrawals show up on days already read
EDINET_NO_KEY = "set EDINET_API_KEY to enable"


@dataclass
class JobResult:
    job: str
    status: str  # ok | skipped | not_entitled | error
    rows: int = 0
    calls: int = 0
    message: str = ""


class Syncer:
    def __init__(self, settings: Settings, conn, client: JQuantsClient,
                 log: Callable[[str], None] = print, today: date | None = None,
                 edinet: EdinetClient | None = None):
        self.s = settings
        self.conn = conn
        self.client = client
        self.edinet = edinet
        self.log = log
        self.today = today or date.today()  # EDINET has no plan delay
        self.start, self.end = settings.plan.window(today)

    # -- helpers ---------------------------------------------------------
    def _run(self, job: str, dataset: str, fn: Callable[[], tuple[int, str]],
             client: JQuantsClient | EdinetClient | None = None) -> JobResult:
        """`client` defaults to J-Quants (plan-gated; a bad key aborts the run). Other
        sources pass their own client: their auth and network errors only fail the job."""
        external = client is not None
        client = client or self.client
        if not external and dataset not in self.s.plan.datasets:
            msg = f"not in the {self.s.plan.name} plan"
            self.log(f"· {job}: skipped ({msg})")
            db.set_entitlement(self.conn, dataset, "not_in_plan", msg)
            return JobResult(job, "skipped", message=msg)
        calls_before = client.calls
        log_id = self.conn.execute(
            "INSERT INTO sync_log (job, started_at) VALUES (?, datetime('now'))", (job,)
        ).lastrowid
        try:
            rows, note = fn()
            result = JobResult(job, "ok", rows, client.calls - calls_before, note)
            db.set_entitlement(self.conn, dataset, "ok")
        except NotEntitled as e:
            result = JobResult(job, "not_entitled", 0, client.calls - calls_before, str(e))
            db.set_entitlement(self.conn, dataset, "not_entitled", str(e))
        except AuthError as e:
            if not external:
                raise
            result = JobResult(job, "error", 0, client.calls - calls_before, str(e))
            db.set_entitlement(self.conn, dataset, "auth_error", str(e))
        except (JQuantsError, httpx.HTTPError) as e:
            if isinstance(e, httpx.HTTPError) and not external:
                raise
            result = JobResult(job, "error", 0, client.calls - calls_before, str(e))
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

    def business_days(self, start: date, end: date) -> list[date]:
        """TSE business days from `calendar` where it reaches, weekdays beyond it (the Free
        plan's calendar stops at its delayed window end)."""
        open_days = {
            r["date"]: r["hol_div"] in TSE_OPEN for r in self.conn.execute(
                "SELECT date, hol_div FROM calendar WHERE date BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()))
        }
        n = (end - start).days + 1
        days = (start + timedelta(i) for i in range(n))
        return [d for d in days if open_days.get(d.isoformat(), d.weekday() < 5)]

    def edinet_days(self) -> list[date]:
        return self.business_days(edinet_start(self.conn, self.s, self.today), self.today)

    # -- jobs ------------------------------------------------------------
    def calendar(self) -> JobResult:
        def fn():
            rows = self.client.get_all("/markets/calendar", **{
                "from": self.start.isoformat(),
                "to": self.end.isoformat(),  # dates past the plan window are a 400
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
                first, last = self.conn.execute(
                    "SELECT MIN(date), MAX(date) FROM daily_bars WHERE code = ?", (_code5(code),)
                ).fetchone()
                ranges = [(self.start, self.end)]
                if last:
                    first, last = date.fromisoformat(first), date.fromisoformat(last)
                    ranges = [(last + timedelta(days=1), self.end)]
                    if first - self.start > timedelta(days=7):  # window grew backwards
                        ranges.append((self.start, first - timedelta(days=1)))
                for start, end in ranges:
                    if start > end:
                        continue
                    rows = self.client.get_all("/equities/bars/daily", code=code, **{
                        "from": start.isoformat(), "to": end.isoformat(),
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

    def edinet_filings(self) -> JobResult:
        job, dataset = "edinet filings", "edinet"
        if self.edinet is None:
            self.log(f"· {job}: skipped ({EDINET_NO_KEY})")
            db.set_entitlement(self.conn, dataset, "no_key", EDINET_NO_KEY)
            return JobResult(job, "skipped", message=EDINET_NO_KEY)

        def fn():
            days = self.edinet_days()
            total = 0
            for d in days:  # oldest first, so an error leaves a clean resume point
                total += db.upsert_edinet_docs(
                    self.conn, self.edinet.list_documents(d), d.isoformat())
            span = f"{days[0]} → {days[-1]}" if days else "up to date"
            return total, f"{len(days)} days, {span}" if days else span
        return self._run(job, dataset, fn, client=self.edinet)

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
            self.edinet_filings(),
        ]
        db.set_meta(self.conn, "last_sync", datetime.now().isoformat(timespec="seconds"))
        return [r for r in results if r]


def edinet_start(conn, settings: Settings, today: date) -> date:
    """First day to list: the newest stored day minus a few, else the history length back."""
    last = conn.execute("SELECT MAX(submit_date) FROM edinet_docs").fetchone()[0]
    if last:
        return date.fromisoformat(last) - timedelta(days=EDINET_REREAD_DAYS)
    return today - timedelta(days=settings.edinet_history_days)
