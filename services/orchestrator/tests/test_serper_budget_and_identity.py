"""V27.3.0 pure tests — serper budget helpers + campaign queue ids."""
from __future__ import annotations

import os
import sys

_SERVICES = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SERVICES not in sys.path:
    sys.path.insert(0, _SERVICES)

from shared.serper_budget import residual_daily_limit, total_daily_limit  # noqa: E402
from shared.campaign_queue import approx_queue_bytes, url_item_id  # noqa: E402
from shared.lead_identity import normalize_user_status  # noqa: E402


def test_residual_limit_default():
    # Clear override
    os.environ.pop("SERPER_RESIDUAL_DAILY_LIMIT", None)
    assert residual_daily_limit() == 800


def test_total_limit_default_unlimited():
    os.environ.pop("SERPER_DAILY_LIMIT", None)
    assert total_daily_limit() == 0


def test_residual_limit_env():
    os.environ["SERPER_RESIDUAL_DAILY_LIMIT"] = "100"
    try:
        assert residual_daily_limit() == 100
    finally:
        os.environ.pop("SERPER_RESIDUAL_DAILY_LIMIT", None)


def test_queue_url_id_stable():
    a = url_item_id("https://example.com/x")
    b = url_item_id("https://example.com/x")
    assert a == b
    assert len(a) == 16


def test_approx_queue_bytes():
    n = approx_queue_bytes(["https://a.com", "https://b.com"])
    assert n > 20


def test_status_aliases():
    assert normalize_user_status("rejected") == "ignored"
