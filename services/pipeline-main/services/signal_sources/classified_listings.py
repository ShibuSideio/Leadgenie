"""
Classified Listing Source — V25.1.0
=====================================
Dedicated B2C/D2C signal source for classified ad sites, property portals,
expat community boards, and consumer forums.

These are the HIGHEST QUALITY B2C intent signals because the buyer is
explicitly writing "I need X" or "Looking for Y" — not discussing a topic,
but actively searching for a product or service.

Signal categories:
  1. WANT/NEED ADS — Buyers posting what they are looking for
       "Looking for 3BR villa Muscat" → expatriates.com wanted section
       "WTB: used laptop Dubai" → dubizzle.com wanted ads
  2. SERVICE INQUIRIES — Consumers asking for vendor recommendations
       "Anyone recommend interior designer in Oman?" → expat forums
  3. CONSUMER PAIN POSTS — Frustration with current solution
       "Terrible experience with X, need alternative"

RSS-accessible B2C classified/forum sites:
  - expatriates.com  → /oman/, /uae/, /india/ etc. + /search/ feeds
  - expat.com        → country-specific community feeds
  - dubizzle.com     → "Wanted" section RSS (UAE)
  - propertyfinder.ae → search result RSS
  - bayut.com        → listing search RSS
  - olx.com.*        → country-specific classified RSS
  - JustProperty     → Oman/UAE property RSS
  - numbeo.com       → cost-of-living forum RSS
  - Google News RSS  → news-triggered consumer buying events

For walled-garden B2C sources (Facebook Groups, WhatsApp Communities),
use SerperDiscoverySource with queries like:
  "site:facebook.com Oman villa rent 2024"  (limited but indexed public posts)
  "site:reddit.com r/oman OR r/expats rent"

Architecture:
  This source delegates to RssFeedSource for feed parsing.
  It provides the archetype-appropriate feed URL templates.
  All feed URLs are dynamically built from geo + keywords — no hardcoding.
"""
from __future__ import annotations

import time
from typing import Optional
from urllib.parse import quote_plus

from core.logging import get_logger                                    # type: ignore[import]
from services.signal_sources.base import BaseSignalSource, SignalItem  # type: ignore[import]
from services.signal_sources.rss_feed import RssFeedSource             # type: ignore[import]

log = get_logger("pipeline.signal_sources.classified_listings")


