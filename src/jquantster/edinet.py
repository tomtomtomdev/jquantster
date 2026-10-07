"""EDINET API v2 client (FSA filings), rate limited through the shared RateLimiter."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from pathlib import PurePosixPath
from typing import Any

import httpx

from .client import AuthError, JQuantsError, RateLimiter
from .config import EDINET_BASE_URL, EDINET_RPM

BUCKET = "edinet"


class NotFound(JQuantsError):
    """The document doesn't exist (any more) or has no file of the asked type."""


class EdinetClient:
    def __init__(self, api_key: str, limiter: RateLimiter,
                 transport: httpx.BaseTransport | None = None, max_retries: int = 4):
        if not api_key:
            raise AuthError("EDINET_API_KEY is not set. Add it to .env to sync EDINET filings.")
        self.api_key = api_key
        self.limiter = limiter
        self.max_retries = max_retries
        self.calls = 0
        self.http = httpx.Client(base_url=EDINET_BASE_URL, timeout=httpx.Timeout(60.0),
                                 transport=transport)

    def _request(self, path: str, params: dict[str, Any]) -> httpx.Response:
        params = {**params, "Subscription-Key": self.api_key}
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire([(BUCKET, EDINET_RPM)])
            self.calls += 1
            try:
                resp = self.http.get(path, params=params)
            except httpx.TransportError:
                if attempt == self.max_retries:
                    raise
                self.limiter.sleep(5 * 3**attempt)
                continue
            if resp.status_code in (401, 403):
                raise AuthError(f"EDINET rejected the key ({resp.status_code}): {_message(resp)}")
            if (resp.status_code >= 500 or resp.status_code == 429) and attempt < self.max_retries:
                self.limiter.sleep(5 * 3**attempt)
                continue
            if resp.status_code == 404:
                raise NotFound(f"EDINET 404 on {path}: {_message(resp)}")
            if resp.status_code != 200:
                raise JQuantsError(f"EDINET {resp.status_code} on {path}: {_message(resp)}")
            # Errors also arrive as HTTP 200 with a JSON body whose metadata.status says otherwise.
            if resp.headers.get("content-type", "").startswith("application/json"):
                meta = resp.json().get("metadata", {})
                if str(meta.get("status")) == "404":
                    raise NotFound(f"EDINET 404 on {path}: {meta.get('message', '')}")
                if str(meta.get("status")) != "200":
                    raise JQuantsError(
                        f"EDINET {meta.get('status')} on {path}: {meta.get('message', '')}")
            return resp
        raise JQuantsError(f"Gave up on EDINET {path} after {self.max_retries} retries")

    def list_documents(self, day: date) -> list[dict]:
        """Every filing submitted on `day`."""
        resp = self._request("/documents.json", {"date": day.isoformat(), "type": 2})
        return resp.json().get("results") or []

    def download_csv(self, doc_id: str) -> dict[str, list[dict]]:
        """The XBRL-to-CSV files of a document, as file name -> rows keyed by header."""
        resp = self._request(f"/documents/{doc_id}", {"type": 5})
        files: dict[str, list[dict]] = {}
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            for info in z.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".csv"):
                    continue
                text = z.read(info).decode("utf-16")
                files[PurePosixPath(info.filename).name] = list(
                    csv.DictReader(io.StringIO(text), delimiter="\t"))
        return files


# Large-shareholding report (jplvh_cor) elements: column -> element name after the prefix.
HOLDING_ELEMENTS = {
    "holding_ratio": "HoldingRatioOfShareCertificatesEtc",
    "prev_holding_ratio": "HoldingRatioOfShareCertificatesEtcPerLastReport",
    "shares_held": "TotalNumberOfStocksEtcHeld",
    "obligation_date": "DateWhenFilingRequirementWasTriggered",
}
_RATIOS = ("holding_ratio", "prev_holding_ratio")
_NIL = ("", "－", "-", "―")


def _clean(v) -> str:
    return (v or "").strip().strip('"').strip()


def _context_rank(ctx: str) -> int:
    """Joint-holder reports repeat each figure per holder in member contexts
    (`FilingDateInstant_JointHolder1Member`); the member-less context, else one naming a
    total, is the aggregate for all holders."""
    if "Member" not in ctx:
        return 0
    return 1 if "Total" in ctx else 2


def _percent(value: str, unit_id: str, unit_name: str) -> float | None:
    """Holding ratios as percent. XBRL stores them as pure fractions (0.0512 = 5.12%), so a
    value <= 1 is a fraction and is multiplied by 100, unless the value or its unit says
    percent ('%', '％', unit ID containing 'percent'). A value > 1 is already a percent."""
    is_pct = (value.endswith(("%", "％")) or "percent" in unit_id.lower()
              or any(c in unit_name for c in "%％"))
    try:
        v = float(value.rstrip("%％").replace(",", ""))
    except ValueError:
        return None
    return v if is_pct or v > 1 else v * 100


