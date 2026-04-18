"""
Orchestrator — Firestore Data Access Layer.

All Firestore read/write operations are centralised here.
Services call repo methods — never ``db.collection(...)`` directly.
This makes each read/write a single named, testable unit.

Dependency injection contract:
  Every function accepts ``db`` as its first positional argument.
  Callers (route handlers, services) pass ``get_db()`` from core.clients.
  Unit tests pass a ``MagicMock`` — no real GCP credentials required.
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

from google.cloud import firestore as fs

from core.firestore_utils import sanitize_update  # type: ignore[import]


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_user(db, uid: str) -> Optional[dict[str, Any]]:
    """Fetch user document.  Returns dict or None if not found.

    Args:
        db:  Firestore client.
        uid: Firebase UID / tenant_id.

    Returns:
        Document dict or ``None``.
    """
    doc = db.collection("users").document(uid).get()
    return doc.to_dict() if doc.exists else None


def create_user(db, uid: str, email: str) -> None:
    """Create a brand-new user document with safe defaults.

    Args:
        db:    Firestore client.
        uid:   Firebase UID.
        email: User's email address.
    """
    db.collection("users").document(uid).set({
        "tenant_id":       uid,
        "role":            "admin",
        "email":           email,
        "is_active":       True,
        "approval_status": "pending",
        "beta_expiry":     None,
        "wallet": {
            "allocated_credits": 0,
            "consumed_credits":  0,
        },
        "createdAt": fs.SERVER_TIMESTAMP,
    })


def update_user(db, uid: str, updates: dict[str, Any]) -> None:
    """Perform a partial update on a user document.

    All ``datetime`` values in *updates* are serialised to ISO-8601 strings
    before the Firestore write to prevent silent SDK rejection on some versions.
    Firestore sentinel objects (``SERVER_TIMESTAMP``, ``Increment``, etc.) are
    passed through unchanged.

    Args:
        db:      Firestore client.
        uid:     Firebase UID.
        updates: Dict of field paths → values.
    """
    db.collection("users").document(uid).update(sanitize_update(updates))


def get_unit_economics(db, tenant_id: str) -> dict[str, Any]:
    """Return ``unit_economics`` sub-dict from user doc (empty if absent).

    Args:
        db:        Firestore client.
        tenant_id: Tenant UID.

    Returns:
        Unit economics dict (may be empty).
    """
    user_doc = db.collection("users").document(tenant_id).get()
    if not user_doc.exists:
        return {}
    return (user_doc.to_dict() or {}).get("unit_economics") or {}


def save_unit_economics(db, tenant_id: str, updates: dict[str, Any]) -> None:
    """Persist unit_economics fields on the user document (merge).

    Args:
        db:        Firestore client.
        tenant_id: Tenant UID.
        updates:   Flattened field-path → value dict (e.g. ``unit_economics.avg_cpl``).
    """
    db.collection("users").document(tenant_id).set(updates, merge=True)


def get_wallet_shards_total(db, tenant_id: str) -> int:
    """Aggregate consumed_credits across all wallet_shards sub-docs.

    Args:
        db:        Firestore client.
        tenant_id: Tenant UID.

    Returns:
        Sum of ``consumed_credits`` across all shards.
    """
    shards = (
        db.collection("users")
        .document(tenant_id)
        .collection("wallet_shards")
        .stream()
    )
    return sum(
        int(s.to_dict().get("consumed_credits", 0) or 0) for s in shards
    )


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

def list_campaigns(db, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """Fetch all campaigns for a tenant.

    Args:
        db:        Firestore client.
        tenant_id: Tenant UID.
        limit:     Maximum number of documents to return.

    Returns:
        List of campaign dicts with ``id`` field injected.
    """
    docs = (
        db.collection("campaigns")
        .where("tenant_id", "==", tenant_id)
        .limit(limit)
        .stream()
    )
    results = []
    for doc in docs:
        d = doc.to_dict() or {}
        d["id"] = doc.id
        results.append(d)
    return results


def get_campaign(db, campaign_id: str) -> Optional[dict[str, Any]]:
    """Fetch a single campaign by ID.

    Args:
        db:          Firestore client.
        campaign_id: Firestore document ID.

    Returns:
        Campaign dict with ``id`` injected, or ``None``.
    """
    doc = db.collection("campaigns").document(campaign_id).get()
    if not doc.exists:
        return None
    d = doc.to_dict() or {}
    d["id"] = doc.id
    return d


def create_campaign(db, campaign_data: dict[str, Any]) -> str:
    """Create a new campaign document.

    Args:
        db:            Firestore client.
        campaign_data: Full campaign payload dict.

    Returns:
        The new Firestore document ID.
    """
    _, ref = db.collection("campaigns").add(campaign_data)
    return ref.id


def update_campaign(db, campaign_id: str, updates: dict[str, Any]) -> None:
    """Partial update on a campaign document.

    All ``datetime`` values in *updates* are serialised to ISO-8601 strings.
    Firestore sentinels pass through unchanged.

    Args:
        db:          Firestore client.
        campaign_id: Firestore document ID.
        updates:     Field-path → value dict.
    """
    db.collection("campaigns").document(campaign_id).update(sanitize_update(updates))


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

def list_leads(
    db,
    tenant_id: str,
    crm_filter: Optional[bool] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Fetch leads for a tenant with optional CRM filter.

    Args:
        db:         Firestore client.
        tenant_id:  Tenant UID.
        crm_filter: ``True`` → CRM leads only; ``False`` → dashboard feed;
                    ``None`` → all leads.
        limit:      Maximum documents to return.

    Returns:
        List of lead dicts with ``id`` field injected.
    """
    q = db.collection("leads").where("tenant_id", "==", tenant_id)
    if crm_filter is not None:
        q = q.where("is_in_crm", "==", crm_filter)
    docs = q.limit(limit).stream()
    results = []
    for doc in docs:
        d = doc.to_dict() or {}
        d["id"] = doc.id
        results.append(d)
    return results


