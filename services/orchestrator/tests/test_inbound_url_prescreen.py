"""Unit tests for Inbound Radar URL pre-screen policy."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

ORCH = Path(__file__).resolve().parents[1]
if str(ORCH) not in sys.path:
    sys.path.insert(0, str(ORCH))


def _load_inbound_service():
    # Lightweight stubs for core deps
    if "core" not in sys.modules:
        core = types.ModuleType("core")
        core.__path__ = [str(ORCH / "core")]
        sys.modules["core"] = core
    if "core.logging" not in sys.modules:
        logging_mod = types.ModuleType("core.logging")

        class _Log:
            def info(self, *a, **k):
                pass

            def warning(self, *a, **k):
                pass

            def error(self, *a, **k):
                pass

            def debug(self, *a, **k):
                pass

        logging_mod.get_logger = lambda name=None: _Log()
        sys.modules["core.logging"] = logging_mod
    if "core.clients" not in sys.modules:
        clients = types.ModuleType("core.clients")
        clients.get_secret_manager_client = MagicMock()
        sys.modules["core.clients"] = clients
    if "core.config" not in sys.modules:
        cfg = types.ModuleType("core.config")
        cfg.PROJECT_ID = "test-project"
        sys.modules["core.config"] = cfg
    if "httpx" not in sys.modules:
        sys.modules["httpx"] = types.ModuleType("httpx")

    if "services" not in sys.modules:
        services = types.ModuleType("services")
        services.__path__ = [str(ORCH / "services")]
        sys.modules["services"] = services

    path = ORCH / "services" / "inbound_sentiment_service.py"
    name = "services.inbound_sentiment_service"
    # Reload so policy constants are current
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _svc(competitors=None):
    mod = _load_inbound_service()
    return mod.InboundSentimentService(
        persona={"competitors": competitors or []},
        campaign={},
    )


def test_trustpilot_and_g2_review_urls_pass():
    svc = _svc()
    keep = [
        "https://www.trustpilot.com/review/acme-software.com",
        "https://uk.trustpilot.com/review/acme.co.uk",
        "https://www.g2.com/products/acme/reviews",
        "https://www.capterra.com/p/123/acme/reviews/",
        "https://www.sitejabber.com/reviews/acme.com",
        "https://www.trustradius.com/products/acme/reviews",
        "https://www.yelp.com/biz/acme-muscat",
        "https://www.glassdoor.com/Reviews/Acme-Reviews-E123.htm",
    ]
    for url in keep:
        is_noise, reason = svc.classify_inbound_url(url)
        assert is_noise is False, f"expected keep for {url}, got {reason}"
        assert reason == "allow_review_platform"


def test_obvious_noise_still_filtered():
    svc = _svc()
    drop = [
        ("https://www.zoominfo.com/c/acme/123", "noise_host"),
        ("https://en.wikipedia.org/wiki/CRM", "noise_host"),
        ("https://www.crunchbase.com/organization/acme", "noise_host"),
        ("https://www.upwork.com/jobs/~abc", "noise_host"),
        ("https://example.com/login", "auth_wall"),
        ("https://example.com/signup", "auth_wall"),
        ("https://example.com/careers/engineer", "jobs_page"),
        ("https://example.com/pricing", "marketing_pricing"),
        ("https://example.com/best-crm-software-2026", "seo_listicle"),
        ("https://example.com/top-10-tools", "seo_listicle"),
        ("https://example.com/vs/hubspot", "seo_comparison"),
        ("https://example.com/compare/salesforce", "seo_comparison"),
    ]
    for url, needle in drop:
        is_noise, reason = svc.classify_inbound_url(url)
        assert is_noise is True, f"expected filter for {url}, got {reason}"
        assert needle in reason, f"{url}: expected {needle} in {reason}"


def test_blog_complaint_kept_seo_blog_dropped():
    svc = _svc()
    # Complaint blog — keep
    is_noise, reason = svc.classify_inbound_url(
        "https://mycompany.example/blog/billing-nightmare",
        title="Terrible support — we want a refund",
        snippet="Worst experience with cancel and billing issues",
    )
    assert is_noise is False
    assert reason == "allow_blog_candidate"

    # SEO listicle path under /blog/ — drop
    is_noise, reason = svc.classify_inbound_url(
        "https://mycompany.example/blog/best-crm-tools-list",
        title="Best CRM tools",
        snippet="Top software roundup",
    )
    assert is_noise is True
    assert "blog" in reason or "seo" in reason

    # Marketing blog with no sentiment cues in title/snippet — drop
    is_noise, reason = svc.classify_inbound_url(
        "https://mycompany.example/blog/announcing-v2",
        title="Announcing version 2",
        snippet="We shipped new features today",
    )
    assert is_noise is True
    assert reason == "blog_no_sentiment_cues"

    # URL-only blog (no title) — keep, Gemini decides
    is_noise, reason = svc.classify_inbound_url(
        "https://mycompany.example/blog/random-post"
    )
    assert is_noise is False
    assert reason == "allow_blog_candidate"


def test_social_and_forums_kept():
    svc = _svc()
    for url in (
        "https://reddit.com/r/sales/comments/abc/looking_for_crm",
        "https://www.facebook.com/groups/x/posts/1",
        "https://news.ycombinator.com/item?id=1",
        "https://github.com/org/repo/issues/9",
        "https://quora.com/What-is-the-best-CRM",
    ):
        is_noise, reason = svc.classify_inbound_url(url)
        assert is_noise is False, f"{url} -> {reason}"


def test_review_constants_exportable():
    mod = _load_inbound_service()
    assert "trustpilot.com" in mod.INBOUND_REVIEW_ALLOW_HOSTS
    assert "g2.com" in mod.INBOUND_REVIEW_ALLOW_HOSTS
    assert "capterra.com" in mod.INBOUND_REVIEW_ALLOW_HOSTS
    # Must NOT still treat trustpilot as noise
    assert "trustpilot.com/review" not in mod.INBOUND_NOISE_HOST_MARKERS
