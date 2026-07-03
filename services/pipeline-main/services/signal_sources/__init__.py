"""
LeadGenie Signal Sources Package — V25.1.0

Provides archetype-aware, multi-source signal discovery without hardcoded
campaign values. Each source implements BaseSignalSource and returns
SignalItem objects with full content for downstream Gemini inline scoring.

Design principles:
  - No hardcoded subreddits, keywords, or URLs — all configured by source_router
  - Full content always (post body, article text, job description) — no snippets
  - PRISM scraping used only when a source cannot provide full text inline
  - Every source handles failures gracefully and returns partial results

Available sources:
  RedditSource         — Reddit JSON API (no auth)
  HackerNewsSource     — HN Algolia API (no auth)
  StackOverflowSource  — Stack Exchange API (free tier)
  RssFeedSource        — Generic RSS/Atom parser
  SerperDiscoverySource— Serper as URL discovery only (thin content)
  JobPostSource        — Job board signals via RSS
"""
from __future__ import annotations

from services.signal_sources.base import SignalItem, BaseSignalSource  # noqa: F401
