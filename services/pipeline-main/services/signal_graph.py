"""
pipeline-main — Signal Graph model.

Defines signal types for the evidence accumulation engine.
Signals from multiple sources (Serper, Inbound Radar, First-Party, etc.)
are stacked over time. Converging signals from different types compound
the lead's confidence score.

V24.0: Initial implementation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional
from enum import Enum


class SignalType(str, Enum):
    PAIN_EXPRESSION = "PAIN_EXPRESSION"
    HIRING_INTENT = "HIRING_INTENT"
    COMPETITOR_CHURN = "COMPETITOR_CHURN"
    TECH_STACK_MATCH = "TECH_STACK_MATCH"
    COMMUNITY_MENTION = "COMMUNITY_MENTION"
    FIRST_PARTY_VISIT = "FIRST_PARTY_VISIT"
    REVIEW_SIGNAL = "REVIEW_SIGNAL"
    FUNDING_EVENT = "FUNDING_EVENT"
    GENERAL_FIT = "GENERAL_FIT"


@dataclass
class SignalEntry:
    signal_type: str
    source: str            # e.g. "reddit.com/r/saas", "careers.acme.com"
    evidence_text: str     # Human-readable evidence
    confidence: float      # 0.0-1.0
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    query_used: str = ""
    campaign_id: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["detected_at"] = self.detected_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SignalEntry":
        d = dict(d)  # shallow copy
        if isinstance(d.get("detected_at"), str):
            d["detected_at"] = datetime.fromisoformat(d["detected_at"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Stacking Score Computation
# ---------------------------------------------------------------------------

# Weights per signal type — tuned for OSINT lead gen context
_SIGNAL_WEIGHTS = {
    SignalType.PAIN_EXPRESSION:   1.0,
    SignalType.HIRING_INTENT:     0.8,
    SignalType.COMPETITOR_CHURN:  0.9,
    SignalType.TECH_STACK_MATCH:  0.5,
    SignalType.COMMUNITY_MENTION: 0.6,
    SignalType.FIRST_PARTY_VISIT: 1.2,   # Highest — they came to YOUR site
    SignalType.REVIEW_SIGNAL:     0.7,
    SignalType.FUNDING_EVENT:     0.6,
    SignalType.GENERAL_FIT:       0.4,
}


def compute_stacked_score(
    signals: list[SignalEntry],
    base_score: float = 0.0,
) -> dict:
    """Compute a stacked confidence score from accumulated signals.

    Returns dict with: stacked_score (1-10), signal_count, unique_types,
    convergence_multiplier, confidence_level.
    """
    if not signals:
        return {
            "stacked_score": int(base_score),
            "signal_count": 0,
            "unique_types": 0,
            "convergence_multiplier": 1.0,
            "confidence_level": "LOW",
        }

    now = datetime.now(timezone.utc)
    weighted_sum = 0.0
    unique_types = set()
    type_counts: dict[str, int] = {}

    for sig in signals:
        # Temporal decay: recent signals worth more
        age = now - sig.detected_at
        if age < timedelta(days=7):
            decay = 1.0
        elif age < timedelta(days=30):
            decay = 0.6
        else:
            decay = 0.3

        # Signal type weight
        type_weight = _SIGNAL_WEIGHTS.get(sig.signal_type, 0.5)

        # Diminishing returns for repeated signals of same type
        type_counts[sig.signal_type] = type_counts.get(sig.signal_type, 0) + 1
        repeat_factor = 1.0 / math.log2(type_counts[sig.signal_type] + 1)

        weighted_sum += sig.confidence * type_weight * decay * repeat_factor
        unique_types.add(sig.signal_type)

    # Convergence bonus: >= 3 different signal types = strong convergence
    n_types = len(unique_types)
    if n_types >= 4:
        convergence = 1.4
    elif n_types >= 3:
        convergence = 1.3
    elif n_types >= 2:
        convergence = 1.15
    else:
        convergence = 1.0

    raw = (base_score * 0.6) + (weighted_sum * convergence * 4.0 * 0.4)
    stacked = int(min(max(round(raw), 1), 10))

    if n_types >= 3 and stacked >= 7:
        level = "HIGH"
    elif n_types >= 2 and stacked >= 5:
        level = "MEDIUM"
    else:
        level = "SPECULATIVE"

    return {
        "stacked_score": stacked,
        "signal_count": len(signals),
        "unique_types": n_types,
        "convergence_multiplier": convergence,
        "confidence_level": level,
    }
