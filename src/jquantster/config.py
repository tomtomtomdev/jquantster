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


@dataclass(frozen=True)
class Plan:
    name: str
    rpm: int
    history_years: int
    delay_days: int
    datasets: frozenset[str] = field(default_factory=frozenset)

    @property
    def budget_rpm(self) -> int:
        return max(1, int(self.rpm * SAFETY))

    @property
    def fins_budget_rpm(self) -> int:
        return min(self.budget_rpm, int(FINS_RPM * SAFETY))

    def window(self, today: date | None = None) -> tuple[date, date]:
        """First and last date this plan can read."""
        today = today or date.today()
        end = today - timedelta(days=self.delay_days)
        start = today.replace(year=today.year - self.history_years) + timedelta(days=1)
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
    )