class ClassifiedListingSource(BaseSignalSource):
    """Intent signal discovery from classified ads and consumer forum boards.

    Targets consumer-intent platforms where buyers directly post what they
    want, need, or are looking for. The full listing/post is retrieved for
    scoring — not just the title.

    Signal quality ranking (highest to lowest):
      1. WANTED ADS: "Looking for 3BR villa Muscat budget 300 OMR"
      2. SERVICE REQUESTS: "Need reliable moving company Oman recommendations"
      3. COMPLAINT + SWITCH INTENT: "Landlord issues, need new place urgently"

    Args:
        categories:        List of product/service category terms (e.g. ["villa",
                           "apartment", "office space"]). Used to filter RSS results.
        geo:               Geographic focus (e.g. "Oman", "Dubai", "India").
                           Used to build feed URLs and filter results.
        platform_types:    Platform categories to include. Options:
                           "property"   — real estate portals (Bayut, Propertyfinder, OLX)
                           "expat"      — expat community boards (expatriates.com, expat.com)
                           "classified" — general classified sites (Dubizzle, OLX)
                           "news"       — Google News consumer event feed
                           Default: all four.
        keyword_filters:   Optional additional keyword filters beyond categories.
        max_age_days:      Discard signals older than this many days.
        max_per_source:    Maximum signals returned per discover() call.
    """

    source_type = "classified_listing"

    _GEO_TO_EXPAT_PATH: dict[str, str] = {
        "oman":         "oman",
        "muscat":       "oman",
        "uae":          "uae",
        "dubai":        "uae",
        "abu dhabi":    "uae",
        "india":        "india",
        "mumbai":       "india",
        "bangalore":    "india",
        "bengaluru":    "india",
        "delhi":        "india",
        "qatar":        "qatar",
        "doha":         "qatar",
        "saudi arabia": "saudi-arabia",
        "riyadh":       "saudi-arabia",
        "bahrain":      "bahrain",
        "kuwait":       "kuwait",
        "singapore":    "singapore",
        "malaysia":     "malaysia",
        "kuala lumpur": "malaysia",
    }

    def __init__(
        self,
        categories: list[str],
        geo: str = "",
        platform_types: Optional[list[str]] = None,
        keyword_filters: Optional[list[str]] = None,
        max_age_days: int = 14,
        max_per_source: int = 40,
    ) -> None:
        self._categories      = categories or []
        self._geo             = geo.strip()
        self._platform_types  = set(platform_types or ["property", "expat", "classified", "news"])
        self._keyword_filters = keyword_filters or []
        self._max_age_days    = max_age_days
        self._max_per_source  = max_per_source

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def discover(self) -> list[SignalItem]:
        """Collect classified listing signals from all configured platforms."""
        feed_urls = self._build_feed_urls()

        if not feed_urls:
            log.warning(
                "classified_no_feeds",
                geo=self._geo or "global",
                categories=self._categories[:3],
            )
            return []

        # Combine category terms + keyword filters for RSS filtering
        all_keywords = list(set(
            [c.lower() for c in self._categories]
            + [k.lower() for k in self._keyword_filters]
        ))

        rss = RssFeedSource(
            feed_urls       = feed_urls,
            keyword_filters = all_keywords if all_keywords else None,
            geo_terms       = [self._geo.lower()] if self._geo else None,
            max_age_days    = self._max_age_days,
            max_per_source  = self._max_per_source,
        )
        raw = rss.discover()

        # Tag as classified_listing and mark as needing PRISM enrichment
        signals: list[SignalItem] = []
        seen: set[str] = set()
        for sig in raw:
            if sig.url and sig.url not in seen:
                seen.add(sig.url)
                signals.append(SignalItem(
                    url         = sig.url,
                    text        = sig.text,
                    title       = sig.title,
                    author      = sig.author,
                    source_type = self.source_type,
                    fetched_at  = sig.fetched_at or self._now_iso(),
                    metadata    = {
                        **sig.metadata,
                        "geo":             self._geo,
                        "categories":      self._categories,
                        "is_thin_content": len(sig.text) < 200,
                        "platform_types":  list(self._platform_types),
                    },
                ))

        log.info(
            "classified_discover_complete",
            geo=self._geo or "global",
            feeds=len(feed_urls),
            signals_found=len(signals),
        )
        return signals[: self._max_per_source]

    # ------------------------------------------------------------------
    # Feed URL builders (all dynamic from geo + categories)
    # ------------------------------------------------------------------

    def _build_feed_urls(self) -> list[str]:
        """Build RSS feed URLs for all configured platform types."""
        urls: list[str] = []

        if "news" in self._platform_types:
            urls.extend(self._build_google_news_feeds())

        if "expat" in self._platform_types:
            urls.extend(self._build_expat_feeds())

        if "property" in self._platform_types:
            urls.extend(self._build_property_feeds())

        if "classified" in self._platform_types:
            urls.extend(self._build_classified_feeds())

        return urls

    def _build_google_news_feeds(self) -> list[str]:
        """Google News RSS — surfaces consumer buying event coverage."""
        feeds: list[str] = []
        geo_part = f" {self._geo}" if self._geo else ""

        # Intent-rich consumer queries
        intent_queries = [
            f"looking for{geo_part} {cat}" for cat in self._categories[:2]
        ] + [
            f"need {cat} recommendation{geo_part}" for cat in self._categories[:2]
        ]

        for q in intent_queries[:3]:
            feeds.append(
                f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en&gl=US&ceid=US:en"
            )
        return feeds

    def _build_expat_feeds(self) -> list[str]:
        """Build expatriates.com and expat.com RSS feeds."""
        feeds: list[str] = []
        geo_lower = self._geo.lower()

        # Resolve geo to expatriates.com path
        expat_path = self._GEO_TO_EXPAT_PATH.get(geo_lower, "")

        if expat_path:
            # expatriates.com — largest expat classifieds forum in the Middle East
            # "Wanted" sections have direct buyer intent ("looking for", "need")
            feeds.append(f"https://www.expatriates.com/classifieds/{expat_path}/")
            # Also try the search feed with category terms
            for cat in self._categories[:2]:
                encoded = quote_plus(f"{cat} {self._geo}")
                feeds.append(
                    f"https://news.google.com/rss/search?q=site:expatriates.com+{encoded}&hl=en"
                )

        # expat.com — global expat community
        if geo_lower:
            feeds.append(
                f"https://news.google.com/rss/search?q=site:expat.com+{quote_plus(self._geo)}+{quote_plus(' '.join(self._categories[:2]))}&hl=en"
            )

        return feeds

    def _build_property_feeds(self) -> list[str]:
        """Build property portal feeds for real estate ICPs."""
        feeds: list[str] = []
        geo_lower = self._geo.lower()

        # Only activate property feeds when ICP is clearly real estate
        property_terms = {
            "villa", "apartment", "flat", "house", "property", "rent",
            "buy", "sale", "lease", "studio", "bedroom", "office space",
            "commercial", "warehouse", "shop", "retail space",
        }
        is_property_icp = any(
            cat.lower() in property_terms for cat in self._categories
        )
        if not is_property_icp:
            return []

        # Propertyfinder.ae (UAE + Oman) — has search result RSS
        if any(g in geo_lower for g in ["uae", "dubai", "oman", "muscat", "abu dhabi"]):
            for cat in self._categories[:2]:
                encoded_cat = quote_plus(cat)
                encoded_geo = quote_plus(self._geo)
                # Google News indexes propertyfinder listings
                feeds.append(
                    f"https://news.google.com/rss/search?q=site:propertyfinder.ae+{encoded_geo}+{encoded_cat}&hl=en"
                )

        # Bayut (UAE/Oman listings)
        if any(g in geo_lower for g in ["uae", "dubai", "oman"]):
            for cat in self._categories[:1]:
                feeds.append(
                    f"https://news.google.com/rss/search?q=site:bayut.com+{quote_plus(self._geo)}+{quote_plus(cat)}&hl=en"
                )

        # India property — 99acres, MagicBricks
        if any(g in geo_lower for g in ["india", "mumbai", "bangalore", "delhi", "bengaluru"]):
            for cat in self._categories[:1]:
                feeds.append(
                    f"https://news.google.com/rss/search?q=site:99acres.com+{quote_plus(self._geo)}+{quote_plus(cat)}&hl=en"
                )

        return feeds

    def _build_classified_feeds(self) -> list[str]:
        """Build general classified ad site feeds (OLX, Dubizzle, etc.)."""
        feeds: list[str] = []
        geo_lower = self._geo.lower()

        for cat in self._categories[:2]:
            encoded_query = quote_plus(f"looking for {cat} {self._geo}")

            # Dubizzle (UAE, Oman, wider MENA)
            if any(g in geo_lower for g in ["uae", "dubai", "oman", "muscat", "bahrain", "qatar"]):
                feeds.append(
                    f"https://news.google.com/rss/search?q=site:dubizzle.com+{quote_plus(self._geo)}+{quote_plus(cat)}&hl=en"
                )

            # OLX — global classifieds (India, South Asia, MENA)
            if any(g in geo_lower for g in ["india", "pakistan", "egypt", "saudi"]):
                feeds.append(
                    f"https://news.google.com/rss/search?q=site:olx.com+{quote_plus(self._geo)}+{quote_plus(cat)}&hl=en"
                )

        return feeds
