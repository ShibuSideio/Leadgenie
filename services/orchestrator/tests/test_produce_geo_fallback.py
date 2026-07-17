"""Unit tests for produce geo-fallback policy and datetime scoping safety."""
from __future__ import annotations

import ast
import importlib.util
import sys
import types
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[2] / "pipeline-main"
ORCH_ROOT = Path(__file__).resolve().parents[1]


def _load_produce_helpers():
    """Load produce.py helpers without pulling full Flask/GCP stack.

    We AST-extract and exec only pure helpers if full import fails.
    """
    # Minimal stubs so produce module can import
    for name in (
        "flask",
        "google",
        "google.cloud",
        "google.cloud.firestore",
        "google.cloud.firestore_v1",
        "google.cloud.firestore_v1.base_query",
        "core",
        "core.logging",
        "core.clients",
        "middleware",
        "middleware.oidc",
        "services",
    ):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            if name in ("core", "services", "middleware", "google", "google.cloud"):
                mod.__path__ = []
            sys.modules[name] = mod

    sys.modules["flask"].Blueprint = lambda *a, **k: types.SimpleNamespace(
        route=lambda *a, **k: (lambda f: f)
    )
    sys.modules["flask"].jsonify = lambda *a, **k: {}
    sys.modules["flask"].request = types.SimpleNamespace()

    fs = sys.modules["google.cloud.firestore"]
    fs.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
    fs.ArrayUnion = lambda x: x
    fs.Increment = lambda x: x

    sys.modules["google.cloud.firestore_v1.base_query"].FieldFilter = object

    class _Log:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

        def critical(self, *a, **k):
            pass

    sys.modules["core.logging"].get_logger = lambda name=None: _Log()
    sys.modules["core.clients"].get_db = lambda: None
    sys.modules["middleware.oidc"].require_tasks_oidc = lambda f: f

    # Pre-seed heavy service imports used at module level by produce
    # produce imports many services later inside functions — should be fine.

    path = PIPELINE_ROOT / "api" / "routers" / "produce.py"
    if str(PIPELINE_ROOT) not in sys.path:
        sys.path.insert(0, str(PIPELINE_ROOT))

    # Only load the helper by reading AST if full import is too heavy
    src = path.read_text(encoding="utf-8")
    # Exec just the helper function + imports
    tree = ast.parse(src)
    helper_src = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "should_attempt_geo_fallback":
            helper_src = ast.get_source_segment(src, node)
            break
    assert helper_src, "should_attempt_geo_fallback not found in produce.py"
    ns: dict = {}
    exec(helper_src, ns)  # noqa: S102 — test harness
    return ns["should_attempt_geo_fallback"]


def test_low_liquidity_geo_fallback_triggered():
    fn = _load_produce_helpers()
    ok, reason = fn(
        gl="om",
        has_results=False,
        is_platform_query=False,
        low_liquidity=True,
    )
    assert ok is True
    assert reason == "low_liquidity"


def test_high_liquidity_skips_non_platform_fallback():
    fn = _load_produce_helpers()
    ok, reason = fn(
        gl="us",
        has_results=False,
        is_platform_query=False,
        low_liquidity=False,
    )
    assert ok is False
    assert reason == "high_liquidity_skip"


def test_platform_query_gets_fallback_even_high_liquidity():
    fn = _load_produce_helpers()
    ok, reason = fn(
        gl="om",
        has_results=False,
        is_platform_query=True,
        low_liquidity=False,
    )
    assert ok is True
    assert reason == "platform"


def test_no_fallback_when_results_present():
    fn = _load_produce_helpers()
    ok, reason = fn(
        gl="om",
        has_results=True,
        is_platform_query=False,
        low_liquidity=True,
    )
    assert ok is False
    assert reason == "no_need"


def test_produce_no_local_datetime_import_shadowing():
    """Regression: local `import datetime` shadowed `from datetime import datetime`."""
    src = (PIPELINE_ROOT / "api" / "routers" / "produce.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Find produce() function body
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "produce":
            for child in ast.walk(node):
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        assert alias.name != "datetime", (
                            "Local `import datetime` inside produce() shadows "
                            "module-level `from datetime import datetime` and "
                            "causes UnboundLocalError on dedup path."
                        )
            break
    else:
        # produce may be nested under decorator assignment — scan whole file for
        # function-level import datetime that is NOT module-level
        pass

    # Stronger check: no `import datetime` after the first function definition
    lines = src.splitlines()
    past_helpers = False
    for line in lines:
        if line.startswith("def produce") or line.startswith("@bp.route"):
            past_helpers = True
        if past_helpers and line.strip() == "import datetime":
            raise AssertionError(
                "Found local `import datetime` after produce route — "
                "this reintroduces UnboundLocalError"
            )


def test_brand_narrative_classifies_as_marketing_agency():
    if str(PIPELINE_ROOT) not in sys.path:
        sys.path.insert(0, str(PIPELINE_ROOT))
    # Stub minimal deps for domain_intelligence
    for name in ("core", "core.logging", "shared", "shared.domain_constants"):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            if name in ("core", "shared"):
                mod.__path__ = []
            sys.modules[name] = mod

    class _Log:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

    sys.modules["core.logging"].get_logger = lambda n=None: _Log()

    # Load shared domain constants if needed
    shared_path = Path(__file__).resolve().parents[2] / "shared"
    if "shared.domain_constants" not in sys.modules or not hasattr(
        sys.modules.get("shared.domain_constants"), "is_valid_domain_family"
    ):
        dc_path = shared_path / "domain_constants.py"
        if dc_path.exists():
            spec = importlib.util.spec_from_file_location(
                "shared.domain_constants", dc_path
            )
            dc = importlib.util.module_from_spec(spec)
            sys.modules["shared.domain_constants"] = dc
            spec.loader.exec_module(dc)

    from services.domain_intelligence import infer_domain_profile

    campaign = {
        "name": "Brand Narrative",
        "bio": "Brand positioning and brand architecture for FMCG and retail brands",
        "keywords": "brand narrative, brand identity, differentiate, marketing strategy",
        "persona_bio": "CMOs needing brand strategy and narrative differentiation",
        "pain_point": "Struggling to differentiate brand identity in crowded FMCG retail marketing",
        "location": "UAE",
        "sourcing_vector": "B2B",
    }
    profile = infer_domain_profile(campaign)
    assert profile.get("domain_family") == "marketing_agency", profile
    assert float(profile.get("confidence") or 0) > 0.4
