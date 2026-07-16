import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PIPELINE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "pipeline-main"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if PIPELINE_ROOT not in sys.path:
    sys.path.insert(0, PIPELINE_ROOT)

from services.adaptive_policy import build_dispatch_policy


def test_dispatch_policy_enters_recovery_on_starvation():
    campaign = {
        "bio": "Find good leads",
        "_query_exhaustion_consecutive_zeros": 3,
        "_query_exhaustion_escalation_level": 2,
    }
    policy = build_dispatch_policy(
        campaign=campaign,
        sourcing_vector="B2C",
        queue_depth=1,
        recent_new_count=0,
        recent_enrichment_pending_count=0,
        velocity_threshold=10,
        gate_read_failed=False,
    )
    assert policy["mode"] == "recovery"
    assert policy["threshold_adjustment"] < 0
    assert policy["medium_budget"] >= 6


def test_dispatch_policy_tightens_when_velocity_pressure_high():
    campaign = {"bio": "Rich context bio with clear intent and depth for scoring"}
    policy = build_dispatch_policy(
        campaign=campaign,
        sourcing_vector="B2B",
        queue_depth=12,
        recent_new_count=20,
        recent_enrichment_pending_count=5,
        velocity_threshold=10,
        gate_read_failed=False,
    )
    assert policy["mode"] == "strict"
    assert policy["threshold_adjustment"] > 0
    assert policy["medium_budget"] <= 2