def get_lead(db, lead_id: str) -> Optional[dict[str, Any]]:
    """Fetch a single lead document.

    Args:
        db:      Firestore client.
        lead_id: Firestore document ID.

    Returns:
        Lead dict with ``id`` injected, or ``None``.
    """
    doc = db.collection("leads").document(lead_id).get()
    if not doc.exists:
        return None
    d = doc.to_dict() or {}
    d["id"] = doc.id
    return d


def update_lead(db, lead_id: str, updates: dict[str, Any]) -> None:
    """Partial update on a lead document.

    All ``datetime`` values in *updates* are serialised to ISO-8601 strings.
    Firestore sentinels pass through unchanged.

    Args:
        db:      Firestore client.
        lead_id: Firestore document ID.
        updates: Field-path → value dict.
    """
    db.collection("leads").document(lead_id).update(sanitize_update(updates))


def count_leads_by_status(
    db,
    tenant_id: str,
    status: str,
    since: datetime.datetime,
) -> int:
    """Count leads with a given status created after *since*.

    Args:
        db:        Firestore client.
        tenant_id: Tenant UID.
        status:    Status string (e.g. ``"converted"``).
        since:     Cutoff datetime (timezone-aware UTC).

    Returns:
        Integer count.
    """
    docs = (
        db.collection("leads")
        .where("tenant_id", "==", tenant_id)
        .where("status", "==", status)
        .where("updatedAt", ">=", since)
        .stream()
    )
    return sum(1 for _ in docs)


# ---------------------------------------------------------------------------
# System config
# ---------------------------------------------------------------------------

def get_router_config(db) -> dict[str, Any]:
    """Fetch ``system_config/router`` document, initialising defaults if absent.

    Args:
        db: Firestore client.

    Returns:
        Router config dict with ``exploit_ratio``, ``discovery_allocation``,
        and ``intent_confidence_threshold`` keys.
    """
    ref = db.collection("system_config").document("router")
    doc = ref.get()
    if not doc.exists:
        defaults: dict[str, Any] = {
            "exploit_ratio":              0.10,
            "discovery_allocation":        0.15,
            "intent_confidence_threshold": 1000.0,
            "initialized_at":              fs.SERVER_TIMESTAMP,
        }
        ref.set(defaults)
        return defaults
    return doc.to_dict() or {}
