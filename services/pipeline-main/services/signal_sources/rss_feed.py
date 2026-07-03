"""
RSS Feed Signal Source — V25.1.0

Generic RSS 2.0 and Atom 1.0 feed parser. Every major forum, news site,
blog, job board, and community platform publishes RSS/Atom feeds — this
module provides a single, robust implementation for consuming any of them.

No external feed-parsing library required. Uses Python stdlib
(xml.etree.ElementTree, urllib) plus requests for HTTP and BeautifulSoup
for HTML stripping — all already in requirements.txt.

Common signal-rich RSS sources this module can consume:
  - Forum categories (expatriates.com, justlanded.com, Stack Exchange tags)
  - Industry blogs (marketing, real estate, tech publications)
  - Google News search results (https://news.google.com/rss/search?q=...)
  - Indeed job search results (https://www.indeed.com/rss?q=...&l=...)
  - Product Hunt new products
  - Any custom forum feed
"""
from __future__ import annotations

import datetime
import email.utils
import time
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup  # type: ignore[import]
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

from core.logging import get_logger                                    # type: ignore[import]
from services.signal_sources.base import BaseSignalSource, SignalItem  # type: ignore[import]

log = get_logger("pipeline.signal_sources.rss_feed")

# XML namespace map — covers RSS 2.0 extensions and Atom
_NS = {
    "dc":      "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom":    "http://www.w3.org/2005/Atom",
    "media":   "http://search.yahoo.com/mrss/",
}
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT    = 20
_INTER_FEED_SLEEP = 0.5


