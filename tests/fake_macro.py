"""Stand-ins for the MOF JGB CSVs and the BOJ time-series API, served through httpx.MockTransport.
Fixtures are trimmed from the live responses of 2026-10-09."""

import httpx

from jquantster.config import BOJ_BASE_URL, JGB_CURRENT_URL, JGB_HISTORY_URL

_HEADER = "Date,1Y,2Y,3Y,4Y,5Y,6Y,7Y,8Y,9Y,10Y,15Y,20Y,25Y,30Y,40Y"
# The footer note is Shift_JIS-ish bytes in the real file; any non-date row ends the data.
_FOOTER = (",,,,,,,,,,,,,,,\r\n"
           '"  \x81\xa6If you cannot download the latest csv data, please clear the browser\'s '
           'cache and download again.",,,,,,,,,,,,,,,\r\n').encode("latin-1")


def jgb_csv(title: str, rows: list[str]) -> bytes:
    head = f"{title},,,,,,,,,,,,,,,(Unit : %)\r\n{_HEADER}\r\n"
    return (head + "".join(r + "\r\n" for r in rows)).encode("ascii") + _FOOTER


JGB_HISTORY = jgb_csv("Interest Rate", [
    "1974/9/24,10.327,9.362,8.83,8.515,8.348,8.29,8.24,8.121,8.127,-,-,-,-,-,-",
    "2026/9/29,1.692,1.976,2.116,2.302,2.425,2.545,2.662,2.818,2.946,3.082,3.603,3.887,4.144,4.126,4.127",
    "2026/9/30,1.684,1.952,2.086,2.273,2.399,2.525,2.639,2.796,2.926,3.057,3.583,3.877,4.131,4.098,4.099",
])
JGB_CURRENT = jgb_csv("Interest Rate (October 2026)", [
    "2026/10/1,1.668,1.939,2.077,2.274,2.407,2.534,2.657,2.82,2.952,3.092,3.62,3.91,4.154,4.122,4.125",
    "2026/10/2,1.65,1.919,2.065,2.259,2.397,2.528,2.657,2.821,2.957,3.097,3.626,3.925,4.171,4.148,4.168",
])

SERIES = {
    ("FM08", "FXERD01"): {
        "NAME_OF_TIME_SERIES": "US.Dollar/Yen Spot Rate at 9:00 in JST, Tokyo Market",
        "UNIT": "Yen per U.S. Dollar", "FREQUENCY": "DAILY",
        "CATEGORY": "Foreign Exchange Rates", "LAST_UPDATE": 20261009,
        "VALUES": {"SURVEY_DATES": [20261001, 20261002, 20261003, 20261004, 20261005],
                   "VALUES": [157.56, 157.94, None, None, 157.74]}},
    ("FM01", "STRDCLUCON"): {
        "NAME_OF_TIME_SERIES": "Call Rate, Uncollateralized Overnight, Average (Daily)",
        "UNIT": "percent per annum", "FREQUENCY": "DAILY", "CATEGORY": "Call Rate",
        "LAST_UPDATE": 20261009,
        "VALUES": {"SURVEY_DATES": [20260922, 20260923, 20260924],
                   "VALUES": [None, None, 1.227]}},
    ("CO", "TK99F1000601GCQ01000"): {
        "NAME_OF_TIME_SERIES": "D.I./Business Conditions/Large Enterprises/Manufacturing/Actual result",
        "UNIT": "% points", "FREQUENCY": "QUARTERLY", "CATEGORY": "TANKAN",
        "LAST_UPDATE": 20261002,
        "VALUES": {"SURVEY_DATES": [202504, 202601, 202602, 202603],
                   "VALUES": [15, 17, 22, 24]}},
    ("CO", "TK99F2000601GCQ11000"): {
        "NAME_OF_TIME_SERIES": "D.I./Business Conditions/Large Enterprises/Nonmanufacturing/Forecast",
        "UNIT": "% points", "FREQUENCY": "QUARTERLY", "CATEGORY": "TANKAN",
        "LAST_UPDATE": 20261002,
        "VALUES": {"SURVEY_DATES": [202603, 202604], "VALUES": [28, 30]}},
}


def boj_ok(db: str, results: list[dict], next_position=None) -> dict:
    return {"STATUS": 200, "MESSAGEID": "M181000I", "MESSAGE": "Successfully completed",
            "DATE": "2026-10-09T14:20:52.901+09:00",
            "PARAMETER": {"FORMAT": "JSON", "LANG": "EN", "DB": db},
            "NEXTPOSITION": next_position, "RESULTSET": results}


class FakeMacro:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.files = {JGB_HISTORY_URL: JGB_HISTORY, JGB_CURRENT_URL: JGB_CURRENT}
        self.series = dict(SERIES)
        self.fail_5xx = 0
        self.page_size = 0  # >0: split each series' values into pages of this many

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if self.fail_5xx:
            self.fail_5xx -= 1
            return httpx.Response(503, text="Service Unavailable")
        url = str(req.url.copy_with(query=None))
        if url in self.files:
            return httpx.Response(200, content=self.files[url],
                                  headers={"content-type": "text/csv"})
        if url == f"{BOJ_BASE_URL}/getDataCode":
            return self._data_code(dict(req.url.params))
        return httpx.Response(404, text="Not Found")

    def _data_code(self, p: dict) -> httpx.Response:
        db, codes = p["db"], p["code"].split(",")
        missing = [c for c in codes if (db, c) not in self.series]
        if missing:
            return httpx.Response(400, json={
                "STATUS": 400, "MESSAGEID": "M181013E",
                "MESSAGE": f"Nonexistent series code：{codes.index(missing[0]) + 1}",
                "DATE": "2026-10-09T14:24:18.973+09:00"})
        start = int(p.get("startPosition") or 0)
        results, nxt = [], None
        for c in codes:
            s = self.series[(db, c)]
            dates, values = s["VALUES"]["SURVEY_DATES"], s["VALUES"]["VALUES"]
            if self.page_size:
                end = start + self.page_size
                if end < len(dates):
                    nxt = end
                dates, values = dates[start:end], values[start:end]
            results.append({"SERIES_CODE": c, **s,
                            "VALUES": {"SURVEY_DATES": dates, "VALUES": values}})
        return httpx.Response(200, json=boj_ok(db, results, nxt))
