"""
Cost-protection tests: automatic harvest must never burn Serper credits.

Proves:
  1. SourceRouter(allow_serper=False) never instantiates Serper-backed sources
     even when an API key and discovery queries are present.
  2. SourceRouter(allow_serper=True) still enables Serper sources (produce path).
  3. RedditSource Serper fallback is blocked when allow_serper=False.
  4. /harvest route hard-codes allow_serper=False and does not load a key.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PIPELINE_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pipeline-main")
)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if PIPELINE_ROOT not in sys.path:
    sys.path.insert(0, PIPELINE_ROOT)

# Minimal package stubs so pipeline modules resolve without full Cloud Run deps.
for _pkg_name, _path in (
    ("services", str(Path(PIPELINE_ROOT) / "services")),
    ("core", str(Path(PIPELINE_ROOT) / "core")),
    ("shared", str(Path(ROOT) / "shared")),
):
    if _pkg_name not in sys.modules:
        _pkg = types.ModuleType(_pkg_name)
        _pkg.__path__ = [_path]
        sys.modules[_pkg_name] = _pkg


def _load_module(module_name: str, relative_path: str):
    module_path = Path(PIPELINE_ROOT) / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    # Pre-seed lightweight deps some modules import at load time.
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

        def get_logger(name=None):
            return _Log()

        logging_mod.get_logger = get_logger
        sys.modules["core.logging"] = logging_mod

    if "services.signal_sources.base" not in sys.modules:
        base_path = Path(PIPELINE_ROOT) / "services" / "signal_sources" / "base.py"
        if base_path.exists():
            base_spec = importlib.util.spec_from_file_location(
                "services.signal_sources.base", base_path
            )
            base_mod = importlib.util.module_from_spec(base_spec)
            sys.modules["services.signal_sources.base"] = base_mod
            assert base_spec.loader is not None
            base_spec.loader.exec_module(base_mod)

    if "services.budget_guard" not in sys.modules:
        bg_path = Path(PIPELINE_ROOT) / "services" / "budget_guard.py"
        if bg_path.exists():
            bg_spec = importlib.util.spec_from_file_location(
                "services.budget_guard", bg_path
            )
            bg_mod = importlib.util.module_from_spec(bg_spec)
            sys.modules["services.budget_guard"] = bg_mod
            assert bg_spec.loader is not None
            bg_spec.loader.exec_module(bg_mod)

    # shared.intelligence_profile used by source_router
    shared_path = Path(ROOT) / "shared"
    if "shared" in sys.modules and not getattr(sys.modules["shared"], "__path__", None):
        pass
    shared_pkg = sys.modules.get("shared")
    if shared_pkg is not None:
        shared_pkg.__path__ = [str(shared_path)]

    if "shared.intelligence_profile" not in sys.modules:
        ip_path = shared_path / "intelligence_profile.py"
        if ip_path.exists():
            ip_spec = importlib.util.spec_from_file_location(
                "shared.intelligence_profile", ip_path
            )
            ip_mod = importlib.util.module_from_spec(ip_spec)
            sys.modules["shared.intelligence_profile"] = ip_mod
            assert ip_spec.loader is not None
            ip_spec.loader.exec_module(ip_mod)

    # Stub heavy optional imports for source_router
    if "services.gemini_service" not in sys.modules:
        gem = types.ModuleType("services.gemini_service")
        gem.call_gemini_2_5 = MagicMock(return_value={})
        sys.modules["services.gemini_service"] = gem

    for sub in (
        "reddit",
        "hackernews",
        "rss_feed",
        "serper_discovery",
        "job_posts",
        "classified_listings",
        "consumer_forum",
        "google_reviews",
        "youtube",
    ):
        full = f"services.signal_sources.{sub}"
        path = Path(PIPELINE_ROOT) / "services" / "signal_sources" / f"{sub}.py"
        if full not in sys.modules and path.exists():
            # Skip full load of sources that pull requests/tenacity if not installed —
            # load via AST for harvest gate tests that only need SourceRouter wiring.
            pass

    spec.loader.exec_module(module)
    return module


def _load_source_router():
    """Load source_router with real signal_source modules where possible."""
    # Ensure signal_sources package
    ss_pkg_name = "services.signal_sources"
    if ss_pkg_name not in sys.modules:
        ss_pkg = types.ModuleType(ss_pkg_name)
        ss_pkg.__path__ = [str(Path(PIPELINE_ROOT) / "services" / "signal_sources")]
        sys.modules[ss_pkg_name] = ss_pkg

    # Load base first
    base_path = Path(PIPELINE_ROOT) / "services" / "signal_sources" / "base.py"
    if "services.signal_sources.base" not in sys.modules:
        base_spec = importlib.util.spec_from_file_location(
            "services.signal_sources.base", base_path
        )
        base_mod = importlib.util.module_from_spec(base_spec)
        sys.modules["services.signal_sources.base"] = base_mod
        base_spec.loader.exec_module(base_mod)

    # Minimal stubs for sources we only care about type-checking
    from services.signal_sources.base import BaseSignalSource, SignalItem  # type: ignore

    class _FakeSource(BaseSignalSource):
        source_type = "fake"

        def discover(self):
            return []

    def _make_source_class(name: str, stype: str):
        class _S(BaseSignalSource):
            source_type = stype

            def __init__(self, *args, **kwargs):
                self.init_kwargs = kwargs
                self.init_args = args

            def discover(self):
                return []

        _S.__name__ = name
        return _S

    # Patch modules used by source_router imports
    fake_modules = {
        "services.signal_sources.reddit": {
            "RedditSource": _make_source_class("RedditSource", "reddit"),
        },
        "services.signal_sources.hackernews": {
            "HackerNewsSource": _make_source_class("HackerNewsSource", "hackernews"),
        },
        "services.signal_sources.rss_feed": {
            "RssFeedSource": _make_source_class("RssFeedSource", "rss_feed"),
        },
        "services.signal_sources.serper_discovery": {
            "SerperDiscoverySource": _make_source_class(
                "SerperDiscoverySource", "serper_url"
            ),
        },
        "services.signal_sources.job_posts": {
            "JobPostSource": _make_source_class("JobPostSource", "job_posts"),
        },
        "services.signal_sources.classified_listings": {
            "ClassifiedListingSource": _make_source_class(
                "ClassifiedListingSource", "classified_listings"
            ),
        },
        "services.signal_sources.consumer_forum": {
            "ConsumerForumSource": _make_source_class(
                "ConsumerForumSource", "consumer_forum"
            ),
        },
        "services.signal_sources.google_reviews": {
            "GoogleReviewSource": _make_source_class(
                "GoogleReviewSource", "google_review"
            ),
        },
        "services.signal_sources.youtube": {
            "YouTubeSource": _make_source_class("YouTubeSource", "youtube"),
        },
    }

    for mod_name, attrs in fake_modules.items():
        mod = types.ModuleType(mod_name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[mod_name] = mod

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

    if "services.budget_guard" not in sys.modules:
        bg_path = Path(PIPELINE_ROOT) / "services" / "budget_guard.py"
        bg_spec = importlib.util.spec_from_file_location(
            "services.budget_guard", bg_path
        )
        bg_mod = importlib.util.module_from_spec(bg_spec)
        sys.modules["services.budget_guard"] = bg_mod
        bg_spec.loader.exec_module(bg_mod)

    if "services.gemini_service" not in sys.modules:
        gem = types.ModuleType("services.gemini_service")
        gem.call_gemini_2_5 = MagicMock(return_value={})
        sys.modules["services.gemini_service"] = gem

    shared_path = Path(ROOT) / "shared"
    if "shared" not in sys.modules:
        shared_pkg = types.ModuleType("shared")
        shared_pkg.__path__ = [str(shared_path)]
        sys.modules["shared"] = shared_pkg
    else:
        sys.modules["shared"].__path__ = [str(shared_path)]

    if "shared.intelligence_profile" not in sys.modules:
        ip_path = shared_path / "intelligence_profile.py"
        ip_spec = importlib.util.spec_from_file_location(
            "shared.intelligence_profile", ip_path
        )
        ip_mod = importlib.util.module_from_spec(ip_spec)
        sys.modules["shared.intelligence_profile"] = ip_mod
        ip_spec.loader.exec_module(ip_mod)

    # Force reload source_router so it picks up fakes
    for name in list(sys.modules):
        if name == "services.source_router":
            del sys.modules[name]

    path = Path(PIPELINE_ROOT) / "services" / "source_router.py"
    spec = importlib.util.spec_from_file_location("services.source_router", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["services.source_router"] = mod
    spec.loader.exec_module(mod)
    return mod


_SERPER_SOURCE_TYPES = frozenset({"serper_url", "google_review"})


def _sample_config() -> dict:
    return {
        "_archetype": "B2B",
        "_icp_context": "B2B SaaS CRM for sales teams",
        "_campaign": {"id": "camp_test", "intelligence_strategy": {}},
        "reddit_sources": [
            {"subreddit": "sales", "search_query": "looking for CRM", "rationale": "x"}
        ],
        "hackernews_queries": ["CRM automation"],
        "rss_feed_urls": [],
        "job_post_keywords": ["Sales Ops"],
        "serper_discovery_queries": [
            'site:community.hubspot.com "CRM" pain',
            "sales automation frustration",
        ],
        "geo_filter_terms": ["US"],
        "buyer_language_context": "need a CRM",
        "classified_listing_config": {},
        "consumer_forum_config": {},
    }


def test_source_router_blocks_serper_sources_when_allow_serper_false():
    router_mod = _load_source_router()
    router = router_mod.SourceRouter(
        serper_api_key="sk-test-should-be-ignored",
        allow_serper=False,
    )
    assert router._serper_key == ""
    assert router._allow_serper is False

    sources = router._instantiate_sources(
        _sample_config(),
        geo="US",
        campaign={"id": "camp_test"},
    )
    types_found = {s.source_type for s in sources}
    assert "serper_url" not in types_found
    assert "google_review" not in types_found
    # Free sources should still be present
    assert "reddit" in types_found


def test_source_router_enables_serper_sources_when_allow_serper_true():
    router_mod = _load_source_router()
    router = router_mod.SourceRouter(
        serper_api_key="sk-test-real",
        allow_serper=True,
    )
    assert router._serper_key == "sk-test-real"
    assert router.allow_serper is True

    sources = router._instantiate_sources(
        _sample_config(),
        geo="US",
        campaign={"id": "camp_test"},
    )
    types_found = {s.source_type for s in sources}
    assert "serper_url" in types_found
    assert "google_review" in types_found


def test_reddit_serper_fallback_blocked_without_allow_serper(monkeypatch):
    """RedditSource must not call search_serper when allow_serper=False."""
    # Load real reddit module with light stubs
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

    ss_pkg_name = "services.signal_sources"
    if ss_pkg_name not in sys.modules:
        ss_pkg = types.ModuleType(ss_pkg_name)
        ss_pkg.__path__ = [str(Path(PIPELINE_ROOT) / "services" / "signal_sources")]
        sys.modules[ss_pkg_name] = ss_pkg

    base_path = Path(PIPELINE_ROOT) / "services" / "signal_sources" / "base.py"
    if "services.signal_sources.base" not in sys.modules:
        base_spec = importlib.util.spec_from_file_location(
            "services.signal_sources.base", base_path
        )
        base_mod = importlib.util.module_from_spec(base_spec)
        sys.modules["services.signal_sources.base"] = base_mod
        base_spec.loader.exec_module(base_mod)

    # Stub rss_feed used by RedditSource
    rss_mod = types.ModuleType("services.signal_sources.rss_feed")

    class RssFeedSource:
        source_type = "rss_feed"

        def __init__(self, *a, **k):
            pass

        def discover(self):
            return []  # force empty → would trigger Serper fallback

    rss_mod.RssFeedSource = RssFeedSource
    sys.modules["services.signal_sources.rss_feed"] = rss_mod

    # Prevent real serper import side effects
    serper_calls = []

    def _fake_search_serper(*a, **k):
        serper_calls.append((a, k))
        return [{"link": "https://reddit.com/r/x", "title": "t", "snippet": "s"}]

    serper_mod = types.ModuleType("services.serper_service")
    serper_mod.search_serper = _fake_search_serper
    sys.modules["services.serper_service"] = serper_mod

    # Reload reddit module
    for name in list(sys.modules):
        if name == "services.signal_sources.reddit":
            del sys.modules[name]

    path = Path(PIPELINE_ROOT) / "services" / "signal_sources" / "reddit.py"
    # Provide requests/tenacity stubs if missing
    if "requests" not in sys.modules:
        req = types.ModuleType("requests")
        req.exceptions = types.SimpleNamespace(RequestException=Exception)
        sys.modules["requests"] = req
    if "tenacity" not in sys.modules:
        ten = types.ModuleType("tenacity")
        ten.retry = lambda **k: (lambda f: f)
        ten.wait_exponential = lambda **k: None
        ten.stop_after_attempt = lambda *a, **k: None
        ten.retry_if_exception_type = lambda *a, **k: None
        sys.modules["tenacity"] = ten

    spec = importlib.util.spec_from_file_location(
        "services.signal_sources.reddit", path
    )
    reddit = importlib.util.module_from_spec(spec)
    sys.modules["services.signal_sources.reddit"] = reddit
    spec.loader.exec_module(reddit)

    src = reddit.RedditSource(
        subreddits=["sales"],
        search_terms=["CRM"],
        allow_serper=False,
    )
    results = src.discover()
    assert results == []
    assert serper_calls == [], "Serper fallback must not run when allow_serper=False"

    # Produce-gated path still allowed
    src_prod = reddit.RedditSource(
        subreddits=["sales"],
        search_terms=["CRM"],
        allow_serper=True,
    )
    results_prod = src_prod.discover()
    assert len(serper_calls) > 0
    assert any(r.url for r in results_prod)


def test_harvest_route_hardcodes_allow_serper_false():
    """AST check: harvest.py must never load Serper key or pass allow_serper=True."""
    src_path = Path(PIPELINE_ROOT) / "api" / "routers" / "harvest.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    source = src_path.read_text(encoding="utf-8")

    assert "allow_serper" in source
    assert "allow_serper=False" in source or "allow_serper   = False" in source
    assert "get_serper_key" not in source
    assert "SERPER_API_KEY" not in source
    assert "serper_api_key = \"\"" in source or 'serper_api_key = ""' in source or "serper_api_key=" in source

    # Ensure harvest_signals call uses allow_serper=False
    harvest_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = ""
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            if name == "harvest_signals":
                harvest_calls.append(node)

    assert harvest_calls, "harvest_signals must be called from harvest route"
    for call in harvest_calls:
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        assert "allow_serper" in kw
        val = kw["allow_serper"]
        assert isinstance(val, ast.Constant) and val.value is False


def test_produce_pathway_opts_into_serper():
    """produce.py harvest pathway must pass allow_serper=True."""
    src_path = Path(PIPELINE_ROOT) / "api" / "routers" / "produce.py"
    source = src_path.read_text(encoding="utf-8")
    assert "allow_serper=True" in source
    assert "harvest_signals(" in source


def test_harvest_signals_defaults_allow_serper_false():
    """Default signature of harvest_signals must be safe (allow_serper=False)."""
    src_path = Path(PIPELINE_ROOT) / "services" / "signal_harvest.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "harvest_signals":
            defaults = {
                arg.arg: default
                for arg, default in zip(
                    node.args.args[-len(node.args.defaults) :],
                    node.args.defaults,
                )
            }
            assert "allow_serper" in defaults
            d = defaults["allow_serper"]
            assert isinstance(d, ast.Constant) and d.value is False
            return
    pytest.fail("harvest_signals not found")