def parse_holdings(files: dict[str, list[dict]]) -> dict | None:
    """Holding figures from a 350/360 report's CSV files, or None when it has no ratio."""
    best: dict[str, tuple[int, dict]] = {}  # column -> (rank, row); first row wins ties
    names = {name: col for col, name in HOLDING_ELEMENTS.items()}
    for rows in files.values():
        for row in rows:
            row = {_clean(k): _clean(v) for k, v in row.items() if k is not None}
            col = names.get(row.get("要素ID", "").rpartition(":")[2])
            if col is None or row.get("値", "") in _NIL:
                continue
            rank = _context_rank(row.get("コンテキストID", ""))
            if col not in best or rank < best[col][0]:
                best[col] = (rank, row)
    out: dict = {}
    for col in HOLDING_ELEMENTS:
        row = best.get(col, (0, None))[1]
        if row is None:
            out[col] = None
        elif col in _RATIOS:
            out[col] = _percent(row["値"], row.get("ユニットID", ""), row.get("単位", ""))
        elif col == "shares_held":
            try:
                out[col] = float(row["値"].replace(",", ""))
            except ValueError:
                out[col] = None
        else:
            out[col] = row["値"][:10]
    return out if out["holding_ratio"] is not None else None


# Financial statements: canonical item -> element IDs, best first. Statements (jppfs_cor =
# J-GAAP, jpigp_cor = IFRS) come before the summary of business results (jpcrp_cor), which is
# only a fallback. Extend by adding IDs; a consolidated figure always beats a non-consolidated
# one whatever its position here.
FIN_ELEMENTS: dict[str, tuple[str, ...]] = {
    "total_assets": (
        "jppfs_cor:Assets", "jpigp_cor:AssetsIFRS",
        "jpcrp_cor:TotalAssetsSummaryOfBusinessResults",
        "jpcrp_cor:TotalAssetsIFRSSummaryOfBusinessResults",
    ),
    # J-GAAP net assets (incl. non-controlling interests); IFRS total equity
    "net_assets": (
        "jppfs_cor:NetAssets", "jpigp_cor:EquityIFRS",
        "jpcrp_cor:NetAssetsSummaryOfBusinessResults",
        "jpcrp_cor:TotalEquityIFRSSummaryOfBusinessResults",
    ),
    # IFRS equity attributable to owners of the parent. J-GAAP has no such element; its
    # shareholders' equity (株主資本) stands in, which leaves out accumulated OCI.
    "equity_parent": (
        "jpigp_cor:EquityAttributableToOwnersOfParentIFRS", "jppfs_cor:ShareholdersEquity",
        "jpcrp_cor:EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults",
    ),
    "cash": (
        "jppfs_cor:CashAndCashEquivalents", "jpigp_cor:CashAndCashEquivalentsIFRS",
        "jpcrp_cor:CashAndCashEquivalentsSummaryOfBusinessResults",
        "jpcrp_cor:CashAndCashEquivalentsIFRSSummaryOfBusinessResults",
    ),
    **{
        f"cf_{kind.lower()}": (
            f"jppfs_cor:NetCashProvidedByUsedIn{kind}Activities",
            f"jpigp_cor:NetCashProvidedByUsedIn{kind}ActivitiesIFRS",
            f"jpcrp_cor:NetCashProvidedByUsedIn{kind}ActivitiesSummaryOfBusinessResults",
            f"jpcrp_cor:CashFlowsFromUsedIn{kind}ActivitiesIFRSSummaryOfBusinessResults",
        ) for kind in ("Operating", "Investing", "Financing")
    },
}
_FIN_RANK = {elem: (item, i) for item, elems in FIN_ELEMENTS.items()
             for i, elem in enumerate(elems)}
# Current-period contexts: annual, semiannual, quarterly (140).
CURRENT_CONTEXTS = ("CurrentYearInstant", "CurrentYearDuration", "InterimInstant",
                    "InterimDuration", "CurrentQuarterInstant", "CurrentYTDDuration")
NON_CONSOLIDATED = "_NonConsolidatedMember"


def parse_fins(files: dict[str, list[dict]]) -> dict[str, tuple[float, str]]:
    """Current-period figures from an annual / semiannual / quarterly report's CSV files:
    item -> (value, 'consolidated' | 'non_consolidated'). Consolidated wins, then the
    element's position in FIN_ELEMENTS. Prior periods and member (segment, component)
    contexts other than _NonConsolidatedMember are ignored."""
    rows = [{_clean(k): _clean(v) for k, v in row.items() if k is not None}
            for rs in files.values() for row in rs]
    consolidated_dei = next(
        (r.get("値", "").lower() for r in rows if r.get("要素ID", "")
         == "jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI"), "")
    best: dict[str, tuple[tuple[int, int], float, str]] = {}
    for row in rows:
        hit = _FIN_RANK.get(row.get("要素ID", ""))
        if hit is None or row.get("値", "") in _NIL:
            continue
        ctx, rel = row.get("コンテキストID", ""), row.get("相対年度", "")
        base, _, rest = ctx.partition("_")
        if base not in CURRENT_CONTEXTS or rest not in ("", NON_CONSOLIDATED[1:]):
            continue
        if rel.startswith("前") or "Prior" in rel:
            continue
        non_con = bool(rest) or row.get("連結・個別") == "個別" or consolidated_dei == "false"
        try:
            value = float(row["値"].replace(",", ""))
        except ValueError:
            continue
        item, elem_rank = hit
        rank = (int(non_con), elem_rank)
        if item not in best or rank < best[item][0]:
            best[item] = (rank, value, "non_consolidated" if non_con else "consolidated")
    return {item: (value, basis) for item, (_, value, basis) in best.items()}


def _message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message", ""))
    except ValueError:
        return resp.text[:200]
