import datetime
from typing import Any, Dict, Tuple


def should_dispatch_produce(campaign: Dict[str, Any], now_utc: datetime.datetime) -> Tuple[bool, str]:
    """Return whether the campaign should dispatch a produce task.

    The sweep should honor an explicit manual override even when the campaign has a
    future next_produce_due timestamp. This lets operators trigger a run by
    setting a flag in Firestore without waiting for the 24h cadence.
    """
    if not campaign:
        return False, "missing_campaign"

    if campaign.get("status") not in {"active", "paused"}:
        return False, "inactive_status"

    if bool(campaign.get("manual_force_produce")):
        return True, "manual_override"

    next_produce_due = campaign.get("next_produce_due")
    if not next_produce_due:
        return True, "missing_due_date"

    if isinstance(next_produce_due, str):
        try:
            parsed = datetime.datetime.fromisoformat(next_produce_due)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            return True, "invalid_due_date"
    elif hasattr(next_produce_due, "timestamp"):
        parsed = next_produce_due
        if hasattr(parsed, "tzinfo") and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    else:
        return True, "invalid_due_date"

    if parsed <= now_utc:
        return True, "due"
    return False, "not_due_yet"
