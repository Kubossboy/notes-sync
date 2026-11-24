"""SEC EDGAR (free, official): company filings and Form 4 insider transactions.

Fair-access policy: descriptive User-Agent with contact e-mail, <= 10 req/s.
Filings come from data.sec.gov/submissions (structured; 8-K item codes included),
so classification needs no document scraping. Form 4 XML is parsed for
transaction codes (P = open-market purchase, S = open-market sale).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from app.core.safety import assert_url_allowed

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

# 8-K items -> (category, direction, impact). direction: +1 / -1 / 0 (depends on content)
ITEM_MAP = {
    "1.01": ("material_agreement", 0, "medium"),
    "1.02": ("agreement_terminated", -1, "medium"),
    "1.03": ("bankruptcy", -1, "high"),
    "1.05": ("cybersecurity_incident", -1, "high"),
    "2.01": ("acquisition_completed", 0, "medium"),
    "2.02": ("earnings", 0, "high"),
    "2.03": ("new_debt", -1, "low"),
    "2.04": ("debt_acceleration", -1, "high"),
    "2.05": ("restructuring", -1, "medium"),
    "2.06": ("impairment", -1, "medium"),
    "3.01": ("delisting_notice", -1, "high"),
    "3.02": ("unregistered_equity_sale", -1, "medium"),
    "4.01": ("auditor_change", -1, "medium"),
    "4.02": ("financials_non_reliance", -1, "high"),
    "5.01": ("change_of_control", 0, "high"),
    "5.02": ("executive_change", 0, "medium"),
    "7.01": ("reg_fd_disclosure", 0, "low"),
    "8.01": ("other_event", 0, "low"),
}
UNEXPECTED_ITEMS = {"1.03", "1.05", "2.04", "3.01", "4.02"}
FORM_MAP = {
    "10-Q": ("quarterly_report", 0, "medium"), "10-K": ("annual_report", 0, "medium"),
    "S-1": ("equity_offering", -1, "medium"), "S-3": ("shelf_registration", -1, "low"),
    "424B5": ("equity_offering", -1, "medium"), "SC 13D": ("activist_stake", 1, "medium"),
    "SC 13G": ("passive_stake", 0, "low"), "4": ("insider_transaction", 0, "low"),
}


@dataclass
class Filing:
    ticker: str
    cik: int
    form: str
    accession: str
    accepted: datetime
    items: list[str]
    primary_doc: str
    description: str

    @property
    def url(self) -> str:
        return ARCHIVE_URL.format(cik=self.cik, acc=self.accession.replace("-", ""), doc=self.primary_doc)


class SecClient:
    def __init__(self, session: requests.Session, contact_email: str | None, cache_dir: Path,
                 timeout: float = 10):
        self.session, self.timeout = session, timeout
        self.ua = f"ATLAS-Trading-AI research {contact_email or 'atlas-paper-research@localhost'}"
        self.cache_dir = cache_dir
        self._last = 0.0
        self._cik: dict[str, int] | None = None
        self._names: dict[str, str] = {}

    def _get(self, url: str):
        assert_url_allowed(url)
        wait = 0.12 - (time.monotonic() - self._last)     # <= ~8 req/s
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        r = self.session.get(url, timeout=self.timeout,
                             headers={"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"})
        if r.status_code != 200:
            raise ConnectionError(f"SEC HTTP {r.status_code} for {url}")
        return r

    # ---------------- ticker <-> CIK (cached for 24h)
    def ticker_map(self) -> dict[str, int]:
        if self._cik is not None:
            return self._cik
        cache = self.cache_dir / "sec_company_tickers.json"
        data = None
        if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
            data = json.loads(cache.read_text())
        if data is None:
            data = self._get(TICKERS_URL).json()
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data))
        self._cik = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
        self._names = {v["ticker"].upper(): v["title"] for v in data.values()}
        return self._cik

    def company_name(self, ticker: str) -> str | None:
        self.ticker_map()
        return self._names.get(ticker.upper())

    # ---------------- filings
    def recent_filings(self, ticker: str, since: datetime) -> list[Filing]:
        cik = self.ticker_map().get(ticker.upper())
        if cik is None:
            return []           # ETFs like SPY/QQQ have no company filings of this kind
        j = self._get(SUBMISSIONS_URL.format(cik=cik)).json()
        r = j["filings"]["recent"]
        out = []
        for i, form in enumerate(r["form"]):
            acc_ts = datetime.fromisoformat(r["acceptanceDateTime"][i].replace("Z", "+00:00"))
            if acc_ts < since:
                break            # sorted newest first
            out.append(Filing(ticker.upper(), cik, form, r["accessionNumber"][i], acc_ts,
                              [x for x in (r["items"][i] or "").split(",") if x], r["primaryDocument"][i],
                              r["primaryDocDescription"][i] or form))
        return out

    def form4_transactions(self, f: Filing) -> list[dict]:
        doc = f.primary_doc.split("/")[-1]        # strip the XSL rendering folder -> raw XML
        t = self._get(ARCHIVE_URL.format(cik=f.cik, acc=f.accession.replace("-", ""), doc=doc)).text
        return parse_form4(t)


def _vals(block: str, tag: str) -> list[str]:
    return [m.strip() for m in re.findall(rf"<{tag}>\s*(?:<value>)?([^<]*)", block)]


def parse_form4(xml: str) -> list[dict]:
    owner = (_vals(xml, "rptOwnerName") or [""])[0]
    title = (_vals(xml, "officerTitle") or [""])[0]
    is_dir = (_vals(xml, "isDirector") or ["0"])[0] in ("1", "true")
    is_off = (_vals(xml, "isOfficer") or ["0"])[0] in ("1", "true")
    out = []
    for block in re.findall(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", xml, re.S):
        code = (_vals(block, "transactionCode") or [""])[0]
        try:
            shares = float((_vals(block, "transactionShares") or ["0"])[0] or 0)
            price = float((_vals(block, "transactionPricePerShare") or ["0"])[0] or 0)
        except ValueError:
            continue
        out.append({"owner": owner, "title": title, "is_director": is_dir, "is_officer": is_off,
                    "code": code, "shares": shares, "price": price,
                    "acq_disp": (_vals(block, "transactionAcquiredDisposedCode") or [""])[0],
                    "date": (_vals(block, "transactionDate") or [""])[0],
                    "plan_10b5_1": "10b5-1" in xml})
    return out
