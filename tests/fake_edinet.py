"""A tiny stand-in for the EDINET API v2, served through httpx.MockTransport."""

import io
import zipfile

import httpx

KEY = "ek"


def tsv_utf16(rows: list[list[str]]) -> bytes:
    text = "\n".join("\t".join(r) for r in rows) + "\n"
    return text.encode("utf-16")  # with BOM, as EDINET sends it


def csv_zip(files: dict[str, list[list[str]]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, rows in files.items():
            z.writestr(f"XBRL_TO_CSV/{name}", tsv_utf16(rows))
    return buf.getvalue()


HEADER = ["要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別", "期間・時点",
          "ユニットID", "単位", "値"]
CSV_FILES = {
    "jpcrp030000-asr-001_E02144-000_2026-03-31_01_2026-06-20.csv": [
        HEADER,
        ["jppfs_cor:Assets", "資産", "CurrentYearInstant", "当期末", "連結", "時点",
         "JPY", "円", "90000000000000"],
        ["jppfs_cor:NetAssets", "純資産", "CurrentYearInstant", "当期末", "連結", "時点",
         "JPY", "円", "35000000000000"],
    ],
    "jpaud-aar-cn-001_E02144-000_2026-03-31_01_2026-06-20.csv": [HEADER],
}

# Large-shareholding reports (jplvh taxonomy). A joint-holder report carries the summary for
# all holders in the member-less context and each holder's figures in a member context.
# Ratios are XBRL percentItemType (pure unit): fractions, 0.0512 = 5.12%.
LVH_350 = "jplvh010000-lvh-001_E11111-000_2026-10-02_01_2026-10-02.csv"
LVH_360 = "jplvh020000-lvh-001_E11111-000_2026-10-05_01_2026-10-05.csv"


def lvh(elem, name, ctx, value, unit="－", unit_name="－"):
    return [elem, name, ctx, "提出日時点", "その他", "時点", unit, unit_name, value]


HOLDINGS_350 = {LVH_350: [
    HEADER,
    lvh("jpdei_cor:EDINETCodeDEI", "EDINETコード、DEI", "FilingDateInstant", "E11111"),
    lvh("jplvh_cor:NameOfIssuer", "発行者の名称", "FilingDateInstant", "トヨタ自動車株式会社"),
    lvh("jplvh_cor:SecurityCodeOfIssuer", "証券コード", "FilingDateInstant", "7203"),
    lvh("jplvh_cor:DateWhenFilingRequirementWasTriggered", "報告義務発生日",
        "FilingDateInstant", "2026-09-28"),
    # each holder first (members), then the joint total (no member)
    lvh("jplvh_cor:TotalNumberOfStocksEtcHeld", "保有株券等の数（総数）",
        "FilingDateInstant_FilerLargeVolumeHolder1Member", "300000000", "shares", "株"),
    lvh("jplvh_cor:HoldingRatioOfShareCertificatesEtc", "株券等保有割合",
        "FilingDateInstant_FilerLargeVolumeHolder1Member", "0.0300", "pure", "－"),
    lvh("jplvh_cor:TotalNumberOfStocksEtcHeld", "保有株券等の数（総数）",
        "FilingDateInstant_JointHolder1Member", "212000000", "shares", "株"),
    lvh("jplvh_cor:HoldingRatioOfShareCertificatesEtc", "株券等保有割合",
        "FilingDateInstant_JointHolder1Member", "0.0212", "pure", "－"),
    lvh("jplvh_cor:TotalNumberOfStocksEtcHeld", "保有株券等の数（総数）",
        "FilingDateInstant", "512000000", "shares", "株"),
    lvh("jplvh_cor:HoldingRatioOfShareCertificatesEtc", "株券等保有割合",
        "FilingDateInstant", "0.0512", "pure", "－"),
]}
# Single-holder change report, element IDs and values quoted as some exports do.
HOLDINGS_360 = {LVH_360: [
    HEADER,
    lvh('"jplvh_cor:DateWhenFilingRequirementWasTriggered"', "報告義務発生日",
        "FilingDateInstant", '"2026-09-30"'),
    lvh('"jplvh_cor:TotalNumberOfStocksEtcHeld"', "保有株券等の数（総数）",
        "FilingDateInstant", '"605,000,000"', "shares", "株"),
    lvh('"jplvh_cor:HoldingRatioOfShareCertificatesEtc"', "株券等保有割合",
        "FilingDateInstant", '"0.0605"', "pure", "－"),
    lvh('"jplvh_cor:HoldingRatioOfShareCertificatesEtcPerLastReport"', "直前の報告書に記載された株券等保有割合",
        "FilingDateInstant", '"0.0512"', "pure", "－"),
]}
# A report with no holding figures we know (e.g. a different taxonomy version).
HOLDINGS_UNKNOWN = {"jplvh010000-lvh-001_E22222-000_2026-10-02_01_2026-10-02.csv": [
    HEADER, lvh("jplvh_cor:NameOfIssuer", "発行者の名称", "FilingDateInstant", "トヨタ自動車株式会社"),
]}

DOC = {"seqNumber": 1, "docID": "S100ABCD", "edinetCode": "E02144", "secCode": "72030",
       "filerName": "トヨタ自動車株式会社", "docTypeCode": "120", "issuerEdinetCode": None,
       "subjectEdinetCode": None, "submitDateTime": "2026-06-20 09:00",
       "periodEnd": "2026-03-31", "docDescription": "有価証券報告書－第122期",
       "csvFlag": "1", "withdrawalStatus": "0"}


def filing(doc_id, day, **fields):
    """A documents.json result row; defaults to a fund filing with no securities code."""
    return {"seqNumber": 1, "docID": doc_id, "edinetCode": "G00001", "secCode": None,
            "filerName": "Some Fund", "docTypeCode": "030", "issuerEdinetCode": None,
            "subjectEdinetCode": None, "submitDateTime": f"{day} 15:00", "periodEnd": None,
            "docDescription": "有価証券届出書", "csvFlag": "0", "withdrawalStatus": "0", **fields}


class FakeEdinet:
    def __init__(self, docs: dict[str, list[dict]] | None = None):
        """docs: date -> results. When given, dates not in it get one filler filing each;
        when None, every date returns DOC."""
        self.docs = docs
        self.calls: list[httpx.Request] = []
        self.dates: list[str] = []  # dates asked of documents.json
        self.fail_5xx = 0  # respond 503 to this many calls first
        self.bad_status = None  # metadata.status to send instead of "200"
        # doc_id -> CSV files served by /documents/{id}?type=5; others are a 404
        self.csvs: dict[str, dict] = {"S100ABCD": CSV_FILES}
        self.fail_docs: set[str] = set()  # downloads that always answer 503

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        path, p = req.url.path.removeprefix("/api/v2"), dict(req.url.params)
        if self.fail_5xx:
            self.fail_5xx -= 1
            return httpx.Response(503, text="Service Unavailable")
        if p.get("Subscription-Key") != KEY:
            return httpx.Response(401, json={"StatusCode": 401, "message": "Access denied due to invalid subscription key."})
        if path == "/documents.json":
            status = self.bad_status or "200"
            meta = {"title": "提出された書類を把握するためのAPI", "parameter": {"date": p["date"], "type": p.get("type")},
                    "resultset": {"count": 1}, "processDateTime": "2026-06-20 10:00",
                    "status": status, "message": "OK" if status == "200" else "Bad Request"}
            body = {"metadata": meta}
            self.dates.append(p["date"])
            if status == "200":
                if p.get("type") != "2":
                    body["results"] = []
                elif self.docs is None:
                    body["results"] = [DOC]
                else:
                    day = p["date"]
                    body["results"] = self.docs.get(day, [filing(f"S1F{day}", day)])
            return httpx.Response(200, json=body)
        doc_id = path.removeprefix("/documents/")
        if doc_id in self.fail_docs:
            return httpx.Response(503, text="Service Unavailable")
        if doc_id in self.csvs and p.get("type") == "5":
            return httpx.Response(200, content=csv_zip(self.csvs[doc_id]),
                                  headers={"content-type": "application/octet-stream"})
        if path.startswith("/documents/"):
            return httpx.Response(200, json={"metadata": {"title": "", "status": "404",
                                                          "message": "Not Found"}})
        return httpx.Response(404, text="unknown")