class RssFeedSource(BaseSignalSource):
    """Intent signal discovery from any RSS 2.0 or Atom 1.0 feed.

    The source fetches each feed URL, parses the items/entries, strips HTML
    from description/content, applies optional keyword filtering and age
    filtering, and returns SignalItems.

    Args:
        feed_urls:       List of RSS or Atom feed URLs to parse.
        keyword_filters: Optional list of keywords. When provided, only items
                         where title+description contains at least one keyword
                         (case-insensitive) are returned.
        geo_terms:       Optional geographic keywords for geo filtering.
        max_age_days:    Discard items older than this many days. Default 14.
        max_per_source:  Maximum signals to return per discover() call.
    """

    source_type = "rss_feed"

    def __init__(
        self,
        feed_urls: list[str],
        keyword_filters: Optional[list[str]] = None,
        geo_terms: Optional[list[str]] = None,
        max_age_days: int = 14,
        max_per_source: int = 50,
    ) -> None:
        self._feed_urls      = feed_urls or []
        self._keyword_filters = [k.lower() for k in (keyword_filters or [])]
        self._geo_terms       = [g.lower() for g in (geo_terms or [])]
        self._max_age_days    = max_age_days
        self._max_per_source  = max_per_source
        self._cutoff          = datetime.datetime.utcnow() - datetime.timedelta(days=max_age_days)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def discover(self) -> list[SignalItem]:
        """Parse all configured feeds and return intent signals."""
        seen_urls: set[str] = set()
        signals:   list[SignalItem] = []

        for feed_url in self._feed_urls:
            if len(signals) >= self._max_per_source:
                break
            try:
                batch = self._parse_feed(feed_url)
                for item in batch:
                    if item.url and item.url not in seen_urls:
                        seen_urls.add(item.url)
                        signals.append(item)
            except Exception as exc:
                log.warning(
                    "rss_feed_failed",
                    feed_url=feed_url[:120],
                    error=str(exc),
                )
            time.sleep(_INTER_FEED_SLEEP)

        # Apply keyword and geo filters
        if self._keyword_filters:
            signals = [s for s in signals if self._matches_keywords(s)]
        if self._geo_terms:
            signals = [s for s in signals if self._matches_geo(s)]

        log.info(
            "rss_discover_complete",
            feeds=len(self._feed_urls),
            signals_found=len(signals),
        )
        return signals[: self._max_per_source]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=3),
        stop=stop_after_attempt(1),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
    )
    def _fetch_feed(self, feed_url: str) -> bytes:
        """Fetch raw feed bytes."""
        resp = requests.get(
            feed_url,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            headers={"User-Agent": "LeadGenie/25.1.0 RSS reader"},
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp.content

    def _parse_feed(self, feed_url: str) -> list[SignalItem]:
        """Detect format (RSS or Atom) and parse accordingly."""
        raw = self._fetch_feed(feed_url)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            log.warning("rss_parse_error", feed_url=feed_url[:120], error=str(exc))
            return []

        # Normalise tag — strip namespace prefix for format detection
        tag = root.tag.split("}")[-1].lower() if "}" in root.tag else root.tag.lower()

        if tag == "rss":
            return self._parse_rss(root, feed_url)
        if tag == "feed":
            return self._parse_atom(root, feed_url)

        log.warning("rss_unknown_format", feed_url=feed_url[:120], root_tag=root.tag)
        return []

    # ---- RSS 2.0 --------------------------------------------------------

    def _parse_rss(self, root: ET.Element, feed_url: str) -> list[SignalItem]:
        channel = root.find("channel")
        if channel is None:
            return []
        signals: list[SignalItem] = []
        for item_el in channel.findall("item"):
            sig = self._rss_item_to_signal(item_el, feed_url)
            if sig:
                signals.append(sig)
        return signals

    def _rss_item_to_signal(self, el: ET.Element, feed_url: str) -> Optional[SignalItem]:
        title       = self._text(el, "title")
        link        = self._text(el, "link")
        description = self._text(el, "description")
        content     = self._text(el, "content:encoded", ns="content")
        author      = self._text(el, "dc:creator", ns="dc") or self._text(el, "author")
        pub_date    = self._text(el, "pubDate")

        if not link:
            return None

        parsed_date = self._parse_rfc822(pub_date)
        if parsed_date and parsed_date < self._cutoff:
            return None

        body = self._strip_html(content or description)
        combined = f"{title}\n\n{body}".strip()

        return SignalItem(
            url         = link,
            text        = combined,
            title       = title,
            author      = author,
            source_type = self.source_type,
            fetched_at  = self._now_iso(),
            metadata    = {
                "feed_url":        feed_url,
                "pub_date":        pub_date,
                "is_thin_content": len(body) < 100,
            },
        )

    # ---- Atom 1.0 -------------------------------------------------------

    def _parse_atom(self, root: ET.Element, feed_url: str) -> list[SignalItem]:
        # Atom namespace is embedded in tag names
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        signals: list[SignalItem] = []
        for entry in root.findall(f"{ns}entry"):
            sig = self._atom_entry_to_signal(entry, ns, feed_url)
            if sig:
                signals.append(sig)
        return signals

    def _atom_entry_to_signal(
        self, el: ET.Element, ns: str, feed_url: str
    ) -> Optional[SignalItem]:
        title   = self._ns_text(el, f"{ns}title")
        updated = self._ns_text(el, f"{ns}updated")
        summary = self._ns_text(el, f"{ns}summary") or self._ns_text(el, f"{ns}content")
        author_el = el.find(f"{ns}author")
        author  = self._ns_text(author_el, f"{ns}name") if author_el is not None else ""

        # Link — prefer rel="alternate" or first link/@href
        link = ""
        for link_el in el.findall(f"{ns}link"):
            rel  = link_el.get("rel", "alternate")
            href = link_el.get("href", "")
            if rel == "alternate" and href:
                link = href
                break
        if not link:
            first = el.find(f"{ns}link")
            link = first.get("href", "") if first is not None else ""

        if not link:
            return None

        parsed_date = self._parse_iso(updated)
        if parsed_date and parsed_date < self._cutoff:
            return None

        body     = self._strip_html(summary or "")
        combined = f"{title}\n\n{body}".strip()

        return SignalItem(
            url         = link,
            text        = combined,
            title       = title,
            author      = author,
            source_type = self.source_type,
            fetched_at  = self._now_iso(),
            metadata    = {
                "feed_url":        feed_url,
                "pub_date":        updated,
                "is_thin_content": len(body) < 100,
            },
        )

    # ---- Utility --------------------------------------------------------

    def _text(self, el: ET.Element, tag: str, ns: str = "") -> str:
        """Get text of a child element, optionally with namespace."""
        if ns:
            namespace_uri = _NS.get(ns, "")
            child = el.find(f"{{{namespace_uri}}}{tag.split(':')[-1]}" if namespace_uri else tag)
        else:
            child = el.find(tag)
        return (child.text or "").strip() if child is not None else ""

    def _ns_text(self, el: Optional[ET.Element], tag: str) -> str:
        if el is None:
            return ""
        child = el.find(tag)
        return (child.text or "").strip() if child is not None else ""

    @staticmethod
    def _strip_html(html: str) -> str:
        if not html:
            return ""
        try:
            return BeautifulSoup(html, "html.parser").get_text(separator=" ").strip()
        except Exception:
            return html

    @staticmethod
    def _parse_rfc822(date_str: str) -> Optional[datetime.datetime]:
        """Parse RFC 822 date (RSS 2.0 pubDate format)."""
        if not date_str:
            return None
        try:
            parsed = email.utils.parsedate_to_datetime(date_str)
            return parsed.replace(tzinfo=None)
        except Exception:
            return None

    @staticmethod
    def _parse_iso(date_str: str) -> Optional[datetime.datetime]:
        """Parse ISO 8601 date (Atom format)."""
        if not date_str:
            return None
        try:
            # Strip timezone suffix for simple comparison
            clean = date_str.rstrip("Z").split("+")[0].split("-")
            # Re-join date parts correctly (yyyy-mm-dd)
            if len(clean) >= 3:
                date_part = f"{clean[0]}-{clean[1]}-{clean[2].split('T')[0]}"
                return datetime.datetime.strptime(date_part, "%Y-%m-%d")
        except Exception:
            pass
        return None

    def _matches_keywords(self, sig: SignalItem) -> bool:
        haystack = (sig.title + " " + sig.text).lower()
        return any(kw in haystack for kw in self._keyword_filters)

    def _matches_geo(self, sig: SignalItem) -> bool:
        haystack = (sig.title + " " + sig.text).lower()
        return any(term in haystack for term in self._geo_terms)
