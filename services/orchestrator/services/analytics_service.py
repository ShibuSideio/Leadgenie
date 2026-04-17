"""
Orchestrator — L1 ROI Analytics Service.

Extracted from the ``get_roi_analytics`` route handler in main.py.
Pure business logic — no Flask/HTTP objects are imported here.

Single Responsibility:
  1. Fetch lead counts in a date window.
  2. Apply the four financial formulas.
  3. Return a structured metrics dict.

The route handler owns: auth, request parsing, HTTP response shaping.
This service owns: query execution and math.
"""
from __future__ import annotations

import datetime
from typing import Any

from core.config import ROI_DEFAULTS
from core.logging import get_logger

log = get_logger(__name__)


def compute_roi_metrics(
    db,
    tenant_id: str,
    date_range_days: int,
) -> dict[str, Any]:
    """Compute L1 ROI metrics for a tenant over a date window.

    Reads ``unit_economics`` from the tenant's user document, then counts
    approved/contacted/total leads in the window and applies the four
    financial formulas defined in V22 TSD §25.3.3.

    Args:
        db:              Firestore client (injected).
        tenant_id:       Tenant UID.
        date_range_days: Lookback window in days (1-365).

    Returns:
        Dict containing ``unit_economics``, ``metrics``,
        ``date_range_days``, and ``generated_at``.
    """
    # ── 1. Load unit economics ────────────────────────────────────────────
    user_doc = db.collection("users").document(tenant_id).get()
    user_data: dict = user_doc.to_dict() if user_doc.exists else {}
    ue_raw: dict = user_data.get("unit_economics") or {}

    ue: dict[str, Any] = {
        "avg_cpl":             float(ue_raw.get("avg_cpl",             ROI_DEFAULTS["avg_cpl"])),
        "avg_deal_size":       float(ue_raw.get("avg_deal_size",       ROI_DEFAULTS["avg_deal_size"])),
        "sdr_hourly_rate":     float(ue_raw.get("sdr_hourly_rate",     ROI_DEFAULTS["sdr_hourly_rate"])),
        "est_conversion_rate": float(ue_raw.get("est_conversion_rate", ROI_DEFAULTS["est_conversion_rate"])),
        "currency":            str(ue_raw.get("currency",              ROI_DEFAULTS["currency"])),
    }

    # ── 2. Date window ────────────────────────────────────────────────────
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now_utc - datetime.timedelta(days=date_range_days)

    # ── 3. Lead counts ────────────────────────────────────────────────────
    n_approved = n_contacted = n_total_feed = 0
    try:
        n_approved = sum(
            1 for _ in db.collection("leads")
            .where("tenant_id", "==", tenant_id)
            .where("status", "==", "converted")
            .where("updatedAt", ">=", cutoff)
            .stream()
        )
        n_contacted = sum(
            1 for _ in db.collection("leads")
            .where("tenant_id", "==", tenant_id)
            .where("status", "==", "contacted")
            .where("updatedAt", ">=", cutoff)
            .stream()
        )
        n_total_feed = sum(
            1 for _ in db.collection("leads")
            .where("tenant_id", "==", tenant_id)
            .where("createdAt", ">=", cutoff)
            .stream()
        )
    except Exception as exc:
        log.error("roi_lead_count_failed", tenant=tenant_id[:8], error=str(exc))

    # ── 4. Financial formulas (V22 TSD §25.3.3) ──────────────────────────
    ad_savings = round(n_approved * ue["avg_cpl"], 2)
    labor_savings = round((n_approved * 15 / 60) * ue["sdr_hourly_rate"], 2)
    total_offset = round(ad_savings + labor_savings, 2)

    pipeline_value = (
        round(n_approved * ue["est_conversion_rate"] * ue["avg_deal_size"], 2)
        if ue["avg_deal_size"] > 0 else 0.0
    )

    sideio_cost = n_approved * 0.10
    roi_ratio = round(total_offset / sideio_cost, 1) if sideio_cost > 0 else 0.0

    log.info(
        "roi_computed",
        tenant=tenant_id[:8],
        n_approved=n_approved,
        total_offset=total_offset,
        date_range_days=date_range_days,
    )

    return {
        "unit_economics":  ue,
        "date_range_days": date_range_days,
        "generated_at":    now_utc.isoformat(),
        "metrics": {
            "n_approved":     n_approved,
            "n_contacted":    n_contacted,
            "n_total_feed":   n_total_feed,
            "ad_savings":     ad_savings,
            "labor_savings":  labor_savings,
            "total_offset":   total_offset,
            "pipeline_value": pipeline_value,
            "roi_ratio":      roi_ratio,
        },
    }


def validate_and_build_ue_update(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a unit_economics PUT payload and return Firestore update dict.

    Args:
        payload: Raw request JSON body.

    Returns:
        Firestore field-path → value update dict.

    Raises:
        :class:`core.exceptions.ValidationError`: If no valid fields provided.
    """
    from core.exceptions import ValidationError  # local to avoid circular

    allowed = {"avg_cpl", "avg_deal_size", "sdr_hourly_rate", "est_conversion_rate", "currency"}
    updates: dict[str, Any] = {}

    for key in allowed:
        if key not in payload:
            continue
        val = payload[key]
        if key == "currency":
            updates[f"unit_economics.{key}"] = str(val)[:3].upper()
        else:
            try:
                updates[f"unit_economics.{key}"] = max(0.0, float(val))
            except (ValueError, TypeError):
                pass  # silently ignore non-numeric values

    if not updates:
        raise ValidationError("No valid unit_economics fields provided.")

    from google.cloud import firestore as fs
    updates["unit_economics.updated_at"] = fs.SERVER_TIMESTAMP
    return updates
