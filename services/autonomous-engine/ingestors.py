"""
ingestors.py - V16 Autonomous Engine Data Adapters
===================================================
BaseIngestor: abstract contract all adapters must satisfy.
JobBoardIngestor: parses tech job RSS or mock_jobs.json.
FundingIngestor:  parses mock_funding.json (maps to paid API schema later).

All HTTP requests wrapped in tenacity exponential backoff.
On complete failure: logs "Yield: 0" and returns [] to keep pipeline alive.
"""

import os
import json
import logging
from abc import ABC, abstractmethod
from typing import List, Dict
from urllib.parse import urlparse

import httpx
import feedparser
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
JOB_FEED_URL  = os.environ.get("JOB_FEED_URL", "https://remoteok.io/remote-jobs.rss")
MOCK_MODE     = os.environ.get("MOCK_MODE", "true").lower() == "true"
_BASE_DIR     = os.path.dirname(__file__)

# Subdomains that indicate this URL belongs to the company, not an ATS
_JOB_SUBDOMAINS = {"jobs", "careers", "career", "apply", "recruiting", "talent", "hiring", "work"}

# ATS providers - the job is NOT hosted on the company domain
_ATS_PROVIDERS = {
    "greenhouse.io", "lever.co", "workday.com", "workdayjobs.com",
    "smartrecruiters.com", "ashbyhq.com", "breezy.hr", "bamboohr.com",
    "jobvite.com", "icims.com", "taleo.net", "successfactors.com",
    "remoteok.io", "weworkremotely.com"
}


# ── Base ──────────────────────────────────────────────────────────────────────
class BaseIngestor(ABC):
    """Abstract contract. All ingestors must implement fetch()."""

    @abstractmethod
    def fetch(self) -> List[Dict]:
        """Return a list of dicts, each containing at minimum company_domain."""
        ...


# ── Job Board Ingestor ────────────────────────────────────────────────────────
class JobBoardIngestor(BaseIngestor):
    """
    Parses a tech job board RSS feed or mock_jobs.json (MOCK_MODE=true).
    Extracts: company_domain, job_title.

    Domain extraction strategy (per Phase 3 ruling - Option A):
      1. Parse job posting URL hostname.
      2. Strip known job subdomains (jobs., careers., etc.) to get root domain.
      3. If URL is on an ATS provider domain, fallback to title heuristic
         ("Senior Engineer at Stripe" -> stripe.com).
    """

    def __init__(self, feed_url: str = JOB_FEED_URL):
        self.feed_url = feed_url

    def fetch(self) -> List[Dict]:
        if MOCK_MODE:
            return self._load_mock()
        try:
            return self._fetch_rss()
        except Exception as e:
            log.error(f"[JobBoard] Yield: 0 — complete failure: {e}")
            return []

    def _load_mock(self) -> List[Dict]:
        path = os.path.join(_BASE_DIR, "mock_jobs.json")
        try:
            with open(path) as f:
                data = json.load(f)
            log.info(f"[JobBoard] Mock mode: loaded {len(data)} entries")
            return data
        except Exception as e:
            log.error(f"[JobBoard] Mock load failed: {e}")
            return []

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException))
    )
    def _fetch_rss(self) -> List[Dict]:
        log.info(f"[JobBoard] Fetching RSS: {self.feed_url}")
        # feedparser handles HTTP natively; wrap in try to let tenacity retry
        feed = feedparser.parse(self.feed_url)
        if feed.bozo and not feed.entries:
            raise httpx.RequestError(f"RSS parse error: {feed.bozo_exception}")

        results = []
        for entry in feed.entries[:50]:  # cap to 50 per fetch cycle
            domain = self._extract_domain(entry)
            if not domain:
                continue
            title  = getattr(entry, "title", "")
            job_title = self._extract_job_title(title)
            results.append({"company_domain": domain, "job_title": job_title})

        log.info(f"[JobBoard] Yield: {len(results)} entries")
        return results

    def _extract_domain(self, entry) -> str:
        """Option A: URL-first extraction with ATS fallback to title heuristic."""
        url = getattr(entry, "link", "")
        if url:
            try:
                parsed   = urlparse(url)
                hostname = parsed.hostname or ""
                domain   = hostname.removeprefix("www.")
                # ATS provider? Can't use this domain.
                if not any(domain.endswith(ats) for ats in _ATS_PROVIDERS):
                    # Strip known job subdomains
                    parts = domain.split(".")
                    if len(parts) > 2 and parts[0] in _JOB_SUBDOMAINS:
                        domain = ".".join(parts[1:])
                    return domain
            except Exception:
                pass

        # Fallback: "X at CompanyName" heuristic
        title = getattr(entry, "title", "")
        if " at " in title:
            company = title.split(" at ")[-1].strip().split("(")[0].strip()
            return company.lower().replace(" ", "").replace(",", "") + ".com"
        return ""

    @staticmethod
    def _extract_job_title(title: str) -> str:
        if " at " in title:
            return title.split(" at ")[0].strip()
        if " - " in title:
            return title.split(" - ")[0].strip()
        return title.strip()


# ── Funding Ingestor ──────────────────────────────────────────────────────────
class FundingIngestor(BaseIngestor):
    """
    Parses mock_funding.json for dev/test.
    Production: swap data_path for an API client mapping to the same schema:
      [{ company_name, company_domain, amount_raised, round }]
    """

    def __init__(self, data_path: str = None):
        self.data_path = data_path or os.path.join(_BASE_DIR, "mock_funding.json")

    def fetch(self) -> List[Dict]:
        try:
            return self._load()
        except Exception as e:
            log.error(f"[Funding] Yield: 0 — complete failure: {e}")
            return []

    def _load(self) -> List[Dict]:
        with open(self.data_path) as f:
            data = json.load(f)
        log.info(f"[Funding] Loaded {len(data)} funding records")
        return data
