"""A tiny stand-in for the J-Quants API, served through httpx.MockTransport."""

from datetime import date, timedelta

import httpx

from jquantster.db import INVESTOR_VALUE_COLS


def bars_for(code, d, close):
    return {"Date": d, "Code": code, "O": close, "H": close + 5, "L": close - 5, "C": close,
            "Vo": 1000.0, "Va": close * 1000 * 1e3, "AdjFactor": 1.0, "AdjO": close,
            "AdjH": close + 5, "AdjL": close - 5, "AdjC": close, "AdjVo": 1000.0}


class FakeAPI:
    def __init__(self, entitled=("calendar", "master", "bars", "fins")):
        self.entitled = set(entitled)
        self.calls = []
        self.fail_429_once = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        path, p = req.url.path.removeprefix("/v2"), dict(req.url.params)
        if req.headers.get("x-api-key") != "k":
            return httpx.Response(403, json={"message": "The incoming api key is invalid or expired."})
        if self.fail_429_once:
            self.fail_429_once = False
            return httpx.Response(429, json={"message": "Too Many Requests"})
        if path == "/markets/calendar":
            start, end = date.fromisoformat(p["from"]), date.fromisoformat(p["to"])
            days = [start + timedelta(i) for i in range((end - start).days + 1)]
            return httpx.Response(200, json={"data": [
                {"Date": d.isoformat(), "HolDiv": "1" if d.weekday() < 5 else "0"} for d in days]})
        if path == "/equities/master":
            return httpx.Response(200, json={"data": [
                {"Date": p.get("date", "2026-01-01"), "Code": f"{1000 + i}0", "CoName": f"会社{i}",
                 "CoNameEn": f"Company {i}", "S33Nm": "Sector", "MktNm": "Prime"} for i in range(1200)]})
        if path == "/equities/bars/daily":
            if "code" in p:
                code = p["code"] + ("0" if len(p["code"]) == 4 else "")
                start, end = date.fromisoformat(p["from"]), date.fromisoformat(p["to"])
                days = [start + timedelta(i) for i in range((end - start).days + 1)]
                rows = [bars_for(code, d.isoformat(), 1000 + i) for i, d in enumerate(days) if d.weekday() < 5]
                # paginate: first half then second half
                if "pagination_key" not in p:
                    return httpx.Response(200, json={"data": rows[: len(rows) // 2], "pagination_key": "next"})
                return httpx.Response(200, json={"data": rows[len(rows) // 2:]})
            day = date.fromisoformat(p["date"])
            return httpx.Response(200, json={"data": [
                bars_for(f"{1000 + i}0", p["date"], 100 + i + day.day) for i in range(1200)]})
        if path == "/fins/summary":
            if "fins" not in self.entitled:
                return httpx.Response(403, json={"message": "This API is not available on your plan."})
            return httpx.Response(200, json={"data": [
                {"Code": p["code"] + "0", "DiscNo": "1", "DiscDate": "2026-05-10", "DocType": "FY",
                 "CurPerType": "FY", "Sales": "1000000000", "OP": "100000000", "NP": "50000000", "EPS": "12.5"}]})
        if path == "/equities/investor-types":
            if "investor_types" not in self.entitled:
                return httpx.Response(403, json={"message": "This API is not available on your plan."})
            row = {c: 1_000_000.0 for c in INVESTOR_VALUE_COLS}
            weeks = []
            for i in range(10):
                st = date(2026, 6, 1) + timedelta(weeks=i)
                weeks.append({**row, "Section": "TSEPrime", "StDate": st.isoformat(),
                              "EnDate": (st + timedelta(4)).isoformat(),
                              "PubDate": (st + timedelta(10)).isoformat(),
                              "FrgnBal": (i - 4) * 1e6})
            # a correction of week 0, published later with a different number
            weeks.append({**weeks[0], "PubDate": "2026-09-01", "FrgnBal": 9e6})
            return httpx.Response(200, json={"data": weeks})
        return httpx.Response(400, json={"message": f"unknown {path}"})
