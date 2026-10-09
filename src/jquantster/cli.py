"""`jquantster sync | status | ui`"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import date, timedelta
from pathlib import Path

from . import db
from .client import AuthError, JQuantsClient, RateLimiter
from .config import BOJ_SERIES, EDINET_RPM, MACRO_RPM, load_settings
from .edinet import EdinetClient
from .macro import MacroClient
from .sync import Syncer, edinet_start


def _sync(args, settings) -> int:
    codes = tuple(args.codes.split(",")) if args.codes else settings.watchlist
    plan = settings.plan
    start, end = plan.window()
    conn = db.connect(settings.db_path)
    estimate = 2 + 2 * len(codes) + args.market_days * 2 + 1
    minutes = estimate / plan.budget_rpm
    edinet_calls = 0
    if settings.edinet_api_key:
        today = date.today()
        first = edinet_start(conn, settings, today)
        edinet_calls = sum((first + timedelta(i)).weekday() < 5 for i in range((today - first).days + 1))
        # holdings and financial reports already listed but not downloaded (new ones found
        # today add more)
        edinet_calls += len(db.edinet_holding_docs_todo(conn, codes))
        edinet_calls += len(db.edinet_fins_docs_todo(conn, codes))
        minutes += edinet_calls / EDINET_RPM
    macro_calls = 0
    if settings.macro_enabled:
        # current-month JGB CSV, the history file when last month isn't in yet, one BOJ call
        # per database and frequency
        macro_calls = 2 + len({(d, f) for d, _, f in BOJ_SERIES.values()})
        minutes += macro_calls / MACRO_RPM
    print(f"Plan {plan.name}: {plan.rpm} req/min published, using {plan.budget_rpm}. "
          f"Readable window {start} → {end}.")
    print(f"Watchlist: {', '.join(codes) or '(none)'}; market snapshot: {args.market_days} day(s); "
          + (f"EDINET: about {edinet_calls} call(s)" if settings.edinet_api_key
             else "EDINET: off (set EDINET_API_KEY)")
          + (f"; macro: up to {macro_calls} call(s)." if settings.macro_enabled
             else "; macro: off."))
    print(f"About {estimate + edinet_calls + macro_calls} calls ≈ {minutes:.1f} min at most "
          "(less on later runs; data already stored is skipped).\n")

    limiter = RateLimiter(conn, on_wait=lambda s: print(f"  … rate limit: waiting {s:.0f}s", flush=True))
    try:
        client = JQuantsClient(settings.api_key, limiter, plan.budget_rpm, plan.fins_budget_rpm)
        edinet = EdinetClient(settings.edinet_api_key, limiter) if settings.edinet_api_key else None
        macro = MacroClient(limiter) if settings.macro_enabled else None
        results = Syncer(settings, conn, client, edinet=edinet, macro=macro).run_all(
            codes, args.market_days)
    except AuthError as e:
        print(f"Auth error: {e}", file=sys.stderr)
        return 2
    print(f"\nDone: {sum(r.calls for r in results)} calls → {settings.db_path}")
    return 0 if all(r.status in ("ok", "skipped") for r in results) else 1


def _status(settings) -> int:
    conn = db.connect(settings.db_path)
    for table in ("issues", "daily_bars", "fins_summary", "investor_types", "calendar",
                  "edinet_docs", "edinet_holdings", "edinet_fins", "jgb_yields", "macro_obs"):
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
    return _ui(extra)


def _ui(extra: list[str]) -> int:
    """Run the dashboard without Streamlit's first-run email prompt, then open it."""
    app = Path(__file__).with_name("app.py")
    flags = {"--server.port": "8510", "--server.headless": "true",
             "--browser.gatherUsageStats": "false"}
    for i, arg in enumerate(extra):  # flags given on the command line win
        key, _, value = arg.partition("=")
        if key in flags:
            flags[key] = value or extra[i + 1]
    given = {a.partition("=")[0] for a in extra}
    port = flags["--server.port"]
    args = [x for k, v in flags.items() if k not in given for x in (k, v)] + extra
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(app), *args])
    url = f"http://localhost:{port}"
    for _ in range(40):
        try:
            urllib.request.urlopen(f"{url}/_stcore/health", timeout=1)
            print(f"Dashboard: {url}")
            if not os.getenv("JQUANTSTER_NO_BROWSER"):
                webbrowser.open(url)
            break
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.5)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    sys.exit(main())
