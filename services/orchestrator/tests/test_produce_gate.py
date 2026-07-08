import datetime
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "core" / "produce_gate.py"

spec = importlib.util.spec_from_file_location("produce_gate", MODULE_PATH)
produce_gate = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(produce_gate)


def test_manual_force_bypasses_schedule_gate():
    now = datetime.datetime(2026, 7, 8, 12, 0, tzinfo=datetime.timezone.utc)
    campaign = {
        "status": "active",
        "next_produce_due": (now + datetime.timedelta(hours=6)).isoformat(),
        "manual_force_produce": True,
    }
    should_dispatch, reason = produce_gate.should_dispatch_produce(campaign, now)
    assert should_dispatch is True
    assert reason == "manual_override"


def test_missing_due_date_dispatches_immediately():
    now = datetime.datetime(2026, 7, 8, 12, 0, tzinfo=datetime.timezone.utc)
    campaign = {"status": "active"}
    should_dispatch, reason = produce_gate.should_dispatch_produce(campaign, now)
    assert should_dispatch is True
    assert reason == "missing_due_date"


def test_future_due_date_defers_dispatch():
    now = datetime.datetime(2026, 7, 8, 12, 0, tzinfo=datetime.timezone.utc)
    campaign = {
        "status": "active",
        "next_produce_due": (now + datetime.timedelta(hours=1)).isoformat(),
    }
    should_dispatch, reason = produce_gate.should_dispatch_produce(campaign, now)
    assert should_dispatch is False
    assert reason == "not_due_yet"
