# Macro tab plan

Adds a fourth dashboard tab, "Macro", with the JGB yield curve from the Ministry of Finance and
selected Bank of Japan series (USD/JPY, the overnight call rate, Tankan business conditions).
Both sources are free government data and need no API key, so the jobs run by default.

## Facts the slices rely on

Checked against the live endpoints on 2026-10-09.

- **MOF JGB yields.** Two English CSVs, no key:
  - `https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv`: the current
    month (~1 KB).
  - `https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv`:
    every day from 1974-09-24 to the end of the previous month (~1.2 MB).
  - Row 1 is a title (`Interest Rate (October 2026),…,(Unit : %)`), row 2 is the header
    `Date,1Y,2Y,…,10Y,15Y,20Y,25Y,30Y,40Y`, then `YYYY/M/D` rows in percent. A missing tenor is
    `-` (for example 10Y+ before the 1980s). The file ends with a blank `,,,` row and a
    non-ASCII note line, so stop at the first row whose date doesn't parse.
  - The current-month file can lag a few business days.
- **BOJ Time-Series Data Search API**, `https://www.stat-search.boj.or.jp/api/v1`, no key:
  - `getDataCode?format=json&lang=en&db=<DB>&code=<SERIES>&startDate=<YYYYMM|YYYYQ…>` returns
    `STATUS`, `MESSAGEID`, `NEXTPOSITION` and `RESULTSET[]`. Each result has `SERIES_CODE`,
    `NAME_OF_TIME_SERIES`, `UNIT`, `FREQUENCY`, `LAST_UPDATE` and
    `VALUES.SURVEY_DATES[]` / `VALUES.VALUES[]` (parallel arrays, `null` on holidays).
  - Daily dates are `YYYYMMDD`. The format of quarterly dates must be checked on the first real
    call.
  - When `NEXTPOSITION` is not null, the result was cut short: repeat the call with
    `startPosition=<NEXTPOSITION>`. Call-rate history from 1998-01 (10,507 days) came back in a
    single response.
  - `getMetadata?format=json&lang=en&db=<DB>` lists the series in a database.
  - `STATUS` ≠ 200 means an error, with the text in `MESSAGE`.
- **BOJ series used** (in a `BOJ_SERIES` dict, so adding one is a one-line change):
  | key | db | code | meaning |
  |---|---|---|---|
  | `usdjpy` | FM08 | `FXERD01` | USD/JPY spot at 9:00 JST, Tokyo, yen per dollar, daily |
  | `call_rate` | FM01 | `STRDCLUCON` | Uncollateralized overnight call rate, average, % p.a., daily |
  | `tankan_lm` | CO | `TK99F1000601GCQ01000` | Tankan business conditions DI, large manufacturers, actual, quarterly |
  | `tankan_ln` | CO | `TK99F2000601GCQ01000` | Same, large non-manufacturers |
  | `tankan_lm_fc` | CO | `TK99F1000601GCQ11000` | Large manufacturers, forecast |
  | `tankan_ln_fc` | CO | `TK99F2000601GCQ11000` | Large non-manufacturers, forecast |
- Neither MOF nor BOJ publishes a rate limit. Stay polite: one `macro` bucket at 20/min in the
  shared `RateLimiter`. A full daily sync is about 2 MOF calls plus 3 BOJ calls (one per
  database, with several codes comma-separated if the API accepts that, otherwise one per series).
- Licence: MOF and BOJ data may be reused with the source credited. The tab shows
  "Source: Ministry of Finance Japan; Bank of Japan".

## Slices

Each slice: write failing tests first (a fake transport in `tests/fake_macro.py` serving fixture
CSV/JSON trimmed from the real responses above), implement, run `uv run pytest -q` until green,
tick the box, commit.

- [ ] **1. Client.** `MacroClient` in `src/jquantster/macro.py` (httpx, shared `RateLimiter`
  with a `macro` bucket, retries on 5xx/transport errors, the same shape as `EdinetClient`).
  `jgb_csv(url)` returns `[(date, {tenor: pct | None})]`: it skips the title row, maps the header
  to tenor labels and stops at the footer. `boj_series(db, codes, start)` returns
  `{code: (meta, [(date, value)])}`. It follows `NEXTPOSITION`, drops `null`s and raises
  `MacroError` when `STATUS` ≠ 200. Unit tests cover the `-` cells, the footer, pagination and
  an error status.
- [ ] **2. Storage + sync jobs.** Tables `jgb_yields (date, tenor, yield_pct, PK(date, tenor))`,
  `macro_series (key PK, db, code, name, unit, frequency, last_update)` and
  `macro_obs (key, date, value, PK(key, date))`. Syncer jobs:
  - `macro jgb`: on the first run, the historical file from `MACRO_HISTORY_START` (default
    2000-01-01, so we don't store 50 years nobody charts) plus the current month. After that,
    only the current month, and the historical file once more when a new month has begun and
    the previous month's rows are incomplete.
  - `macro boj`: each `BOJ_SERIES` entry from its newest stored date minus 1 month (BOJ revises
    recent figures), or from `MACRO_HISTORY_START`.

  Both jobs upsert, are wired into `run_all` (after the J-Quants jobs, still run when J-Quants
  fails) and the CLI call estimate, and are shown in `status`. `MACRO_ENABLED` (default true)
  turns them off.
- [ ] **3. Macro tab: rates.** A new "Macro" tab after "Investor flows":
  - Yield curve: the latest curve plus curves from 1 month, 1 year and 3 years earlier (nearest
    stored date at or before each), x = tenor in years, one line per date, hover tooltips.
  - 10Y and 2Y history, with the 10Y−2Y spread as a second chart. Date range selector with
    1Y / 5Y / max.
  - Call-rate history as a step chart.
  - Each chart is captioned with its "as of" date and source.
- [ ] **4. Macro tab: FX + Tankan.** USD/JPY daily line with the same range selector. Tankan:
  quarterly bars of large manufacturers and non-manufacturers (actual DI), with the latest
  forecast shown as a hollow bar, and a zero line. Optional overlay on the Market tab's TOPIX
  chart: USD/JPY on a secondary axis, behind a checkbox, off by default. Empty states say which
  job to run. Extend the UI smoke test to render the Macro tab with fixture data and while empty.
- [ ] **5. Docs.** README section "Macro data" (what's shown, sources and credit line, no key
  needed, first sync downloads ~1.2 MB from MOF, how to add a BOJ series by finding its code
  with `getMetadata` and adding a `BOJ_SERIES` line). `.env.example` gets `MACRO_ENABLED` and
  `MACRO_HISTORY_START`.

## Open points to settle in slice 1

- Whether `getDataCode` accepts several comma-separated codes. If it does, use one call per
  database.
- The quarterly `SURVEY_DATES` format for Tankan. Store it as the quarter-end date.
- The Tankan metadata listed 2026-03 as the last actual result. Check whether the June and
  September 2026 surveys show up in `getDataCode`, or whether the metadata is just stale.

## Later (not in this run)

- More BOJ series: monetary base, TONA, real effective exchange rate, CPI from e-Stat.
- Overlay a stock's price on JGB 10Y or USD/JPY in the Stock tab.
