"""Deterministic lead confidence scoring for MVP qualification.

The goal is to replace brittle score thresholds with an explainable confidence
score that combines evidence strength, intent, urgency, geography, and source
trust. The score is designed to be lightweight and deterministic so the app
can promote leads without depending entirely on the LLM.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def calculate_lead_confidence(
    evaluation: Optional[Dict[str, Any]] = None,
    text: str = "",
    url: str = "",
    source_tier: str = "High",
    is_harvest_lead: bool = False,
    is_thin_payload: bool = False,
    is_thin_bio: bool = False,
    campaign: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a deterministic confidence bundle and promotion decision."""
    eval_data = evaluation or {}
    score = int(eval_data.get("score", 0) or 0)
    tier = str(eval_data.get("tier", "LOW")).upper()
    topic_coherence = float(eval_data.get("topic_coherence", 0.0) or 0.0)
    pain_summary = str(eval_data.get("pain_summary", "") or "")
    contact_point = str(eval_data.get("contact_point", "") or "")
    geo_match = bool(eval_data.get("geo_match", False))

    lowered_text = (text or "").lower()
    lowered_url = (url or "").lower()

    evidence_strength = 0.0
    if tier == "HIGH":
        evidence_strength += 0.45
    elif tier == "MEDIUM":
        evidence_strength += 0.25

    if topic_coherence >= 0.8:
        evidence_strength += 0.25
    elif topic_coherence >= 0.6:
        evidence_strength += 0.15
    elif topic_coherence >= 0.3:
        evidence_strength += 0.05

    if pain_summary:
        evidence_strength += 0.1
    if contact_point:
        evidence_strength += 0.1
    if geo_match:
        evidence_strength += 0.05

    intent_signal = 0.0
    if any(term in lowered_text for term in ["looking for", "need help", "urgent", "broken", "problem", "hire", "buy", "recommend", "budget", "switch"]):
        intent_signal += 0.3
    if any(term in lowered_url for term in ["reddit", "forum", "community", "review", "jobs", "careers"]):
        intent_signal += 0.1
    if is_harvest_lead:
        intent_signal += 0.2

    urgency = 0.0
    if any(term in lowered_text for term in ["urgently", "immediately", "today", "now", "need", "soon", "budget"]):
        urgency += 0.2
    if any(term in lowered_text for term in ["looking for", "need help", "recommend"]):
        urgency += 0.1

    source_bonus = 0.0
    if source_tier == "High":
        source_bonus += 0.1
    elif source_tier == "Medium":
        source_bonus += 0.05

    if is_thin_payload:
        source_bonus -= 0.1
    if is_thin_bio:
        source_bonus -= 0.05

    confidence = min(1.0, max(0.0, evidence_strength + intent_signal + urgency + source_bonus))
    confidence_score = round(confidence * 100, 1)

    # Promotion logic: promotion threshold is intentionally explained and tunable.
    threshold = 62.0
    if is_harvest_lead:
        threshold = 56.0
    if is_thin_payload:
        threshold = 68.0
    if is_thin_bio:
        threshold = 64.0

    promotion = confidence_score >= threshold
    return {
        "confidence_score": confidence_score,
        "confidence_threshold": threshold,
        "promotion": promotion,
        "reason": (
            "high-intent evidence" if confidence_score >= 80 else
            "medium-intent evidence" if confidence_score >= 65 else
            "weak-evidence fallback"
        ),
        "evidence_strength": round(evidence_strength, 3),
        "intent_signal": round(intent_signal, 3),
        "urgency": round(urgency, 3),
        "source_bonus": round(source_bonus, 3),
        "source_tier": source_tier,
        "is_harvest_lead": is_harvest_lead,
        "is_thin_payload": is_thin_payload,
        "is_thin_bio": is_thin_bio,
    }
