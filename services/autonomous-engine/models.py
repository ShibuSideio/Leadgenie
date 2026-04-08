"""
Universal Data Contract — autonomous-engine local copy.
Kept in sync with services/pipeline-main/models.py.
"""
from typing import List, Literal, Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator


class ContactEndpoint(BaseModel):
    platform: Literal["instagram","reddit","whatsapp","gmb","email","linkedin","facebook","other"]
    uri: str


class LeadPayload(BaseModel):
    id: str
    source_url: str
    tenant_id: str
    origin_engine: Literal["cartographer", "autonomous"]
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
    company_name: Optional[str] = None
    dossier_text: Optional[str] = None

    @field_validator("status")
    @classmethod
    def status_allowlist(cls, v: str) -> str:
        allowed = {
            "new","contacted","replied","negotiating","won","lost",
            "ignored","converted","failed","failed_scrape",
            "dropped_no_context","schema_violation","processing"
        }
        if v not in allowed:
            raise ValueError(f"Invalid status: {v}")
        return v

    @field_validator("dm", "pain_point", "intent_signal")
    @classmethod
    def strip_na_sentinel(cls, v: str) -> str:
        return "" if v.strip().upper() == "N/A" else v

    def to_firestore_dict(self) -> Dict[str, Any]:
        raw = self.model_dump(exclude_none=True)
        raw["contact_endpoints"] = [ep.model_dump() for ep in self.contact_endpoints]
        return raw
