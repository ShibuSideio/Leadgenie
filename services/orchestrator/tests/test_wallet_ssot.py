"""V27.2.0 wallet SSOT unit tests — pure formula, no Firestore."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ORCH = os.path.dirname(_HERE)
_SHARED = os.path.join(os.path.dirname(_ORCH), "shared")
for p in (_SHARED, _ORCH):
    if p not in sys.path:
        sys.path.insert(0, p)

from wallet import (  # noqa: E402
    api_wallet_payload,
    has_available_credits,
    shard_consumed_sum,
    wallet_snapshot,
)
from lead_identity import (  # noqa: E402
    apply_lead_identity_fields,
    is_terminal_non_lead,
    normalize_user_status,
    resolve_lead_url,
)


def test_wallet_max_of_total_and_shards():
    snap = wallet_snapshot(
        {
            "allocated_credits": 100,
            "total_consumed": 10,
            "consumed_credits": 5,
            "reserved_credits": 2,
        },
        shard_sum=20,
    )
    # effective = max(10, 5+20) = 25; available = 100 - 25 - 2 = 73
    assert snap["effective_consumed"] == 25
    assert snap["available"] == 73
    assert has_available_credits(
        {"allocated_credits": 100, "total_consumed": 10, "reserved_credits": 2},
        shard_sum=20,
        need=73,
    )
    # max(10, 0+20)=20 → available=100-20-2=78
    assert has_available_credits(
        {"allocated_credits": 100, "total_consumed": 10, "reserved_credits": 2},
        shard_sum=20,
        need=78,
    )
    assert not has_available_credits(
        {"allocated_credits": 100, "total_consumed": 10, "reserved_credits": 2},
        shard_sum=20,
        need=79,
    )


def test_wallet_prefers_total_when_higher():
    snap = wallet_snapshot(
        {"allocated_credits": 50, "total_consumed": 40, "consumed_credits": 1, "reserved_credits": 0},
        shard_sum=2,
    )
    assert snap["effective_consumed"] == 40
    assert snap["available"] == 10


def test_api_wallet_payload_includes_available():
    payload = api_wallet_payload(
        {"allocated_credits": 100, "total_consumed": 30, "reserved_credits": 5},
        shard_sum=0,
    )
    assert payload["allocated_credits"] == 100
    assert payload["consumed_credits"] == 30
    assert payload["reserved_credits"] == 5
    assert payload["available_credits"] == 65


def test_shard_consumed_sum_from_dicts():
    assert shard_consumed_sum([{"consumed_credits": 3}, {"consumed_credits": 7}]) == 10


def test_lead_identity_url_and_campaign():
    out = apply_lead_identity_fields(
        {},
        url="https://example.com/a",
        source_url="",
        campaign_id="c1",
    )
    assert out["url"] == "https://example.com/a"
    assert out["source_url"] == "https://example.com/a"
    assert out["campaign_id"] == "c1"
    assert "c1" in out["matched_campaigns"]


def test_resolve_prefers_source_url():
    assert resolve_lead_url({"url": "a", "source_url": "b"}) == "b"
    assert resolve_lead_url({"url": "a"}) == "a"


def test_status_normalize():
    assert normalize_user_status("rejected") == "ignored"
    assert normalize_user_status("approved") == "converted"
    assert is_terminal_non_lead("scored_out")
    assert not is_terminal_non_lead("new")
