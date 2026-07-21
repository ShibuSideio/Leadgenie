"""V27.5 stale social filter + sanitize_query hygiene."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ORCH = os.path.dirname(_HERE)
_PIPE = os.path.join(os.path.dirname(_ORCH), "pipeline-main")
for p in (_PIPE, os.path.join(os.path.dirname(_ORCH)), _ORCH):
    if p not in sys.path:
        sys.path.insert(0, p)

# Minimal env so config imports don't fail if loaded
os.environ.setdefault("PROJECT_ID", "lead-sniper-prod")
os.environ.setdefault("LOCATION", "asia-south1")
os.environ.setdefault("VELOCITY_THRESHOLD", "10")
os.environ.setdefault("ENCRYPTION_KEY", "x" * 44)

from services.serper_service import sanitize_query  # noqa: E402


def test_sanitize_strips_numbered_list_label():
    q = 'linkedin.com" 1. Seed Investment'
    out = sanitize_query(q)
    assert "1. Seed" not in out
    assert 'linkedin.com"' not in out or "linkedin.com" in out


def test_sanitize_orphan_quote_on_domain():
    out = sanitize_query('reddit.com" angelinvestors comments')
    assert 'reddit.com"' not in out
    assert "reddit.com" in out


def test_stale_social_one_year_ago():
    # Import produce helpers without full app boot when possible
    import importlib.util
    path = os.path.join(_PIPE, "api", "routers", "produce.py")
    # produce.py has heavy imports — call logic inline mirror
    from api.routers import produce as produce_mod  # type: ignore

    r = {
        "link": "https://www.reddit.com/r/startups/comments/abc/old",
        "date": "1 year ago",
        "title": "Old thread",
        "snippet": "from last year",
    }
    assert produce_mod._is_stale_content(r, is_consumer=False) is True


def test_stale_social_two_months_ok():
    from api.routers import produce as produce_mod  # type: ignore

    r = {
        "link": "https://www.reddit.com/r/startups/comments/xyz/new",
        "date": "2 months ago",
        "title": "Recent",
        "snippet": "fresh",
    }
    assert produce_mod._is_stale_content(r, is_consumer=False) is False


def test_stale_nonsocial_no_date_fail_open():
    from api.routers import produce as produce_mod  # type: ignore

    r = {
        "link": "https://www.example-startup.com/about",
        "date": "",
        "title": "About",
        "snippet": "We raise seed",
    }
    assert produce_mod._is_stale_content(r, is_consumer=False) is False


def test_walled_garden_single_query():
    from services.prism_pipeline import WalledGardenHook  # type: ignore

    hook = WalledGardenHook(db_client=None, serper_key="x")
    qs = hook._build_queries(
        "https://www.linkedin.com/in/smhaaz",
        "linkedin.com",
        "1. Seed Investment — Asia angels",
    )
    assert len(qs) == 1
    assert qs[0].startswith("site:linkedin.com")
    assert "1. Seed" not in qs[0]
