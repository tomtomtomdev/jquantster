"""Settings and per-plan limits for the J-Quants V2 API.

Plan facts come from https://jpx-jquants.com/en/spec/rate-limits and
https://jpx-jquants.com/en/spec/data-spec (checked 2026-10-07).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_URL = "https://api.jquants.com/v2"

# Run below the published limit: the server counts over a sliding minute we can't
# see exactly, and repeated 429s get the whole account blocked for ~5 minutes.
SAFETY = 0.8
FINS_RPM = 60  # /fins/summary and /fins/details have their own cap on every plan
COOLDOWN_AFTER_429 = 120  # seconds; the API sends no Retry-After

EDINET_BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"
EDINET_RPM = 50  # unpublished limit; stay under ~1 request per second

# MOF JGB yields (English CSVs) and the BOJ Time-Series Data Search API: no key, no published
# limit, checked 2026-10-09.
JGB_CURRENT_URL = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv"
JGB_HISTORY_URL = ("https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
                   "historical/jgbcme_all.csv")
BOJ_BASE_URL = "https://www.stat-search.boj.or.jp/api/v1"
MACRO_RPM = 20

# BOJ series to sync: key -> (database, series code, frequency). Codes from
# getMetadata?format=json&lang=en&db=<database>; adding a line is all it takes.
BOJ_SERIES = {
    "usdjpy": ("FM08", "FXERD01", "DAILY"),  # USD/JPY spot at 9:00 JST, Tokyo
    "call_rate": ("FM01", "STRDCLUCON", "DAILY"),  # uncollateralized overnight call rate, avg
    "tankan_lm": ("CO", "TK99F1000601GCQ01000", "QUARTERLY"),  # Tankan DI, large mfg, actual
    "tankan_ln": ("CO", "TK99F2000601GCQ01000", "QUARTERLY"),  # large non-mfg, actual
    "tankan_lm_fc": ("CO", "TK99F1000601GCQ11000", "QUARTERLY"),  # large mfg, forecast
    "tankan_ln_fc": ("CO", "TK99F2000601GCQ11000", "QUARTERLY"),  # large non-mfg, forecast
}


@dataclass(frozen=True)
class Plan:
    name: str
    rpm: int
    history_years: int
    delay_days: int
    datasets: frozenset[str] = field(default_factory=frozenset)

    @property
    def budget_rpm(self) -> int:
        # Free (5/min) still drew a 429 at 4/min in practice, so small plans keep 2 spare.
        return max(1, min(int(self.rpm * SAFETY), self.rpm - 2))

    @property
    def fins_budget_rpm(self) -> int:
        return min(self.budget_rpm, int(FINS_RPM * SAFETY))

    def window(self, today: date | None = None) -> tuple[date, date]:
        """First and last date this plan can read."""
        # The API counts history back from the (delayed) end, e.g. Free on 2026-10-07
        # covers 2024-07-15 ~ 2026-07-15.
        today = today or date.today()
        end = today - timedelta(days=self.delay_days)
        try:
            start = end.replace(year=end.year - self.history_years)
        except ValueError:  # 29 February
            start = end.replace(year=end.year - self.history_years, day=28)
        return start, end


_FREE = {"calendar", "master", "bars", "fins_summary"}
_LIGHT = _FREE | {"investor_types"}

PLANS = {
    "free": Plan("free", 5, 2, 12 * 7, frozenset(_FREE)),
    "light": Plan("light", 60, 5, 0, frozenset(_LIGHT)),
    "standard": Plan("standard", 120, 10, 0, frozenset(_LIGHT)),
    "premium": Plan("premium", 500, 20, 0, frozenset(_LIGHT)),
}


@dataclass(frozen=True)
class Settings:
    api_key: str
    plan: Plan
    watchlist: tuple[str, ...]
    db_path: Path
    edinet_api_key: str = ""
    edinet_history_days: int = 365
    macro_enabled: bool = True
    macro_history_start: date = date(2000, 1, 1)


def load_settings() -> Settings:
    load_dotenv()
    plan_name = os.getenv("JQUANTS_PLAN", "free").strip().lower()
    if plan_name not in PLANS:
        raise SystemExit(f"JQUANTS_PLAN must be one of {', '.join(PLANS)}, got {plan_name!r}")
    watchlist = tuple(
        c.strip() for c in os.getenv("JQUANTS_WATCHLIST", "").split(",") if c.strip()
    )
    return Settings(
        api_key=os.getenv("JQUANTS_API_KEY", "").strip(),
        plan=PLANS[plan_name],
        watchlist=watchlist,
        db_path=Path(os.getenv("JQUANTS_DB", "data/jquantster.db")),
        edinet_api_key=os.getenv("EDINET_API_KEY", "").strip(),
        edinet_history_days=int(os.getenv("EDINET_HISTORY_DAYS", "365")),
        macro_enabled=os.getenv("MACRO_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off"),
        macro_history_start=date.fromisoformat(os.getenv("MACRO_HISTORY_START", "2000-01-01").strip()),
    )
