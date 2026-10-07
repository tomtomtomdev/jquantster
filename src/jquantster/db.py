"""SQLite storage. One file, safe to share between the sync CLI and the UI."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

INVESTOR_PREFIXES = (
    "Prop", "Brk", "Tot", "Ind", "Frgn", "SecCo", "InvTr",
    "BusCo", "OthCo", "InsCo", "Bank", "TrstBnk", "OthFin",
)
INVESTOR_SUFFIXES = ("Sell", "Buy", "Tot", "Bal")
INVESTOR_VALUE_COLS = [p + s for p in INVESTOR_PREFIXES for s in INVESTOR_SUFFIXES]

BAR_COLS = {
    # column: API field
    "o": "O", "h": "H", "l": "L", "c": "C", "vo": "Vo", "va": "Va",
    "adj_factor": "AdjFactor", "adj_o": "AdjO", "adj_h": "AdjH",
    "adj_l": "AdjL", "adj_c": "AdjC", "adj_vo": "AdjVo",
}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS api_calls (ts REAL NOT NULL, bucket TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS api_calls_ts ON api_calls (bucket, ts);
CREATE TABLE IF NOT EXISTS entitlements (
    dataset TEXT PRIMARY KEY, status TEXT NOT NULL, message TEXT, checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY, job TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT, status TEXT, rows INTEGER, calls INTEGER, message TEXT
);
CREATE TABLE IF NOT EXISTS calendar (date TEXT PRIMARY KEY, hol_div TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS issues (
    code TEXT PRIMARY KEY, as_of TEXT, co_name TEXT, co_name_en TEXT,
    s17_nm TEXT, s33_nm TEXT, scale_cat TEXT, mkt_nm TEXT
);
CREATE TABLE IF NOT EXISTS daily_bars (
    code TEXT NOT NULL, date TEXT NOT NULL,
    {", ".join(f"{c} REAL" for c in BAR_COLS)},
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS daily_bars_date ON daily_bars (date);
CREATE TABLE IF NOT EXISTS fins_summary (
    code TEXT NOT NULL, disc_no TEXT NOT NULL, disc_date TEXT, doc_type TEXT,
    raw TEXT NOT NULL, PRIMARY KEY (code, disc_no)
);
-- Every published version is kept: J-Quants re-issues corrected weeks with a new PubDate.
CREATE TABLE IF NOT EXISTS investor_types (
    section TEXT NOT NULL, st_date TEXT NOT NULL, en_date TEXT NOT NULL, pub_date TEXT NOT NULL,
    {", ".join(f"{c} REAL" for c in INVESTOR_VALUE_COLS)},
    PRIMARY KEY (section, st_date, pub_date)
);
CREATE VIEW IF NOT EXISTS investor_types_latest AS
    SELECT t.* FROM investor_types t
    JOIN (SELECT section, st_date, MAX(pub_date) AS pub_date
          FROM investor_types GROUP BY section, st_date) m
      USING (section, st_date, pub_date);
-- EDINET filings index (documents.json); submit_date is submitDateTime's date, for ranges.
CREATE TABLE IF NOT EXISTS edinet_docs (
    doc_id TEXT PRIMARY KEY, edinet_code TEXT, sec_code TEXT, filer_name TEXT,
    doc_type_code TEXT, issuer_edinet_code TEXT, subject_edinet_code TEXT,
    submit_datetime TEXT, submit_date TEXT NOT NULL, period_end TEXT, doc_description TEXT,
    csv_flag TEXT, withdrawal_status TEXT
);
CREATE INDEX IF NOT EXISTS edinet_docs_submit_date ON edinet_docs (submit_date);
CREATE INDEX IF NOT EXISTS edinet_docs_issuer ON edinet_docs (issuer_edinet_code);
-- Listed companies file their own reports with secCode set: EDINET code -> 5-digit code.
CREATE VIEW IF NOT EXISTS edinet_codes AS
    SELECT edinet_code, sec_code, filer_name FROM (
        SELECT edinet_code, sec_code, filer_name, ROW_NUMBER() OVER (
            PARTITION BY edinet_code ORDER BY submit_datetime DESC, doc_id DESC) AS rn
        FROM edinet_docs
        WHERE edinet_code IS NOT NULL AND sec_code IS NOT NULL AND sec_code != '')
    WHERE rn = 1;
"""

