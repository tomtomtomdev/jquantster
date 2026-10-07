# jquantster

Fetches Japanese market data from [J-Quants](https://jpx-jquants.com/en) (the JPX official API, V2),
stores it in a local SQLite file, and shows it in a small Streamlit dashboard:

- **Stock**: adjusted price and volume history for your watchlist, plus reported results
- **Market**: advancers and decliners, top movers and most-traded stocks for a trading day
- **Investor flows**: weekly net buying and selling by investor type (foreigners, individuals,
  trust banks and so on) per TSE section (needs the Light plan or higher)

## Setup

1. Create a J-Quants account at <https://jpx-jquants.com>. The Free plan is enough to start.
2. In the dashboard, open **API Keys** and create a key.
3. Install and configure:

```bash
uv sync
cp .env.example .env   # paste your key, set JQUANTS_PLAN and JQUANTS_WATCHLIST
uv run jquantster sync # fetch (safe to re-run; it only fetches what's new)
uv run jquantster ui   # open the dashboard
uv run jquantster status
```

## Plans and rate limits

| Plan | Requests/min (used) | History | Delay | Investor flows |
|---|---|---|---|---|
| free | 5 (4) | 2 years | 12 weeks | – |
| light | 60 (48) | 5 years | none | ✓ |
| standard | 120 (96) | 10 years | none | ✓ |
| premium | 500 (400) | 20 years | none | ✓ |

The fetcher stays at 80% of the published limit. `/fins/*` also has its own 60/min cap.

- The limit applies to the whole account, so every call is logged in SQLite. Separate
  processes therefore share one budget.
- A 429 response pauses all callers for 2 minutes. Repeated 429s get the account blocked for
  about 5 minutes, which this avoids.
- If the API returns 403 because a dataset isn't in your plan, the sync records that and
  carries on with the next dataset.

A first sync on the Free plan with a 5-stock watchlist makes about 20 calls, which takes
around 5 minutes.

## How it fetches

- **Watchlist**: one request per stock returns its whole history (`code` + `from`/`to`).
- **Market snapshot**: one request per trading day returns every stock (`date`).
- **Investor types**: one ranged request. Each run re-reads the last 3 weeks, because JPX
  re-issues corrected weeks with a new `PubDate`. Every version is kept, and the dashboard
  reads the latest one (`investor_types_latest` view).
- **Incremental**: each job resumes from the newest date already stored.

## Development

```bash
uv run pytest   # runs against a fake API; no key needed
```
