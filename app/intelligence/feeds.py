"""Fetch and parse RSS 2.0 / Atom feeds (stdlib XML, no external parser)."""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from app.core.safety import assert_url_allowed

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass
class RawItem:
    source: str
    title: str
    url: str | None
    published: datetime | None
    summary: str
    guid: str | None = None
    extra: dict | None = None


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(_TAG.sub(" ", html.unescape(s)))
    return _WS.sub(" ", s).strip()


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)   # RFC822 without zone: assume UTC, flagged by caller? (rare)
    return d.astimezone(timezone.utc)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_text: str | bytes, source: str) -> list[RawItem]:
    try:
        data = xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text
        data = data.lstrip(b"\xef\xbb\xbf").lstrip()   # UTF-8 BOM / leading whitespace (federalreserve.gov)
        if data.startswith("\ufeff".encode("utf-8")):
            data = data[3:]
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"{source}: invalid XML: {e}")
    items = []
    for el in root.iter():
        name = _local(el.tag)
        if name not in ("item", "entry"):
            continue
        f: dict[str, str] = {}
        link = None
        for ch in el:
            n = _local(ch.tag)
            if n == "link":
                link = ch.get("href") or (ch.text or "").strip() or link
            elif n not in f:
                f[n] = ch.text or ""
        title = clean_text(f.get("title"))
        if not title:
            continue
        pub = parse_date(f.get("pubDate") or f.get("published") or f.get("updated") or f.get("date"))
        items.append(RawItem(source, title, (link or "").strip() or None, pub,
                             clean_text(f.get("description") or f.get("summary") or f.get("content"))[:1000],
                             (f.get("guid") or f.get("id") or "").strip() or None))
    return items


def fetch_feed(url: str, source: str, session: requests.Session, user_agent: str,
               timeout: float = 10) -> list[RawItem]:
    assert_url_allowed(url)
    r = session.get(url, timeout=timeout, headers={"User-Agent": user_agent,
                                                   "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    if r.status_code != 200:
        raise ConnectionError(f"{source}: HTTP {r.status_code}")
    return parse_feed(r.content, source)   # bytes: let the XML declaration pick the encoding