EDINET_DOC_COLS = {
    # column: API field
    "doc_id": "docID", "edinet_code": "edinetCode", "sec_code": "secCode",
    "filer_name": "filerName", "doc_type_code": "docTypeCode",
    "issuer_edinet_code": "issuerEdinetCode", "subject_edinet_code": "subjectEdinetCode",
    "submit_datetime": "submitDateTime", "period_end": "periodEnd",
    "doc_description": "docDescription", "csv_flag": "csvFlag",
    "withdrawal_status": "withdrawalStatus",
}


def code5(code: str) -> str:
    """APIs use 5-digit codes; 4-digit input means the common stock (suffix 0)."""
    return code + "0" if len(code) == 4 else code


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def upsert_calendar(conn, rows) -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO calendar VALUES (?, ?)",
        [(r["Date"], str(r["HolDiv"])) for r in rows],
    )
    return len(rows)


def upsert_issues(conn, rows) -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO issues VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (r["Code"], r.get("Date"), r.get("CoName"), r.get("CoNameEn"),
             r.get("S17Nm"), r.get("S33Nm"), r.get("ScaleCat"), r.get("MktNm"))
            for r in rows
        ],
    )
    return len(rows)


def upsert_bars(conn, rows) -> int:
    cols = ["code", "date", *BAR_COLS]
    conn.executemany(
        f"INSERT OR REPLACE INTO daily_bars ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        [(r["Code"], r["Date"], *(_num(r.get(f)) for f in BAR_COLS.values())) for r in rows],
    )
    return len(rows)


def upsert_fins(conn, rows) -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO fins_summary VALUES (?, ?, ?, ?, ?)",
        [
            (r["Code"], str(r.get("DiscNo") or f"{r.get('DiscDate')}-{r.get('DocType')}"),
             r.get("DiscDate"), r.get("DocType"), json.dumps(r, ensure_ascii=False))
            for r in rows
        ],
    )
    return len(rows)


def upsert_investor_types(conn, rows) -> int:
    cols = ["section", "st_date", "en_date", "pub_date", *INVESTOR_VALUE_COLS]
    conn.executemany(
        f"INSERT OR REPLACE INTO investor_types ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        [
            (r["Section"], r["StDate"], r["EnDate"], r["PubDate"],
             *(_num(r.get(c)) for c in INVESTOR_VALUE_COLS))
            for r in rows
        ],
    )
    return len(rows)


def upsert_edinet_docs(conn, rows, day: str) -> int:
    """`day` is the date the list was asked for; used when submitDateTime is missing."""
    cols = [*EDINET_DOC_COLS, "submit_date"]
    conn.executemany(
        f"INSERT OR REPLACE INTO edinet_docs ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        [
            (*(None if r.get(f) in (None, "") else str(r[f]) for f in EDINET_DOC_COLS.values()),
             (r.get("submitDateTime") or day)[:10])
            for r in rows
        ],
    )
    return len(rows)


def edinet_codes_for(conn, codes) -> dict[str, str]:
    """5-digit securities code -> EDINET code, for the codes EDINET has seen filing."""
    wanted = [code5(c) for c in codes]
    if not wanted:
        return {}
    return {
        r["sec_code"]: r["edinet_code"] for r in conn.execute(
            f"SELECT sec_code, edinet_code FROM edinet_codes "
            f"WHERE sec_code IN ({', '.join('?' * len(wanted))})", wanted)
    }


def set_entitlement(conn, dataset: str, status: str, message: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO entitlements VALUES (?, ?, ?, datetime('now'))",
        (dataset, status, message),
    )


def set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


def get_meta(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default
