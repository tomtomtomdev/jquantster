# EDINET integration plan

Adds FSA EDINET filings (API v2, https://api.edinet-fsa.go.jp/api/v2) to jquantster:
a filings feed, large-shareholding (5%) reports and balance-sheet / cash-flow figures
for watchlist stocks. EDINET is government open data and needs a free Subscription-Key.

## Facts the slices rely on

- `GET /documents.json?date=YYYY-MM-DD&type=2&Subscription-Key=…` lists every filing
  submitted that day (`metadata` + `results`). There is no per-company query, so syncing means
  one call per business day, then filtering locally.
- Result fields used: `docID`, `edinetCode`, `secCode` (5-digit, may be null), `filerName`,
  `docTypeCode`, `issuerEdinetCode`, `subjectEdinetCode`, `submitDateTime`, `periodEnd`,
  `docDescription`, `csvFlag`, `withdrawalStatus`.
- `GET /documents/{docID}?type=5` returns a ZIP of CSV files (UTF-16, tab-separated)
  converted from XBRL. Columns include element ID, item name, context ID, unit and value.
- docTypeCode: 120 annual securities report, 130 amended annual, 160 semiannual,
  140 quarterly (abolished for periods after April 2024), 350 large-shareholding report,
  360 change report. Other codes are stored but not specially handled.
- For 350/360 the filer is the holder; the stock concerned is `issuerEdinetCode`.
- The rate limit isn't published. Stay polite: about 1 request per second, using a separate
  `edinet` bucket in the shared `RateLimiter`.
- Mapping EDINET code to securities code: taken from any stored document whose filer has
  `secCode` set (every listed company files its own reports).

## Slices

Each slice: write failing tests first (extend `tests/` with a fake EDINET transport like
`tests/fake_api.py`), implement, run `uv run pytest -q` until green, tick the box, commit.

- [x] **1. Config + client.** `EDINET_API_KEY` and `EDINET_HISTORY_DAYS` (default 365) in
  `Settings` / `.env.example`. `EdinetClient` (httpx, Subscription-Key query param, shared
  `RateLimiter` with an `edinet` bucket at 50/min, retries on 5xx/transport errors, 401/403 →
  `AuthError`, `metadata.status` ≠ 200 → error). `list_documents(date)` and
  `download_csv(doc_id)` → dict of filename → rows (parsed UTF-16 TSV).
- [x] **2. Filings index sync.** Table `edinet_docs` (doc_id PK and the fields above) and view or
  table `edinet_codes` (edinet_code → sec_code, name). Syncer job `edinet filings`: one call per
  business day (use `calendar` when present, else weekdays) from the newest stored date
  (re-reading the last 3 days) or `today - EDINET_HISTORY_DAYS`, up to today. Skipped with a
  clear message when no EDINET key is set. `status` shows the count. Wired into `run_all`
  and the CLI call estimate.
- [x] **3. Large shareholdings.** Job `edinet holdings`: for 350/360 docs whose issuer maps to a
  watchlist code, not withdrawn, `csvFlag` = 1 and not yet parsed, download the CSV and store
  holder, holding ratio, previous ratio when present, shares held, reporting obligation date into
  `edinet_holdings`. Only parse what's needed; keep unparseable docs marked so they aren't
  refetched. (Done: parse state per doc in `edinet_parsed`; ratios stored as percent — XBRL
  fractions ≤ 1 are ×100 unless the value/unit says percent; joint-holder reports use the
  member-less context, else a "Total" member, else the first.)
- [ ] **4. Financial statements.** Job `edinet financials`: for 120/130/160 docs of watchlist
  companies, download the CSV and extract consolidated (fallback non-consolidated) current-period
  values for total assets, net assets / equity, cash and equivalents, operating / investing /
  financing cash flow, for both J-GAAP (`jppfs_cor`) and IFRS (`jpigp_cor`) element IDs, into
  `edinet_fins` (code, doc_id, period_end, item, value).
- [ ] **5. Dashboard.** Stock tab gets three sections below "Reported results": recent filings
  (date, type, description, link to the EDINET document page), large shareholders (latest ratio per
  holder, with a ratio-over-time chart), balance sheet and cash flow by period. Empty states
  explain how to enable EDINET. Extend the UI smoke test.
- [ ] **6. Setup and docs.** `run.sh` asks for an optional EDINET key on first run; README
  sections for EDINET (what you get, key signup at https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1,
  call volume of the first sync, licence note); `.env.example` updated.

## Later (not in this run)

- Macro tab: MOF JGB yield curve (daily CSV, no key), and selected BOJ series (FX, call rate,
  Tankan).
