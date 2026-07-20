"""
Wallet SSOT — single formula for tenant credit balance (V27.2.0 scale).

True available balance (architecture §4.1 + dual-path max):

  available = allocated
              − max(total_consumed, consumed_credits + SUM(wallet_shards))
              − reserved_credits

Every gate, reserve, settle display, and convert path MUST use these helpers.
Writers of consumption should prefer ``wallet.total_consumed`` (atomic settle).
Legacy ``wallet_shards`` are still included on *read* for migration safety.
"""
from __future__ import annotations

from typing import Any, Mapping


def shard_consumed_sum(shard_docs: list | None) -> int:
    """Sum ``consumed_credits`` from wallet_shards stream snapshots or dicts."""
    total = 0
    for s in shard_docs or []:
        if hasattr(s, "to_dict"):
            data = s.to_dict() or {}
        elif isinstance(s, Mapping):
            data = s
        else:
            continue
        total += int(data.get("consumed_credits", 0) or 0)
    return total


def wallet_snapshot(
    wallet: Mapping[str, Any] | None,
    *,
    shard_sum: int = 0,
) -> dict[str, int]:
    """Return allocated / effective_consumed / reserved / available as ints."""
    w = wallet or {}
    allocated = int(w.get("allocated_credits", 0) or 0)
    total_consumed = int(w.get("total_consumed", 0) or 0)
    legacy_consumed = int(w.get("consumed_credits", 0) or 0)
    reserved = max(0, int(w.get("reserved_credits", 0) or 0))
    effective_consumed = max(total_consumed, legacy_consumed + int(shard_sum or 0))
    available = allocated - effective_consumed - reserved
    return {
        "allocated": allocated,
        "total_consumed": total_consumed,
        "legacy_consumed": legacy_consumed,
        "shard_sum": int(shard_sum or 0),
        "effective_consumed": effective_consumed,
        "reserved": reserved,
        "available": available,
    }


def has_available_credits(
    wallet: Mapping[str, Any] | None,
    *,
    shard_sum: int = 0,
    need: int = 1,
) -> bool:
    """True when available >= need (need defaults to 1)."""
    snap = wallet_snapshot(wallet, shard_sum=shard_sum)
    return snap["available"] >= max(0, int(need or 0))


def api_wallet_payload(
    wallet: Mapping[str, Any] | None,
    *,
    shard_sum: int = 0,
) -> dict[str, int]:
    """Public API shape for /api/me and banners (includes reserved + available)."""
    snap = wallet_snapshot(wallet, shard_sum=shard_sum)
    return {
        "allocated_credits": snap["allocated"],
        # Historical field name: report *effective* consumption (not raw legacy).
        "consumed_credits": snap["effective_consumed"],
        "total_consumed": snap["total_consumed"],
        "reserved_credits": snap["reserved"],
        "available_credits": snap["available"],
    }
