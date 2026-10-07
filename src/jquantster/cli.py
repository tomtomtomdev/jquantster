"""`jquantster sync | status | ui`"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import db
from .client import AuthError, JQuantsClient, RateLimiter
from .config import load_settings
from .sync import Syncer


def _sync(args, settings) -> int:
    codes = tuple(args.codes.split(",")) if args.codes else settings.watchlist
    plan = settings.plan
    start, end = plan.window()
    estimate = 2 + 2 * len(codes) + args.market_days * 2 + 1
    print(f"Plan {plan.name}: {plan.rpm} req/min published, using {plan.budget_rpm}. "
          f"Readable window {start} → {end}.")
    print(f"Watchlist: {', '.join(codes) or '(none)'}; market snapshot: {args.market_days} day(s).")
    print(f"About {estimate} calls ≈ {estimate / plan.budget_rpm:.1f} min at most "
          "(less on later runs; data already stored is skipped).\n")

    conn = db.connect(settings.db_path)
    limiter = RateLimiter(conn, on_wait=lambda s: print(f"  … rate limit: waiting {s:.0f}s", flush=True))
    try:
        client = JQuantsClient(settings.api_key, limiter, plan.budget_rpm, plan.fins_budget_rpm)
        results = Syncer(settings, conn, client).run_all(codes, args.market_days)
    except AuthError as e:
        print(f"Auth error: {e}", file=sys.stderr)
        return 2
    print(f"\nDone: {sum(r.calls for r in results)} calls → {settings.db_path}")
    return 0 if all(r.status in ("ok", "skipped") for r in results) else 1


def _status(settings) -> int:
    conn = db.connect(settings.db_path)
    for table in ("issues", "daily_bars", "fins_summary", "investor_types", "calendar"):
        print(f"{table:16} {conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:>9,}")
    print(f"last sync        {db.get_meta(conn, 'last_sync', 'never')}")
    for r in conn.execute("SELECT * FROM entitlements ORDER BY dataset"):
        print(f"  {r['dataset']:16} {r['status']}  {r['message'] or ''}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jquantster")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sync", help="fetch new data into the database")
    p.add_argument("--codes", help="comma-separated codes (default: JQUANTS_WATCHLIST)")
    p.add_argument("--market-days", type=int, default=2,
                   help="latest N trading days for all stocks (default 2, needed for the Market tab)")
    sub.add_parser("status", help="show what is stored")
    sub.add_parser("ui", help="open the dashboard (extra args go to streamlit)")
    args, extra = parser.parse_known_args(argv)
    if extra and args.cmd != "ui":
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    settings = load_settings()

    if args.cmd == "sync":
        return _sync(args, settings)
    if args.cmd == "status":
        return _status(settings)
    app = Path(__file__).with_name("app.py")
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app), *extra])


if __name__ == "__main__":
    sys.exit(main())
