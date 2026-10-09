"""Macro data: MOF JGB yield CSVs and the BOJ time-series API, rate limited through the
shared RateLimiter. Neither needs a key."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from .client import JQuantsError, RateLimiter
from .config import BOJ_BASE_URL, MACRO_RPM

BUCKET = "macro"


class MacroError(JQuantsError):
    """MOF or BOJ answered with an error or something we can't read."""


def boj_start(d: date, frequency: str) -> str:
    """BOJ `startDate`: YYYYMM for daily and monthly series, YYYYQQ for quarterly ones."""
    if frequency == "QUARTERLY":
        return f"{d.year}{(d.month - 1) // 3 + 1:02d}"
    return f"{d.year}{d.month:02d}"


def _period_end(year: int, month: int) -> date:
    nxt = date(year + month // 12, month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def _survey_date(raw: int | str, frequency: str) -> date:
    s = str(raw)
    if frequency == "DAILY":
        return datetime.strptime(s, "%Y%m%d").date()
    year, n = int(s[:4]), int(s[4:])
    if frequency == "QUARTERLY":
        return _period_end(year, n * 3)
    if frequency == "MONTHLY":
        return _period_end(year, n)
    raise MacroError(f"BOJ frequency {frequency} is not supported")


def _bojdate(raw) -> str | None:
    return datetime.strptime(str(raw), "%Y%m%d").date().isoformat() if raw else None


class MacroClient:
    def __init__(self, limiter: RateLimiter, transport: httpx.BaseTransport | None = None,
                 max_retries: int = 4):
        self.limiter = limiter
        self.max_retries = max_retries
        self.calls = 0
        self.http = httpx.Client(timeout=httpx.Timeout(60.0), transport=transport,
                                 follow_redirects=True)

    def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire([(BUCKET, MACRO_RPM)])
            self.calls += 1
            try:
                resp = self.http.get(url, params=params)
            except httpx.TransportError:
                if attempt == self.max_retries:
                    raise
                self.limiter.sleep(5 * 3**attempt)
                continue
            if (resp.status_code >= 500 or resp.status_code == 429) and attempt < self.max_retries:
                self.limiter.sleep(5 * 3**attempt)
                continue
            return resp
        raise MacroError(f"Gave up on {url} after {self.max_retries} retries")

    def jgb_csv(self, url: str) -> list[tuple[date, dict[str, float | None]]]:
        """Rows of an MOF JGB yield CSV as (date, {tenor: percent or None})."""
        resp = self._get(url)
        if resp.status_code != 200:
            raise MacroError(f"MOF {resp.status_code} on {url}")
        # The footer note is Shift_JIS; the data rows are ASCII.
        lines = list(csv.reader(io.StringIO(resp.content.decode("cp932", errors="replace"))))
        if len(lines) < 2 or lines[1][:2] != ["Date", "1Y"]:
            raise MacroError(f"MOF CSV at {url} has an unexpected header")
        tenors = [t.strip() for t in lines[1][1:] if t.strip()]
        rows = []
        for line in lines[2:]:
            try:
                d = datetime.strptime(line[0].strip(), "%Y/%m/%d").date()
            except (ValueError, IndexError):
                break  # blank row, then the note
            cells = [c.strip() for c in line[1:len(tenors) + 1]]
            rows.append((d, {t: float(c) if c not in ("", "-") else None
                             for t, c in zip(tenors, cells)}))
        return rows

    def boj_series(self, db: str, codes: list[str], start: date, frequency: str = "DAILY",
                   ) -> dict[str, tuple[dict, list[tuple[date, float]]]]:
        """Observations of BOJ series (one database, one `frequency`) since `start`, as
        code -> (metadata, [(date, value)]). Holidays (null values) are dropped."""
        out: dict[str, tuple[dict, list]] = {}
        params: dict[str, Any] = {"format": "json", "lang": "en", "db": db,
                                  "code": ",".join(codes),
                                  "startDate": boj_start(start, frequency)}
        while True:
            body = self._boj("getDataCode", params)
            for r in body.get("RESULTSET") or []:
                freq = r.get("FREQUENCY", "")
                code = r["SERIES_CODE"]
                meta = {"name": r.get("NAME_OF_TIME_SERIES", ""), "unit": r.get("UNIT", ""),
                        "frequency": freq, "last_update": _bojdate(r.get("LAST_UPDATE"))}
                vals = r.get("VALUES") or {}
                obs = [(_survey_date(d, freq), float(v))
                       for d, v in zip(vals.get("SURVEY_DATES", []), vals.get("VALUES", []))
                       if v is not None]
                if code in out:
                    out[code][1].extend(obs)
                else:
                    out[code] = (meta, obs)
            nxt = body.get("NEXTPOSITION")
            if nxt in (None, ""):
                return out
            params = {**params, "startPosition": nxt}

    def _boj(self, api: str, params: dict[str, Any]) -> dict:
        url = f"{BOJ_BASE_URL}/{api}"
        resp = self._get(url, params)
        try:
            body = resp.json()
        except ValueError:
            raise MacroError(f"BOJ {resp.status_code} on {api}: {resp.text[:200]}") from None
        if resp.status_code != 200 or str(body.get("STATUS")) != "200":
            raise MacroError(f"BOJ {body.get('STATUS', resp.status_code)} on {api} "
                             f"{params.get('db')}/{params.get('code')}: {body.get('MESSAGE', '')}")
        return body
