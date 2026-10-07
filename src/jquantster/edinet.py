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
            if resp.status_code != 200:
                raise JQuantsError(f"EDINET {resp.status_code} on {path}: {_message(resp)}")
            # Errors also arrive as HTTP 200 with a JSON body whose metadata.status says otherwise.
            if resp.headers.get("content-type", "").startswith("application/json"):
                meta = resp.json().get("metadata", {})
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


def _message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message", ""))
    except ValueError:
        return resp.text[:200]
