"""
Project-wide Serper daily budget (V27.3.0) — multi-instance safe.

Uses Firestore ``system_telemetry/serper_daily_budget`` document fields:
  day: YYYY-MM-DD (UTC)
  spent: int
  residual_spent: int   # non-produce paths

Env:
  SERPER_DAILY_LIMIT          — total project cap (0 = unlimited, BC default)
  SERPER_RESIDUAL_DAILY_LIMIT — cap for inbound/mesh/deep_context/PRISM/agent
                                default 800 when unset (scale guard for 1000+ tenants)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return default


def residual_daily_limit() -> int:
    """Cap for residual (non-produce) Serper spend. Default 800/day project-wide."""
    return _int_env("SERPER_RESIDUAL_DAILY_LIMIT", 800)


def total_daily_limit() -> int:
    """Optional hard project cap. 0 = unlimited (legacy)."""
    return _int_env("SERPER_DAILY_LIMIT", 0)


def can_spend_serper(
    db: Any,
    *,
    amount: int = 1,
    residual: bool = False,
    log: Optional[Callable[..., None]] = None,
) -> bool:
    """Return True if spend is allowed. Fail-open on Firestore errors."""
    amount = max(0, int(amount or 0))
    if amount == 0:
        return True
    total_lim = total_daily_limit()
    residual_lim = residual_daily_limit() if residual else 0
    if total_lim <= 0 and (not residual or residual_lim <= 0):
        return True
    try:
        ref = db.collection("system_telemetry").document("serper_daily_budget")
        snap = ref.get()
        data = snap.to_dict() if snap.exists else {}
        day = _today()
        if data.get("day") != day:
            spent = 0
            residual_spent = 0
        else:
            spent = int(data.get("spent", 0) or 0)
            residual_spent = int(data.get("residual_spent", 0) or 0)
        if total_lim > 0 and spent + amount > total_lim:
            if log:
                log(
                    "serper_budget_total_blocked",
                    spent=spent,
                    amount=amount,
                    limit=total_lim,
                )
            return False
        if residual and residual_lim > 0 and residual_spent + amount > residual_lim:
            if log:
                log(
                    "serper_budget_residual_blocked",
                    residual_spent=residual_spent,
                    amount=amount,
                    limit=residual_lim,
                )
            return False
        return True
    except Exception as exc:
        if log:
            log("serper_budget_check_fail_open", error=str(exc))
        return True


def record_serper_spend(
    db: Any,
    *,
    amount: int = 1,
    residual: bool = False,
    log: Optional[Callable[..., None]] = None,
) -> bool:
    """Atomically record spend if under cap. Returns False if blocked."""
    amount = max(0, int(amount or 0))
    if amount == 0:
        return True
    if not can_spend_serper(db, amount=amount, residual=residual, log=log):
        return False
    try:
        from google.cloud import firestore  # type: ignore[import]

        ref = db.collection("system_telemetry").document("serper_daily_budget")
        day = _today()
        snap = ref.get()
        data = snap.to_dict() if snap.exists else {}
        if data.get("day") != day:
            # Reset day
            updates = {
                "day": day,
                "spent": amount,
                "residual_spent": amount if residual else 0,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }
            ref.set(updates, merge=True)
            return True
        # Re-check after read (soft race; Increment is eventual)
        spent = int(data.get("spent", 0) or 0)
        residual_spent = int(data.get("residual_spent", 0) or 0)
        total_lim = total_daily_limit()
        residual_lim = residual_daily_limit() if residual else 0
        if total_lim > 0 and spent + amount > total_lim:
            return False
        if residual and residual_lim > 0 and residual_spent + amount > residual_lim:
            return False
        upd: dict = {
            "day": day,
            "spent": firestore.Increment(amount),
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }
        if residual:
            upd["residual_spent"] = firestore.Increment(amount)
        ref.set(upd, merge=True)
        return True
    except Exception as exc:
        if log:
            log("serper_budget_record_fail_open", error=str(exc))
        return True
