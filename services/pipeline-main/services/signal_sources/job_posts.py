"""
Job Post Signal Source — V25.1.0

Job postings are the highest-confidence B2B buying signal:
  - A company posting a role they cannot fill = capability gap = pain
  - The job description contains the buyer's exact pain language
  - The hiring manager is typically the decision maker
  - Salary range approximates project budget

Sources used (all via RSS — no auth, no scraping):
  1. Indeed RSS:
       https://www.indeed.com/rss?q={keywords}&l={location}&sort=date&fromage={days}
  2. Google News RSS (discovers LinkedIn/Glassdoor job posts via news index):
       https://news.google.com/rss/search?q={role}+{geo}+job+posting&hl=en

Note on LinkedIn: LinkedIn's job board does not publish public RSS.
Google News RSS surfaces LinkedIn job links when they appear in news context.
For direct LinkedIn job data, use the Serper Discovery source with
site:linkedin.com/jobs queries.

The signal text for job posts is the RSS description, which typically
contains the first 200-400 chars of the job description. PRISM scraping
the job URL retrieves the full description.
"""
from __future__ import annotations

import time
from typing import Optional
from urllib.parse import urlencode, quote_plus

from core.logging import get_logger                                    # type: ignore[import]
from services.signal_sources.base import BaseSignalSource, SignalItem  # type: ignore[import]
from services.signal_sources.rss_feed import RssFeedSource             # type: ignore[import]

log = get_logger("pipeline.signal_sources.job_posts")


class JobPostSource(BaseSignalSource):
    """Job posting signal discovery via RSS feeds.

    Job posts signal that a company has an unfilled capability — a direct
    buying trigger. The role keyword and location are fully dynamic.

    Args:
        role_keywords:  List of job role keywords (e.g. ["Head of Brand",
                        "CMO", "Senior Brand Manager"]). Each generates
                        independent RSS queries.
        geo:            Geographic location for job search (e.g. "India",
                        "Dubai", "Oman"). Empty = no geo filter.
        max_age_days:   Only return postings from this many days ago. Default 30.
        max_per_source: Maximum signals returned per discover() call.
    """

    source_type = "job_post"

    def __init__(
        self,
        role_keywords: list[str],
        geo: str = "",
        max_age_days: int = 30,
        max_per_source: int = 40,
    ) -> None:
        self._role_keywords  = role_keywords or []
        self._geo            = geo.strip()
        self._max_age_days   = max_age_days
        self._max_per_source = max_per_source

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def discover(self) -> list[SignalItem]:
        """Fetch job posting signals for all configured role keywords."""
        seen_urls: set[str] = set()
        signals:   list[SignalItem] = []

        for keyword in self._role_keywords:
            if len(signals) >= self._max_per_source:
                break

            feed_urls = self._build_feed_urls(keyword)
            try:
                rss = RssFeedSource(
                    feed_urls   = feed_urls,
                    max_age_days = self._max_age_days,
                )
                batch = rss.discover()
                for item in batch:
                    # Override source_type to "job_post" and tag with keyword
                    job_item = SignalItem(
                        url         = item.url,
                        text        = item.text,
                        title       = item.title,
                        author      = item.author,
                        source_type = self.source_type,
                        fetched_at  = item.fetched_at or self._now_iso(),
                        metadata    = {
                            **item.metadata,
                            "role_keyword":    keyword,
                            "geo":             self._geo,
                            "is_thin_content": True,  # Job RSS = partial, PRISM for full JD
                        },
                    )
                    if job_item.url and job_item.url not in seen_urls:
                        seen_urls.add(job_item.url)
                        signals.append(job_item)
            except Exception as exc:
                log.warning(
                    "job_post_source_failed",
                    keyword=keyword[:80],
                    geo=self._geo,
                    error=str(exc),
                )
            time.sleep(0.5)

        log.info(
            "job_posts_discover_complete",
            keywords=len(self._role_keywords),
            signals_found=len(signals),
        )
        return signals[: self._max_per_source]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_feed_urls(self, keyword: str) -> list[str]:
        """Build RSS URLs for Indeed and Google News for a given keyword."""
        feeds: list[str] = []

        # 1. Indeed RSS
        indeed_params: dict = {
            "q":       keyword,
            "sort":    "date",
            "fromage": str(self._max_age_days),
            "limit":   "20",
        }
        if self._geo:
            indeed_params["l"] = self._geo
        feeds.append(f"https://www.indeed.com/rss?{urlencode(indeed_params)}")

        # 2. Google News RSS — discovers LinkedIn, Glassdoor, and press release job posts
        gnews_query = f'"{keyword}" job OR hiring OR "we are looking for"'
        if self._geo:
            gnews_query += f" {self._geo}"
        gnews_url = (
            f"https://news.google.com/rss/search?q={quote_plus(gnews_query)}"
            f"&hl=en&gl=US&ceid=US:en"
        )
        feeds.append(gnews_url)

        return feeds
