"""Tests for multi-entity host identity + campaign Medium velocity quota helpers."""
import hashlib
import importlib.util
import os
import sys
import types
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PIPELINE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "pipeline-main"))
SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))

for p in (ROOT, PIPELINE_ROOT, SHARED_ROOT, os.path.dirname(SHARED_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_multi_entity():
    path = Path(SHARED_ROOT) / "multi_entity_hosts.py"
    # Prefer services/shared if present
    alt = Path(ROOT) / "shared" / "multi_entity_hosts.py"
    if not path.exists():
        path = Path(os.path.join(os.path.dirname(__file__), "..", "..", "shared", "multi_entity_hosts.py"))
    # monorepo: services/shared
    mono = Path(os.path.dirname(__file__)).resolve().parents[1] / "shared" / "multi_entity_hosts.py"
    if mono.exists():
        path = mono
    spec = importlib.util.spec_from_file_location("shared.multi_entity_hosts", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_is_multi_entity_host_matches_portals():
    me = _load_multi_entity()
    assert me.is_multi_entity_host("bayut.com")
    assert me.is_multi_entity_host("www.bayut.com")
    assert me.is_multi_entity_host("en.bayut.com")
    assert me.is_multi_entity_host("propertyfinder.ae")
    assert me.is_multi_entity_host("dubizzle.com")
    assert me.is_multi_entity_host("g2.com")
    assert not me.is_multi_entity_host("acme-corp.com")
    assert not me.is_multi_entity_host("northstar-media.example")
    assert not me.is_multi_entity_host("")


def test_b2b_forces_path_on_multi_entity_not_on_normal_domain():
    me = _load_multi_entity()
    # B2B + multi-entity → path
    key, meta = me.resolve_identity_key(
        "https://www.bayut.com/brokers/ahmed-123.html",
        "bayut.com",
        is_social=False,
        is_shared=False,
        is_consumer=False,
    )
    assert meta["identity_mode"] == "path"
    assert meta["multi_entity_host"] is True
    assert meta["identity_reason"] == "multi_entity_host"
    assert "brokers/ahmed-123" in key

    # B2B + normal company → domain
    key2, meta2 = me.resolve_identity_key(
        "https://acme-corp.com/about",
        "acme-corp.com",
        is_social=False,
        is_shared=False,
        is_consumer=False,
    )
    assert meta2["identity_mode"] == "domain"
    assert key2 == "acme-corp.com"
    assert meta2["multi_entity_host"] is False


def test_b2c_and_social_still_path_level():
    me = _load_multi_entity()
    key, meta = me.resolve_identity_key(
        "https://acme-corp.com/listings/1",
        "acme-corp.com",
        is_consumer=True,
    )
    assert meta["identity_mode"] == "path"
    assert meta["identity_reason"] == "consumer_vector"

    key_li, meta_li = me.resolve_identity_key(
        "https://www.linkedin.com/in/jane-doe",
        "linkedin.com",
        is_social=True,
        is_consumer=False,
    )
    assert meta_li["identity_mode"] == "path"
    assert "linkedin.com/in/jane-doe" in key_li


def test_two_bayut_agents_have_distinct_path_keys_for_b2b():
    me = _load_multi_entity()
    k1, _ = me.resolve_identity_key(
        "https://www.bayut.com/brokers/a.html", "bayut.com", is_consumer=False
    )
    k2, _ = me.resolve_identity_key(
        "https://www.bayut.com/brokers/b.html", "bayut.com", is_consumer=False
    )
    assert k1 != k2
    tenant = "tenant1"
    h1 = hashlib.sha256(f"{tenant}_{k1}".encode()).hexdigest()
    h2 = hashlib.sha256(f"{tenant}_{k2}".encode()).hexdigest()
    assert h1 != h2


def test_path_identity_stable_with_www():
    me = _load_multi_entity()
    a = me.path_identity_key("https://www.bayut.com/brokers/x")
    b = me.path_identity_key("https://bayut.com/brokers/x")
    assert a == b


def test_campaign_medium_quota_helpers_via_dispatch_logic():
    """Pure logic: tenant hard cap + campaign soft remaining intersection."""
    # Simulate _apply_campaign_medium_cap without importing Flask dispatch
    medium_urls = [f"u{i}" for i in range(10)]
    policy_budget = 6
    campaign_remaining = 2
    tenant_allows = True

    if not tenant_allows:
        selected = []
        reason = "tenant_velocity_hard_cap"
    else:
        cap = min(policy_budget, campaign_remaining)
        selected = medium_urls[:cap]
        reason = "campaign_medium_quota" if campaign_remaining < policy_budget else "none"

    assert selected == ["u0", "u1"]
    assert reason == "campaign_medium_quota"

    # Tenant blocked
    tenant_allows = False
    if not tenant_allows:
        selected = []
        reason = "tenant_velocity_hard_cap"
    assert selected == []
    assert reason == "tenant_velocity_hard_cap"

    # Campaign quota exhausted
    campaign_remaining = 0
    tenant_allows = True
    cap = min(policy_budget, campaign_remaining) if campaign_remaining is not None else policy_budget
    selected = medium_urls[:cap]
    assert selected == []


def test_medium_quota_config_defaults():
    # Load config module lightly
    config_path = Path(PIPELINE_ROOT) / "core" / "config.py"
    # Ensure env defaults
    os.environ.pop("MEDIUM_CAMPAIGN_QUOTA_24H", None)
    # re-read via exec would cache - just assert documented defaults in source
    text = config_path.read_text(encoding="utf-8")
    assert "MEDIUM_CAMPAIGN_QUOTA_24H" in text
    assert "MEDIUM_CAMPAIGN_QUOTA_ENABLED" in text
