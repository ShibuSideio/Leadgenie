"""
Universal Data Contract — Sideio Leads Pipeline
================================================
LeadPayload is the canonical write schema for the Firestore `leads` collection.

Both pipeline engines MUST validate their output against this model before
any doc_ref.update() call reaches Firestore:

    V14 Cartographer  →  origin_engine = "cartographer"
    V16 Autonomous    →  origin_engine = "autonomous"

Fields marked Optional[str] = None are forward-compat placeholders for V16.
The validate_and_update_lead() helper in main.py enforces this at runtime.
"""

from typing import List, Literal, Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator


class ContactEndpoint(BaseModel):
    platform: Literal["instagram", "reddit", "whatsapp", "gmb", "email", "linkedin", "facebook", "other"]
    uri: str


class LeadPayload(BaseModel):
    # ── Identity ────────────────────────────────────────────────────────────
    id: str
    source_url: str
    tenant_id: str

    # ── Engine origin (contract enforcer) ───────────────────────────────────
    origin_engine: Literal["cartographer", "autonomous"]

    # ── V14 Cartographer fields ──────────────────────────────────────────────
    score: int = Field(ge=0, le=10)
    intent_signal: str = ""
    pain_point: str = ""
    dm: str = ""
    icebreaker_angle: str = ""
    hiring_intent_found: Literal["Yes", "No"] = "No"
    tech_stack_found: List[str] = []
    contact_endpoints: List[ContactEndpoint] = []
    decision_maker_name: str = "Unknown"
    decision_maker_title: str = "Unknown"
    company_size_tier: str = "Unknown"
    primary_objection_hypothesis: str = "Unknown"
    sourcing_vector: str = ""
    confidence_tier: str = "High"
    status: str = "new"
    
    # ── V18 Multi-Campaign fields ────────────────────────────────────────────
    matched_campaign_ids: List[str] = []
    trend_mapped: bool = False
    highest_campaign_id: str = "Unknown"

    # ── Optional extraction fields ───────────────────────────────────────────
    company_name: Optional[str] = None       # V14 attempts; V16 populates natively

    # ── V16 forward-compat placeholders (Optional — V14 passes None) ─────────
    dossier_text: Optional[str] = None       # V16: consolidated intelligence string

    # ── Validators ───────────────────────────────────────────────────────────
    @field_validator("status")
    @classmethod
    def status_allowlist(cls, v: str) -> str:
        allowed = {
            "new", "contacted", "replied", "negotiating",
            "won", "lost", "ignored", "converted",
            "failed", "failed_scrape", "dropped_no_context",
            "schema_violation", "processing"
        }
        if v not in allowed:
            raise ValueError(f"Invalid status '{v}'. Allowed: {allowed}")
        return v

    @field_validator("dm", "pain_point", "intent_signal")
    @classmethod
    def strip_na_sentinel(cls, v: str) -> str:
        """Gemini sometimes returns literal 'N/A'. Normalise to empty string."""
        return "" if v.strip().upper() == "N/A" else v

    def to_firestore_dict(self) -> Dict[str, Any]:
        """
        Returns a dict safe for Firestore writes.
        Excludes None values so optional fields don't overwrite existing data.
        contact_endpoints are serialised back to plain dicts.
        """
        raw = self.model_dump(exclude_none=True)
        raw["contact_endpoints"] = [ep.model_dump() for ep in self.contact_endpoints]
        return raw
