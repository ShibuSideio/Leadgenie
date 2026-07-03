"""
Signal Sources — Base Definitions V25.1.0

SignalItem:  Immutable value object representing a single discovered intent
             signal. Carries the URL, full text, provenance metadata, and
             source type so downstream modules can make PRISM/score decisions.

BaseSignalSource: Abstract contract every signal source must satisfy.
                  Enforces the discover() → list[SignalItem] interface and
                  provides the shared _now_iso() helper.
"""
from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SignalItem:
    """A single discovered intent signal from any source.

    Attributes:
        url:         Canonical URL — used as the dedup and scraped_cache key.
        text:        Full signal content. For Reddit/HN this is the post body.
                     For Serper discovery sources this is a thin discovery hint
                     and ``metadata["is_thin_content"]`` will be True.
        title:       Signal title (post title, article headline, job title).
        author:      Author identifier (username, display name, or empty).
        source_type: One of: "reddit", "hackernews", "stackoverflow", "rss_feed",
                     "job_post", "serper_url".
        metadata:    Source-specific extras. Always a dict — never None.
                     Common keys:
                       "is_thin_content": bool — True if PRISM scrape needed
                       "subreddit": str
                       "score": int (upvotes)
                       "num_comments": int
                       "tags": list[str]
        fetched_at:  ISO 8601 UTC timestamp when this signal was collected.
    """

    url: str = ""
    text: str = ""
    title: str = ""
    author: str = ""
    source_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    fetched_at: str = ""

    @property
    def has_rich_content(self) -> bool:
        """True if the text field has substantial content (≥100 chars)."""
        return len(self.text.strip()) >= 100

    @property
    def is_thin(self) -> bool:
        """True if PRISM scraping is needed to enrich the content."""
        return bool(self.metadata.get("is_thin_content", False)) or not self.has_rich_content

    def combined_text(self, max_chars: int = 6000) -> str:
        """Title + text combined, truncated to max_chars for Gemini prompts."""
        combined = f"{self.title}\n\n{self.text}".strip()
        return combined[:max_chars] if len(combined) > max_chars else combined


class BaseSignalSource(ABC):
    """Abstract contract for all signal discovery sources.

    Subclasses implement ``discover()`` and ``source_type``. They should:
      - Return all signals without pre-filtering (let the scorer decide)
      - Handle network failures gracefully and return partial results
      - Never raise exceptions from ``discover()`` — log and return []
    """

    @abstractmethod
    def discover(self) -> list[SignalItem]:
        """Fetch and return intent signals from this source.

        Returns:
            List of SignalItem objects. May be empty on failure or no results.
            Never raises — catches all exceptions internally.
        """

    @property
    @abstractmethod
    def source_type(self) -> str:
        """Short identifier string for this source (e.g. "reddit")."""

    def _now_iso(self) -> str:
        """Return current UTC time as ISO 8601 string."""
        return datetime.datetime.utcnow().isoformat() + "Z"
